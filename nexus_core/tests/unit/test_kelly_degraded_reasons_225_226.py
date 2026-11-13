"""#225／#226：/x Kelly 降級原因——OI PCR 僅快取判定。"""

from typing import Any, Optional

import pytest

from cogs.unified_terminal.symbol_deep_dive import _oi_pcr_degraded_reason


@pytest.mark.parametrize(
    "pcr_data, expected",
    [
        ({"oi_pcr": 0.85}, None),
        ({"oi_pcr": None, "oi_pcr_cached": 0.85}, "OI PCR 僅快取"),
        ({"oi_pcr": None, "oi_pcr_cached": None}, "OI PCR 缺失"),
        ({"oi_pcr": None, "oi_pcr_cached": 0}, "OI PCR 缺失"),
        ({"oi_pcr": None}, "OI PCR 缺失"),
        ({"oi_pcr": 0, "oi_pcr_cached": 0.9}, "OI PCR 僅快取"),
        (None, "OI PCR 缺失"),
        ("bad", "OI PCR 缺失"),
        ({"pcr": 0.9}, None),
    ],
)
def test_oi_pcr_degraded_reason(pcr_data: Any, expected: Optional[str]) -> None:
    assert _oi_pcr_degraded_reason(pcr_data) == expected
