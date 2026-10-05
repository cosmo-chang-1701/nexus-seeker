"""單元測試：Schedule 13D 激進投資人與及時性審查 (activist_gate.py)。"""

from __future__ import annotations

from datetime import date
from market_analysis.fundamental_pipeline.activist_gate import (
    count_business_days,
    evaluate_activist_filing,
)


def test_count_business_days() -> None:
    """測試計算兩日期之間的營業日（排除週六與週日）。"""
    # 2026-10-02 (週五) 到 2026-10-05 (週一)：1 個營業日 (週一當日)
    assert count_business_days(date(2026, 10, 2), date(2026, 10, 5)) == 1

    # 2026-10-01 (週四) 到 2026-10-08 (週四)：5 個營業日 (週五, 週一, 週二, 週三, 週四)
    assert count_business_days(date(2026, 10, 1), date(2026, 10, 8)) == 5

    # 跨週超過 5 營業日: 2026-10-01 到 2026-10-09 (週五) -> 6 個營業日
    assert count_business_days(date(2026, 10, 1), date(2026, 10, 9)) == 6


def test_evaluate_activist_filing_timely_and_intents() -> None:
    """測試及時申報之 13D 與關鍵訴求識別。"""
    item_4_text = (
        "The Reporting Persons believe that the shares are undervalued and intend to have "
        "discussions with management and the board of directors regarding strategic alternatives "
        "and seeking board representation to enhance shareholder value."
    )

    signal = evaluate_activist_filing(
        symbol="XYZ",
        accession="0001193125-26-000001",
        investor_name="Starboard Value LP",
        ownership_pct=6.5,
        item_4_text=item_4_text,
        event_date="2026-10-01",
        filing_date="2026-10-06",  # 3 營業日 <= 5
    )

    assert signal.symbol == "XYZ"
    assert signal.ownership_pct == 6.5
    assert signal.is_delayed_filing is False
    assert "BOARD_SEAT" in signal.key_intents
    assert "STRATEGIC_REVIEW" in signal.key_intents
    assert "UNDERVALUED_STANCE" in signal.key_intents
    assert "爭取董事會席次" in signal.summary_text


def test_evaluate_activist_filing_delayed() -> None:
    """測試逾期申報（> 5 營業日）觸發 is_delayed_filing 標記。"""
    item_4_text = "The Reporting Persons purchased shares for investment purposes."

    signal = evaluate_activist_filing(
        symbol="ABC",
        accession="0001193125-26-000002",
        investor_name="Carl Icahn",
        ownership_pct=8.2,
        item_4_text=item_4_text,
        event_date="2026-09-15",
        filing_date="2026-10-01",  # 歷經十多個營業日 > 5
    )

    assert signal.is_delayed_filing is True
    assert "申報逾期" in signal.summary_text
