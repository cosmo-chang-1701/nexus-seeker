import sqlite3
import logging
from typing import Any
from dataclasses import dataclass
import config

logger = logging.getLogger(__name__)


@dataclass
class UserContext:
    user_id: int
    capital: float  # 總資金
    risk_limit: float  # 基準風險上限 %
    total_weighted_delta: float  # 組合總加權 Delta (目前持倉)
    total_theta: float  # 組合總每日 Theta (目前持倉)
    total_gamma: float  # 組合總 Gamma (目前持倉)
    total_vanna: float = 0.0  # 組合總 Vanna
    last_rehedge_alert_time: int = 0  # 上次發送回補警報的時間 (Unix Timestamp)
    dynamic_tau: float = 1.0  # 自動優化對沖係數
    option_alert_mode: int = 1  # 期權警報模式: 0=OFF, 1=ALL, 2=PORTFOLIO_ONLY
    enable_vtr: bool = True  # 是否啟用虛擬交易室 (GhostTrader) 自動跟單
    enable_psq_watchlist: bool = False  # 是否對 add_watch 標的執行 PowerSqueeze 追蹤
    enable_local_tunnel: bool = False  # 是否允許呼叫本地 Tunnel (Edge Scraper)
    enable_analyst_agent: bool = False  # 是否啟用 Wall Street Analyst Agent 每日推播
    polymarket_threshold: float = 10000.0  # Polymarket 巨鯨監控門檻 (USD, 0=關閉)
    polymarket_use_llm: bool = True  # 是否使用 LLM 進行 Polymarket 交易分析
    polymarket_slippage: float = 2.0  # Polymarket 巨鯨判定目標滑價百分比 (預設 2.0%)
    is_professional_mode: bool = True  # 專業模式 / 觀戰模式切換
    monthly_expense: float = 0.0  # 每月支出預算

    tax_reserve_rate: float = 0.20  # 稅務預留比例 (預設 20%)
    cash_reserve: float = 0.0  # 現金儲備 (用於生存天數計算)
    escape_window_start: str = "07-15"
    escape_window_end: str = "07-31"


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

    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()

        # 1. 確保使用者紀錄存在
        cursor.execute(
            """
            INSERT OR IGNORE INTO user_settings (user_id, capital, risk_limit)
            VALUES (?, 100000.0, 15.0)
        """,
            (user_id,),
        )

        # 2. 轉譯別名 (Aliases) 確保與 README/CLI 參數對齊
        if "expense" in kwargs and kwargs["expense"] is not None:
            kwargs["monthly_expense"] = kwargs.pop("expense")

        # 3. 動態構建 SQL SET 子句 (白名單防護)
        allowed_keys = {
            "capital",
            "risk_limit",
            "last_rehedge_alert_time",
            "dynamic_tau",
            "option_alert_mode",
            "enable_vtr",
            "enable_psq_watchlist",
            "enable_local_tunnel",
            "enable_analyst_agent",
            "polymarket_threshold",
            "polymarket_use_llm",
            "polymarket_slippage",
            "monthly_expense",
            "tax_reserve_rate",
            "cash_reserve",
            "escape_window_start",
            "escape_window_end",
        }
        update_pairs = []
        values = []

        for key, value in kwargs.items():
            if key in allowed_keys and value is not None:
                if key == "capital":
                    value = max(float(value), 1.0)
                elif key == "risk_limit":
                    value = max(1.0, min(value, 50.0))
                elif key == "option_alert_mode":
                    value = max(0, min(int(value), 2))
                elif key in [
                    "polymarket_threshold",
                    "monthly_expense",
                    "cash_reserve",
                ]:
                    value = max(0.0, float(value))
                elif key == "polymarket_slippage":
                    value = max(0.1, min(float(value), 10.0))
                elif key == "tax_reserve_rate":
                    value = max(0.0, min(float(value), 1.0))

                update_pairs.append(f"{key} = ?")
                values.append(value)

        if not update_pairs:
            return False

        # 4. 參數化執行更新
        sql = f"UPDATE user_settings SET {', '.join(update_pairs)} WHERE user_id = ?"
        values.append(user_id)

        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        cursor.execute(sql, tuple(values))
        conn.commit()
        return True

    except Exception as e:
        logger.error(f"執行 upsert_user_config 失敗 (User: {user_id}): {e}")
        return False
    finally:
        if conn:
            conn.close()


