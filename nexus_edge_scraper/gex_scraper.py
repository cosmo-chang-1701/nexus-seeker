"""
gex_scraper.py

單一標的 GEX (Gamma Exposure) 抓取與計算核心邏輯，從 local_api.py 的
`/api/v1/scrape/options/{symbol}/gex` 端點抽出，改為接收一個已存在的
Playwright `Browser` 實例，而非每次呼叫自行 launch 一顆新的 headless browser。

供兩處共用：
- local_api.py 的即時端點（每次請求仍各自 launch 一顆短命 browser）
- scheduler.py 的背景排程（重用一顆長駐 browser，逐一輪詢多個標的以降低成本）
"""

from typing import Any
import logging
import math
import re
import statistics
from datetime import date

from bs4 import BeautifulSoup
from playwright.async_api import Browser, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

FALLBACK_GEX: dict[str, Any] = {
    "spot": 0.0,
    "net_gex": 0.0,
    "call_wall": 0.0,
    "put_wall": 0.0,
    "gex_profile": {},
}

_GEX_MIN_DELTA_THRESHOLD = 0.02


def _ndtr_prime(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _ndtr(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _calculate_gamma(
    S: float, K: float, t: float, r: float, sigma: float, q: float = 0.0
) -> float:
    if S <= 0 or K <= 0 or t <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * t) / (
            sigma * math.sqrt(t)
        )
        return (math.exp(-q * t) * _ndtr_prime(d1)) / (S * sigma * math.sqrt(t))
    except Exception:
        return 0.0


def _calculate_delta(
    S: float, K: float, t: float, r: float, sigma: float, is_call: bool, q: float = 0.0
) -> float:
    if S <= 0 or K <= 0 or t <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * t) / (
            sigma * math.sqrt(t)
        )
        if is_call:
            return math.exp(-q * t) * _ndtr(d1)
        else:
            return math.exp(-q * t) * (_ndtr(d1) - 1.0)
    except Exception:
        return 0.0


