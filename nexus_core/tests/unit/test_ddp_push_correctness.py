"""DDP 推播正確性：NaN 防護、真實 P/E 歷史、財季去重、強制掃描不外送。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from market_analysis.ddp_inspector import DDPInspector

_FUND = "services.market_data_service.fundamentals.get_pe_ttm_profile"
_COLS = pd.to_datetime(
    [
        "2026-07-26",
        "2026-04-26",
        "2026-01-25",
        "2025-10-26",
        "2025-07-27",
        "2025-04-27",
    ]
)


@pytest.fixture(autouse=True)
def _clear_pe_cache() -> Any:
    from services.market_data_service import fundamentals

    fundamentals._pe_profile_cache.clear()
    yield
    fundamentals._pe_profile_cache.clear()


def _q_inc(
    eps: Optional[List[float]] = None, rev: Optional[List[float]] = None
) -> pd.DataFrame:
    eps = eps or [150.0, 140.0, 130.0, 120.0, 100.0, 90.0]
    rev = rev or [500.0, 440.0, 430.0, 420.0, 410.0, 400.0]
    return pd.DataFrame(
        {c: [e, r] for c, e, r in zip(_COLS, eps, rev)},
        index=["Diluted EPS", "Total Revenue"],
    )


async def _inspect(
    q_inc: pd.DataFrame,
    info: Optional[Dict[str, Any]] = None,
    profile: Any = (15.0, [20.0] * 12),
) -> tuple[DDPInspector, Optional[Dict[str, Any]]]:
    insp = DDPInspector()
    info = info if info is not None else {"trailingPE": 15.0, "forwardPE": 10.0}
    with patch(
        "market_analysis.ddp_inspector.market_data_service.call_yf",
        new_callable=AsyncMock,
        return_value=(info, q_inc),
    ), patch(
        "market_analysis.ddp_inspector.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=pd.DataFrame(),
    ), patch(_FUND, new_callable=AsyncMock, return_value=profile):
        return insp, await insp.inspect_symbol("NVDA")


@pytest.mark.asyncio
async def test_nan_eps_fails() -> None:
    nan = float("nan")
    insp, report = await _inspect(_q_inc(eps=[150.0, 140.0, 130.0, 120.0, nan, 90.0]))
    assert report is None
    assert insp.last_fail_reason["NVDA"] == "資料不足（季報缺值）"
    insp, report = await _inspect(_q_inc(eps=[nan, 140.0, 130.0, 120.0, 100.0, 90.0]))
    assert report is None
    assert insp.last_fail_reason["NVDA"] == "資料不足（季報缺值）"


@pytest.mark.asyncio
async def test_nan_rev5_uses_fallback_threshold() -> None:
    nan = float("nan")
    # 營收年增 5% (<10%)：舊邏輯 prev_growth=0 → 通過；新邏輯走 >10% 備案 → 失敗
    rev = [420.0, 440.0, 430.0, 420.0, 400.0, nan]
    insp, report = await _inspect(_q_inc(rev=rev))
    assert report is None
    assert insp.last_fail_reason["NVDA"] == "未達門檻（營收未加速）"
    # 年增 > 10% 則通過
    rev = [500.0, 440.0, 430.0, 420.0, 400.0, nan]
    _, report = await _inspect(_q_inc(rev=rev))
    assert report is not None


@pytest.mark.asyncio
async def test_negative_forward_pe_fails() -> None:
    insp, report = await _inspect(
        _q_inc(), info={"trailingPE": 15.0, "forwardPE": -8.0}
    )
    assert report is None
    assert insp.last_fail_reason["NVDA"] == "未達門檻（Forward P/E ≤ 0，預期虧損）"


@pytest.mark.asyncio
async def test_pe_history_insufficient_fails() -> None:
    insp, report = await _inspect(_q_inc(), profile=(15.0, [20.0] * 7))
    assert report is None
    assert insp.last_fail_reason["NVDA"] == "資料不足（歷史本益比不足 3 年）"


@pytest.mark.asyncio
async def test_pe_finnhub_failure_is_transient() -> None:
    insp, report = await _inspect(_q_inc(), profile=None)
    assert report is None
    assert insp.last_fail_reason["NVDA"] == "資料暫不可用（Finnhub 本益比歷史）"


@pytest.mark.asyncio
async def test_pe_percentile_pass_and_fail() -> None:
    hist = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 28.0, 30.0, 32.0]
    # 25 分位 = 15.5：目前 P/E 15 通過、16 失敗；僅取最新 12 季
    _, ok = await _inspect(_q_inc(), profile=(15.0, hist + [1.0] * 5))
    assert ok is not None
    assert ok["pe_25th"] == pytest.approx(15.5)
    assert ok["pe_mean_3y"] == pytest.approx(21.0)  # 中位數
    assert ok["pe_history_quarters"] == 12
    insp, bad = await _inspect(_q_inc(), profile=(16.0, hist))
    assert bad is None
    assert insp.last_fail_reason["NVDA"] == "未達門檻（估值未壓縮）"


def test_no_price_percentile_path_remains() -> None:
    import inspect as _inspect_mod

    import market_analysis.ddp_inspector as mod

    src = _inspect_mod.getsource(mod)
    for token in ("price_25th", "price_mean", "fiveYearAvgPE"):
        assert token not in src


@pytest.mark.asyncio
async def test_run_scan_memo_once_per_day() -> None:
    insp = DDPInspector()
    calls: List[str] = []
    transient = {"on": True}

    async def fake_inspect(sym: str) -> Optional[Dict[str, Any]]:
        calls.append(sym)
        if sym == "BAD" and transient["on"]:
            insp._fail(sym, "資料暫不可用（Yahoo 限流冷卻中）")
            return None
        if sym == "NO":
            insp._fail(sym, "未達門檻（EPS 年增 3% < 15%）")
            return None
        insp.last_fail_reason.pop(sym, None)
        return {"symbol": sym, "is_ddp": True}

    with patch.object(insp, "inspect_symbol", side_effect=fake_inspect), patch(
        "market_analysis.ddp_inspector.asyncio.sleep", new_callable=AsyncMock
    ):
        r1 = await insp.run_scan(["OK", "NO", "BAD"])
        r2 = await insp.run_scan(["OK", "NO", "BAD"])
        assert [r["symbol"] for r in r1] == ["OK"]
        assert [r["symbol"] for r in r2] == ["OK"]
        # OK／NO 只抓一次；暫時性失敗的 BAD 每輪重抓
        assert calls == ["OK", "NO", "BAD", "BAD"]
        assert insp.last_fail_reason["NO"] == "未達門檻（EPS 年增 3% < 15%）"
        transient["on"] = False
        await insp.run_scan(["BAD"])
        await insp.run_scan(["BAD"])
        assert calls.count("BAD") == 3
        # 日期更替後重新評估
        with patch("market_analysis.ddp_inspector.datetime") as m_dt:
            m_dt.now.return_value.strftime.return_value = "29991231"
            await insp.run_scan(["OK"])
        assert calls.count("OK") == 2


@pytest.mark.asyncio
async def test_record_signal_once_per_fiscal_period(db_conn: Any) -> None:
    insp = DDPInspector()
    report = {
        "symbol": "NVDA",
        "current_pe": 15.0,
        "pe_mean_3y": 20.0,
        "eps_growth": 0.5,
        "rev_accel": True,
        "confidence_score": 80.0,
        "fiscal_period": "2026-07-26",
    }
    assert await insp.record_signal(report) is True
    assert await insp.record_signal(report) is False
    assert await insp.record_signal({**report, "fiscal_period": "2026-10-25"}) is True
    n = db_conn.execute(
        "SELECT COUNT(*) FROM ddp_signals WHERE symbol='NVDA'"
    ).fetchone()[0]
    assert n == 2


def _scan_cog(reports: List[Dict[str, Any]], record_result: bool = True) -> Any:
    from cogs.trading.scan import MarketScanCog

    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    cog = MarketScanCog(bot)
    ts = MagicMock()
    ts.run_ddp_scan = AsyncMock(return_value=reports)
    ts.ddp_inspector.record_signal = AsyncMock(return_value=record_result)
    ts.run_iv_opportunity_scan = AsyncMock(
        return_value=[
            {"symbol": "AAA", "status": "x", "strategy": "S", "is_high_risk_vol": False}
        ]
    )
    ts.run_market_scan = AsyncMock(return_value={})
    cog.trading_service = ts
    return cog


_REPORT = {"symbol": "AAA", "fiscal_period": "2026-07-26"}
_WATCH = [(1, "AAA", 0), (2, "AAA", 0), (2, "BBB", 0)]


def _scan_patches() -> Any:
    ctx = MagicMock()
    ctx.option_alert_mode = 1
    return (
        patch("cogs.trading.scan.database.get_all_watchlist", return_value=_WATCH),
        patch("cogs.trading.scan.database.get_full_user_context", return_value=ctx),
        patch(
            "cogs.trading.scan.is_channel_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("cogs.embed_builder.create_ddp_embed", return_value=MagicMock()),
        patch("cogs.embed_builder.create_volatility_embed", return_value=MagicMock()),
    )


@pytest.mark.asyncio
async def test_auto_scan_pushes_only_new_fiscal_period() -> None:
    for is_new, expected in ((True, 2), (False, 0)):
        cog = _scan_cog([_REPORT], record_result=is_new)
        p1, p2, p3, p4, p5 = _scan_patches()
        with p1, p2, p3, p4, p5, patch(
            "cogs.trading.scan.notify", new_callable=AsyncMock
        ) as m_notify, patch.object(
            cog, "_should_send_alert", new_callable=AsyncMock, return_value=True
        ):
            await cog._run_market_scan_logic(is_auto=True)
        ddp_calls = [
            c
            for c in m_notify.await_args_list
            if c.args[2] == "alpha_market_signals"
            and str(c.kwargs.get("dedup_key", "")).startswith("ddp_alert_")
        ]
        assert len(ddp_calls) == expected
        cog.trading_service.ddp_inspector.record_signal.assert_awaited_once()


@pytest.mark.asyncio
async def test_forced_scan_replies_only_to_admin() -> None:
    cog = _scan_cog([_REPORT])
    admin = MagicMock()
    admin.id = 1
    admin.send = AsyncMock()
    p1, p2, p3, p4, p5 = _scan_patches()
    with p1, p2, p3, p4, p5, patch(
        "cogs.trading.scan.notify", new_callable=AsyncMock
    ) as m_notify, patch(
        "cogs.trading.scan.notify_many", new_callable=AsyncMock
    ), patch.object(
        cog, "_should_send_alert", new_callable=AsyncMock, return_value=True
    ):
        await cog._run_market_scan_logic(is_auto=False, triggered_by=admin)

    cog.trading_service.ddp_inspector.record_signal.assert_not_awaited()
    cog.bot.queue_dm.assert_not_called()
    m_notify.assert_not_awaited()
    # IV 只掃管理員（uid=1）自己的自選
    iv_calls = cog.trading_service.run_iv_opportunity_scan.await_args_list
    assert [c.args for c in iv_calls] == [(["AAA"], 1)]
    embeds = [c.kwargs.get("embed") for c in admin.send.await_args_list]
    assert len([e for e in embeds if e is not None]) == 2  # DDP + IV


def test_ddp_embed_labels_fit_and_new_wording() -> None:
    from cogs.embed_builders._ansi_utils import _visual_len
    from cogs.embed_builders.report_embeds import create_ddp_embed

    report = {
        "symbol": "NVDA",
        "current_pe": 15.0,
        "pe_mean_3y": 20.0,
        "pe_25th": 16.0,
        "eps_growth": 0.3,
        "rev_accel": True,
        "confidence_score": 80.0,
        "forward_pe": 12.0,
        "rvol": 1.0,
    }
    embed = create_ddp_embed(report)
    table = str(embed.fields[0].value)
    assert "3 年 P/E 中位數 (3Y)" in table
    assert "均值" not in table
    for line in table.splitlines()[3:]:
        if line.startswith("```"):
            continue
        label = line.split(" | ")[0]
        assert _visual_len(label.rstrip()) <= 24
    logic = " ".join(str(f.value) for f in embed.fields[1:])
    assert "25 分位" in logic and "> 0" in logic
