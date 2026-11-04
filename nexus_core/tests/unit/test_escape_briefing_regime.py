"""盤前「宏觀逃頂」簡報：因子 4 與進場閘門同源、依逃頂分級的指引、逃頂窗口退役。"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_analysis.analyst_runners import strategy_runner
from market_analysis.squeeze_entry.vetoes import compute_macro_escape_tier

_FORBIDDEN_WORDS = ("窗口", "前移", "後推", "減倉", "減碼", "撤退", "網格", "裸賣")


async def _run_briefing(
    *,
    regime: str,
    vts_ratio: Optional[float],
    fear_greed: Optional[float],
    prob: Optional[float],
    prob_stale: bool = False,
    defense_enabled: bool = False,
    prob_details: Optional[dict[str, Any]] = None,
) -> tuple[Any, list[str]]:
    """以同一組輸入跑簡報；回傳 (embed, 逃頂評分 tier 紀錄)。"""
    import market_analysis.index_microstructure as ims

    tiers: list[str] = []
    real_score = ims.evaluate_macro_top_escape_score

    def _spy(**kwargs: Any) -> Any:
        result = real_score(**kwargs)
        tiers.append(str(result[1]))
        return result

    vts_payload: dict[str, Any] = (
        {"vts_ratio": vts_ratio, "vts_state": "Contango", "is_valid": True}
        if vts_ratio is not None
        else {"vts_ratio": 0.0, "vts_state": "UNKNOWN", "is_valid": False}
    )
    core_metrics: dict[str, Any] = {
        "fear_greed": fear_greed,
        "_is_fallback": fear_greed is None,
    }
    ctx = MagicMock()
    ctx.enable_macro_top_escape_defense = defense_enabled
    from services.calendar_service import calendar_service

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "market_analysis.index_microstructure.get_market_regime",
                new_callable=AsyncMock,
                return_value=regime,
            )
        )
        stack.enter_context(
            patch(
                "market_analysis.analyst_runners.strategy_runner.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value=vts_payload,
            )
        )
        stack.enter_context(
            patch(
                "services.market_data_service.get_vix_term_structure",
                new_callable=AsyncMock,
                return_value=vts_payload,
            )
        )
        stack.enter_context(
            patch(
                "market_analysis.index_microstructure.fetch_core_macro_metrics",
                new_callable=AsyncMock,
                return_value=core_metrics,
            )
        )
        stack.enter_context(
            patch.object(
                calendar_service,
                "get_latest_fedwatch_info",
                return_value=(prob, False, prob_details or {}, prob_stale),
            )
        )
        stack.enter_context(
            patch(
                "database.cache.get_fedwatch_probability_fresh",
                return_value=(prob, prob_stale),
            )
        )
        stack.enter_context(patch("database.cache.get_kv_cache", return_value=None))
        stack.enter_context(
            patch("database.user_settings.get_full_user_context", return_value=ctx)
        )
        stack.enter_context(patch.object(ims, "evaluate_macro_top_escape_score", _spy))
        embed = await strategy_runner.run_fomc_escape_window_analysis(1)
        # 同一組輸入再跑一次進場閘門的 tier（spy 會多記一筆）
        gate_tier = await compute_macro_escape_tier()
    assert embed is not None
    # 簡報與進場閘門各跑一次評分 + 閘門回傳值 = 3 筆，確保兩邊都真的算到分數
    assert len(tiers) == 2
    return embed, tiers + [gate_tier]


def _fields(embed: Any) -> dict[str, str]:
    return {str(f.name): str(f.value or "") for f in embed.fields}


def _factor4_row(embed: Any) -> str:
    panel = _fields(embed)["📊 多因子監測結果"]
    rows = [r for r in panel.splitlines() if "大盤微觀結構" in r]
    assert len(rows) == 1
    return rows[0]


def _directive(embed: Any) -> str:
    return _fields(embed)["🎯 戰術行動指引"]


@pytest.mark.asyncio
async def test_factor4_short_gamma_regime_is_negative_and_tightens() -> None:
    embed, tiers = await _run_briefing(
        regime="SHORT_GAMMA_CRITICAL",
        vts_ratio=0.85,
        fear_greed=50.0,
        prob=0.5,
    )
    assert "🚨 負 Gamma 踩踏" in _factor4_row(embed)
    # 負 Gamma 計入收縮 → 逃頂評分 1 分 = WATCH
    assert tiers[0] == "WATCH"
    assert "收縮" in _fields(embed)["💡 推演邏輯與風控分析"]


@pytest.mark.asyncio
async def test_factor4_normal_regime_is_not_called_positive_gamma() -> None:
    embed, _ = await _run_briefing(
        regime="NORMAL", vts_ratio=0.85, fear_greed=50.0, prob=0.5
    )
    row = _factor4_row(embed)
    assert "🟢 未達踩踏條件" in row
    assert "正 Gamma" not in row
    assert (
        "正 Gamma"
        not in _fields(embed)["🎯 宏觀逃頂綜合評分 (Top-Escape Composite Score)"]
    )


@pytest.mark.asyncio
async def test_factor4_unknown_regime_not_scored_and_tier_unknown() -> None:
    embed, tiers = await _run_briefing(
        regime="UNKNOWN", vts_ratio=0.85, fear_greed=50.0, prob=0.5
    )
    assert "⚪ 未知（不計分）" in _factor4_row(embed)
    assert tiers[0] == "UNKNOWN"
    assert "資料不足，無法判定逃頂分級" in _directive(embed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected_tier"),
    [
        (
            {"regime": "NORMAL", "vts_ratio": 0.85, "fear_greed": 50.0, "prob": 0.5},
            "NORMAL",
        ),
        (
            {
                "regime": "SHORT_GAMMA_CRITICAL",
                "vts_ratio": 0.85,
                "fear_greed": 50.0,
                "prob": 0.5,
            },
            "WATCH",
        ),
        (
            {
                "regime": "SYSTEMIC_LIQUIDITY_CRISIS",
                "vts_ratio": 1.05,
                "fear_greed": 50.0,
                "prob": 0.5,
            },
            "ELEVATED",
        ),
        (
            {
                "regime": "SHORT_GAMMA_CRITICAL",
                "vts_ratio": 1.05,
                "fear_greed": 85.0,
                "prob": 0.5,
            },
            "CRITICAL",
        ),
        (
            {"regime": "UNKNOWN", "vts_ratio": 0.85, "fear_greed": 50.0, "prob": 0.5},
            "UNKNOWN",
        ),
        (
            {
                "regime": "NORMAL",
                "vts_ratio": 0.85,
                "fear_greed": 50.0,
                "prob": None,
                "prob_stale": True,
            },
            "UNKNOWN",
        ),
    ],
)
async def test_briefing_tier_equals_entry_gate_tier(
    kwargs: dict[str, Any], expected_tier: str
) -> None:
    """同一組輸入下，簡報的逃頂 tier 必須等於擠壓進場閘門算出的 tier。"""
    _, tiers = await _run_briefing(**kwargs)
    briefing_tier, gate_tier = tiers[0], tiers[-1]
    assert briefing_tier == gate_tier == expected_tier


@pytest.mark.asyncio
async def test_directive_normal_keeps_holding() -> None:
    embed, _ = await _run_briefing(
        regime="NORMAL", vts_ratio=0.85, fear_greed=50.0, prob=0.5
    )
    text = _directive(embed)
    assert "維持持有" in text and "無需額外避險" in text
    assert "保護性 Put" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("regime", ["SHORT_GAMMA_CRITICAL"])
async def test_directive_watch_defense_on_vs_off(regime: str) -> None:
    on, _ = await _run_briefing(
        regime=regime,
        vts_ratio=0.85,
        fear_greed=50.0,
        prob=0.5,
        defense_enabled=True,
    )
    off, _ = await _run_briefing(
        regime=regime,
        vts_ratio=0.85,
        fear_greed=50.0,
        prob=0.5,
        defense_enabled=False,
    )
    for text in (_directive(on), _directive(off)):
        assert "SPY 保護性 Put" in text
        assert "情境 6" in text and "30%" in text and "保留上檔" in text
    assert "具體口數見盤中情境 6 推播" in _directive(on)
    assert "/settings" not in _directive(on)
    assert "可於 /settings 開啟「宏觀逃頂前瞻防禦」以收到口數建議" in _directive(off)
    assert "具體口數" not in _directive(off)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"regime": "NORMAL", "vts_ratio": 0.85, "fear_greed": 50.0, "prob": 0.5},
        {
            "regime": "SHORT_GAMMA_CRITICAL",
            "vts_ratio": 1.05,
            "fear_greed": 85.0,
            "prob": 0.9,
        },
        {"regime": "UNKNOWN", "vts_ratio": None, "fear_greed": None, "prob": None},
    ],
)
async def test_embed_has_no_window_or_trim_wording(kwargs: dict[str, Any]) -> None:
    embed, _ = await _run_briefing(**kwargs)
    names = [str(f.name) for f in embed.fields]
    assert not any("窗口" in n for n in names)
    blob = " ".join(
        [str(embed.title), str(embed.footer.text)]
        + [f"{f.name} {f.value}" for f in embed.fields]
    )
    for word in _FORBIDDEN_WORDS:
        assert word not in blob, word
    assert embed.title == "🧭 宏觀逃頂：總經流動性狀態與逃頂評分 (Macro Escape Matrix)"
    assert str(embed.footer.text).endswith("Risk Engine | 宏觀流動性狀態與逃頂評分")


def test_matrix_returns_no_days_and_keeps_te_rules() -> None:
    from market_analysis.index_microstructure import evaluate_escape_window_regime

    result = evaluate_escape_window_regime(
        prob=0.9, cpi_dev=0.4, wti=95.0, vts_ratio=1.1, is_negative_gamma=True
    )
    assert len(result) == 5
    t, e, direction, _tier, status = result
    assert (t, e) == (4, 0)
    assert direction == "收縮"
    assert status == "⚠️ 收縮 (高利率+通膨油價雙升+波動倒掛+結構承壓)"
    assert "天" not in status

    t, e, direction, _tier, status = evaluate_escape_window_regime(
        prob=0.3, cpi_dev=-0.1, wti=70.0, vts_ratio=0.8, is_negative_gamma=False
    )
    assert (t, e, direction) == (0, 4, "寬鬆")
    assert status == "🟢 寬鬆擴張 (流動性擴張)"

    t, e, direction, _tier, status = evaluate_escape_window_regime(
        prob=0.55, cpi_dev=0.0, wti=75.0, vts_ratio=0.95, is_negative_gamma=None
    )
    assert direction == "均衡"
    assert status == "🟢 均衡"


def test_escape_window_helpers_removed() -> None:
    for name in (
        "_resolve_escape_window",
        "_shift_business_days",
        "_find_next_window_month",
        "_get_period_label",
    ):
        assert not hasattr(strategy_runner, name)
