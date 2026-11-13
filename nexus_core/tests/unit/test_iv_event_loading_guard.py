"""鎖定「1.4x 事件加載不得套用於跨式反推 IV」的不變式。

跨式反推 IV（straddle_implied_iv）已內含事件定價，若再 ×1.4 等於重複計入事件溢價。
本檔以 fetch_and_calculate_iv_metrics 的 STORED_IV 路徑驗證，並直接測試純函式
_should_apply_event_loading 的判定表。
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from market_analysis.sentiment import iv_metrics
from market_analysis.sentiment.iv_metrics import (
    IvSource,
    _should_apply_event_loading,
    straddle_implied_annual_iv,
)

_SPOT = 100.0
_STORED_IV = 0.50
# 週度 1σ 跨式 EM 7.0 @ spot 100 → 反推年化 IV ≈ 0.5054（與 STORED_IV 不同，便於區分）
_STRADDLE_EM = 7.0


@contextmanager
def _stored_iv_patches(
    *,
    earnings_in_window: bool,
    straddle_em: float | None,
) -> Iterator[dict[str, Any]]:
    """把 fetch_and_calculate_iv_metrics 的外部依賴全部替換為固定值（STORED_IV 路徑）。"""
    earnings_date_str = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    earnings_payload = (
        {"earnings_date": earnings_date_str} if earnings_in_window else None
    )
    with patch.object(
        iv_metrics, "get_last_stored_iv", return_value=_STORED_IV
    ), patch.object(iv_metrics, "is_market_open", return_value=False), patch.object(
        iv_metrics,
        "_calculate_straddle_implied_em",
        new_callable=AsyncMock,
        return_value=straddle_em,
    ), patch.object(
        iv_metrics,
        "_calculate_iv_term_structure",
        new_callable=AsyncMock,
        return_value=(None, None),
    ), patch(
        "services.market_data_service.get_quote",
        new_callable=AsyncMock,
        return_value={"c": _SPOT},
    ), patch(
        "services.market_data_service.get_history_df",
        new_callable=AsyncMock,
        return_value=pd.DataFrame(),
    ), patch(
        "services.market_data_service.get_all_option_expiries",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "database.calendar_cache.get_cached_earnings",
        return_value=earnings_payload,
    ), patch(
        "database.calendar_cache.get_macro_events_between",
        return_value=[],
    ), patch("database.cache.save_kv_cache", new_callable=AsyncMock), patch(
        "database.connection.get_read_connection",
        side_effect=RuntimeError("db disabled in unit test"),
    ), patch.dict("market_analysis.sentiment.iv_metrics._iv_cache"):
        yield {"earnings_date": earnings_date_str}


@pytest.mark.asyncio
async def test_stored_iv_event_loading_skips_straddle_value() -> None:
    expected_straddle_iv = straddle_implied_annual_iv(_STRADDLE_EM, _SPOT)
    assert expected_straddle_iv is not None

    with _stored_iv_patches(earnings_in_window=True, straddle_em=_STRADDLE_EM):
        metrics = await iv_metrics.fetch_and_calculate_iv_metrics(
            "ZZIVGUARD", force_refresh=True
        )

    assert metrics.iv_source == "STORED_IV"
    assert metrics.event_loading_applied is True
    assert metrics.current_iv == pytest.approx(_STORED_IV * 1.4)
    # 跨式反推值原樣揭露，絕不被 ×1.4 加載
    assert metrics.straddle_implied_iv == pytest.approx(expected_straddle_iv)
    # 跨式值本身未被 ×1.4
    assert metrics.straddle_implied_iv != pytest.approx(expected_straddle_iv * 1.4)
    assert metrics.iv_scale_corrected is False


@pytest.mark.asyncio
async def test_stored_iv_without_event_is_not_loaded() -> None:
    with _stored_iv_patches(earnings_in_window=False, straddle_em=_STRADDLE_EM):
        metrics = await iv_metrics.fetch_and_calculate_iv_metrics(
            "ZZIVGUARD", force_refresh=True
        )

    assert metrics.iv_source == "STORED_IV"
    assert metrics.event_loading_applied is False
    assert metrics.current_iv == pytest.approx(_STORED_IV)


@pytest.mark.asyncio
async def test_helper_false_disables_event_loading_on_stored_path() -> None:
    with _stored_iv_patches(
        earnings_in_window=True, straddle_em=_STRADDLE_EM
    ), patch.object(
        iv_metrics, "_should_apply_event_loading", return_value=False
    ) as mock_helper:
        metrics = await iv_metrics.fetch_and_calculate_iv_metrics(
            "ZZIVGUARD", force_refresh=True
        )

    # 分支必須經由 helper 判定：helper 回 False 時不得加載
    mock_helper.assert_called_once()
    assert mock_helper.call_args.args == ()
    assert mock_helper.call_args.kwargs["iv_source"] == "STORED_IV"
    assert mock_helper.call_args.kwargs["has_event"] is True
    assert mock_helper.call_args.kwargs["iv_scale_corrected"] is False
    assert metrics.event_loading_applied is False
    assert metrics.current_iv == pytest.approx(_STORED_IV)


@pytest.mark.parametrize(
    ("iv_source", "has_event", "iv_scale_corrected", "expected"),
    [
        ("STORED_IV", True, False, True),
        ("HV_PROXY", True, False, True),
        ("STORED_IV", False, False, False),
        ("HV_PROXY", False, False, False),
        # 守門：跨式反推已取代 current_iv，即使來源為快取／HV 也不得再加載
        ("STORED_IV", True, True, False),
        ("HV_PROXY", True, True, False),
        ("STORED_IV", False, True, False),
        # LIVE_IV 與 UNAVAILABLE 本來就不走 ×1.4
        ("LIVE_IV", True, False, False),
        ("LIVE_IV", True, True, False),
        ("UNAVAILABLE", True, False, False),
    ],
)
def test_should_apply_event_loading_truth_table(
    iv_source: IvSource, has_event: bool, iv_scale_corrected: bool, expected: bool
) -> None:
    assert (
        _should_apply_event_loading(
            iv_source=iv_source,
            has_event=has_event,
            iv_scale_corrected=iv_scale_corrected,
        )
        is expected
    )


def test_helper_is_keyword_only() -> None:
    # 三個參數皆為關鍵字參數：位置呼叫必須拋出 TypeError（避免兩個 bool 被靜默對調）
    fn: Any = _should_apply_event_loading
    with pytest.raises(TypeError):
        fn("STORED_IV", True, False)