def calculate_auto_capital(user_id: int, conn: sqlite3.Connection = None) -> float:
    """
    自動計算總資金：加總持倉 (HOLDING)、期權 (TRADE) 與現金儲備 (cash_reserve)
    持倉價值 = avg_cost * quantity
    期權價值 = entry_price * quantity * 100
    現金儲備 = cash_reserve
    """
    should_close = False
    if conn is None:
        conn = sqlite3.connect(config.DB_NAME)
        should_close = True
    try:
        cursor = conn.cursor()
        # 1. 取得現金儲備
        cursor.execute(
            "SELECT cash_reserve FROM user_settings WHERE user_id = ?", (user_id,)
        )
        row = cursor.fetchone()
        cash_reserve = float(row[0]) if row and row[0] is not None else 0.0

        # 2. 取得 HOLDING 和 TRADE 的 metadata
        cursor.execute(
            "SELECT context_type, metadata FROM assets WHERE user_id = ? AND context_type IN ('HOLDING', 'TRADE')",
            (user_id,),
        )
        rows = cursor.fetchall()

        holdings_value = 0.0
        options_value = 0.0

        import json

        for ctx_type, metadata_json in rows:
            if not metadata_json:
                continue
            try:
                meta = json.loads(metadata_json)
                qty = float(meta.get("quantity", 0.0))
                if ctx_type == "HOLDING":
                    avg_cost = float(meta.get("avg_cost", 0.0))
                    holdings_value += qty * avg_cost
                elif ctx_type == "TRADE":
                    entry_price = float(meta.get("entry_price", 0.0))
                    options_value += qty * entry_price * 100.0
            except Exception as ex:
                logger.error(f"解析 asset metadata 失敗: {ex}")

        total_val = holdings_value + options_value + cash_reserve
        if total_val == 0.0:
            return 100000.0
        return max(total_val, 1.0)
    finally:
        if should_close:
            conn.close()


def get_user_capital(user_id: int) -> float:
    """取得自動計算的總資金：加總持倉 (HOLDING)、期權 (TRADE) 與現金儲備 (cash_reserve)"""
    return calculate_auto_capital(user_id)


def get_user_risk_limit(user_id: int) -> float:
    """從資料庫獲取使用者的個人化風險上限 (Base Risk Limit %)"""
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT risk_limit FROM user_settings WHERE user_id = ?", (user_id,)
        )
        row = cursor.fetchone()
        return float(row[0]) if row and row[0] is not None else 15.0
    except Exception as e:
        logger.error(f"無法讀取使用者 {user_id} 的風險限制: {e}")
        return 15.0
    finally:
        if conn:
            conn.close()


def get_all_user_ids():
    """取得資料庫中所有出現過的使用者 ID"""
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        # UNION 自動去重
        cursor.execute("""
            SELECT user_id FROM portfolio
            UNION
            SELECT user_id FROM watchlist
            UNION
            SELECT user_id FROM user_settings
        """)
        rows = cursor.fetchall()
        return [row[0] for row in rows]
    except Exception as e:
        logger.error(f"獲取所有使用者 ID 失敗: {e}")
        return []
    finally:
        if conn:
            conn.close()


def any_user_local_tunnel_enabled() -> bool:
    """是否存在任何使用者啟用本地 Tunnel。用於全域背景任務的呼叫閘門。"""
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM user_settings WHERE COALESCE(enable_local_tunnel, 0) = 1 LIMIT 1"
        )
        return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"檢查 enable_local_tunnel 失敗: {e}")
        return False
    finally:
        if conn:
            conn.close()


