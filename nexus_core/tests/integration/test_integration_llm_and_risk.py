import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from market_analysis.risk_engine import MacroContext, optimize_position_risk
from services.llm_service import RiskAssessment, evaluate_trade_risk


class TestLlmRiskIntegration(unittest.TestCase):
    def test_structured_output_is_parsed(self):
        parsed = RiskAssessment(
            decision="VETO",
            tags=["Black Swan Risk", "Retail Mania"],
            reasoning="疑似黑天鵝事件，否決賣方策略。",
        )
        response = SimpleNamespace(output_parsed=parsed)

        with patch(
            "services.llm_service.client.responses.parse",
            new=AsyncMock(return_value=response),
        ):
            result = asyncio.run(
                evaluate_trade_risk(
                    symbol="TSLA",
                    strategy="STO_PUT",
                    news_context="SEC investigation",
                    reddit_context="Consensus score 9000",
                )
            )

        self.assertEqual(result["decision"], "VETO")
        self.assertIn("Black Swan Risk", result["reasoning"])

    def test_fail_open_when_llm_is_unavailable(self):
        with patch(
            "services.llm_service.client.responses.parse",
            new=AsyncMock(side_effect=RuntimeError("network down")),
        ):
            result = asyncio.run(
                evaluate_trade_risk(
                    symbol="MSFT",
                    strategy="STO_CALL",
                    news_context="normal earnings",
                    reddit_context="normal traffic",
                )
            )

        self.assertEqual(result["decision"], "APPROVE")
        self.assertIn("AI", result["reasoning"])


class TestRiskEngineIntegration(unittest.TestCase):
    def test_high_vix_increases_position_capacity(self):
        """VIX 戰情階梯攻守互換：高 VIX = 高 IV 溢酬，NRO 放大風險額度。

        VIX=16 (Caution, w_vix=0.5) 應產生 *較少* 口數；
        VIX=36 (All-in,  w_vix=2.0) 應產生 *較多* 口數。
        """
        caution = MacroContext(vix=16.0, oil_price=70.0, vix_change=0.0)
        allin = MacroContext(vix=36.0, oil_price=70.0, vix_change=0.0)

        caution_qty, _ = optimize_position_risk(
            current_delta=5.0,
            unit_weighted_delta=8.0,
            user_capital=50000.0,
            spy_price=500.0,
            stock_iv=0.30,
            strategy="STO_PUT",
            macro_data=caution,
            base_risk_limit_pct=50.0,
            vix_spot=16.0,
        )
        allin_qty, _ = optimize_position_risk(
            current_delta=5.0,
            unit_weighted_delta=8.0,
            user_capital=50000.0,
            spy_price=500.0,
            stock_iv=0.30,
            strategy="STO_PUT",
            macro_data=allin,
            base_risk_limit_pct=50.0,
            vix_spot=36.0,
        )

        # All-in tier should allow MORE contracts than Caution tier
        self.assertGreater(allin_qty, caution_qty)
