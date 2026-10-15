"""/dash 安全提領紅線與可動用現金比較（純函式，僅供呈現，不阻擋任何操作）。

可動用現金 = 現金儲備 − 未平倉 Short Put 擔保金。Short Put 由同標的、同到期、
較低履約價的 Long Put 組成價差時，該口只需 (short − long) × 100 擔保。
"""

from collections import defaultdict
from typing import Any, Dict, List, Tuple


def _is_put(opt_type: Any) -> bool:
    return str(opt_type or "").strip().lower() in ("put", "p")


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def short_put_collateral(trades: List[Dict[str, Any]]) -> float:
    """未平倉 Short Put 的擔保金（美元）。

    裸賣：strike × 100 × |qty|。同標的＋同到期、較低履約價的 Long Put 以價差
    涵蓋：由最高 long 履約價優先配對，每口 long 至多涵蓋一口 short，
    被涵蓋的口只計 (short_strike − long_strike) × 100。
    """
    shorts: Dict[Tuple[str, str], List[Tuple[float, int]]] = defaultdict(list)
    longs: Dict[Tuple[str, str], List[Tuple[float, int]]] = defaultdict(list)
    for t in trades or []:
        if not isinstance(t, dict) or not _is_put(t.get("opt_type")):
            continue
        strike = _num(t.get("strike"))
        qty = _num(t.get("quantity"))
        if strike is None or qty is None or strike <= 0 or qty == 0:
            continue
        key = (str(t.get("symbol") or "").upper(), str(t.get("expiry") or ""))
        n = int(abs(qty))
        if n <= 0:
            continue
        (shorts if qty < 0 else longs)[key].append((strike, n))

    total = 0.0
    for key, short_rows in shorts.items():
        long_pool = sorted(longs.get(key, []), key=lambda r: -r[0])
        long_units: List[float] = []
        for k, n in long_pool:
            long_units.extend([k] * n)
        short_units: List[float] = []
        for k, n in short_rows:
            short_units.extend([k] * n)
        short_units.sort(reverse=True)
        for s_k in short_units:
            cover = None
            for i, l_k in enumerate(long_units):
                if l_k < s_k:
                    cover = i
                    break
            if cover is None:
                total += s_k * 100.0
            else:
                l_k = long_units.pop(cover)
                total += (s_k - l_k) * 100.0
    return total


def payout_coverage(
    cash_reserve: float, collateral: float, threshold: float
) -> Tuple[float, float]:
    """回傳 (可動用現金, 相對紅線的餘裕)。"""
    usable = cash_reserve - collateral
    return usable, usable - threshold
