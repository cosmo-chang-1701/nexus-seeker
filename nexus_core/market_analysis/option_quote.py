"""期權報價的單一定價規則 (純函式葉模組，零 I/O、零專案內相依)。

`market_analysis.portfolio` 與 `market_analysis.trading_orchestration` 共用，
放在葉模組避免兩者互相匯入。
"""

from typing import Any


def resolve_option_mid(bid: Any, ask: Any) -> tuple[float, str]:
    """由 bid/ask 推導期權現價，回傳 (mid, source)。

    - bid > 0 且 ask > 0：(bid+ask)/2，source="MID"
    - bid <= 0 且 ask > 0：ask/2，source="ASK_HALF" (呼叫端必須標示為估算)
    - 其餘：0.0，source="MISSING" (報價缺失)

    **不使用 lastPrice**：零 bid 的深價外合約，lastPrice 可能是數日前的成交，
    拿來估值會讓損益與 NAV 失真。
    """
    try:
        b = float(bid or 0.0)
        a = float(ask or 0.0)
    except (TypeError, ValueError):
        return 0.0, "MISSING"
    if b != b or a != a:  # NaN
        return 0.0, "MISSING"
    if b > 0 and a > 0:
        return (b + a) / 2.0, "MID"
    if a > 0:
        return a / 2.0, "ASK_HALF"
    return 0.0, "MISSING"