def _filter_noise_contracts(
    chain: list[dict[str, Any]], spot: float, r: float = 0.04, q: float = 0.0
) -> list[dict[str, Any]]:
    """過濾雜訊合約：先剔除 oi<=0（本就對曝險零貢獻），再對每邊
    (calls / puts) 各自嘗試以 |delta| < _GEX_MIN_DELTA_THRESHOLD 剔除
    深度價外、Delta 趨近於零的合約（避免掛牌雜訊/長天期價外合約的異常
    OI 堆積扭曲 Wall 判定與 Gamma Flip 估算）。若對某一邊套用 delta 過濾
    後會導致該邊清空，則該邊 fail-safe 退回只套用 oi>0 過濾，確保 Wall /
    Gamma Flip 計算永遠有資料可用。
    """
    oi_filtered = [c for c in chain if c["oi"] > 0]
    if not oi_filtered:
        return oi_filtered

    def _delta_filter(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = []
        for c in contracts:
            delta = _calculate_delta(
                spot, c["strike"], c["t"], r, c["iv"], c["is_call"], q=q
            )
            if abs(delta) >= _GEX_MIN_DELTA_THRESHOLD:
                kept.append(c)
        return kept

    calls = [c for c in oi_filtered if c["is_call"]]
    puts = [c for c in oi_filtered if not c["is_call"]]

    calls_filtered = _delta_filter(calls) or calls
    puts_filtered = _delta_filter(puts) or puts

    return calls_filtered + puts_filtered


async def scrape_symbol_gex_core(
    symbol: str,
    browser: Browser,
    risk_free_rate: float = 0.04,
    dividend_yield: Any | None = None,
) -> dict[str, Any]:
    """對已存在的 Playwright browser 實例執行單一標的的 GEX 抓取與計算。

    永遠回傳一個 data dict（成功時為實際計算結果，任何解析/抓取失敗時
    回傳 `FALLBACK_GEX` 的副本），與原本端點行為一致，不拋出例外。
    """
    symbol_upper = symbol.upper()
    fallback = dict(FALLBACK_GEX)

    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        )
        try:
            await Stealth().apply_stealth_async(context)

            # Speed up loading by blocking images and CSS
            async def safe_route(route: Any) -> None:
                try:
                    if route.request.resource_type in ["image", "stylesheet", "font"]:
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    pass

            await context.route("**/*", safe_route)
            page = await context.new_page()
            try:
                try:
                    await page.goto(
                        f"https://finance.yahoo.com/quote/{symbol_upper}/options",
                        timeout=10000,
                        wait_until="commit",
                    )
                except PlaywrightTimeoutError:
                    logger.info(
                        f"Page.goto timeout for {symbol_upper}, attempting to proceed with loaded content..."
                    )

                try:
                    # 等待關鍵資料(表格)出現，最多等待 10 秒
                    await page.wait_for_selector("table", timeout=10000)
                except PlaywrightTimeoutError:
                    pass

                # 短暫等待以確保動態渲染(React/Client-side)完成
                await page.wait_for_timeout(1500)

                html = await page.content()
            finally:
                await context.unroute_all(behavior="ignoreErrors")
                await page.close()
        finally:
            await context.close()

        soup = BeautifulSoup(html, "lxml")

        # Parse spot price
        spot_elem = soup.select_one('[data-testid="qsp-price"]')
        spot_price = 0.0
        if spot_elem and spot_elem.text:
            try:
                spot_price = float(spot_elem.text.replace(",", ""))
            except ValueError:
                pass

        if spot_price <= 0:
            logger.warning(
                f"{symbol_upper} spot price parsed <= 0 from Yahoo Finance, using fallbacks."
            )
            return fallback

        # Parse option tables
        tables = soup.select("table")
        if len(tables) < 2:
            logger.warning(
                f"Yahoo Finance options tables not found for {symbol_upper}, using fallbacks."
            )
            return fallback

        option_chain: list[dict[str, Any]] = []
        today = date.today()

        def parse_table(table: Any, is_call: bool) -> None:
            rows = table.select("tr")
            for r in rows[1:]:
                cols = [td.text.strip() for td in r.select("td")]
                if len(cols) < 11:
                    continue
                try:
                    contract_name = cols[0]
                    strike = float(cols[2].replace(",", ""))

                    oi_text = cols[9].replace(",", "")
                    oi = int(oi_text) if oi_text and oi_text != "-" else 0

                    iv_text = cols[10].replace("%", "").replace(",", "")
                    try:
                        iv_val_parsed = (
                            float(iv_text) / 100.0
                            if iv_text and iv_text != "-"
                            else None
                        )
                    except ValueError:
                        iv_val_parsed = None

                    match = re.match(
                        r"[A-Za-z]+(\d{2})(\d{2})(\d{2})[CP]", contract_name
                    )
                    if match:
                        exp_yr = 2000 + int(match.group(1))
                        exp_mo = int(match.group(2))
                        exp_dy = int(match.group(3))
                        exp_date = date(exp_yr, exp_mo, exp_dy)
                        days_to_exp = (exp_date - today).days
                    else:
                        days_to_exp = 7

                    # 防範假牆 (ISSUE-3.4)：0-DTE / 1-DTE 合約在結算日當天 t->0 導致 Gamma 虛高膨脹，
                    # 至少以 2.0 天作為 Gamma 定價底限，過濾即將歸零的幻影假牆 (Phantom Wall)。
                    t = max(days_to_exp, 2.0) / 365.0

                    option_chain.append(
                        {
                            "strike": strike,
                            "oi": oi,
                            "iv": iv_val_parsed,
                            "t": t,
                            "is_call": is_call,
                        }
                    )
                except Exception:
                    pass

        parse_table(tables[0], is_call=True)
        parse_table(tables[1], is_call=False)

        if not option_chain:
            logger.warning(
                f"No option chain parsed for {symbol_upper}, using fallbacks."
            )
            return fallback

        # 自適應 IV 估計 (ISSUE-3.4)：嚴禁硬編碼 0.20 導致高波/低波股 Gamma 嚴重扭曲。
        # 提取全鏈有效 IV 中位數作為缺失合約的自適應基準。
        valid_ivs = [
            c["iv"] for c in option_chain if c["iv"] is not None and c["iv"] > 0.01
        ]
        adaptive_iv = float(statistics.median(valid_ivs)) if valid_ivs else 0.30
        for c in option_chain:
            if c["iv"] is None or c["iv"] <= 0.01:
                c["iv"] = adaptive_iv

        # 解析或補齊股息率 q (ISSUE-4.3)
        effective_div_yield = 0.0
        if dividend_yield is not None and float(dividend_yield) >= 0:
            effective_div_yield = float(dividend_yield)
        else:
            try:
                import yfinance as yf

                t_obj = yf.Ticker(symbol_upper)
                fast_div = getattr(t_obj.fast_info, "dividend_yield", None)
                if fast_div is not None and float(fast_div) > 0:
                    effective_div_yield = float(fast_div)
                elif hasattr(t_obj, "info") and t_obj.info:
                    raw_div = t_obj.info.get("dividendYield") or t_obj.info.get(
                        "trailingAnnualDividendYield"
                    )
                    if raw_div:
                        effective_div_yield = float(raw_div)
            except Exception:
                effective_div_yield = 0.0

        option_chain = _filter_noise_contracts(
            option_chain, spot_price, r=risk_free_rate, q=effective_div_yield
        )
        if not option_chain:
            logger.warning(
                f"All contracts filtered as noise for {symbol_upper}, using fallbacks."
            )
            return fallback

        net_gex = 0.0
        gex_by_strike: dict[float, float] = {}
        call_gex_by_strike: dict[float, float] = {}
        put_gex_by_strike: dict[float, float] = {}

        for contract in option_chain:
            strike = contract["strike"]
            oi = contract["oi"]
            iv = contract["iv"]
            t = contract["t"]
            is_call = contract["is_call"]

            gamma = _calculate_gamma(
                spot_price, strike, t, risk_free_rate, iv, q=effective_div_yield
            )
            # 補齊美股 100 股合約乘數 (ISSUE-4.2)：Dollar GEX = OI * 100 * Gamma * S^2
            raw_gex = oi * 100.0 * gamma * spot_price * spot_price

            if is_call:
                call_gex_by_strike[strike] = (
                    call_gex_by_strike.get(strike, 0.0) + raw_gex
                )
                signed_gex = raw_gex
            else:
                put_gex_by_strike[strike] = put_gex_by_strike.get(strike, 0.0) + raw_gex
                signed_gex = -raw_gex

            net_gex += signed_gex
            gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + signed_gex

        call_wall = spot_price
        put_wall = spot_price

        # Call Wall (Resistance Ceiling): 現價以上，Call GEX 曝險最大的履約價
        call_wall_candidates: dict[float, float] = {
            k: v for k, v in call_gex_by_strike.items() if k >= spot_price and v > 0
        }
        if call_wall_candidates:
            call_wall = max(call_wall_candidates, key=lambda k: call_wall_candidates[k])

        # Put Wall (GEX Support Wall): 現價以下，Put GEX 曝險最大的履約價
        put_wall_candidates: dict[float, float] = {
            k: v for k, v in put_gex_by_strike.items() if k <= spot_price and v > 0
        }
        if put_wall_candidates:
            put_wall = max(put_wall_candidates, key=lambda k: put_wall_candidates[k])

        return {
            "spot": round(spot_price, 2),
            "net_gex": round(net_gex, 2),
            "call_wall": round(call_wall, 2),
            "put_wall": round(put_wall, 2),
            "gex_profile": {k: round(v, 2) for k, v in gex_by_strike.items()},
        }
    except Exception as e:
        logger.warning(
            f"Symbol GEX scrape failed with exception: {e}, using fallbacks."
        )
        return fallback
