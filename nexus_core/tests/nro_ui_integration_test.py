import unittest
import pandas as pd
import numpy as np
import logging

# 封鎖 yfinance 噪音
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# ==========================================
# 模擬 Discord Embed (用於脫離 Bot 環境測試 UI)
# ==========================================
class MockEmbed:
    def __init__(self):
        self.fields = []
        self.footer = None

    def add_field(self, name, value, inline=False):
        self.fields.append({"name": name, "value": value})

    def set_footer(self, text):
        self.footer = text

# ==========================================
# 待測核心 UI 函數 (對齊您之前的邏輯)
# ==========================================
def _add_risk_optimization_fields(embed, data, risk_limit_pct=15.0):
    projected_pct = data.get('projected_exposure_pct')
    if projected_pct is None: return

    safe_qty = data.get('safe_qty', 0)
    hedge_spy = data.get('hedge_spy', 0.0)
    suggested = data.get('suggested_contracts', 0)
    spy_p = data.get('spy_price', 690.0) # 2026 基準
    
    # 1. 曝險現況判定
    if abs(projected_pct) > risk_limit_pct:
        sim_status = "🚨 警告：曝險過載"
        sim_block = f"```diff\n- 成交後預期總曝險: {projected_pct:+.1f}%\n- 超過 {risk_limit_pct}% 宏觀紅線\n```"
    else:
        sim_status = "✅ 狀態：風險受控"
        sim_block = f"```yaml\n成交後預期總曝險: {projected_pct:+.1f}%\n符合資產組合平衡標準\n```"
    
    embed.add_field(name=f"🛡️ What-if 曝險模擬 | {sim_status}", value=sim_block)

    # 2. 自動優化建議
    if suggested > safe_qty:
        actions = [f"--- 偵測到風險超標，執行自動降規 ---"]
        actions.append(f"❌ 原始建議: {suggested} 口")
        actions.append(f"✅ 安全成交: {safe_qty} 口")
        
        if safe_qty == 0 and hedge_spy != 0:
            actions.append(f"\n⚠️ 警告: 即使下 1 口也過載")
            direction = "賣出" if hedge_spy > 0 else "買入"
            actions.append(f"🛡️ 建議對沖: {direction} {abs(hedge_spy):.1f} 股 SPY (@${spy_p:.1f})")
        
        embed.add_field(name="⚖️ Nexus Risk Optimizer", value="```diff\n" + "\n".join(actions) + "\n```")

# ==========================================
# 自動化測試案例
# ==========================================
class TestNROFullSystem(unittest.TestCase):
    
    def test_ui_overload_red_rendering(self):
        """[UI] 驗證過載時是否正確顯示紅色 (diff -) 標籤"""
        embed = MockEmbed()
        # 模擬一個超標數據 (+26.1%)
        data = {
            'projected_exposure_pct': 26.1,
            'suggested_contracts': 1,
            'safe_qty': 0,
            'hedge_spy': 22.2,
            'spy_price': 691.4
        }
        
        _add_risk_optimization_fields(embed, data)
        
        # 驗證標題
        self.assertIn("🚨 警告：曝險過載", embed.fields[0]['name'])
        # 驗證內容是否包含 diff 的紅色標籤 '-'
        self.assertIn("- 成交後預期總曝險", embed.fields[0]['value'])
        # 驗證對沖文字與價格
        self.assertIn("建議對沖: 賣出 22.2 股 SPY (@$691.4)", embed.fields[1]['value'])
        print("✅ UI 紅色過載渲染測試通過")

    def test_ui_safe_green_rendering(self):
        """[UI] 驗證受控時是否正確顯示綠色 (yaml) 標籤"""
        embed = MockEmbed()
        data = {
            'projected_exposure_pct': 8.5,
            'suggested_contracts': 1,
            'safe_qty': 1,
            'hedge_spy': 0.0
        }
        
        _add_risk_optimization_fields(embed, data)
        
        self.assertIn("✅ 狀態：風險受控", embed.fields[0]['name'])
        self.assertIn("```yaml", embed.fields[0]['value'])
        # 驗證不應出現優化建議區塊
        self.assertEqual(len(embed.fields), 1)
        print("✅ UI 綠色安全渲染測試通過")

if __name__ == '__main__':
    unittest.main()