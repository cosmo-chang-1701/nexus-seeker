"""
gamma_cliff_confirmation.py — 負 Gamma 懸崖連續貫穿確認引擎。

在 STRUCTURAL_BREAKDOWN 場景分類器觸發後，本模組提供第二層確認：
檢查最近 N 分鐘的 1 分鐘 K 線是否「全部」以實體收盤價
貫穿負 Gamma 懸崖線（即 PutWall 或指定的 gamma_cliff_level）。

若確認條件不成立，視為正 Gamma 區內的雜訊晃動，不觸發清倉。
"""

import logging

import pandas as pd

logger = logging.getLogger(__name__)

# 可調整的確認窗口長度（分鐘）
DEFAULT_CONFIRMATION_WINDOW_MINUTES: int = 15
# 硬性上下限，防止不合理的窗口設定
MIN_CONFIRMATION_WINDOW: int = 5
MAX_CONFIRMATION_WINDOW: int = 30


async def is_gamma_cliff_confirmed(
    symbol: str,
    gamma_cliff_level: float,
    confirmation_window_minutes: int = DEFAULT_CONFIRMATION_WINDOW_MINUTES,
) -> bool:
    """
    檢查標的是否已連續 N 分鐘以實體 K 線收盤價貫穿負 Gamma 懸崖線。

    使用 Close（收盤價）而非 Low 作為判定基準：Low 可能是影線的瞬時穿刺
    （假穿透），不代表市場真正進入負 Gamma 區域。

    Fail-safe 原則：任何數據異常（API 失敗、K 線不足）均返回 False
    （不觸發清倉），避免因數據問題造成不必要的恐慌性出場。

    Args:
        symbol: 標的代碼
        gamma_cliff_level: 負 Gamma 懸崖價位（通常為 min(PutWall, GammaFlip)）
        confirmation_window_minutes: 確認窗口長度（分鐘），預設 15，
                                     硬性範圍 [5, 30]

    Returns:
        True 如果確認穿透（應觸發清倉），False 如果判定為雜訊
    """
    if gamma_cliff_level <= 0.0:
        logger.warning(f"[{symbol}] Gamma cliff level <= 0, 無法進行確認")
        return False

    # 窗口參數鉗制
    confirmation_window_minutes = max(
        MIN_CONFIRMATION_WINDOW,
        min(MAX_CONFIRMATION_WINDOW, confirmation_window_minutes),
    )

    try:
        from services import market_data_service

        # 取得最近 confirmation_window_minutes 根 1 分鐘 K 線
        df_1m: pd.DataFrame = await market_data_service.get_history_df(
            symbol, period="1d", interval="1m"
        )

        if df_1m.empty or len(df_1m) < confirmation_window_minutes:
            logger.warning(
                f"[{symbol}] 1 分鐘 K 線數據不足 "
                f"({len(df_1m)}/{confirmation_window_minutes})，"
                f"無法確認 Gamma 懸崖貫穿，保守返回 False"
            )
            return False

        # 取最近 N 根 K 線
        recent_candles = df_1m.tail(confirmation_window_minutes)

        # 確認條件：每根 K 線的「實體收盤價」(Close) 均低於懸崖線
        all_closed_below = all(
            float(row["Close"]) < gamma_cliff_level
            for _, row in recent_candles.iterrows()
        )

        if all_closed_below:
            logger.info(
                f"[{symbol}] ⚠️ 負 Gamma 懸崖確認：連續 "
                f"{confirmation_window_minutes} 分鐘實體 K 線收盤價 "
                f"貫穿 ${gamma_cliff_level:.2f}，確認結構性破位"
            )
        else:
            closes_above = sum(
                1
                for _, row in recent_candles.iterrows()
                if float(row["Close"]) >= gamma_cliff_level
            )
            logger.info(
                f"[{symbol}] 正 Gamma 區雜訊晃動：最近 "
                f"{confirmation_window_minutes} 根 K 線中有 "
                f"{closes_above} 根收盤價仍在懸崖線上方 "
                f"(${gamma_cliff_level:.2f})，不觸發清倉"
            )

        return all_closed_below

    except Exception as e:
        logger.error(
            f"[{symbol}] Gamma 懸崖確認引擎異常: {e}，" f"保守返回 False（不觸發清倉）"
        )
        return False
