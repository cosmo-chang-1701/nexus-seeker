import sqlite3
import logging
from typing import Any, Dict, Optional
from dataclasses import dataclass
from config import DB_NAME

logger = logging.getLogger(__name__)

@dataclass
class UserContext:
    user_id: int
    capital: float              # 總資金
    risk_limit_base: float      # 基準風險上限 %
    total_weighted_delta: float # 組合總加權 Delta (目前持倉)
    total_theta: float          # 組合總每日 Theta (目前持倉)
    total_gamma: float          # 組合總 Gamma (目前持倉)
    last_rehedge_alert_time: int = 0 # 上次發送回補警報的時間 (Unix Timestamp)
    dynamic_tau: float = 1.0        # 自動優化對沖係數
    enable_option_alerts: bool = True # 是否接收選項策略推播
    enable_vtr: bool = True           # 是否啟用虛擬交易室 (GhostTrader) 自動跟單
    enable_psq_watchlist: bool = False # 是否對 add_watch 標的執行 PowerSqueeze 追蹤
    enable_analyst_agent: bool = False # 是否啟用 Wall Street Analyst Agent 每日推播
    polymarket_threshold: float = 10000.0 # Polymarket 巨鯨監控門檻 (USD, 0=關閉)
    polymarket_use_llm: bool = True       # 是否使用 LLM 進行 Polymarket 交易分析
    polymarket_slippage: float = 2.0      # Polymarket 巨鯨判定目標滑價百分比 (預設 2.0%)
    monthly_expense: float = 0.0          # 每月支出預算
    tax_reserve_rate: float = 0.20        # 稅務預留比例 (預設 20%)
    cash_reserve: float = 0.0             # 現金儲備 (用於生存天數計算)


# ==========================================
# 使用者設定檔 (User Settings) CRUD
# ==========================================

def upsert_user_config(user_id: int, **kwargs) -> bool:
    """
    單一更新路徑 (Single Update Path)：
    根據傳入的關鍵字參數動態更新 user_settings 表中的欄位。
    """
    if not kwargs:
        return False

    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            
            # 1. 確保使用者紀錄存在
            cursor.execute('''
                INSERT OR IGNORE INTO user_settings (user_id, portfolio_value, risk_limit_pct) 
                VALUES (?, 100000.0, 15.0)
            ''', (user_id,))
            
            # 2. 轉譯 capital 為 portfolio_value
            if 'capital' in kwargs and kwargs['capital'] is not None:
                kwargs['portfolio_value'] = kwargs.pop('capital')
            
            # 3. 動態構建 SQL SET 子句 (白名單防護)
            allowed_keys = {
                'portfolio_value', 'risk_limit_pct', 'last_rehedge_alert_time', 'dynamic_tau', 
                'enable_option_alerts', 'enable_vtr', 'enable_psq_watchlist', 'enable_analyst_agent',
                'polymarket_threshold', 'polymarket_use_llm', 'polymarket_slippage',
                'monthly_expense', 'tax_reserve_rate', 'cash_reserve'
            }
            update_pairs = []
            values = []
            
            for key, value in kwargs.items():
                if key in allowed_keys and value is not None:
                    if key == 'portfolio_value':
                        value = max(float(value), 1.0)
                    elif key == 'risk_limit_pct':
                        value = max(1.0, min(value, 50.0))
                    elif key in ['polymarket_threshold', 'monthly_expense', 'cash_reserve']:
                        value = max(0.0, float(value))
                    elif key == 'polymarket_slippage':
                        value = max(0.1, min(float(value), 10.0))
                    elif key == 'tax_reserve_rate':
                        value = max(0.0, min(float(value), 1.0))
                        
                    update_pairs.append(f"{key} = ?")
                    values.append(value)
            
            if not update_pairs:
                return False
                
            # 4. 參數化執行更新
            sql = f"UPDATE user_settings SET {', '.join(update_pairs)} WHERE user_id = ?"
            values.append(user_id)
            
            cursor.execute(sql, tuple(values))
            conn.commit()
            return True
            
    except Exception as e:
        logger.error(f"執行 upsert_user_config 失敗 (User: {user_id}): {e}")
        return False

def get_user_capital(user_id: int) -> float:
    """取得使用者設定的總資金，若未設定則預設為 100000"""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT portfolio_value FROM user_settings WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            return float(row[0]) if row and row[0] is not None else 100000.0
    except Exception as e:
        logger.error(f"無法讀取使用者 {user_id} 的資金: {e}")
        return 100000.0

