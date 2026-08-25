from typing import Optional

from market_analysis.option_guidance import is_spread_illiquid


def format_illiquidity_warning(bid: float, ask: float) -> Optional[str]:
    """流動性閘門警示文字格式化：期權部位若帶有 bid/ask 且點差過寬
    (is_spread_illiquid) 時，回傳附加於 reason 文字的警示片段；否則回傳
    None。判斷閾值本身沿用既有的 is_spread_illiquid，本函式僅負責文字
    格式化，避免各情境模組各自重複維護同一段警示字串 (opportunity_cost.py /
    margin_defense.py / core_deployment.py 曾各自維護逐字相同的片段)。
    呼叫端仍需自行判斷 asset_class == "OPTIONS" 等前置條件是否成立。
    """
    if not is_spread_illiquid(bid, ask):
        return None
    spread_pct = (ask - bid) / ((ask + bid) / 2)
    return (
        f"\n⚠️ **流動性警告**：合約點差過寬 (Bid ${bid:.2f} / Ask ${ask:.2f}，"
        f"點差 {spread_pct:.1%})，建議採限價單並留意滑價。"
    )


def format_cash_impact(recovered_cash: float) -> Optional[str]:
    """資金影響金額字串格式化：正值回傳千分位美元字串，否則回傳 None
    (供 instruction dict 的 cash_impact 欄位與 database.log_rollover_instruction
    直接使用)。"""
    return f"${recovered_cash:,.0f}" if recovered_cash > 0 else None


def resolve_current_value(current_value: float, quantity: float, spot: float) -> float:
    """持倉市值解析：優先採用既有 current_value，缺失 (<=0) 時退回
    quantity * spot 估算。呼叫端須自行決定傳入的 quantity 是否已取絕對值
    (例如空頭期權部位的負股數)，本函式不代為判斷正負號語意。"""
    if current_value <= 0 and spot > 0:
        return quantity * spot
    return current_value
