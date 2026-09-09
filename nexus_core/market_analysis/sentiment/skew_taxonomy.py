"""Skew 判讀門檻與文案的單一來源。

`options_flow.calculate_skew()` 產生的 `state` 字串，與
`intraday_pipeline/skew_commentary.py` 的極端分支，原本是同一套 80/20 規則
各自寫在兩處，會各自漂移。兩邊改為共用這裡的常數與分類函式。

刻意保持為 leaf module（只依賴標準函式庫），避免
`intraday_pipeline` ↔ `sentiment` 之間出現循環匯入。
"""

from typing import Optional


# Skew 的歷史序列命名空間。
# 舊的 "SKEW" 序列是「固定 ±5% 履約價」代理值；calculate_skew() 已改為真
# 25-Delta，兩者數值定義不同，不能排在同一個百分位池裡互比，因此換用新 key。
# `sentiment_history.indicator` 本來就是既有欄位，不需要 migration；
# 舊 "SKEW" 列自然失效，保留不刪。
SKEW_INDICATOR = "SKEW_D25"


# Skew 正負號慣例：skew = IV(25Δ Put) - IV(25Δ Call)，單位為百分點。
# 正值 = Put 較貴 = 左偏 = 下檔避險需求；負值 = Call 較貴 = 右偏 = 追漲需求。
SKEW_DEFENSIVE_PERCENTILE = 80.0
SKEW_BULLISH_PERCENTILE = 20.0

SKEW_STATE_DEFENSIVE = (
    "⚠️ 市場下行保護需求極高，隱含避險情緒升溫（機構大舉購入 Put 保險）"
)
SKEW_STATE_BULLISH = (
    "🔥 市場上行看漲需求爆發，動能抄底/追高情緒極端亢奮（散戶搶購末日 Call）"
)
SKEW_STATE_LEFT = "左偏 (Put 昂貴)"
SKEW_STATE_RIGHT = "右偏 (Call 昂貴)"
SKEW_STATE_FLAT = "平穩"


def classify_skew_state(
    skew_val: Optional[float], skew_percentile: Optional[float]
) -> str:
    """由 skew 正負號 + 百分位推導型態字串。

    百分位缺失（樣本不足／查詢失敗）時只做正負號分類，不升級為極端判讀——
    極端標籤的成立條件本來就包含分位，沒有分位就沒有極端。
    """
    if skew_val is None:
        return "N/A"

    if skew_percentile is not None:
        if skew_val > 0 and skew_percentile >= SKEW_DEFENSIVE_PERCENTILE:
            return SKEW_STATE_DEFENSIVE
        if skew_val < 0 and skew_percentile <= SKEW_BULLISH_PERCENTILE:
            return SKEW_STATE_BULLISH

    if skew_val > 0:
        return SKEW_STATE_LEFT
    if skew_val < 0:
        return SKEW_STATE_RIGHT
    return SKEW_STATE_FLAT
