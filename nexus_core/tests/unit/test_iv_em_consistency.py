"""
tests/unit/test_iv_em_consistency.py

IV 與 Expected Move 同源化：
  * 即時 IV 與跨式反推 IV 相差 > 4 倍 → 判為尺度錯誤並改用跨式反推值（且寫入 DB 的是修正值）
  * 財報週正常的 2~3 倍事件溢價不得被誤修正
  * 財報日晚於期限結構近月到期日 → 標記 earnings_after_near_term
  * 呈現層：EM 揭露跨式隱含 IV、財報文案依 IV 來源分流、期限結構死區改稱「持平」、
    IV Rank 未知不得被當成 0%、高波建議不得與 STO 禁用互相矛盾
fixture 取自實盤回報（SNDK IV 6.9% / EM ±$218.44）。
"""

from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.embed_builders.portfolio_embeds import create_tactical_symbol_embed
from market_analysis.risk_engine import OptimizationResult
from market_analysis.sentiment.iv_metrics import (
    fetch_and_calculate_iv_metrics,
    straddle_implied_annual_iv,
)
from models.quant import IVMetrics

SNDK_SPOT = 1767.37
SNDK_STRADDLE_EM = 218.44


def _db_conn(rows: list[tuple[str, float]]) -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn.cursor.return_value = cursor
    return conn


async def _run_metrics(
    live_iv: float,
    straddle_em: float,
    spot: float = SNDK_SPOT,
    earnings: dict[str, Any] | None = None,
    expiries: list[str] | None = None,
    term: tuple[str | None, float | None] = ("Contango", 0.74),
) -> tuple[IVMetrics, AsyncMock]:
    save_mock = AsyncMock()
    with (
        patch(
            "services.market_data_service.get_quote",
            new=AsyncMock(return_value={"c": spot, "h": spot, "l": spot}),
        ),
        patch("market_analysis.sentiment.iv_metrics.is_market_open", return_value=True),
        patch(
            "services.market_data_service.call_yf",
            new=AsyncMock(return_value={"impliedVolatility": live_iv}),
        ),
        patch(
            "database.connection.get_read_connection",
            return_value=_db_conn([]),
        ),
        patch("market_analysis.sentiment.iv_metrics.save_historical_iv", new=save_mock),
        patch(
            "market_analysis.sentiment.iv_metrics._calculate_straddle_implied_em",
            new=AsyncMock(return_value=straddle_em),
        ),
        patch(
            "market_analysis.sentiment.iv_metrics._calculate_iv_term_structure",
            new=AsyncMock(return_value=term),
        ),
        patch("database.calendar_cache.get_cached_earnings", return_value=earnings),
        patch("database.calendar_cache.get_macro_events_between", return_value=[]),
        patch(
            "services.market_data_service.get_all_option_expiries",
            new=AsyncMock(return_value=expiries or []),
        ),
        patch(
            "services.market_data_service.get_history_df",
            new=AsyncMock(return_value=__import__("pandas").DataFrame()),
        ),
    ):
        res = await fetch_and_calculate_iv_metrics("SNDK", force_refresh=True)
    return res, save_mock


def test_straddle_implied_iv_matches_report_back_solve() -> None:
    iv = straddle_implied_annual_iv(SNDK_STRADDLE_EM, SNDK_SPOT)
    assert iv is not None
    assert iv == pytest.approx(0.8925, abs=0.002)
    assert straddle_implied_annual_iv(0.0, SNDK_SPOT) is None


@pytest.mark.asyncio
async def test_scale_error_is_corrected_before_db_write() -> None:
    res, save_mock = await _run_metrics(live_iv=0.069, straddle_em=SNDK_STRADDLE_EM)
    assert res.iv_scale_corrected
    assert res.current_iv == pytest.approx(0.8925, abs=0.002)
    assert res.straddle_implied_iv == pytest.approx(res.current_iv)
    # 寫進 historical_iv 的必須是修正值，錯誤值不得污染 IV Rank 母體
    assert save_mock.await_args is not None
    saved_iv = save_mock.await_args.args[1]
    assert saved_iv == pytest.approx(0.8925, abs=0.002)
    assert res.expected_move_weekly == pytest.approx(SNDK_STRADDLE_EM)


