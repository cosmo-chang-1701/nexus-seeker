from typing import Any
import logging
import asyncio
import numpy as np
import yfinance as yf
from typing import Dict, Optional, List

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

    def __init__(self, bot: Any = None):
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
        info = ticker.info

        # 1. 產業過濾
        sector = info.get("sector")
        if sector in ["Energy", "Basic Materials"]:
            logger.info(f"[{symbol}] DDP Fail: Sector {sector} is highly cyclical")
            return None

        # 2. 獲取財務報表
        q_inc = ticker.quarterly_income_stmt
        if q_inc.empty:
            logger.info(f"[{symbol}] DDP Fail: quarterly_income_stmt is empty")
            return None

        try:
            # EPS Momentum Check (YoY)
            if "Diluted EPS" in q_inc.index:
                eps_series = q_inc.loc["Diluted EPS"]
            elif "Basic EPS" in q_inc.index:
                eps_series = q_inc.loc["Basic EPS"]
            elif "Net Income" in q_inc.index:
                eps_series = q_inc.loc["Net Income"]
            else:
                logger.info(f"[{symbol}] DDP Fail: No EPS/Net Income data")
                return None

            if q_inc.shape[1] < 5:
                return None

            rev = q_inc.loc["Total Revenue"] if "Total Revenue" in q_inc.index else None
            if rev is None or len(rev) < 5:
                return None

            curr_eps_val = float(eps_series.iloc[0])
            prev_y_eps_val = float(eps_series.iloc[4])

            # EPS 基期防護與 ZeroDivisionError 防護
            if prev_y_eps_val <= 0:
                logger.info(f"[{symbol}] DDP Fail: Base EPS <= 0 ({prev_y_eps_val})")
                return None

            eps_growth = (curr_eps_val - prev_y_eps_val) / prev_y_eps_val

            if eps_growth < 0.15:
                logger.info(f"[{symbol}] DDP Fail: EPS growth {eps_growth:.2%} < 15%")
                return None

            # Revenue Acceleration Check & ZeroDivision 防護
            rev_curr = float(rev.iloc[0])
            rev_1 = float(rev.iloc[1])
            rev_4 = float(rev.iloc[4])

            curr_rev_growth = (rev_curr - rev_4) / rev_4 if rev_4 > 0 else 0

            if q_inc.shape[1] >= 6:
                rev_5 = float(rev.iloc[5])
                prev_rev_growth = (rev_1 - rev_5) / rev_5 if rev_5 > 0 else 0
                rev_accel = curr_rev_growth > prev_rev_growth
            else:
                prev_rev_growth = 0
                rev_accel = curr_rev_growth > 0.10

            if not rev_accel:
                logger.info(f"[{symbol}] DDP Fail: Revenue growth not accelerating")
                return None

            # Operating Margin Bonus
            op_margin_bonus = 0
            if "Operating Income" in q_inc.index:
                op_inc = q_inc.loc["Operating Income"]
                if len(op_inc) >= 2 and rev_curr > 0 and rev_1 > 0:
                    curr_margin = float(op_inc.iloc[0]) / rev_curr
                    prev_margin = float(op_inc.iloc[1]) / rev_1
                    if curr_margin >= prev_margin:
                        op_margin_bonus = 5

            # 3. P/E Analysis
            curr_pe = info.get("trailingPE")
            if curr_pe is not None and float(curr_pe) > 500.0:
                return None
            if not curr_pe or curr_pe <= 0:
                return None

            # Forward Alignment
            fwd_pe = info.get("forwardPE")
            if not fwd_pe:
                q_cash = ticker.quarterly_cashflow
                if not q_cash.empty and "Operating Cash Flow" in q_cash.index:
                    ocf = float(q_cash.loc["Operating Cash Flow"].iloc[0])
                    if ocf <= 0:
                        logger.info(
                            f"[{symbol}] DDP Fail: No Forward P/E and negative OCF"
                        )
                        return None
                else:
                    return None
            elif fwd_pe >= curr_pe:
                logger.info(f"[{symbol}] DDP Fail: Forward P/E >= Trailing P/E")
                return None

            # 估值壓縮 (P/E 算法修正)
            five_yr_pe = info.get("fiveYearAvgPE")
            pe_25th = 0.0
            pe_mean = 0.0

            if five_yr_pe and five_yr_pe > 0:
                pe_mean = five_yr_pe
                pe_25th = five_yr_pe * 0.8
                if curr_pe > pe_25th:
                    logger.info(f"[{symbol}] DDP Fail: Current P/E > 80% of 5Y Avg")
                    return None
            else:
                # 降級方案：比對股價
                hist = await market_data_service.get_history_df(
                    symbol, period="3y", interval="1wk"
                )
                if hist.empty:
                    return None
                price_25th = np.percentile(hist["Close"].dropna(), 25)
                price_mean = hist["Close"].dropna().mean()
                curr_price = info.get("currentPrice", hist["Close"].iloc[-1])
                if curr_price > price_25th:
                    logger.info(f"[{symbol}] DDP Fail: Price not compressed")
                    return None
                # 用股價相對位置推算假的 PE 供 UI 顯示
                pe_25th = curr_pe
                pe_mean = (
                    curr_pe * (price_mean / price_25th) if price_25th > 0 else curr_pe
                )

            # 4. RVOL 催化劑
            df_1d = await market_data_service.get_history_df(
                symbol, period="1mo", interval="1d"
            )
            rvol = 0.0
            rvol_bonus = 0
            if not df_1d.empty and len(df_1d) >= 20:
                avg_vol = df_1d["Volume"].tail(20).mean()
                curr_vol = df_1d["Volume"].iloc[-1]
                if avg_vol > 0:
                    rvol = float(curr_vol / avg_vol)
                    if rvol > 1.5:
                        rvol_bonus = 5

            # 5. Confidence Score
            score = 60.0
            score += min(20, (eps_growth - 0.15) * 100)
            score += 10 if rev_accel else 0
            score += 10 if curr_pe < (pe_mean * 0.8) else 0
            score += op_margin_bonus
            score += rvol_bonus

            logger.info(f"[{symbol}] DDP PASS!")
            return {
                "symbol": symbol,
                "is_ddp": True,
                "current_pe": curr_pe,
                "pe_mean_3y": pe_mean,
                "pe_25th": pe_25th,
                "eps_growth": eps_growth,
                "forward_pe": fwd_pe or curr_pe,
                "rev_accel": rev_accel,
                "curr_rev_growth": curr_rev_growth,
                "prev_rev_growth": prev_rev_growth,
                "confidence_score": min(100, score),
                "rvol": rvol,
                "has_margin_bonus": op_margin_bonus > 0,
            }

        except Exception as e:
            logger.info(f"[{symbol}] DDP 深度分析跳過: {e}")
            return None

    async def record_signal(self, report: Dict[str, Any]) -> Any:
        """將信號存入資料庫"""
        try:
            from database.connection import execute_write_async

            await execute_write_async(
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
