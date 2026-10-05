"""單元測試：內部人交易訊號聚合評估 (insider_signal.py)。"""

from __future__ import annotations

from market_analysis.fundamental_pipeline.insider_signal import (
    evaluate_insider_signal,
)
from market_analysis.fundamental_pipeline.models import InsiderTxRecord


def test_evaluate_insider_signal_cluster_buy_two_insiders() -> None:
    """測試 2 位內部人自費增持觸發 CLUSTER_BUY。"""
    txs = [
        InsiderTxRecord(
            accession="ACC-1",
            line_no=1,
            symbol="NVDA",
            owner_name="Director Alpha",
            is_c_suite=False,
            tx_date="2026-10-01",
            tx_code="P",
            shares=1000.0,
            price=120.0,
            acquired_disposed="A",
        ),
        InsiderTxRecord(
            accession="ACC-2",
            line_no=1,
            symbol="NVDA",
            owner_name="Director Beta",
            is_c_suite=False,
            tx_date="2026-10-03",
            tx_code="P",
            shares=1500.0,
            price=122.0,
            acquired_disposed="A",
        ),
    ]

    summary = evaluate_insider_signal(
        "NVDA", txs, as_of_date="2026-10-05", window_days=30
    )
    assert summary.verdict == "CLUSTER_BUY"
    assert summary.cluster_buy_count == 2
    assert "2 位內部人自費增持" in summary.summary_text


def test_evaluate_insider_signal_cluster_buy_c_suite_large() -> None:
    """測試單一 C-Suite 高管買入達 10 萬美元觸發 CLUSTER_BUY。"""
    txs = [
        InsiderTxRecord(
            accession="ACC-3",
            line_no=1,
            symbol="TSLA",
            owner_name="Elon Musk",
            is_c_suite=True,
            tx_date="2026-10-02",
            tx_code="P",
            shares=1000.0,
            price=250.0,  # 250,000 USD > 100,000
            acquired_disposed="A",
        ),
    ]

    summary = evaluate_insider_signal(
        "TSLA", txs, as_of_date="2026-10-05", window_days=30
    )
    assert summary.verdict == "CLUSTER_BUY"
    assert summary.c_suite_buy_count == 1
    assert "C-Suite" in summary.summary_text


def test_evaluate_insider_signal_heavy_sale_discretionary() -> None:
    """測試多位高管非 10b5-1 自主拋售觸發 HEAVY_INSIDER_SALE。"""
    txs = [
        InsiderTxRecord(
            accession="ACC-4",
            line_no=1,
            symbol="MOCK",
            owner_name="Seller A",
            is_c_suite=False,
            tx_date="2026-09-20",
            tx_code="S",
            shares=10000.0,
            price=50.0,
            acquired_disposed="D",
            is_10b5_1=False,
        ),
        InsiderTxRecord(
            accession="ACC-5",
            line_no=1,
            symbol="MOCK",
            owner_name="Seller B",
            is_c_suite=False,
            tx_date="2026-09-22",
            tx_code="S",
            shares=20000.0,
            price=50.0,
            acquired_disposed="D",
            is_10b5_1=False,
        ),
        InsiderTxRecord(
            accession="ACC-6",
            line_no=1,
            symbol="MOCK",
            owner_name="Seller C",
            is_c_suite=False,
            tx_date="2026-09-25",
            tx_code="S",
            shares=30000.0,
            price=50.0,
            acquired_disposed="D",
            is_10b5_1=False,
        ),
    ]

    summary = evaluate_insider_signal(
        "MOCK", txs, as_of_date="2026-10-05", window_days=30
    )
    assert summary.verdict == "HEAVY_INSIDER_SALE"
    assert summary.cluster_sale_count == 3
    assert "非計畫性拋售" in summary.summary_text


def test_evaluate_insider_signal_10b5_neutral() -> None:
    """測試完全由 10b5-1 預先排程組成的賣單判定為 NEUTRAL，不產生恐慌誤報。"""
    txs = [
        InsiderTxRecord(
            accession="ACC-7",
            line_no=1,
            symbol="AAPL",
            owner_name="Tim Cook",
            is_c_suite=True,
            tx_date="2026-09-15",
            tx_code="S",
            shares=50000.0,
            price=220.0,
            acquired_disposed="D",
            is_10b5_1=True,  # 預先排程
        ),
    ]

    summary = evaluate_insider_signal(
        "AAPL", txs, as_of_date="2026-10-05", window_days=30
    )
    assert summary.verdict == "NEUTRAL"
    assert summary.cluster_sale_count == 0  # 自主非排程賣家為 0


