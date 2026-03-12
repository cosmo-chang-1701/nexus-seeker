import unittest
import sys
import discord

# mock import since cogs references it
import sys
import builtins

sys.path.insert(0, '/app/nexus_core')
from cogs.embed_builder import create_portfolio_report_embed

class TestEmbedBuilder(unittest.TestCase):
    def test_empty_report_lines(self):
        embed = create_portfolio_report_embed([])
        self.assertEqual(embed.title, "📊 Nexus Seeker 盤後風險結算報告")
        self.assertEqual(embed.description, "目前無持倉部位，亦無風險數據。\n\u200b")
        self.assertEqual(len(embed.fields), 0)

    def test_no_macro_risk(self):
        lines = [
            "Position 1: AAPL 150C",
            "Position 2: NVDA 400P"
        ]
        embed = create_portfolio_report_embed(lines)
        self.assertEqual(len(embed.fields), 2)
        self.assertEqual(embed.fields[0].name, "📦 當前持倉明細")
        self.assertIn("Position 1", embed.fields[0].value)
        self.assertEqual(embed.fields[1].name, "🛡️ 風控管線評估與對沖決策")
        self.assertEqual(embed.fields[1].value, "目前無宏觀風險數據。\n\u200b")

    def test_with_macro_risk(self):
        lines = [
            "Position 1: AAPL 150C",
            "🌐 **【宏觀風險與資金水位報告】**",
            "Risk metrics here"
        ]
        embed = create_portfolio_report_embed(lines)
        self.assertEqual(len(embed.fields), 2)
        self.assertEqual(embed.fields[0].name, "📦 當前持倉明細")
        self.assertIn("Position 1", embed.fields[0].value)
        self.assertEqual(embed.fields[1].name, "🛡️ 風控管線評估與對沖決策")
        self.assertTrue(embed.fields[1].value.startswith("🌐 **"))
        self.assertIn("Risk metrics here", embed.fields[1].value)

    def test_positions_field_truncates_to_discord_limit(self):
        long_line = "Position A " + ("x" * 250)
        lines = [long_line for _ in range(10)]

        embed = create_portfolio_report_embed(lines)

        self.assertLessEqual(len(embed.fields[0].value), 1024)
        self.assertIn("...", embed.fields[0].value)

    def test_hedge_analysis_missing_keys_fallback(self):
        lines = [
            "Position 1: AAPL 150C",
            "🌐 **【宏觀風險與資金水位報告】**",
            "Risk metrics here"
        ]
        hedge_analysis = {
            "status": "OPTIMAL"
        }

        embed = create_portfolio_report_embed(lines, hedge_analysis)

        self.assertGreaterEqual(len(embed.fields), 3)
        self.assertEqual(embed.fields[2].name, "🛡️ 對沖有效性診斷")
        self.assertIn("對沖比率", embed.fields[2].value)
        self.assertLessEqual(len(embed.fields[2].value), 1024)

if __name__ == '__main__':
    unittest.main()
