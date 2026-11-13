"""
/x 面板三項自相矛盾修正：
  F1 財報鎖定窗口下，情緒背離建議不得鼓勵賣方收租／賣 Put
  F2 結構性背離 overlay 依實際觸發分支描述
  F3 Gamma Flip 列不得「無法估算」與局部翻轉線並存
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.index_microstructure import estimate_symbol_gamma_flip

DRAM_PROFILE = {
    "60.0": 3_000_000,
    "61.0": 2_000_000,
    "62.0": 1_500_000,
    "63.0": -800_000,
    "64.0": -600_000,
}


def _text(data: dict[str, Any]) -> str:
    with patch("database.calendar_cache.get_cached_earnings", return_value=None):
        embed = create_tactical_symbol_embed(data)
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _base(
    skew_pct: float,
    pcr: float,
    dp: float = -0.2,
    iv_rank: float = 50.0,
    reddit: str = "⚖️ 中性",
    earnings: bool = False,
    catalyst: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "symbol": "NVDA",
        "price": 130.0,
        "quote": {
            "c": 130.0,
            "dp": dp,
            "d": -0.3,
            "o": 130.5,
            "h": 131.0,
            "l": 129.5,
            "pc": 130.3,
        },
        "skew": 1.0,
        "skew_percentile": skew_pct,
        "polymarket_odds": "N/A",
        "reddit_sentiment_score": reddit,
        "pcr": {"volume_pcr": pcr},
        "iv_data": {
            "iv_rank": iv_rank,
            "current_iv": 0.50,
            "iv_percentile": 55.0,
            "iv_status": "Normal",
            "has_earnings_event": earnings,
        },
    }
    if catalyst:
        data["catalysts"] = [SimpleNamespace(date="2026-12-31", days_to_earnings=4.3)]
    return data


OLD_HIGH = "僅允許小倉位收租"
LOCK_HIGH = "高度背離：嚴禁追價買權與賣方收租，僅限現貨防守減碼"


def test_high_divergence_with_earnings_flag_locks_out_income() -> None:
    text = _text(_base(0.0, 1.8, earnings=True))
    assert LOCK_HIGH in text
    assert OLD_HIGH not in text


def test_high_divergence_with_earnings_catalyst_locks_out_income() -> None:
    text = _text(_base(0.0, 1.8, catalyst=True))
    assert LOCK_HIGH in text
    assert OLD_HIGH not in text


def test_high_divergence_without_earnings_unchanged() -> None:
    text = _text(_base(0.0, 1.8))
    assert "高度背離：避免追價買權；僅允許小倉位收租並搭配保護" in text
    assert LOCK_HIGH not in text


def test_csp_advice_blocked_before_earnings() -> None:
    data = _base(12.0, 0.9, reddit="💀 恐慌 (Bearish)", earnings=True)
    text = _text(data)
    assert "考慮賣出賣權" not in text
    assert "財報鎖定窗口：勿賣出賣權" in text
    assert "考慮賣出賣權 (Cash Secured Put)" in _text(
        _base(12.0, 0.9, reddit="💀 恐慌 (Bearish)")
    )


def test_overlay_branch_a_put_skew_call_volume() -> None:
    text = _text(_base(88.0, 0.25))
    assert "避險分位極端（Put 貴）" in text
    assert "Volume PCR 極低" in text


def test_overlay_branch_b_call_skew_put_volume() -> None:
    text = _text(_base(5.0, 1.8))
    assert "Call 溢價分位極端" in text
    assert "Volume PCR 極高" in text
    assert "Skew 避險分位極端" not in text


def test_overlay_branch_c_d_make_no_pcr_claim() -> None:
    c = _text(_base(92.0, 0.9, dp=1.5))
    assert "現價上漲但 Skew 避險分位 >90%" in c
    d = _text(_base(50.0, 0.9, dp=-4.0, iv_rank=10.0))
    assert "現價大跌但 IV Rank 極低，留意快取異常" in d
    for t in (c, d):
        overlay = [ln for ln in t.splitlines() if "請以防守為主" in ln]
        assert overlay
        assert all("PCR" not in ln for ln in overlay)


def _gex_data(profile: dict[str, int], price: float, net: float) -> dict[str, Any]:
    return {
        "symbol": "DRAM",
        "price": price,
        "gex_profile_data": {
            "put_wall": 60.0,
            "call_wall": 62.0,
            "net_gex": net,
            "gex_profile": profile,
        },
    }


def test_local_flip_merged_into_single_gamma_flip_row() -> None:
    text = _text(_gex_data(DRAM_PROFILE, 62.90, 5_100_000.0))
    assert "Gamma Flip: ≈ $" in text
    assert "局部翻轉線" in text
    assert "無法估算" not in text
    assert "局部 Gamma 翻轉線" not in text
    # 閘門用的 flip 不採用局部值
    assert estimate_symbol_gamma_flip(DRAM_PROFILE, 62.90) == 0.0


def test_all_positive_profile_keeps_unestimable_row() -> None:
    profile = {"60.0": 1_000_000, "61.0": 2_000_000, "62.0": 1_500_000}
    text = _text(_gex_data(profile, 61.0, 4_500_000.0))
    assert "Gamma Flip: -- (無法估算)" in text
    assert "局部翻轉線" not in text
