from fastapi import FastAPI, Query
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from bs4 import BeautifulSoup
import logging

app = FastAPI()
logger = logging.getLogger(__name__)


@app.get("/scrape/reddit/{symbol}")
async def scrape_reddit(
    symbol: str, limit: int = Query(5, description="回傳的貼文數量上限")
):
    symbol_clean = symbol.replace("$", "")
    url = (
        f"https://old.reddit.com/r/wallstreetbets+stocks+options/search"
        f"?q=%22{symbol_clean}%22"
        f"&restrict_sr=on"
        f"&sort=new"
        f"&t=day"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                java_script_enabled=False,
            )

            await context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type
                in ["image", "stylesheet", "font", "script"]
                else route.continue_(),
            )

            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)

            try:
                await page.wait_for_selector("div.search-result-link", timeout=5000)
            except PlaywrightTimeoutError:
                page_title = await page.title()
                if "Blocked" in page_title:
                    logger.warning(f"[{symbol}] 被 Reddit 阻擋 (IP Blocked)")
                    return {"status": "error", "data": "被 Reddit 防火牆攔截 (Blocked)"}

                logger.info(f"[{symbol}] 搜尋完成，過去 24 小時無相關討論。")
                return {"status": "success", "data": "過去 24 小時內無相關討論。"}

            html_content = await page.content()
            soup = BeautifulSoup(html_content, "lxml")
            results = soup.select("div.search-result-link")[:limit]

            posts_text = ""
            for res in results:
                title_elem = res.select_one("a.search-title")
                title = title_elem.text.strip() if title_elem else "N/A"

                sub_elem = res.select_one("a.search-subreddit-link")
                sub = sub_elem.text.strip().replace("r/", "") if sub_elem else "unknown"

                score_elem = res.select_one("span.search-score")
                score_text = score_elem.text.strip() if score_elem else "0"
                score = "".join(filter(str.isdigit, score_text))

                posts_text += f"[{sub} | 共識分數:{score if score else 0}] {title}\n"

            return {"status": "success", "data": posts_text}

        except Exception as e:
            logger.error(f"Playwright 執行嚴重例外: {str(e)}")
            return {"status": "error", "data": f"本地端執行例外: {str(e)}"}
        finally:
            await browser.close()


@app.get("/scrape/macro/gex")
async def scrape_gex():
    import math
    import re
    from datetime import date

    # Standard fallback values
    fallback = {"spy_spot": 510.0, "gamma_flip": 515.0, "put_wall": 505.0}

    # Black-Scholes math helper
    def ndtr_prime(x):
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    def calculate_gamma(S, K, t, r, sigma):
        if S <= 0 or K <= 0 or t <= 0 or sigma <= 0:
            return 0.0
        try:
            d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * t) / (
                sigma * math.sqrt(t)
            )
            return ndtr_prime(d1) / (S * sigma * math.sqrt(t))
        except Exception:
            return 0.0

    def calculate_total_gex(S, option_chain, r=0.04):
        total_gex = 0.0
        for contract in option_chain:
            strike = contract["strike"]
            oi = contract["oi"]
            iv = contract["iv"]
            t = contract["t"]
            is_call = contract["is_call"]

            gamma = calculate_gamma(S, strike, t, r, iv)
            gex = oi * gamma * S * S * 0.01
            if not is_call:
                gex = -gex
            total_gex += gex
        return total_gex

    def find_gamma_flip(spot_price, option_chain):
        low_price = spot_price * 0.8
        high_price = spot_price * 1.2
        steps = 100
        prices = [
            low_price + (high_price - low_price) * i / steps for i in range(steps + 1)
        ]
        gex_values = [calculate_total_gex(p, option_chain) for p in prices]

        flip_price = spot_price
        for i in range(len(prices) - 1):
            if gex_values[i] * gex_values[i + 1] <= 0:
                p1, p2 = prices[i], prices[i + 1]
                g1, g2 = gex_values[i], gex_values[i + 1]
                if g2 - g1 != 0:
                    flip_price = p1 - g1 * (p2 - p1) / (g2 - g1)
                else:
                    flip_price = (p1 + p2) / 2.0
                break
        return flip_price

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            )
            # Speed up loading by blocking images and CSS
            await context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ["image", "stylesheet", "font"]
                else route.continue_(),
            )
            page = await context.new_page()
            await page.goto(
                "https://finance.yahoo.com/quote/SPY/options",
                timeout=25000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(3000)

            html = await page.content()
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
                    "SPY spot price parsed <= 0 from Yahoo Finance, using fallbacks."
                )
                return {"status": "success", "data": fallback}

            # Parse option tables
            tables = soup.select("table")
            if len(tables) < 2:
                logger.warning(
                    "Yahoo Finance options tables not found, using fallbacks."
                )
                return {"status": "success", "data": fallback}

            option_chain = []
            put_oi_by_strike = {}
            today = date.today()

            def parse_table(table, is_call):
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
                        iv = (
                            float(iv_text) / 100.0
                            if iv_text and iv_text != "-"
                            else 0.20
                        )
                        if iv <= 0:
                            iv = 0.20

                        match = re.match(r"SPY(\d{2})(\d{2})(\d{2})[CP]", contract_name)
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

                        if not is_call:
                            put_oi_by_strike[strike] = (
                                put_oi_by_strike.get(strike, 0) + oi
                            )
                    except Exception:
                        pass

            parse_table(tables[0], is_call=True)
            parse_table(tables[1], is_call=False)

            if not option_chain:
                logger.warning("No option chain contracts parsed, using fallbacks.")
                return {"status": "success", "data": fallback}

            # Calculate Put Wall
            put_wall = spot_price - 5.0
            if put_oi_by_strike:
                put_wall = max(put_oi_by_strike, key=put_oi_by_strike.get)

            # Calculate Gamma Flip
            gamma_flip = find_gamma_flip(spot_price, option_chain)

            return {
                "status": "success",
                "data": {
                    "spy_spot": round(spot_price, 2),
                    "gamma_flip": round(gamma_flip, 2),
                    "put_wall": round(put_wall, 2),
                },
            }
        except Exception as e:
            logger.warning(f"GEX scrape failed with exception: {e}, using fallbacks.")
            return {"status": "success", "data": fallback}
        finally:
            await browser.close()


