"""做空進場測試共用的資料建構器 (非測試模組，不含 test_ 前綴)。"""

from typing import Any

from market_analysis.dynamic_rollover.models import ShortEntryEvaluation


def make_short_entry_evaluation(**overrides: Any) -> ShortEntryEvaluation:
    """建立一份合法的「區間內做空」評估結果。

    預設值：現價 100、頂牆 104、Call Wall 105、Put Wall 90、ATR₁₅ₘ 1.0、ATR₁D 3.0。
    結構停損 = 104 + 0.5 = 104.5；出場引擎停損 = Call Wall 105 + 0.5 = 105.5
    (取較遠者 105.5)；目標 90；R:R = 10 / 5.5 ≈ 1.82。
    """
    base: dict[str, Any] = {
        "all_passed": True,
        "reason": "做空條件一✅ | 做空條件二✅ | 做空條件三✅ | 做空條件四✅ | 做空條件五✅ | 做空條件六✅",
        "structure_directive": "Long Put (輕度 OTM)",
        "sub_mode": "區間內做空",
        "conditions": (True, True, True, True, True, True),
        "spot": 100.0,
        "resistance_wall": 104.0,
        "call_wall": 105.0,
        "put_wall": 90.0,
        "gamma_flip": 101.0,
        "next_negative_node": 0.0,
        "net_gex": -1_000_000.0,
        "session_vwap": 101.0,
        "atr_15m": 1.0,
        "atr_1d": 3.0,
        "ivr": 30.0,
    }
    base.update(overrides)
    return ShortEntryEvaluation(**base)
