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


# ==========================================
# 使用者設定檔 (User Settings) CRUD
# ==========================================

def upsert_user_config(user_id: int, **kwargs) -> bool:
    """
    單一更新路徑 (Single Update Path)：
    根據傳入的關鍵字參數動態更新 user_settings 表中的欄位。
    支援欄位：capital / portfolio_value, risk_limit_pct, polymarket_threshold ...
    """
    if not kwargs:
        return False

    try:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            
            # 1. 確保使用者紀錄存在 (如果不存在則初始化，給予預設值以符合 NOT NULL 限制)
            cursor.execute('''
                INSERT OR IGNORE INTO user_settings (user_id, portfolio_value, risk_limit_pct) 
                VALUES (?, 100000.0, 15.0)
            ''', (user_id,))
            
            # 2. 轉譯 capital 為 portfolio_value以符合真實 Schema
            if 'capital' in kwargs and kwargs['capital'] is not None:
                kwargs['portfolio_value'] = kwargs.pop('capital')
            
            # 3. 動態構建 SQL SET 子句 (白名單防護)
            allowed_keys = {'portfolio_value', 'risk_limit_pct', 'last_rehedge_alert_time', 'dynamic_tau', 
                            'enable_option_alerts', 'enable_vtr', 'enable_psq_watchlist', 'enable_analyst_agent',
                            'polymarket_threshold'}
            update_pairs = []
            values = []
            
            for key, value in kwargs.items():
                if key in allowed_keys and value is not None:
                    if key == 'portfolio_value':
                        value = max(float(value), 1.0)
                    # 針對風險限制做數值防護
                    if key == 'risk_limit_pct':
                        value = max(1.0, min(value, 50.0))
                    # Polymarket 門檻防護
                    if key == 'polymarket_threshold':
                        value = max(0.0, float(value))
                        
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
    """
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.row_factory = sqlite3.Row  # 允許透過欄位名稱存取
            cursor = conn.cursor()
            
            # 1. 查詢使用者基本設定
            cursor.execute("""
                SELECT portfolio_value, risk_limit_pct, last_rehedge_alert_time, dynamic_tau,
                       enable_option_alerts, enable_vtr, enable_psq_watchlist, enable_analyst_agent,
                       polymarket_threshold
                FROM user_settings 
                WHERE user_id = ?
            """, (user_id,))
            user_row = cursor.fetchone()
            
            # 2. 查詢使用者目前的投資組合統計 (整合真實持倉與虛擬交易)
            sum_delta, sum_theta, sum_gamma = 0.0, 0.0, 0.0
            try:
                # 使用 UNION ALL 將 portfolio 與 virtual_trades 的 Greeks 指標匯總
                cursor.execute("""
                    SELECT 
                        SUM(weighted_delta) as sum_delta, 
                        SUM(theta) as sum_theta,
                        SUM(gamma) as sum_gamma
                    FROM (
                        SELECT weighted_delta, theta, gamma FROM portfolio WHERE user_id = ?
                        UNION ALL
                        SELECT weighted_delta, theta, gamma FROM virtual_trades WHERE user_id = ? AND status = 'OPEN'
                    )
                """, (user_id, user_id))
                stats_row = cursor.fetchone()
                if stats_row:
                    sum_delta = stats_row['sum_delta'] or 0.0
                    sum_theta = stats_row['sum_theta'] or 0.0
                    sum_gamma = stats_row['sum_gamma'] or 0.0
            except sqlite3.OperationalError as e:
                logger.warning(f"Greeks 統計查詢失敗 (可能尚未完成 Migration): {e}")
                pass
            
            # 3. 處理空值並封裝回傳
            capital_raw = float(user_row['portfolio_value']) if user_row and user_row['portfolio_value'] is not None else 100000.0
            capital = capital_raw if capital_raw > 0 else 100000.0
            risk_limit = float(user_row['risk_limit_pct']) if user_row and user_row['risk_limit_pct'] is not None else 15.0
            last_rehedge = int(user_row['last_rehedge_alert_time']) if user_row and 'last_rehedge_alert_time' in user_row.keys() and user_row['last_rehedge_alert_time'] is not None else 0
            dynamic_tau = float(user_row['dynamic_tau']) if user_row and 'dynamic_tau' in user_row.keys() and user_row['dynamic_tau'] is not None else 1.0
            poly_threshold = float(user_row['polymarket_threshold']) if user_row and 'polymarket_threshold' in user_row.keys() and user_row['polymarket_threshold'] is not None else 10000.0
            
            # Helper for booleans
            def _get_bool(key: str, default: bool) -> bool:
                if user_row and key in user_row.keys() and user_row[key] is not None:
                    return bool(user_row[key])
                return default

            return UserContext(
                user_id=user_id,
                capital=capital,
                risk_limit_base=risk_limit,
                total_weighted_delta=sum_delta,
                total_theta=sum_theta,
                total_gamma=sum_gamma,
                last_rehedge_alert_time=last_rehedge,
                dynamic_tau=dynamic_tau,
                enable_option_alerts=_get_bool('enable_option_alerts', True),
                enable_vtr=_get_bool('enable_vtr', True),
                enable_psq_watchlist=_get_bool('enable_psq_watchlist', False),
                enable_analyst_agent=_get_bool('enable_analyst_agent', False),
                polymarket_threshold=poly_threshold
            )
            
    except Exception as e:
        logger.error(f"獲取 UserContext 失敗 (UID: {user_id}): {e}")
        # 發生異常時回傳保守的預設物件
        return UserContext(user_id, 100000.0, 15.0, 0.0, 0.0, 0.0)

