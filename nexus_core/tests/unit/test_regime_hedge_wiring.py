"""兩級體制避險接線的純函式與旗標測試。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import config
from cogs.trading.portfolio_monitor import (
    is_regime_hedge_tier1_dry_run,
    regime_hedge_dedup_suffix,
)


def _ins(tier: int, mode: str, prelim: bool) -> dict[str, Any]:
    return {
        "scenario": "REGIME_HEDGE",
        "hedge_tier": tier,
        "is_preliminary": prelim,
        "hedge_plan": SimpleNamespace(tier=tier, mode=mode),
    }


def test_dedup_suffix_prelim_and_final_differ() -> None:
    prelim = regime_hedge_dedup_suffix(_ins(2, "HEDGE", True))
    final = regime_hedge_dedup_suffix(_ins(2, "HEDGE", False))
    assert prelim == "_T2_HEDGE_PRELIM"
    assert final == "_T2_HEDGE_FINAL"
    assert prelim != final
    # 同為第二級，HEDGE 轉 STOP_SHORT 也是新事件
    assert regime_hedge_dedup_suffix(_ins(2, "STOP_SHORT", False)) != final
    # 非 REGIME_HEDGE 情境不附加後綴
    assert regime_hedge_dedup_suffix({"scenario": "SHORT_ENTRY"}) == ""


def test_tier1_dry_run_flag_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGIME_HEDGE_TIER1_DRY_RUN", raising=False)
    assert (
        config.get_env_or_secret("REGIME_HEDGE_TIER1_DRY_RUN", "true").lower() == "true"
    )
    with patch.object(config, "REGIME_HEDGE_TIER1_DRY_RUN", True):
        assert is_regime_hedge_tier1_dry_run(_ins(1, "TIER1_PSQ", False)) is True
    with patch.object(config, "REGIME_HEDGE_TIER1_DRY_RUN", False):
        assert is_regime_hedge_tier1_dry_run(_ins(1, "TIER1_PSQ", False)) is False


def test_tier2_never_dry_run() -> None:
    with patch.object(config, "REGIME_HEDGE_TIER1_DRY_RUN", True):
        assert is_regime_hedge_tier1_dry_run(_ins(2, "HEDGE", False)) is False
        assert is_regime_hedge_tier1_dry_run(_ins(2, "STOP_SHORT", True)) is False
        # 非 REGIME_HEDGE 情境即使帶 hedge_tier=1 也不受影響
        assert (
            is_regime_hedge_tier1_dry_run({"scenario": "SHORT_ENTRY", "hedge_tier": 1})
            is False
        )
