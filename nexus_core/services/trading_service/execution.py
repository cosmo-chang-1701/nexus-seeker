"""執行決策、DDP/IV 掃描、交易驗證管線 Mixin。"""

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from services import market_data_service
from models.execution import MarketCondition, Signal

if TYPE_CHECKING:
    from market_analysis.ddp_inspector import DDPInspector
    from market_analysis.volatility_inspector import VolatilityInspector
    from services.execution_router import ExecutionRouter

logger = logging.getLogger(__name__)


class ExecutionMixin:
    if TYPE_CHECKING:
        ddp_inspector: DDPInspector
        vol_inspector: VolatilityInspector
        execution_router: ExecutionRouter

    def _clean_market_condition_inputs(
        self, price: float, ma20: Any, atr: Any, rsi: Any
    ) -> Tuple[float, float, float]:
        """
        清理指標資料，防止 NaN / None 導致 Pydantic MarketCondition 驗證錯誤。
        """
        import math
        import pandas as pd

        # ma20 fallback to price
        if (
            ma20 is None
            or pd.isna(ma20)
            or (isinstance(ma20, float) and math.isnan(ma20))
        ):
            clean_ma20 = price
        else:
            clean_ma20 = float(ma20)

        # atr fallback to 2% of price
        if (
            atr is None
            or pd.isna(atr)
            or (isinstance(atr, float) and math.isnan(atr))
            or atr < 0
        ):
            clean_atr = 0.02 * price
        else:
            clean_atr = float(atr)

        # rsi fallback to 50.0
        if (
            rsi is None
            or pd.isna(rsi)
            or (isinstance(rsi, float) and math.isnan(rsi))
            or not (0 <= rsi <= 100)
        ):
            clean_rsi = 50.0
        else:
            clean_rsi = float(rsi)

        return clean_ma20, clean_atr, clean_rsi

    async def run_ddp_scan(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """執行 Davis Double Play (DDP) 掃描"""
        return await self.ddp_inspector.run_scan(symbols)

    async def run_iv_opportunity_scan(
        self, symbols: List[str], user_id: int
    ) -> List[Dict[str, Any]]:
        """執行波動率優勢掃描 (IV Opportunity)"""
        return await self.vol_inspector.run_scan(symbols, user_id)

    async def get_execution_decision(
        self, symbol: str, stock_cost: float = 0.0
    ) -> Optional[Any]:
        """
        獲取標的的執行決策 (SHIELD/SPEAR/STANDBY)。
        整合市場數據並調用 ExecutionRouter。
        """
        try:
            # 1. 獲取核心市場指標
            macro = await market_data_service.get_macro_environment()
            df_hist_1d = await market_data_service.get_history_df(
                symbol, period="60d", interval="1d"
            )

            if df_hist_1d.empty:
                return None

            # 計算 MA20 與 ATR
            import pandas_ta as ta

            df_hist_1d["SMA20"] = ta.sma(df_hist_1d["Close"], length=20)
            df_hist_1d["ATR14"] = ta.atr(
                df_hist_1d["High"], df_hist_1d["Low"], df_hist_1d["Close"], length=14
            )
            df_hist_1d["RSI14"] = ta.rsi(df_hist_1d["Close"], length=14)

            last_row = df_hist_1d.iloc[-1]
            price = last_row["Close"]
            ma20 = last_row["SMA20"]
            atr = last_row["ATR14"]
            rsi = last_row["RSI14"]

            # 清理指標防範空值/NaN
            clean_ma20, clean_atr, clean_rsi = self._clean_market_condition_inputs(
                price, ma20, atr, rsi
            )

            # 獲取 Skew 與 UOA (這裡簡化，實戰中可從 SentimentEngine 獲取)
            from market_analysis.sentiment_engine import SentimentEngine

            skew_res = await SentimentEngine.calculate_skew(symbol)
            skew_val = (skew_res.get("skew") or 0.0) / 100.0  # 轉為小數

            # 偵測 UOA
            uoa_list = await SentimentEngine.detect_uoa(symbol)
            uoa_detected = len(uoa_list) > 0

            # 計算相對強度 (Relative Strength)
            from market_analysis.risk_engine import (
                get_sector_benchmark,
                calculate_relative_strength_index,
            )

            benchmark_symbol = get_sector_benchmark(symbol)
            df_bench = await market_data_service.get_history_df(
                benchmark_symbol, period="60d", interval="1d"
            )
            relative_strength = calculate_relative_strength_index(
                df_hist_1d, df_bench, n=20
            )

            # 2. 構建 MarketCondition與調用 Router
            try:
                condition = MarketCondition(
                    vix=macro.get("vix", 18.0),
                    skew_percent=skew_val,
                    asset_price=price,
                    ma20=clean_ma20,
                    atr_14=clean_atr,
                    rsi_14=clean_rsi,
                    uoa_detected=uoa_detected,
                    relative_strength=relative_strength,
                )
                return self.execution_router.evaluate_market(condition)
            except Exception as e:
                logger.debug(
                    f"ExecutionRouter construct/evaluate failed for {symbol}: {e}"
                )
                return Signal.SKIP
        except Exception as e:
            logger.error(f"獲獲取執行決策失敗 for {symbol}: {e}")
            return None

    def _validate_trade_pipeline(
        self, user_context: Any, data: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        4-Stage Validation Pipeline: Macro -> Alpha -> Risk -> Financials.
        """
        strategy = data.get("strategy", "")

        # --- Stage 1: Macro (VIX Battle Ladder & Regime) ---
        vix_allow = data.get("vix_allow_signal", True)
        if "STO" in strategy and not vix_allow:
            return (
                False,
                f"MACRO_REJECT: VIX {data.get('vix_spot'):.1f} tier '{data.get('vix_tier_name')}' restricts STO entry.",
            )

        # --- Stage 2: Alpha (AROC & Signal Strength) ---
        aroc = data.get("aroc", 0.0)
        if "STO" in strategy and aroc < 15.0:
            return False, f"STO 訊號遭攔截：低於 15% AROC 閾值 (目前: {aroc:.1f}%)"
        if "BTO" in strategy and aroc < 30.0:
            return False, f"ALPHA_REJECT: BTO AROC {aroc:.1f}% < 30.0% 閾值。"

        # --- Stage 3: Risk (NRO & Kelly Sizing) ---
        if data.get("safe_qty", 0) <= 0:
            return (
                False,
                "RISK_REJECT: NRO optimization determined zero safe quantity (Risk budget exceeded).",
            )

        # --- Stage 4: Financials (Runway & Survival) ---
        # If runway < 180 days, reject any non-hedging trades that increase margin
        # (Actual implementation will use the runway helper in Phase 4)
        pass

        return True, "APPROVED"
