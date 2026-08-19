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


def _ndtr_prime(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _calculate_gamma(S: float, K: float, t: float, r: float, sigma: float) -> float:
    if S <= 0 or K <= 0 or t <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        return _ndtr_prime(d1) / (S * sigma * math.sqrt(t))
    except Exception:
        return 0.0


async def scrape_symbol_gex_core(symbol: str, browser: Browser) -> dict[str, Any]:
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
                    iv = float(iv_text) / 100.0 if iv_text and iv_text != "-" else 0.20
                    if iv <= 0:
                        iv = 0.20

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

                    t = max(days_to_exp, 0.5) / 365.0

                    option_chain.append(
                        {
                            "strike": strike,
                            "oi": oi,
                            "iv": iv,
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

        net_gex = 0.0
        gex_by_strike: dict[float, float] = {}

        for contract in option_chain:
            strike = contract["strike"]
            oi = contract["oi"]
            iv = contract["iv"]
            t = contract["t"]
            is_call = contract["is_call"]

            gamma = _calculate_gamma(spot_price, strike, t, 0.04, iv)
            gex = oi * gamma * spot_price * spot_price
            if not is_call:
                gex = -gex

            net_gex += gex
            gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + gex

        call_wall = spot_price
        put_wall = spot_price

        if gex_by_strike:
            # Put Wall (GEX Support Wall): Strike with max positive GEX (dealers long gamma, buying dips)
            support_candidates: dict[float, float] = {
                k: v for k, v in gex_by_strike.items() if v > 0
            }
            if support_candidates:
                put_wall = max(support_candidates, key=lambda k: support_candidates[k])

            # Call Wall (Resistance Ceiling): Strike with lowest negative GEX / heavy resistance
            resistance_candidates: dict[float, float] = {
                k: v for k, v in gex_by_strike.items() if v < 0
            }
            if resistance_candidates:
                call_wall = min(
                    resistance_candidates, key=lambda k: resistance_candidates[k]
                )
            elif support_candidates and put_wall > 0:
                # Fallback if all GEX is positive: set call_wall to highest strike with positive GEX above spot
                otm_calls = [k for k in gex_by_strike.keys() if k > spot_price]
                if otm_calls:
                    call_wall = max(otm_calls)

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
