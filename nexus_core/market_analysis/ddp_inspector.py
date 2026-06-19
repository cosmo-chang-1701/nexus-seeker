import logging
import asyncio
import numpy as np
import yfinance as yf
from typing import Dict, Any, Optional, List

from services import market_data_service

logger = logging.getLogger(__name__)


class DDPInspector:
    """
    Davis Double Play (DDP) Detection Engine.
    Formula: Price = EPS * P/E
    Criteria:
    1. EPS Momentum: Quarterly EPS Growth (YoY) > 15%
    2. P/E Compression: Current Trailing P/E < 25th percentile of 3Y range
    3. Forward Alignment: Forward P/E < Trailing P/E
    4. Confirmation: Revenue growth acceleration (last 2 periods)
    """

    def __init__(self, bot=None):
        self.bot = bot

    async def run_scan(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """執行 DDP 掃描並回傳符合條件的標的"""
        results = []
        for sym in symbols:
            try:
                report = await self.inspect_symbol(sym)
                if report and report.get("is_ddp"):
                    results.append(report)
            except Exception as e:
                logger.error(f"DDP 掃描標的 {sym} 失敗: {e}")
            # 避免過快請求
            await asyncio.sleep(0.5)
        return results

    async def inspect_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """分析單一標的是否符合 DDP 條件"""
        ticker = yf.Ticker(symbol)

        # 1. 獲取財務報表 (EPS 與 Revenue)
        q_inc = ticker.quarterly_income_stmt

        if q_inc.empty:
            logger.info(f"[{symbol}] DDP Fail: quarterly_income_stmt is empty")
            return None

        try:
            # EPS Momentum Check (YoY)
            if "Net Income" not in q_inc.index:
                logger.info(f"[{symbol}] DDP Fail: 'Net Income' not in index")
                return None

            if q_inc.shape[1] < 5:
                logger.info(
                    f"[{symbol}] DDP Fail: Not enough data points ({q_inc.shape[1]})"
                )
                return None

            # 使用 Net Income
            net_inc = q_inc.loc["Net Income"]
            rev = q_inc.loc["Total Revenue"]

            curr_eps_val = net_inc.iloc[0]
            prev_y_eps_val = net_inc.iloc[4]  # 去年同期

            eps_growth = (
                (curr_eps_val - prev_y_eps_val) / abs(prev_y_eps_val)
                if prev_y_eps_val != 0
                else 0
            )

            if eps_growth < 0.15:
                logger.info(f"[{symbol}] DDP Fail: EPS growth {eps_growth:.2%} < 15%")
                return None

            # Revenue Acceleration Check
            rev_curr = rev.iloc[0]
            rev_1 = rev.iloc[1]
            rev_4 = rev.iloc[4]
            rev_5 = rev.iloc[5]

            curr_rev_growth = (rev_curr - rev_4) / rev_4 if rev_4 != 0 else 0
            prev_rev_growth = (rev_1 - rev_5) / rev_5 if rev_5 != 0 else 0

            rev_accel = curr_rev_growth > prev_rev_growth
            if not rev_accel:
                logger.info(
                    f"[{symbol}] DDP Fail: Revenue growth not accelerating (Curr: {curr_rev_growth:.2%}, Prev: {prev_rev_growth:.2%})"
                )
                return None

            # 2. P/E Analysis (Historical Range)
            info = ticker.info
            curr_pe = info.get("trailingPE")
            fwd_pe = info.get("forwardPE")

            if curr_pe is not None and float(curr_pe) > 500.0:
                logger.warning(
                    f"[{symbol}] DDP Intercepted: P/E {curr_pe} is extreme (>500), likely a quarterly EPS drop noise."
                )
                return None

            if not curr_pe or not fwd_pe or curr_pe <= 0:
                logger.info(
                    f"[{symbol}] DDP Fail: P/E missing or invalid (Trailing: {curr_pe}, Forward: {fwd_pe})"
                )
                return None

            # Forward Alignment: Forward P/E < Trailing P/E
            if fwd_pe >= curr_pe:
                logger.info(
                    f"[{symbol}] DDP Fail: Forward P/E {fwd_pe} >= Trailing P/E {curr_pe}"
                )
                return None

            # P/E Historical Range (3Y)
            hist = await market_data_service.get_history_df(
                symbol, period="3y", interval="1wk"
            )
            if hist.empty:
                logger.info(f"[{symbol}] DDP Fail: History empty")
                return None

            ttm_eps = info.get("trailingEps")
            if not ttm_eps or ttm_eps <= 0:
                logger.info(f"[{symbol}] DDP Fail: TTM EPS missing")
                return None

            hist_pe = hist["Close"] / ttm_eps
            pe_25th = np.percentile(hist_pe, 25)
            pe_mean = hist_pe.mean()

            if curr_pe > pe_25th:
                logger.info(
                    f"[{symbol}] DDP Fail: Current P/E {curr_pe} > 25th percentile {pe_25th:.2f}"
                )
                return None

            # Confidence Score Calculation
            score = 60.0
            score += min(20, (eps_growth - 0.15) * 100)
            score += 10 if rev_accel else 0
            score += 10 if curr_pe < (pe_mean * 0.8) else 0

            logger.info(f"[{symbol}] DDP PASS!")
            return {
                "symbol": symbol,
                "is_ddp": True,
                "current_pe": curr_pe,
                "pe_mean_3y": pe_mean,
                "pe_25th": pe_25th,
                "eps_growth": eps_growth,
                "forward_pe": fwd_pe,
                "rev_accel": rev_accel,
                "curr_rev_growth": curr_rev_growth,
                "prev_rev_growth": prev_rev_growth,
                "confidence_score": min(100, score),
            }

        except Exception as e:
            logger.info(f"[{symbol}] DDP 深度分析跳過: {e}")
            return None

    def record_signal(self, report: Dict[str, Any]):
        """將信號存入資料庫"""
        try:
            from database.connection import execute_write

            execute_write(
                """
                INSERT INTO ddp_signals (symbol, current_pe, pe_mean_3y, eps_growth, rev_accel_status, confidence_score)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    report["symbol"],
                    report["current_pe"],
                    report["pe_mean_3y"],
                    report["eps_growth"],
                    "加速 (Accelerating)" if report["rev_accel"] else "穩定 (Stable)",
                    report["confidence_score"],
                ),
            )
        except Exception as e:
            logger.error(f"記錄 DDP 信號失敗: {e}")
