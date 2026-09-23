"""Skew 判讀門檻與文案的單一來源。

`options_flow.calculate_skew()` 產生的 `state` 字串，與
`intraday_pipeline/skew_commentary.py` 的極端分支，原本是同一套 80/20 規則
各自寫在兩處，會各自漂移。兩邊改為共用這裡的常數與分類函式。

刻意保持為 leaf module（只依賴標準函式庫），避免
`intraday_pipeline` ↔ `sentiment` 之間出現循環匯入。
"""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


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

# 下游閘門共用的 Skew 分位門檻（0~100 量綱）。原本以數字字面值散落在
# evaluation / skew_commentary / 各 embed / ExecutionRouter，同一個概念在不同
# 檔案各寫一次，改一處就漂移。數值為改版前的現值；百分位母體已改為日級規範
# 母體（見 canonical_history.py），新門檻需以 calibration/ 前向蒐集資料審核後
# 再調整，不在此憑推論修改。
# - 結構性背離（Skew 高分位 + PCR 極低、或 Skew 低分位 + PCR 極高）與
#   SQZ 微觀背離偽突破閘門的上下緣
SKEW_DIVERGENCE_HIGH_PERCENTILE = 85.0
SKEW_DIVERGENCE_LOW_PERCENTILE = 15.0
# - 左尾避險需求極高（防洗盤處置 / Skew Divergence Gate）
SKEW_HIGH_DEFENSE_PERCENTILE = 90.0
# - 三重結構性風險合流的 Skew 條件
SKEW_TRIPLE_CONFLUENCE_PERCENTILE = 98.0

# 百分位缺失或越界時的中性值：落在所有尾端門檻之外，不觸發任何防禦動作。
SKEW_NEUTRAL_PERCENTILE = 50.0


def ensure_percentile_pct(value: Optional[float], context: str = "") -> float:
    """驗證 Skew 百分位為 0~100 量綱；缺失、非有限值或越界時回傳中性值 50.0。

    全系統的 Skew 百分位一律是 0~100。刻意**不**猜測「0~1 小數形式」並自動
    乘以 100：在 0~100 量綱下，0~1% 正是合法的最低分位（看漲極端），自動放大
    會把它翻轉成看跌極端。量綱錯誤只能由呼叫端修正，這裡只負責攔截越界值並
    fail-open，不讓它觸發尾端防禦。
    """
    if value is None:
        return SKEW_NEUTRAL_PERCENTILE
    try:
        fval = float(value)
    except (TypeError, ValueError):
        fval = float("nan")
    if not math.isfinite(fval) or fval < 0.0 or fval > 100.0:
        logger.error(
            f"Skew 百分位越界 ({value!r}{f', {context}' if context else ''})，"
            f"預期 0~100，改用中性值 {SKEW_NEUTRAL_PERCENTILE}。"
        )
        return SKEW_NEUTRAL_PERCENTILE
    return fval


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

    # ISSUE-2.7: 引入 [-0.5%, +0.5%] 零軸死區抑制微觀噪聲 (Deadband)，避免常態波動率微笑誤診為結構偏斜
    if abs(skew_val) <= 0.5:
        return SKEW_STATE_FLAT
    if skew_val > 0.5:
        return SKEW_STATE_LEFT
    return SKEW_STATE_RIGHT