@app.get("/scrape/macro/fedwatch")
async def scrape_fedwatch():
    import re

    fallback = {"probability": 0.72, "decision": "maintain"}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-http2",  # Crucial to bypass Cloudflare protocol error
            ],
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            # Speed up loading by blocking images and CSS
            await context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ["image", "stylesheet", "font"]
                else route.continue_(),
            )
            page = await context.new_page()
            await page.add_init_script("delete navigator.__proto__.webdriver;")

            await page.goto(
                "https://www.investing.com/central-banks/fed-rate-monitor",
                timeout=25000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(3000)

            text = await page.evaluate("() => document.body.innerText")

            # Parse Current Fed Rate
            current_rate = 3.75
            rate_match = re.search(r"FED\s+(\d+\.\d+)%", text)
            if rate_match:
                current_rate = float(rate_match.group(1))

            # Find next meeting block using anchor
            anchor_idx = text.find("Meeting Time:")
            if anchor_idx == -1:
                logger.warning(
                    "Investing.com Meeting Time anchor not found, using fallbacks."
                )
                return {"status": "success", "data": fallback}

            start_idx = max(0, anchor_idx - 100)
            search_text = text[start_idx : start_idx + 600]

            # Parse line-by-line range and current probabilities
            prob_map = {}
            for line in search_text.split("\n"):
                m = re.search(r"(\d+\.\d+)\s*-\s*(\d+\.\d+)\s+(\d+\.\d+)%", line)
                if m:
                    low = float(m.group(1))
                    high = float(m.group(2))
                    prob_val = float(m.group(3)) / 100.0
                    prob_map[(low, high)] = prob_val

            if not prob_map:
                logger.warning(
                    "No probabilities parsed from Investing.com, using fallbacks."
                )
                return {"status": "success", "data": fallback}

            # Sum probability of maintaining or hiking (range low >= current_rate - 0.25)
            current_range_low = current_rate - 0.25
            maintain_or_hike_prob = 0.0
            for (low, high), p_val in prob_map.items():
                if low >= current_range_low:
                    maintain_or_hike_prob += p_val

            return {
                "status": "success",
                "data": {
                    "probability": round(maintain_or_hike_prob, 4),
                    "decision": "maintain",
                },
            }
        except Exception as e:
            logger.warning(
                f"FedWatch scrape failed with exception: {e}, using fallbacks."
            )
            return {"status": "success", "data": fallback}
        finally:
            await browser.close()
