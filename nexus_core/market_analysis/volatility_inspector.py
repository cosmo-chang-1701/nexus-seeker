import logging
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
from typing import Dict, Any, Optional, List

from services import market_data_service
from .psq_engine import analyze_psq
from .strategy import evaluate_ema_trend
from database.user_settings import get_full_user_context

logger = logging.getLogger(__name__)


class VolatilityInspector:
    """
    IV Opportunity Detection Agent (Volatility Strategist).
    Mission: Identify "Cheap Volatility" in the watchlist.
    """

    def __init__(self, bot=None):
        self.bot = bot

    async def run_scan(self, symbols: List[str], user_id: int) -> List[Dict[str, Any]]:
        """執行波動率優勢掃描"""
        results = []
        # 使用 to_thread 因為 get_full_user_context 是同步資料庫讀取
        user_ctx = await asyncio.to_thread(get_full_user_context, user_id)

        for sym in symbols:
            try:
                report = await self.inspect_symbol(sym, user_ctx)
                if report and (
                    report.get("is_opportunity") or report.get("is_high_risk_vol")
                ):
                    results.append(report)
            except Exception as e:
                logger.error(f"IV 掃描標的 {sym} 失敗: {e}")
            await asyncio.sleep(0.5)
        return results

    async def inspect_symbol(
        self, symbol: str, user_ctx: Any
    ) -> Optional[Dict[str, Any]]:
        """分析單一標的是否具備波動率優勢或高風險事件"""
        ticker = yf.Ticker(symbol)

        # 1. 獲取歷史數據 (252天) 用於 HV 與 IVP
        df = await market_data_service.get_history_df(symbol, period="1y")
        if df.empty or len(df) < 252:
            return None

        # 2. 計算 20天 HV 序列
        df["Log_Ret"] = np.log(df["Close"] / df["Close"].shift(1))
        # 滾動 HV (20天窗口)
        df["HV_20"] = df["Log_Ret"].rolling(window=20).std() * np.sqrt(252)
        hv_current = df["HV_20"].iloc[-1]

        if pd.isna(hv_current):
            return None

        # 3. 獲取當前 IV (Implied Volatility)
        info = ticker.info
        iv_current = info.get("impliedVolatility")
        if not iv_current or iv_current <= 0:
            # Fallback: 嘗試從 ATM 期權鏈獲取
            try:
                expirations = ticker.options
                if expirations:
                    chain = ticker.option_chain(expirations[0])
                    price = info.get("currentPrice") or df["Close"].iloc[-1]
                    atm_call_idx = (chain.calls["strike"] - price).abs().idxmin()
                    iv_current = chain.calls.loc[atm_call_idx].get(
                        "impliedVolatility", 0.0
                    )
            except Exception:
                return None

        if not iv_current or iv_current <= 0:
            return None

        # 4. IV Rank (IVR) 計算 (基於 252 天 HV 區間)
        hv_range = df["HV_20"].dropna()
        hv_min = hv_range.min()
        hv_max = hv_range.max()

        ivr = (
            ((iv_current - hv_min) / (hv_max - hv_min)) * 100
            if hv_max > hv_min
            else 0.0
        )

        # 5. 財報事件與 IV Crush 偵測
        from services.calendar_service import calendar_service

        earnings_info = await calendar_service.get_symbol_earnings(symbol)
        tte_hours = earnings_info.tte_hours if earnings_info else 9999.0

        is_high_risk_vol = False
        if ivr > 80.0 and tte_hours < 24.0:
            is_high_risk_vol = True

        # 6. Momentum Alignment (EMA / PSQ)
        price = info.get("currentPrice") or df["Close"].iloc[-1]
        ema_eval = await evaluate_ema_trend(symbol, price)
        psq_res = analyze_psq(df)

        has_momentum = (ema_eval["trend"] == "BULLISH_STRONG") or (
            psq_res and psq_res.signal_direction == "Long"
        )

        # 7. 判定是否為機會
        # 機會定義：IVP < 25% 且 IV < HV 且具備趨勢動能
        iv_p = ivr
        is_opportunity = iv_p < 25.0 and iv_current < hv_current and has_momentum

        if not is_opportunity and not is_high_risk_vol:
            return None

        # 建議策略
        days_to_earnings = tte_hours / 24.0
        if 0 <= days_to_earnings <= 7:
            strategy = "牛市價差 (Bull Call Spread)"
            trigger_logic = f"IV 處於歷史極低位且具備看漲動能，但因財報在即 ({days_to_earnings:.1f}天內)，建議使用價差以規避潛在的 IV Crush 並降低成本。"
        elif is_high_risk_vol:
            strategy = "風險中性 (Risk Neutral / Defensive)"
            trigger_logic = f"⚠️ **高風險波動率事件**：IV Rank ({ivr:.1f}%) 極高且財報將於 {tte_hours:.1f} 小時內發布。建議進入防禦模式，嚴防 IV Crush 導致的權利金暴跌。"
        else:
            strategy = "單邊 Call (BTO)"
            trigger_logic = "IV 處於歷史極低位且低於歷史波動率 (IV < HV)，同時價格呈現看漲突破，適合利用廉價權利金建立槓桿部位。"

        # 8. Runway Impact (NRO)
        daily_theta = price * iv_current * 0.01
        runway_impact_days = 0
        if user_ctx.cash_reserve > 0 and user_ctx.monthly_expense > 0:
            daily_burn = user_ctx.monthly_expense / 30.0
            runway_impact_days = (daily_theta / daily_burn) if daily_burn > 0 else 0

        return {
            "symbol": symbol,
            "price": price,
            "iv_current": round(iv_current * 100, 1),
            "hv_current": round(hv_current * 100, 1),
            "iv_rank": round(ivr, 1),
            "is_opportunity": is_opportunity,
            "is_high_risk_vol": is_high_risk_vol,
            "tte_hours": tte_hours,
            "strategy": strategy,
            "trigger_logic": trigger_logic,
            "trend": ema_eval["trend"],
            "psq_signal": psq_res.signal_direction if psq_res else "None",
            "runway_impact": round(runway_impact_days, 1),
        }
