"""組合層風控的方向感知化測試（做空缺口補強）。

背景：做空進場系統 (SHORT_SIDE / Regime V) 上線前，整個 risk_engine /
portfolio / hedging 管線都建立在「所有部位皆為多頭」的假設上。空頭**選擇權**
(Covered Call / CSP) 早已存在且多數路徑處理正確，但空頭**現貨**是全新輸入，
會觸發一整組從未被負 quantity 走過的分支。

本檔案逐條鎖定那些缺口的修復，避免日後回歸。
"""

import pytest

from market_analysis.margin import (
    _REG_T_SHORT_STOCK_INITIAL_MARGIN_RATE,
    calculate_option_margin,
)
from market_analysis.risk_engine import (
    is_short_exposure_strategy,
    simulate_exposure_impact,
)


# ---------------------------------------------------------------- 保證金
class TestShortStockMargin:
    def test_short_stock_consumes_reg_t_initial_margin(self) -> None:
        """空頭現貨必須回報 Reg-T 初始保證金 (市值 × 50%)。

        修復前 opt_type="stock" 會落到函式尾端的 `return 0.0`，使純空頭帳戶的
        portfolio_heat 顯示 0%——而 heat 的 30%/50% 警戒線正是系統阻止繼續
        開倉的主要防線。
        """
        margin = calculate_option_margin("stock", 0.0, 100.0, 0.0, -200, 0.0)
        assert margin == pytest.approx(
            100.0 * 200 * _REG_T_SHORT_STOCK_INITIAL_MARGIN_RATE
        )
        assert margin == pytest.approx(10_000.0)

    def test_long_stock_consumes_no_margin(self) -> None:
        assert calculate_option_margin("stock", 0.0, 100.0, 0.0, 200, 0.0) == 0.0

    def test_stock_margin_uses_share_multiplier_not_contract(self) -> None:
        """現貨以「股」計價，乘數是 1 不是 100。"""
        margin = calculate_option_margin("stock", 0.0, 50.0, 0.0, -100, 0.0)
        assert margin == pytest.approx(2_500.0)  # 50 × 100 × 0.5

    def test_existing_option_margin_paths_unchanged(self) -> None:
        """既有的空頭選擇權保證金行為必須在位元層級不變。"""
        # Covered Call → 0
        assert calculate_option_margin("call", 110.0, 100.0, 2.0, -1, 95.0) == 0.0
        # CSP → strike × 100 × |qty|
        assert calculate_option_margin(
            "put", 90.0, 100.0, 2.0, -2, 0.0
        ) == pytest.approx(18_000.0)
        # Naked Call → 既有公式
        naked = calculate_option_margin("call", 110.0, 100.0, 2.0, -1, 0.0)
        assert naked == pytest.approx(max((0.20 * 100) - 10, 0.10 * 100 + 2) * 100)


# ---------------------------------------------------------------- 方向判定
class TestShortExposureStrategyDetection:
    @pytest.mark.parametrize(
        "strategy",
        [
            "STO_PUT",
            "STO_CALL",
            "SHORT_SIDE",
            "SHORT_STOCK",
            "Bear Call Spread",
            "BEAR_PUT_SPREAD",
            "Long Put (輕度 OTM)",
            "BTO_PUT",
        ],
    )
    def test_short_strategies_detected(self, strategy: str) -> None:
        assert is_short_exposure_strategy(strategy) is True

    @pytest.mark.parametrize(
        "strategy",
        ["BTO_CALL", "Long Call (ATM/輕度 OTM)", "Bull Call Spread", "Buy Shares"],
    )
    def test_long_strategies_not_detected(self, strategy: str) -> None:
        assert is_short_exposure_strategy(strategy) is False

    def test_short_side_entry_projects_negative_delta(self) -> None:
        """做空策略必須被投影為**減少**組合 Delta。

        修復前方向判定只看 "STO"，`SHORT_SIDE` 這類標籤會被誤判為 +1，
        使 NRO 倉位模型把一筆空單當成「增加多頭曝險」來編列預算。
        """
        projected, _pct = simulate_exposure_impact(
            current_total_delta=1000.0,
            new_trade_data={"strategy": "SHORT_SIDE", "weighted_delta": 100.0},
            user_capital=100_000.0,
            spy_price=500.0,
            suggested_contracts=2,
        )
        assert projected == pytest.approx(800.0)  # 1000 − 100×2

    def test_long_side_entry_unchanged(self) -> None:
        projected, _pct = simulate_exposure_impact(
            current_total_delta=1000.0,
            new_trade_data={"strategy": "BTO_CALL", "weighted_delta": 100.0},
            user_capital=100_000.0,
            spy_price=500.0,
            suggested_contracts=2,
        )
        assert projected == pytest.approx(1200.0)


# ---------------------------------------------------------------- 資本規模
class TestCapitalIsGrossNotNet:
    def test_short_position_does_not_shrink_capital(self) -> None:
        """空頭部位不得從總資本中扣除自身名目價值。

        capital 是曝險百分比、portfolio_heat、凱利預算與財務跑道的**共同分母**。
        修復前用帶號 quantity，一個夠大的空頭會把分母壓向 max(..., 1.0) 的
        地板，讓所有百分比同時爆表。
        """
        import json
        from unittest.mock import MagicMock

        from database.user_settings import calculate_auto_capital

        rows = [
            ("HOLDING", json.dumps({"quantity": -100.0, "avg_cost": 50.0})),
            ("HOLDING", json.dumps({"quantity": 200.0, "avg_cost": 50.0})),
        ]
        conn = MagicMock()
        conn.cursor.return_value.fetchall.return_value = rows
        conn.cursor.return_value.fetchone.return_value = None

        capital = calculate_auto_capital(0, conn=conn)
        # 兩筆皆計入量值：100×50 + 200×50 = 15,000（而非淨額 5,000）
        assert capital == pytest.approx(15_000.0)


# ---------------------------------------------------------------- 對沖語意
class TestHedgingDoesNotMistakeAlphaShortForHedge:
    def test_rehedge_threshold_is_two_sided(self) -> None:
        """個人風險上限必須雙邊判定——淨空頭曝險再大也要能觸發。"""
        from types import SimpleNamespace

        from market_analysis.hedging import evaluate_rehedge_necessity

        u_ctx = SimpleNamespace(
            capital=100_000.0,
            risk_limit=30.0,
            total_weighted_delta=-120.0,  # −60% 曝險 @ SPY 500
            user_id=1,
        )
        out = evaluate_rehedge_necessity(
            u_ctx,  # type: ignore[arg-type]
            {"symbol": "SPY", "spy_price": 500.0, "price": 0.0, "vix": 15.0},
        )
        assert out is not None
        assert out["action"] == "RE_HEDGE"
        assert "淨空頭" in out["reason"]

    def test_regime_target_delta_never_negative(self) -> None:
        """對沖引擎的目標 Delta 不得為負——它的職責是消除非預期的方向性
        曝險，不是代替使用者建立做空觀點。"""
        import inspect

        from market_analysis import hedging

        src = inspect.getsource(hedging.get_market_regime_target)
        assert "target_delta = 0.0" in src
        assert "user_capital * -" not in src
