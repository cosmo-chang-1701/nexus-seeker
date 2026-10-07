"""實體替代數據純函式計算層 (alt_data_metrics) 單元測試。

fixture 皆為真實資料裁切：
- SEC companyconcept：2026-10-07 以合規 UA 自 data.sec.gov 抓取後，只保留 2024 年後事實。
- TSA：tsa.gov 對本機回 HTTP 403（Akamai），改經 web.archive.org `id_` 原始 HTML 取得，
  只保留表格與 2025/2026 年 6–10 月的列。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from market_analysis.fundamental_pipeline.alt_data_metrics import (
    XbrlFact,
    completed_periods,
    dio_yoy,
    discrete_quarters,
    duration_period,
    flow_quarter_yoy,
    instant_period,
    instant_yoy,
    monthly_period_yoy,
    parse_companyconcept_facts,
    parse_roc_date,
    parse_roc_year_month,
    parse_tsa_table,
    period_bounds,
    shift_period,
    tsa_period_yoy,
    validate_period,
)

_FIX = Path(__file__).parent / "fixtures" / "alt_data"


def _facts(name: str) -> list[XbrlFact]:
    return parse_companyconcept_facts(json.loads((_FIX / name).read_text()))


# ---------------------------------------------------------------------------
# 期別工具
# ---------------------------------------------------------------------------


def test_validate_period_accepts_only_calendar_quarter() -> None:
    assert validate_period("2026-Q2") == "2026-Q2"
    for bad in ("2026-09", "2026Q2", "2026-Q5", "26-Q1", "2026-q1", ""):
        with pytest.raises(ValueError):
            validate_period(bad)


def test_period_helpers() -> None:
    assert shift_period("2026-Q1", -1) == "2025-Q4"
    assert shift_period("2026-Q2", -4) == "2025-Q2"
    assert period_bounds("2026-Q4") == (date(2026, 10, 1), date(2026, 12, 31))
    assert completed_periods(date(2026, 10, 7), 2) == ["2026-Q3", "2026-Q2"]
    assert completed_periods(date(2027, 1, 2), 2) == ["2026-Q4", "2026-Q3"]


def test_non_calendar_fiscal_quarters_map_by_midpoint() -> None:
    """NVDA（季末 7 月下旬）、WMT（季末 7/31）歸屬曆年 Q2，與 SEC frames 一致。"""
    assert duration_period(date(2026, 4, 27), date(2026, 7, 26)) == "2026-Q2"
    assert duration_period(date(2026, 5, 1), date(2026, 7, 31)) == "2026-Q2"
    assert instant_period(date(2026, 7, 31)) == "2026-Q2"
    assert instant_period(date(2026, 6, 28)) == "2026-Q2"


# ---------------------------------------------------------------------------
# SEC XBRL 單季推導與年增率（真實 fixture）
# ---------------------------------------------------------------------------


def test_msft_capex_q4_derived_from_fy_minus_9m() -> None:
    """MSFT 財年 Q4（2026-04~06）只在 10-K 有全年值：單季 = FY − 9M 累計。"""
    facts = _facts("sec_concept_msft_PaymentsToAcquirePropertyPlantAndEquipment.json")
    quarters = discrete_quarters(facts, date(2026, 10, 7))
    q = quarters[date(2026, 6, 30)]
    assert q.derived is True
    assert q.start == date(2026, 4, 1)
    assert q.val == pytest.approx(115_948_000_000 - 80_146_000_000)

    res = flow_quarter_yoy(facts, "2026-Q2", date(2026, 10, 7))
    assert res is not None
    assert res.current_end == date(2026, 6, 30)
    assert res.prior_end == date(2025, 6, 30)
    assert res.yoy_pct == pytest.approx(109.63, abs=0.01)


def test_xbrl_filed_after_as_of_is_excluded() -> None:
    """前視防護：10-K 於 2026-07-29 申報，as_of 2026-07-01 時不得使用。"""
    facts = _facts("sec_concept_msft_PaymentsToAcquirePropertyPlantAndEquipment.json")
    assert flow_quarter_yoy(facts, "2026-Q2", date(2026, 7, 1)) is None
    assert flow_quarter_yoy(facts, "2026-Q2", date(2026, 7, 29)) is not None


def test_amzn_capex_uses_productive_assets_tag() -> None:
    """AMZN 自 2018 起改用 PaymentsToAcquireProductiveAssets；舊標籤無近期資料。"""
    old = _facts("sec_concept_amzn_PaymentsToAcquirePropertyPlantAndEquipment.json")
    assert flow_quarter_yoy(old, "2026-Q2", date(2026, 10, 7)) is None
    new = _facts("sec_concept_amzn_PaymentsToAcquireProductiveAssets.json")
    res = flow_quarter_yoy(new, "2026-Q2", date(2026, 10, 7))
    assert res is not None
    assert res.yoy_pct == pytest.approx(68.44, abs=0.01)


def test_restated_value_uses_latest_filing() -> None:
    """同一期間多次申報取 as_of 之前最新申報值（重編後才採用新值）。"""
    facts = [
        XbrlFact(date(2025, 4, 1), date(2025, 6, 30), 100.0, date(2025, 7, 30)),
        XbrlFact(date(2025, 4, 1), date(2025, 6, 30), 110.0, date(2026, 8, 15)),
        XbrlFact(date(2026, 4, 1), date(2026, 6, 30), 132.0, date(2026, 7, 30)),
    ]
    before = flow_quarter_yoy(facts, "2026-Q2", date(2026, 8, 1))
    assert before is not None
    assert before.yoy_pct == pytest.approx(32.0)
    after = flow_quarter_yoy(facts, "2026-Q2", date(2026, 8, 20))
    assert after is not None
    assert after.yoy_pct == pytest.approx(20.0)


def test_lmt_rpo_instant_yoy() -> None:
    facts = _facts("sec_concept_lmt_RevenueRemainingPerformanceObligation.json")
    res = instant_yoy(facts, "2026-Q2", date(2026, 10, 7))
    assert res is not None
    assert res.current_end == date(2026, 6, 28)
    assert res.prior_end == date(2025, 6, 29)
    assert res.yoy_pct == pytest.approx(38.38, abs=0.01)


def test_wmt_dio_computed_from_inventory_and_cost_of_revenue() -> None:
    """DIO = 季末存貨 / 單季銷貨成本 × 天數（不使用周轉率）。"""
    inv = _facts("sec_concept_wmt_InventoryNet.json")
    cogs = _facts("sec_concept_wmt_CostOfRevenue.json")
    res = dio_yoy(inv, cogs, "2026-Q2", date(2026, 10, 7))
    assert res is not None
    assert res.current_end == date(2026, 7, 31)
    assert res.yoy_pct == pytest.approx(2.07, abs=0.01)
    assert "DIO" in res.detail and "天" in res.detail


def test_parse_companyconcept_ignores_malformed_rows() -> None:
    payload = {
        "units": {
            "USD": [
                {"end": "2026-06-30", "val": 1, "filed": "2026-07-30"},
                {"end": "bad", "val": 1, "filed": "2026-07-30"},
                {"end": "2026-06-30", "val": "x", "filed": "2026-07-30"},
            ]
        }
    }
    assert len(parse_companyconcept_facts(payload)) == 1
    assert parse_companyconcept_facts({}) == []


# ---------------------------------------------------------------------------
# TSA（真實兩欄表格 fixture）
# ---------------------------------------------------------------------------


def test_parse_real_tsa_two_column_table() -> None:
    cur = parse_tsa_table(
        (_FIX / "tsa_passenger_volumes_current_2026.html").read_text()
    )
    prior = parse_tsa_table((_FIX / "tsa_passenger_volumes_2025.html").read_text())
    assert cur[date(2026, 10, 5)] == 2_615_107
    assert cur[date(2026, 9, 30)] == 2_205_144
    assert prior[date(2025, 7, 1)] > 0
    assert min(cur) == date(2026, 6, 25) and max(cur) == date(2026, 10, 5)


def test_tsa_period_yoy_aligns_52_weeks_and_respects_as_of() -> None:
    daily = parse_tsa_table(
        (_FIX / "tsa_passenger_volumes_current_2026.html").read_text()
    )
    daily.update(
        parse_tsa_table((_FIX / "tsa_passenger_volumes_2025.html").read_text())
    )

    full = tsa_period_yoy(daily, "2026-Q3", date(2026, 10, 7))
    assert full is not None
    assert full.observations == 92
    assert full.window_start == date(2026, 7, 1)
    assert full.window_end == date(2026, 9, 30)
    assert full.yoy_pct == pytest.approx(-2.77, abs=0.01)

    # as_of 截斷：只用 as_of 之前的日資料
    partial = tsa_period_yoy(daily, "2026-Q3", date(2026, 8, 15))
    assert partial is not None
    assert partial.window_end == date(2026, 8, 15)
    # 不足 28 個配對日 → None
    assert tsa_period_yoy(daily, "2026-Q3", date(2026, 7, 20)) is None


# ---------------------------------------------------------------------------
# FRED 月度與台股民國日期
# ---------------------------------------------------------------------------


def test_monthly_period_yoy_uses_same_months_and_never_falls_back_to_mom() -> None:
    obs = [
        (date(2025, 7, 1), 100.0),
        (date(2025, 8, 1), 110.0),
        (date(2026, 7, 1), 105.0),
        (date(2026, 8, 1), 121.0),
    ]
    res = monthly_period_yoy(obs, "2026-Q3")
    assert res is not None
    assert res.observations == 2
    assert res.yoy_pct == pytest.approx((113.0 - 105.0) / 105.0 * 100, abs=0.01)
    # 沒有去年同月 → None（不改用月增率冒充年增率）
    assert (
        monthly_period_yoy(
            [(date(2026, 7, 1), 1.0), (date(2026, 8, 1), 2.0)], "2026-Q3"
        )
        is None
    )


def test_parse_roc_dates() -> None:
    assert parse_roc_year_month("11508") == date(2026, 8, 1)
    assert parse_roc_year_month("115/02") == date(2026, 2, 1)
    assert parse_roc_date("1150917") == date(2026, 9, 17)
    assert parse_roc_date("115/03/10") == date(2026, 3, 10)
    assert parse_roc_year_month(None) is None