def get_full_user_context(user_id: int) -> UserContext:
    """
    帳戶上下文提供者 (User Context Provider)：
    一次性獲取帳戶設定與組合希臘字母指標，極大化 I/O 效率。
    使用單一 SQL 查詢同時聚合設定與 Greeks。
    """
    conn = None
    try:
        conn = sqlite3.connect(config.DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 🚀 [Unified Asset Lifecycle] 從新的 assets 表聚合 Greeks
        sql = """
            SELECT
                u.*,
                g.sum_delta, g.sum_theta, g.sum_gamma, g.sum_vanna
            FROM user_settings u
            LEFT JOIN (
                SELECT
                    user_id,
                    SUM(COALESCE(CAST(json_extract(metadata, '$.weighted_delta') AS REAL), 0.0)) as sum_delta,
                    SUM(COALESCE(CAST(json_extract(metadata, '$.theta') AS REAL), 0.0)) as sum_theta,
                    SUM(COALESCE(CAST(json_extract(metadata, '$.gamma') AS REAL), 0.0)) as sum_gamma,
                    SUM(COALESCE(CAST(json_extract(metadata, '$.vanna') AS REAL), 0.0)) as sum_vanna
                FROM assets
                WHERE context_type IN ('TRADE', 'HOLDING')
                GROUP BY user_id
            ) g ON u.user_id = g.user_id
            WHERE u.user_id = ?
        """
        cursor.execute(sql, (user_id,))
        user_row = cursor.fetchone()

        if not user_row:
            capital = calculate_auto_capital(user_id, conn)
            return UserContext(user_id, capital, 15.0, 0.0, 0.0, 0.0, 0.0)

        # 提取 Greeks (Annual from DB -> Daily for Context)
        sum_delta = user_row["sum_delta"] if user_row["sum_delta"] is not None else 0.0
        sum_theta = (
            user_row["sum_theta"] if user_row["sum_theta"] is not None else 0.0
        ) / 365.0
        sum_gamma = user_row["sum_gamma"] if user_row["sum_gamma"] is not None else 0.0
        sum_vanna = user_row["sum_vanna"] if user_row["sum_vanna"] is not None else 0.0

        # 處理基本設定與空值
        capital = calculate_auto_capital(user_id, conn)
        risk_limit = (
            float(user_row["risk_limit"])
            if user_row["risk_limit"] is not None
            else 15.0
        )

        # Helper for booleans and defaults
        def _get_val(key: str, default: Any) -> Any:
            if key in user_row.keys() and user_row[key] is not None:
                return user_row[key]
            return default

        return UserContext(
            user_id=user_id,
            capital=capital,
            risk_limit=risk_limit,
            total_weighted_delta=sum_delta,
            total_theta=sum_theta,
            total_gamma=sum_gamma,
            total_vanna=sum_vanna,
            last_rehedge_alert_time=_get_val("last_rehedge_alert_time", 0),
            dynamic_tau=_get_val("dynamic_tau", 1.0),
            option_alert_mode=_get_val("option_alert_mode", 1),
            enable_vtr=bool(_get_val("enable_vtr", True)),
            enable_psq_watchlist=bool(_get_val("enable_psq_watchlist", False)),
            enable_local_tunnel=bool(_get_val("enable_local_tunnel", False)),
            enable_analyst_agent=bool(_get_val("enable_analyst_agent", False)),
            polymarket_threshold=_get_val("polymarket_threshold", 10000.0),
            polymarket_use_llm=bool(_get_val("polymarket_use_llm", True)),
            polymarket_slippage=_get_val("polymarket_slippage", 2.0),
            is_professional_mode=bool(_get_val("is_professional_mode", True)),
            monthly_expense=_get_val("monthly_expense", 0.0),
            tax_reserve_rate=_get_val("tax_reserve_rate", 0.20),
            cash_reserve=_get_val("cash_reserve", 0.0),
            escape_window_start=_get_val("escape_window_start", "07-15"),
            escape_window_end=_get_val("escape_window_end", "07-31"),
        )

    except Exception as e:
        logger.error(f"獲取 UserContext 失敗 (UID: {user_id}): {e}")
        return UserContext(user_id, 100000.0, 15.0, 0.0, 0.0, 0.0)
    finally:
        if conn:
            conn.close()