def test_evaluate_insider_signal_empty_or_out_of_window() -> None:
    """測試空資料或超出 30 天滾動窗口之交易。"""
    txs = [
        InsiderTxRecord(
            accession="ACC-OLD",
            line_no=1,
            symbol="TSLA",
            owner_name="Old Buyer",
            is_c_suite=False,
            tx_date="2026-05-01",  # 超過 30 天
            tx_code="P",
            shares=5000.0,
            price=200.0,
            acquired_disposed="A",
        ),
    ]

    summary = evaluate_insider_signal(
        "TSLA", txs, as_of_date="2026-10-05", window_days=30
    )
    assert summary.verdict == "NEUTRAL"
    assert summary.cluster_buy_count == 0


def test_evaluate_insider_signal_ignores_grants_and_option_exercises() -> None:
    """測試股權授予 (A)、期權行權 (M) 與贈與 (G) 不計入自費增持或非計畫拋售。"""
    txs = [
        InsiderTxRecord(
            accession="ACC-GRANT",
            line_no=1,
            symbol="AMD",
            owner_name="Lisa Su",
            is_c_suite=True,
            tx_date="2026-10-01",
            tx_code="A",  # Grant
            shares=50000.0,
            price=0.0,
            acquired_disposed="A",
        ),
        InsiderTxRecord(
            accession="ACC-EXERCISE",
            line_no=2,
            symbol="AMD",
            owner_name="Lisa Su",
            is_c_suite=True,
            tx_date="2026-10-02",
            tx_code="M",  # Option Exercise
            shares=20000.0,
            price=30.0,
            acquired_disposed="A",
        ),
    ]
    summary = evaluate_insider_signal(
        "AMD", txs, as_of_date="2026-10-05", window_days=30
    )
    assert summary.verdict == "NEUTRAL"
    assert summary.cluster_buy_count == 0
    assert summary.total_net_bought_usd == 0.0


def test_evaluate_insider_signal_conflict_heavy_sale_trumps_minor_buy() -> None:
    """測試當少數內部人微額買入但遭遇高管巨額拋售時，仲裁狀態機必須判定為 HEAVY_INSIDER_SALE。"""
    txs = [
        # 2 位內部人自費買入 200 美元
        InsiderTxRecord(
            accession="ACC-B1",
            line_no=1,
            symbol="CONFLICT",
            owner_name="Buyer 1",
            is_c_suite=False,
            tx_date="2026-10-01",
            tx_code="P",
            shares=10.0,
            price=10.0,
            acquired_disposed="A",
        ),
        InsiderTxRecord(
            accession="ACC-B2",
            line_no=1,
            symbol="CONFLICT",
            owner_name="Buyer 2",
            is_c_suite=False,
            tx_date="2026-10-01",
            tx_code="P",
            shares=10.0,
            price=10.0,
            acquired_disposed="A",
        ),
        # 3 位高管非排程拋售合計 3000 萬美元
        InsiderTxRecord(
            accession="ACC-S1",
            line_no=1,
            symbol="CONFLICT",
            owner_name="Exec 1",
            is_c_suite=True,
            tx_date="2026-10-02",
            tx_code="S",
            shares=100000.0,
            price=100.0,
            acquired_disposed="D",
            is_10b5_1=False,
        ),
        InsiderTxRecord(
            accession="ACC-S2",
            line_no=1,
            symbol="CONFLICT",
            owner_name="Exec 2",
            is_c_suite=True,
            tx_date="2026-10-02",
            tx_code="S",
            shares=100000.0,
            price=100.0,
            acquired_disposed="D",
            is_10b5_1=False,
        ),
        InsiderTxRecord(
            accession="ACC-S3",
            line_no=1,
            symbol="CONFLICT",
            owner_name="Exec 3",
            is_c_suite=True,
            tx_date="2026-10-02",
            tx_code="S",
            shares=100000.0,
            price=100.0,
            acquired_disposed="D",
            is_10b5_1=False,
        ),
    ]

    summary = evaluate_insider_signal(
        "CONFLICT", txs, as_of_date="2026-10-05", window_days=30
    )
    assert summary.verdict == "HEAVY_INSIDER_SALE"
    assert "非計畫性拋售" in summary.summary_text
    assert summary.cluster_buy_count == 2
    assert summary.cluster_sale_count == 3
