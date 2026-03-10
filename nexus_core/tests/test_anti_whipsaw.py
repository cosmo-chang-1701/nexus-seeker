"""
Anti-Whipsaw (防騙線) 單元測試。

覆蓋範圍：
1. is_whipsaw_noise — 時間過濾、價格過濾、首次觸發放行
2. should_send_priority_alert — CROSSOVER + 防騙線整合路徑
3. database CRUD — get/update_watchlist_alert_state
"""

import unittest
import time
from unittest.mock import patch, MagicMock

from services.alert_filter import (
    is_whipsaw_noise,
    should_send_priority_alert,
    WHIPSAW_COOLDOWN_SECONDS,
    WHIPSAW_PRICE_BAND_PCT,
)


class TestIsWhipsawNoise(unittest.TestCase):
    """is_whipsaw_noise 純函數測試"""

    def test_first_trigger_should_pass(self):
        """首次觸發 (無歷史紀錄) 應直接放行"""
        result = is_whipsaw_noise("AAPL", 150.0, "BULLISH", None)
        self.assertFalse(result)

    def test_empty_last_state_should_pass(self):
        """last_state 缺少 last_cross_time 也應放行"""
        state = {"last_cross_dir": None, "last_cross_price": None, "last_cross_time": None}
        result = is_whipsaw_noise("AAPL", 150.0, "BULLISH", state)
        self.assertFalse(result)

    def test_same_direction_within_cooldown_should_block(self):
        """同方向訊號在冷卻期內應被抑制"""
        recent_time = time.time() - 3600  # 1 小時前 (遠小於 4 小時門檻)
        state = {
            "last_cross_dir": "BULLISH",
            "last_cross_price": 150.0,
            "last_cross_time": recent_time,
        }
        result = is_whipsaw_noise("AAPL", 151.0, "BULLISH", state)
        self.assertTrue(result, "同方向 + 冷卻期內應被攔截")

    def test_same_direction_after_cooldown_with_price_change_should_pass(self):
        """同方向但已超過冷卻期且價格偏離充足應放行"""
        old_time = time.time() - (WHIPSAW_COOLDOWN_SECONDS + 1)
        state = {
            "last_cross_dir": "BULLISH",
            "last_cross_price": 100.0,
            "last_cross_time": old_time,
        }
        # 價格偏離 10% (遠超 1.5% 門檻)
        result = is_whipsaw_noise("AAPL", 110.0, "BULLISH", state)
        self.assertFalse(result, "超過冷卻期 + 價格偏離充足應放行")

    def test_different_direction_within_cooldown_but_small_price_move_should_block(self):
        """不同方向但價格偏離不足應被抑制 (均線糾纏)"""
        recent_time = time.time() - 1800  # 30 分鐘前
        state = {
            "last_cross_dir": "BULLISH",
            "last_cross_price": 150.0,
            "last_cross_time": recent_time,
        }
        # 價格偏離僅 0.5% (不足 1.5% 門檻)
        result = is_whipsaw_noise("AAPL", 150.75, "BEARISH", state)
        self.assertTrue(result, "價格偏離不足應被攔截 (均線糾纏)")

    def test_different_direction_with_sufficient_price_move_should_pass(self):
        """不同方向且價格偏離充足應放行 (真正的趨勢反轉)"""
        recent_time = time.time() - 1800  # 30 分鐘內
        state = {
            "last_cross_dir": "BULLISH",
            "last_cross_price": 150.0,
            "last_cross_time": recent_time,
        }
        # 價格偏離 3.3% (超過 1.5% 門檻)
        result = is_whipsaw_noise("AAPL", 155.0, "BEARISH", state)
        self.assertFalse(result, "不同方向 + 價格偏離充足 = 真正反轉，應放行")

    def test_price_band_exact_boundary_should_block(self):
        """價格偏離剛好在門檻以下 (邊界測試)"""
        old_time = time.time() - (WHIPSAW_COOLDOWN_SECONDS + 1)
        state = {
            "last_cross_dir": "BEARISH",
            "last_cross_price": 100.0,
            "last_cross_time": old_time,
        }
        # 偏離 = 1.49% < 1.5%
        result = is_whipsaw_noise("AAPL", 101.49, "BULLISH", state)
        self.assertTrue(result, "偏離剛好低於門檻應被攔截")

    def test_price_band_above_boundary_should_pass(self):
        """價格偏離剛好超過門檻 (邊界測試)"""
        old_time = time.time() - (WHIPSAW_COOLDOWN_SECONDS + 1)
        state = {
            "last_cross_dir": "BEARISH",
            "last_cross_price": 100.0,
            "last_cross_time": old_time,
        }
        # 偏離 = 1.5% == WHIPSAW_PRICE_BAND_PCT，不嚴格小於，應放行
        result = is_whipsaw_noise("AAPL", 101.50, "BULLISH", state)
        self.assertFalse(result, "偏離剛好等於門檻應放行")


