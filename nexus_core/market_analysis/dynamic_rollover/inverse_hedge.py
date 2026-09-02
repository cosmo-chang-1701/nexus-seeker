"""邏輯 (4) 延伸：保證金防禦第三轉倉目的地 —— 反向ETF標的解析與現貨動能確認。

僅用於 margin_defense.py::evaluate_margin_defense_impl 在判定 SATELLITE 持倉
結構性無勝率、且無真實保證金現金赤字 (has_actual_deficit=False) 時，於 BOXX
之外多提供一個「方向性對沖」選項：轉入該標的的反向ETF，而非單純停泊現金等價物。

刻意不套用選擇權鏈為基礎的訊號引擎 (GEX/Skew/UOA/_confirm_entry_signal)——槓桿反向
ETP 的選擇權鏈流動性普遍偏差，套用既有引擎容易被既有的利差流動性防護閘門誤擋或
產生雜訊。改用純現貨技術面 (RSI + 短期均線 + 成交額流動性門檻) 做最後一道確認，
任何資料取得失敗或條件不滿足一律 fail-closed，呼叫端會退回既有 BOXX 行為。
"""

from typing import Optional

import pandas as pd
import pandas_ta as ta

from . import logger
from .constants import (
    INDEX_INVERSE_MAP,
    SECTOR_INVERSE_MAP,
    SINGLE_STOCK_INVERSE_MAP,
    _INVERSE_HEDGE_DEFAULT_LEVERAGE_TIER,
    _INVERSE_HEDGE_HIGH_CONVICTION_LEVERAGE_TIER,
    _INVERSE_HEDGE_HISTORY_PERIOD,
    _INVERSE_HEDGE_MA_LOOKBACK,
    _INVERSE_HEDGE_MIN_ADV_USD,
    _INVERSE_HEDGE_RSI_BULLISH_THRESHOLD,
    _INVERSE_HEDGE_VOLUME_LOOKBACK_BARS,
)


def select_inverse_leverage_tier(
    is_structural_breakdown: bool, is_whale_sto_block: bool
) -> str:
    """依個股當下結構性風險的嚴重程度動態決定 1x 或 2x 反向ETF槓桿層級。

    結構性破位與主力空頭封殺「雙重確認」同時成立時，視為高信心度空頭情境，
    優先採用槓桿較高的 2x 商品；僅單一條件成立時，採用槓桿較低的 1x 商品，
    降低槓桿反向ETP的波動耗損風險。
    """
    if is_structural_breakdown and is_whale_sto_block:
        return _INVERSE_HEDGE_HIGH_CONVICTION_LEVERAGE_TIER
    return _INVERSE_HEDGE_DEFAULT_LEVERAGE_TIER


def get_inverse_symbol(
    symbol: str, leverage_tier: str = _INVERSE_HEDGE_DEFAULT_LEVERAGE_TIER
) -> Optional[str]:
    """解析標的對應的反向ETF：個股直接映射 > 大盤指數直接映射 > 依產業分類回退。

    未列入 SINGLE_STOCK_INVERSE_MAP/INDEX_INVERSE_MAP 的標的一律透過
    market_analysis.risk_engine.get_sector_benchmark 的產業分類回退至對應的產業
    反向ETF，若該產業亦無已確認的反向商品 (含 get_sector_benchmark 找不到分類、
    預設回傳 SPY 的情況)，最終回退至 SPY 的大盤反向ETF (SH)——因此本函式對任何
    輸入皆保證回傳一個候選代號，實際是否採用仍由呼叫端的
    confirm_inverse_hedge_spot_momentum 現貨動能確認把關。
    """
    symbol = symbol.upper()

    variants = SINGLE_STOCK_INVERSE_MAP.get(symbol)
    if variants:
        return (
            variants.get(leverage_tier)
            or variants.get(_INVERSE_HEDGE_DEFAULT_LEVERAGE_TIER)
            or variants.get(_INVERSE_HEDGE_HIGH_CONVICTION_LEVERAGE_TIER)
        )

    if symbol in INDEX_INVERSE_MAP:
        return INDEX_INVERSE_MAP[symbol]

    from market_analysis.risk_engine import get_sector_benchmark

    sector_etf = get_sector_benchmark(symbol)
    if sector_etf in SECTOR_INVERSE_MAP:
        return SECTOR_INVERSE_MAP[sector_etf]

    return INDEX_INVERSE_MAP.get("SPY")


async def confirm_inverse_hedge_spot_momentum(symbol: str) -> bool:
    """純現貨技術面確認反向ETF自身是否也出現買入動能 (RSI/短期均線/成交額流動性)。

    刻意不查詢選擇權鏈，任何資料不足或例外一律回傳 False (fail-closed)，
    呼叫端會因此退回既有 BOXX 轉倉行為，不會因猜測性訊號誤導使用者。
    """
    from services.market_data_service import get_history_df

    try:
        df = await get_history_df(
            symbol, period=_INVERSE_HEDGE_HISTORY_PERIOD, interval="1d"
        )
    except Exception as e:
        logger.error(f"反向ETF現貨動能確認取得歷史資料失敗 ({symbol}): {e}")
        return False

    min_bars = max(_INVERSE_HEDGE_MA_LOOKBACK, _INVERSE_HEDGE_VOLUME_LOOKBACK_BARS) + 1
    if df is None or df.empty or len(df) < min_bars:
        return False

    try:
        close = df["Close"]

        rsi_series = ta.rsi(close, length=14)
        if rsi_series is None or rsi_series.dropna().empty:
            return False
        rsi_14 = float(rsi_series.dropna().iloc[-1])

        ma_short = close.rolling(_INVERSE_HEDGE_MA_LOOKBACK).mean().dropna()
        if ma_short.empty:
            return False
        last_close = float(close.iloc[-1])
        last_ma = float(ma_short.iloc[-1])

        dollar_volume = (
            (df["Close"] * df["Volume"])
            .tail(_INVERSE_HEDGE_VOLUME_LOOKBACK_BARS)
            .mean()
        )
        avg_dollar_volume = 0.0 if pd.isna(dollar_volume) else float(dollar_volume)
    except Exception as e:
        logger.error(f"反向ETF現貨動能確認計算失敗 ({symbol}): {e}")
        return False

    return (
        rsi_14 > _INVERSE_HEDGE_RSI_BULLISH_THRESHOLD
        and last_close > last_ma
        and avg_dollar_volume >= _INVERSE_HEDGE_MIN_ADV_USD
    )
