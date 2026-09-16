"""kelly_priors.py — 凱利公式勝率先驗的單一來源 (方向感知)。

刻意只依賴 stdlib 的葉模組 (比照 `room_threshold.py` / `sentiment/skew_taxonomy.py`)，
故可同時被 `services/execution_router.py` 與 `dynamic_rollover/` 匯入而不產生
循環相依。

早期 `ExecutionRouter._calculate_kelly_size` 寫死 `0.55 if RSI < 50 else 0.45`。
那是**多頭均值回歸先驗**——RSI 偏低時做多勝率較高——套用到做空會把「超賣區
追空」判為高勝率，方向正好相反。做空先驗不宜機械翻轉 (RSI > 50 ⇒ 0.55)：追空
面對的是負偏態報酬 (軋空跳空) 與借券成本，在校準資料到位前一律採保守值。

⚠️ `KELLY_PRIOR_STATUS = "PRE_CALIBRATION"`：本表數值尚未經回測校準。
`calibration/` 工具會產出建議值報告，**人工審核後**以 PR 修改本檔，工具不自動寫入。
"""

import math
from typing import Final, Literal, Mapping, NamedTuple, Optional

TradeSide = Literal["LONG", "SHORT"]


class WinRateBucket(NamedTuple):
    rsi_min: float  # 含
    rsi_max: float  # 不含 (最後一桶含 100)
    win_rate: float


KELLY_PRIOR_STATUS: Final[str] = "PRE_CALIBRATION"

KELLY_WIN_RATE_PRIORS: Final[Mapping[TradeSide, tuple[WinRateBucket, ...]]] = {
    "LONG": (
        WinRateBucket(0.0, 50.0, 0.55),
        WinRateBucket(50.0, 100.0, 0.45),
    ),
    "SHORT": (
        WinRateBucket(0.0, 50.0, 0.45),
        WinRateBucket(50.0, 100.0, 0.40),
    ),
}

# 預期賠率 (Profit/Loss Ratio)。做空與做多同值：呼叫端若有實際 R:R，應取
# min(實際 R:R, 本值)，永遠不信任高於先驗的賠率。
KELLY_PRIOR_ODDS: Final[Mapping[TradeSide, float]] = {"LONG": 1.8, "SHORT": 1.8}
KELLY_PRIOR_SCALE: Final[float] = 0.5  # Half-Kelly
KELLY_PRIOR_CAP: Final[Mapping[TradeSide, float]] = {"LONG": 0.15, "SHORT": 0.10}


def _lookup(side: TradeSide, rsi: float) -> float:
    buckets = KELLY_WIN_RATE_PRIORS[side]
    for bucket in buckets:
        if bucket.rsi_min <= rsi < bucket.rsi_max:
            return bucket.win_rate
    # rsi == 100 (或超出範圍) 落在最後一桶
    return buckets[-1].win_rate


def get_win_rate_prior(side: TradeSide, rsi: Optional[float]) -> float:
    """查表取得勝率先驗。

    * RSI 為 None / NaN / 超出 [0, 100]：取該方向所有桶的最小值 (保守)。
    * SHORT 結果結構性夾制為 `min(做空桶, 同 RSI 的做多桶)`——即使日後誤改表，
      做空先驗也永遠不會比做多激進。校準若證明做空確實更優，須連同本夾制一起
      以 PR 明確移除，而不是只改數字。
    """
    if rsi is None or math.isnan(rsi) or rsi < 0.0 or rsi > 100.0:
        short_min = min(b.win_rate for b in KELLY_WIN_RATE_PRIORS["SHORT"])
        long_min = min(b.win_rate for b in KELLY_WIN_RATE_PRIORS["LONG"])
        if side == "SHORT":
            return min(short_min, long_min)
        return long_min
    if side == "SHORT":
        return min(_lookup("SHORT", rsi), _lookup("LONG", rsi))
    return _lookup("LONG", rsi)