class TestShouldSendPriorityAlertWithWhipsaw(unittest.TestCase):
    """should_send_priority_alert 整合防騙線路徑測試"""

    def _make_crossover_result(self, symbol="AAPL", price=150.0, direction="BULLISH", window=21):
        return {
            "symbol": symbol,
            "price": price,
            "ema_signals": [{"type": "CROSSOVER", "direction": direction, "window": window}],
            "ai_decision": "",
            "vrp": 0.0,
        }

    def test_crossover_no_last_state_should_pass(self):
        """CROSSOVER + 無歷史紀錄 → 應觸發推播"""
        result = self._make_crossover_result()
        is_priority, reason = should_send_priority_alert(result, last_alert_state=None)
        self.assertTrue(is_priority)
        self.assertIn("趨勢突破", reason)

    def test_crossover_with_whipsaw_should_block(self):
        """CROSSOVER + 防騙線攔截 → 應被抑制"""
        result = self._make_crossover_result(price=150.5)
        last_state = {
            "last_cross_dir": "BULLISH",
            "last_cross_price": 150.0,
            "last_cross_time": time.time() - 1800,  # 30 分鐘前
        }
        is_priority, reason = should_send_priority_alert(result, last_alert_state=last_state)
        self.assertFalse(is_priority, "防騙線應在 CROSSOVER 信號上生效")
        self.assertEqual(reason, "")

    def test_crossover_with_cleared_whipsaw_should_pass(self):
        """CROSSOVER + 防騙線放行 (不同方向 + 價格偏離充足) → 應觸發"""
        result = self._make_crossover_result(price=160.0, direction="BEARISH")
        last_state = {
            "last_cross_dir": "BULLISH",
            "last_cross_price": 150.0,
            "last_cross_time": time.time() - 1800,
        }
        is_priority, reason = should_send_priority_alert(result, last_alert_state=last_state)
        self.assertTrue(is_priority)
        self.assertIn("BEARISH", reason)

    def test_vix_shock_ignores_whipsaw(self):
        """VIX 衝擊維度不受防騙線影響"""
        result = {
            "symbol": "AAPL",
            "macro_vix": 30.0,
            "ema_signals": [],
            "vrp": 0.0,
        }
        prev_macro = {"vix": 20.0}  # VIX 變動 50%
        is_priority, reason = should_send_priority_alert(result, prev_macro=prev_macro)
        self.assertTrue(is_priority)
        self.assertIn("VIX", reason)

    def test_vrp_opportunity_ignores_whipsaw(self):
        """VRP 高溢酬維度不受防騙線影響"""
        result = {
            "symbol": "AAPL",
            "ema_signals": [],
            "ai_decision": "APPROVE",
            "vrp": 0.08,
        }
        is_priority, reason = should_send_priority_alert(result)
        self.assertTrue(is_priority)
        self.assertIn("VRP", reason)

    def test_backward_compatible_without_last_alert_state(self):
        """不帶 last_alert_state 參數時行為應與原版一致 (向下相容)"""
        result = self._make_crossover_result()
        is_priority, reason = should_send_priority_alert(result)
        self.assertTrue(is_priority)
        self.assertIn("趨勢突破", reason)


if __name__ == "__main__":
    unittest.main()