def get_user_risk_limit(user_id: int) -> float:
    """從資料庫獲取使用者的個人化風險上限 (Base Risk Limit %)"""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT risk_limit_pct FROM user_settings WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return float(row[0]) if row and row[0] is not None else 15.0
    except Exception as e:
        logger.error(f"無法讀取使用者 {user_id} 的風險限制: {e}")
        return 15.0

def get_all_user_ids():
    """取得資料庫中所有出現過的使用者 ID"""
    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            # UNION 自動去重
            cursor.execute('''
                SELECT user_id FROM portfolio
                UNION
                SELECT user_id FROM watchlist
                UNION
                SELECT user_id FROM user_settings
            ''')
            rows = cursor.fetchall()
            return [row[0] for row in rows]
    except Exception as e:
        logger.error(f"獲取所有使用者 ID 失敗: {e}")
        return []

def get_full_user_context(user_id: int) -> UserContext:
    """
    帳戶上下文提供者 (User Context Provider)：
    一次性獲取帳戶設定與組合希臘字母指標，極大化 I/O 效率。
    使用單一 SQL 查詢同時聚合設定與 Greeks。
    """
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 使用 LEFT JOIN 將使用者設定與聚合後的 Greeks 連結
            # Greeks 由 portfolio 與 virtual_trades (OPEN) 的 UNION ALL 構成
            sql = """
                SELECT 
                    u.*,
                    g.sum_delta, g.sum_theta, g.sum_gamma
                FROM user_settings u
                LEFT JOIN (
                    SELECT 
                        user_id,
                        SUM(COALESCE(weighted_delta, 0.0)) as sum_delta, 
                        SUM(COALESCE(theta, 0.0)) as sum_theta,
                        SUM(COALESCE(gamma, 0.0)) as sum_gamma
                    FROM (
                        SELECT user_id, weighted_delta, theta, gamma FROM portfolio
                        UNION ALL
                        SELECT user_id, weighted_delta, theta, gamma FROM virtual_trades WHERE status = 'OPEN'
                    )
                    GROUP BY user_id
                ) g ON u.user_id = g.user_id
                WHERE u.user_id = ?
            """
            cursor.execute(sql, (user_id,))
            user_row = cursor.fetchone()
            
            if not user_row:
                return UserContext(user_id, 100000.0, 15.0, 0.0, 0.0, 0.0)

            # 提取 Greeks (Annual from DB -> Daily for Context)
            # COALESCE ensures we don't get None here even if the JOIN failed to find trades
            sum_delta = user_row['sum_delta'] if user_row['sum_delta'] is not None else 0.0
            sum_theta = (user_row['sum_theta'] if user_row['sum_theta'] is not None else 0.0) / 365.0
            sum_gamma = user_row['sum_gamma'] if user_row['sum_gamma'] is not None else 0.0
            
            # 處理基本設定與空值
            capital_raw = float(user_row['portfolio_value']) if user_row['portfolio_value'] is not None else 100000.0
            capital = max(capital_raw, 1.0)
            risk_limit = float(user_row['risk_limit_pct']) if user_row['risk_limit_pct'] is not None else 15.0
            
            # Helper for booleans and defaults
            def _get_val(key: str, default: Any) -> Any:
                if key in user_row.keys() and user_row[key] is not None:
                    return user_row[key]
                return default

            return UserContext(
                user_id=user_id,
                capital=capital,
                risk_limit_base=risk_limit,
                total_weighted_delta=sum_delta,
                total_theta=sum_theta,
                total_gamma=sum_gamma,
                last_rehedge_alert_time=_get_val('last_rehedge_alert_time', 0),
                dynamic_tau=_get_val('dynamic_tau', 1.0),
                enable_option_alerts=bool(_get_val('enable_option_alerts', True)),
                enable_vtr=bool(_get_val('enable_vtr', True)),
                enable_psq_watchlist=bool(_get_val('enable_psq_watchlist', False)),
                enable_analyst_agent=bool(_get_val('enable_analyst_agent', False)),
                polymarket_threshold=_get_val('polymarket_threshold', 10000.0),
                polymarket_use_llm=bool(_get_val('polymarket_use_llm', True)),
                polymarket_slippage=_get_val('polymarket_slippage', 2.0),
                monthly_expense=_get_val('monthly_expense', 0.0),
                tax_reserve_rate=_get_val('tax_reserve_rate', 0.20),
                cash_reserve=_get_val('cash_reserve', 0.0)
            )
            
    except Exception as e:
        logger.error(f"獲取 UserContext 失敗 (UID: {user_id}): {e}")
        return UserContext(user_id, 100000.0, 15.0, 0.0, 0.0, 0.0)