@pytest.mark.asyncio
async def test_event_premium_within_4x_is_not_corrected() -> None:
    """財報週週度 IV 為 30D IV 的 2.5 倍屬正常事件溢價。"""
    straddle_em = SNDK_SPOT * 1.0 * (7.0 / 365.0) ** 0.5  # 反推 IV = 100%
    res, _ = await _run_metrics(live_iv=0.40, straddle_em=straddle_em)
    assert not res.iv_scale_corrected
    assert res.current_iv == pytest.approx(0.40)
    assert res.straddle_implied_iv == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_earnings_after_near_term_expiry_is_flagged() -> None:
    """MU：Contango 0.74 與「臨近財報」並存——財報落在近月到期日之後。"""
    today = datetime.now().date()
    near = (today + timedelta(days=6)).isoformat()
    far = (today + timedelta(days=30)).isoformat()
    earn = (today + timedelta(days=10)).isoformat()
    res, _ = await _run_metrics(
        live_iv=0.60,
        straddle_em=SNDK_SPOT * 0.6 * (7.0 / 365.0) ** 0.5,
        earnings={"earnings_date": earn},
        expiries=[near, far],
    )
    assert res.has_earnings_event
    assert res.earnings_date == earn
    assert res.earnings_after_near_term


@pytest.mark.asyncio
async def test_earnings_inside_near_term_is_not_flagged() -> None:
    today = datetime.now().date()
    near = (today + timedelta(days=12)).isoformat()
    far = (today + timedelta(days=30)).isoformat()
    earn = (today + timedelta(days=8)).isoformat()
    res, _ = await _run_metrics(
        live_iv=0.60,
        straddle_em=SNDK_SPOT * 0.6 * (7.0 / 365.0) ** 0.5,
        earnings={"earnings_date": earn},
        expiries=[near, far],
    )
    assert res.has_earnings_event
    assert not res.earnings_after_near_term


# ── 呈現層 ──────────────────────────────────────────────────


def _embed_text(data: dict[str, Any]) -> str:
    embed = create_tactical_symbol_embed(data)
    return "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)


def _iv(**overrides: Any) -> IVMetrics:
    base: dict[str, Any] = dict(
        symbol="SNDK",
        current_iv=0.8925,
        iv_rank=None,
        iv_percentile=None,
        expected_move_weekly=SNDK_STRADDLE_EM,
        iv_status="Normal",
        iv_source="LIVE_IV",
        reference_spot_price=SNDK_SPOT,
        iv_term_structure_status="Normal",
        term_structure_ratio=1.04,
        straddle_implied_iv=0.8925,
    )
    base.update(overrides)
    return IVMetrics(**base)


def test_em_line_discloses_straddle_iv_and_correction() -> None:
    text = _embed_text(
        {
            "symbol": "SNDK",
            "quote": {"c": SNDK_SPOT},
            "iv_data": _iv(iv_scale_corrected=True),
        }
    )
    assert "跨式定價，隱含 IV 89.2%" in text
    assert "已改用跨式反推值" in text


def test_term_structure_deadband_is_labelled_flat() -> None:
    text = _embed_text({"symbol": "MRNA", "quote": {"c": 170.0}, "iv_data": _iv()})
    assert "持平 (Flat, 0.95~1.05)" in text
    assert "正常 (Normal)" not in text


def test_live_iv_earnings_after_near_term_wording() -> None:
    iv = _iv(
        has_earnings_event=True,
        earnings_date=(date.today() + timedelta(days=10)).isoformat(),
        earnings_after_near_term=True,
        iv_term_structure_status="Contango",
        term_structure_ratio=0.74,
    )
    text = _embed_text({"symbol": "MU", "quote": {"c": 1073.0}, "iv_data": iv})
    assert "晚於近月到期，期限結構未含事件溢價" in text
    assert "快取波動率可能低估" not in text


def _psq() -> dict[str, Any]:
    return {
        "is_squeezing": False,
        "momentum": 0.1,
        "squeeze_level": "Release",
        "direction": "Neutral",
        "vix_momentum_label": "NORMAL",
    }


def test_high_vol_note_respects_sto_lockout_and_names_real_trigger() -> None:
    """SNDK 第 4 版：STO 禁用與「建議使用賣方策略」不得同時出現；觸發條件是 IV 不是 IVR。"""
    text = _embed_text(
        {
            "symbol": "SNDK",
            "quote": {"c": SNDK_SPOT},
            "iv_data": _iv(current_iv=1.04),
            "psq_result": _psq(),
            "kelly_sizing": OptimizationResult(
                suggested_contracts=0,
                exposure_pct=0.0,
                warnings=["VIX Dormant: STO 禁用（大盤 VIX < 15）"],
            ),
        }
    )
    assert "極端高波環境 (IV 104%)" in text
    assert "賣方策略目前受風控禁用" in text
    assert "建議縮小部位或使用期權賣方策略保護" not in text
    assert "IVR > 50%" not in text


def test_unknown_iv_rank_does_not_produce_low_vol_note() -> None:
    text = _embed_text(
        {
            "symbol": "BE",
            "quote": {"c": 280.97},
            "iv_data": _iv(current_iv=0.30, straddle_implied_iv=None),
            "psq_result": _psq(),
        }
    )
    assert "低波期" not in text
