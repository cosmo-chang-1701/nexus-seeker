"""local_api：總經 GEX/流動性/FedWatch/暗池 Playwright 抓取，以及個股 GEX 端點。"""

from typing import Any
import logging

from bs4 import BeautifulSoup
from fastapi import APIRouter
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from playwright_stealth import Stealth

from gex_scraper import scrape_symbol_gex_core

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/v1/scrape/macro/gex")
async def scrape_gex() -> dict[str, Any]:
    import math
    import re
    from datetime import date

    # Standard fallback values
    fallback = {"spy_spot": 510.0, "gamma_flip": 515.0, "put_wall": 505.0}

    # Black-Scholes math helper
    def ndtr_prime(x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    def calculate_gamma(S: float, K: float, t: float, r: float, sigma: float) -> float:
        if S <= 0 or K <= 0 or t <= 0 or sigma <= 0:
            return 0.0
        try:
            d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * t) / (
                sigma * math.sqrt(t)
            )
            return ndtr_prime(d1) / (S * sigma * math.sqrt(t))
        except Exception:
            return 0.0

    def calculate_total_gex(
        S: float, option_chain: list[dict[str, Any]], r: float = 0.04
    ) -> float:
        total_gex = 0.0
        for contract in option_chain:
            strike = contract["strike"]
            oi = contract["oi"]
            iv = contract["iv"]
            t = contract["t"]
            is_call = contract["is_call"]

            gamma = calculate_gamma(S, strike, t, r, iv)
            gex = oi * gamma * S * S
            if not is_call:
                gex = -gex
            total_gex += gex
        return total_gex

    def find_gamma_flip(spot_price: float, option_chain: list[dict[str, Any]]) -> float:
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
                        "https://finance.yahoo.com/quote/SPY/options",
                        timeout=10000,
                        wait_until="commit",
                    )
                except PlaywrightTimeoutError:
                    logger.info(
                        "Page.goto timeout for SPY, attempting to proceed with loaded content..."
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

            option_chain: list[dict[str, Any]] = []
            put_oi_by_strike: dict[float, int] = {}
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
                put_wall = max(put_oi_by_strike, key=lambda k: put_oi_by_strike[k])

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


@router.get("/api/v1/scrape/macro/core_metrics")
async def scrape_core_macro_metrics() -> dict[str, Any]:
    import httpx
    import asyncio
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    fallback = {
        "rrp": 420.5,
        "fed_balance": 7.25,
        "uer": 4.0,
        "sahm_rule": 0.35,
        "fear_greed": 48.0,
    }

    async def fetch_fred_csv_all(
        series_id: str, context: Any
    ) -> list[tuple[str, float]]:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        data: list[tuple[str, float]] = []
        try:
            page = await context.new_page()
            try:
                async with page.expect_download(timeout=15000) as download_info:
                    try:
                        await page.goto(url)
                    except Exception as e:
                        if "Download is starting" not in str(e):
                            raise e
                download = await download_info.value
                path = await download.path()
                with open(path, "r") as f:
                    lines = f.readlines()
                    for line in reversed(lines):
                        parts = line.strip().split(",")
                        if len(parts) >= 2:
                            try:
                                data.append((parts[0].strip(), float(parts[1].strip())))
                            except ValueError:
                                continue
            finally:
                await page.close()
        except Exception:
            pass
        return data

    async def fetch_fred_csv(series_id: str, context: Any) -> float | None:
        data = await fetch_fred_csv_all(series_id, context)
        return data[0][1] if data else None

    async def fetch_cnn_fgi() -> float | None:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://edition.cnn.com/",
            "Origin": "https://edition.cnn.com",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    return float(data["fear_and_greed"]["score"])
        except Exception:
            pass
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    accept_downloads=True,
                )
                await Stealth().apply_stealth_async(context)

                rrp_data, walcl, unrate, sahm, fgi = await asyncio.gather(
                    fetch_fred_csv_all("RRPONTSYD", context),
                    fetch_fred_csv("WALCL", context),
                    fetch_fred_csv("UNRATE", context),
                    fetch_fred_csv("SAHMREALTIME", context),
                    fetch_cnn_fgi(),
                )
            finally:
                await browser.close()

        rrp = rrp_data[0][1] if rrp_data else None
        rrp_change = 0.0
        if rrp_data and len(rrp_data) > 30:
            # RRPONTSYD is daily, so index 30 is roughly 30 days ago
            past_rrp = rrp_data[30][1]
            if past_rrp > 0 and rrp is not None:
                rrp_change = round(((rrp - past_rrp) / past_rrp) * 100.0, 1)

        return {
            "status": "success",
            "data": {
                "rrp": round(rrp, 1) if rrp is not None else fallback["rrp"],
                "rrp_change_30d": rrp_change,
                "fed_balance": round(walcl / 1000000.0, 2)
                if walcl is not None
                else fallback["fed_balance"],
                "uer": round(unrate, 1) if unrate is not None else fallback["uer"],
                "sahm_rule": round(sahm, 2)
                if sahm is not None
                else fallback["sahm_rule"],
                "fear_greed": round(fgi, 1)
                if fgi is not None
                else fallback["fear_greed"],
            },
        }
    except Exception as e:
        logger.warning(
            f"Macro core metrics scrape failed with exception: {e}, using fallbacks."
        )
        return {"status": "success", "data": fallback}


@router.get("/api/v1/scrape/macro/liquidity")
async def scrape_liquidity() -> dict[str, Any]:
    import asyncio
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    fallback = {
        "ted_spread": 0.15,
        "sofr_90": 5.3,
        "dtb3": 5.15,
        "high_yield_spread": 3.1,
    }

    async def fetch_fred_csv(series_id: str, context: Any) -> float | None:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        try:
            page = await context.new_page()
            try:
                async with page.expect_download(timeout=15000) as download_info:
                    try:
                        await page.goto(url)
                    except Exception as e:
                        if "Download is starting" not in str(e):
                            raise e
                download = await download_info.value
                path = await download.path()
                val = None
                with open(path, "r") as f:
                    lines = f.readlines()
                    for line in reversed(lines):
                        parts = line.strip().split(",")
                        if len(parts) >= 2:
                            try:
                                val = float(parts[1].strip())
                                break
                            except ValueError:
                                continue
            finally:
                await page.close()
            return val
        except Exception:
            pass
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    accept_downloads=True,
                )
                await Stealth().apply_stealth_async(context)

                sofr_90, dtb3, hy_spread = await asyncio.gather(
                    fetch_fred_csv("SOFR90DAYAVG", context),
                    fetch_fred_csv("DTB3", context),
                    fetch_fred_csv("BAMLH0A0HYM2", context),
                )
            finally:
                await browser.close()

        if sofr_90 is None or dtb3 is None:
            return {"status": "success", "data": fallback}

        ted_spread = round(sofr_90 - dtb3, 4)

        return {
            "status": "success",
            "data": {
                "ted_spread": ted_spread,
                "sofr_90": round(sofr_90, 4),
                "dtb3": round(dtb3, 4),
                "high_yield_spread": round(hy_spread, 4)
                if hy_spread is not None
                else fallback["high_yield_spread"],
            },
        }
    except Exception as e:
        logger.warning(
            f"Macro liquidity scrape failed with exception: {e}, using fallbacks."
        )
        return {"status": "success", "data": fallback}


@router.get("/api/v1/scrape/macro/fedwatch")
async def scrape_fedwatch() -> dict[str, Any]:
    import re
    import calendar
    import requests
    import asyncio
    from datetime import datetime, date
    import openpyxl
    import yfinance as yf

    fallback: dict[str, Any] = {
        "probability": 0.50,
        "meeting_date": "",
        "current_target": "3.50%-3.75%",
        "prob_maintain": 50.0,
        "prob_hike": 0.0,
        "prob_cut": 50.0,
        "decision": "maintain",
        "source": "fallback",
    }

    def _fetch_and_calculate_zq_futures() -> dict[str, Any]:
        """從 CBOT 30 天期聯邦基金期貨 (ZQ) 報價即時計算 CME FedWatch 利率定價機率。"""
        fomc_schedule: list[date] = [
            # 2026
            date(2026, 1, 28),
            date(2026, 3, 18),
            date(2026, 5, 6),
            date(2026, 6, 17),
            date(2026, 7, 29),
            date(2026, 9, 16),
            date(2026, 11, 4),
            date(2026, 12, 16),
            # 2027
            date(2027, 1, 27),
            date(2027, 3, 17),
            date(2027, 5, 5),
            date(2027, 6, 16),
            date(2027, 7, 28),
            date(2027, 9, 22),
            date(2027, 11, 3),
            date(2027, 12, 15),
            # 2028
            date(2028, 1, 26),
            date(2028, 3, 15),
            date(2028, 5, 3),
            date(2028, 6, 14),
            date(2028, 7, 26),
            date(2028, 9, 20),
            date(2028, 11, 1),
            date(2028, 12, 13),
        ]
        month_codes: dict[int, str] = {
            1: "F",
            2: "G",
            3: "H",
            4: "J",
            5: "K",
            6: "M",
            7: "N",
            8: "Q",
            9: "U",
            10: "V",
            11: "X",
            12: "Z",
        }

        today = date.today()
        future_meetings = [m for m in fomc_schedule if m >= today]
        next_meeting = min(future_meetings) if future_meetings else date(2026, 9, 16)

        m_code = month_codes.get(next_meeting.month, "U")
        y_suffix = str(next_meeting.year)[-2:]
        ticker_symbol = f"ZQ{m_code}{y_suffix}.CBT"

        # CME 官方定價邏輯：獲取前一個月 (Prior Month) 期貨合約以獲取進入會議月時的精確預期利率 R_start
        prior_month = 12 if next_meeting.month == 1 else next_meeting.month - 1
        prior_year = (
            next_meeting.year - 1 if next_meeting.month == 1 else next_meeting.year
        )
        prior_m_code = month_codes.get(prior_month, "Q")
        prior_y_suffix = str(prior_year)[-2:]
        prior_ticker_symbol = f"ZQ{prior_m_code}{prior_y_suffix}.CBT"

        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="5d")
        if hist.empty:
            ticker = yf.Ticker("ZQ=F")
            hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError(f"Unable to fetch futures data for {ticker_symbol}")

        latest_price = float(hist["Close"].iloc[-1])

        # 動態獲取當前基準目標利率區間與中位數
        r1 = 3.625
        current_target = "3.50%-3.75%"

        # 優先從前一個月連續期貨獲取精確 R_start (CME 官方錨定法)
        prior_price: float | None = None
        try:
            prior_ticker = yf.Ticker(prior_ticker_symbol)
            prior_hist = prior_ticker.history(period="5d")
            if not prior_hist.empty:
                prior_price = float(prior_hist["Close"].iloc[-1])
                r1 = 100.0 - prior_price
        except Exception:
            pass

        try:
            irx_hist = yf.Ticker("^IRX").history(period="5d")
            if not irx_hist.empty:
                irx_val = float(irx_hist["Close"].iloc[-1])
                if irx_val > 0.0:
                    b_idx = round((irx_val - 0.125) / 0.25)
                    low_r = b_idx * 0.25
                    high_r = low_r + 0.25
                    if prior_price is None:
                        r1 = (low_r + high_r) / 2.0
                    current_target = f"{low_r:.2f}%-{high_r:.2f}%"
        except Exception:
            pass

        _, days_in_month = calendar.monthrange(next_meeting.year, next_meeting.month)
        d_prior = next_meeting.day
        d_post = max(1, days_in_month - d_prior)
        implied_avg = 100.0 - latest_price

        # CME 階梯權重反推會議後目標利率 R2
        r2 = (days_in_month * implied_avg - d_prior * r1) / d_post
        delta_r = r2 - r1

        if delta_r >= 0.0:
            prob_hike = round(min(100.0, max(0.0, (delta_r / 0.25) * 100.0)), 1)
            prob_cut = 0.0
            prob_maintain = round(100.0 - prob_hike, 1)
        else:
            prob_cut = round(min(100.0, max(0.0, (-delta_r / 0.25) * 100.0)), 1)
            prob_hike = 0.0
            prob_maintain = round(100.0 - prob_cut, 1)

        # 分類與宏觀緊縮純量計算
        if prob_hike >= 50.0 or (
            prob_hike >= 30.0 and prob_hike > prob_cut and prob_hike > prob_maintain
        ):
            decision = "hike"
            prob = round(min(0.95, (50.0 + prob_hike / 2.0) / 100.0), 4)
        elif prob_cut >= 50.0 or (
            prob_cut >= 30.0 and prob_cut > prob_hike and prob_cut > prob_maintain
        ):
            decision = "cut"
            prob = round(max(0.05, (50.0 - prob_cut / 2.0) / 100.0), 4)
        elif prob_maintain >= 50.0:
            decision = "maintain"
            prob = round(
                max(0.05, min(0.95, (50.0 + (prob_hike - prob_cut) / 2.0) / 100.0)),
                4,
            )
        else:
            decision = "split"
            prob = round(
                max(0.05, min(0.95, (50.0 + (prob_hike - prob_cut) / 2.0) / 100.0)),
                4,
            )

        return {
            "probability": prob,
            "meeting_date": next_meeting.strftime("%m/%d"),
            "current_target": current_target,
            "prob_maintain": round(prob_maintain, 1),
            "prob_hike": round(prob_hike, 1),
            "prob_cut": round(prob_cut, 1),
            "decision": decision,
            "futures_price": latest_price,
            "source": "CME 30-Day Fed Funds Futures (ZQ)",
        }

    def _fetch_and_parse_excel() -> dict[str, Any]:
        url = "https://www.atlantafed.org/-/media/Project/Atlanta/FRBA/Documents/cenfis/market-probability-tracker/mpt_histdata.xlsx"
        local_path = "/tmp/mpt_histdata.xlsx"

        response = requests.get(url, stream=True)
        response.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        wb = openpyxl.load_workbook(local_path, data_only=True, read_only=True)
        ws = wb["DATA"]

        # 1. Group rows by date
        data_by_date: dict[str, list[Any]] = {}
        for row in ws.iter_rows(max_row=1000000, max_col=5, values_only=True):
            if not row or row[0] == "date" or row[0] is None:
                continue
            dt_str = str(row[0]).strip()
            if dt_str not in data_by_date:
                data_by_date[dt_str] = []
            data_by_date[dt_str].append(row)

        if not data_by_date:
            raise ValueError("No data found in the Excel sheet")

        # Get the latest date
        sorted_dates = sorted(data_by_date.keys())
        latest_date_str = sorted_dates[-1]

        latest_rows = data_by_date[latest_date_str]

        # 2. Group latest rows by meeting date (reference_start)
        by_meeting: dict[date, list[Any]] = {}
        for r in latest_rows:
            meeting_dt = r[1]
            if not isinstance(meeting_dt, (datetime, date)):
                if isinstance(meeting_dt, str):
                    try:
                        meeting_dt = datetime.fromisoformat(meeting_dt)
                    except ValueError:
                        continue
                else:
                    continue
            meeting_date = (
                meeting_dt.date() if isinstance(meeting_dt, datetime) else meeting_dt
            )
            if meeting_date not in by_meeting:
                by_meeting[meeting_date] = []
            by_meeting[meeting_date].append(r)

        # Find the next meeting date >= today
        today = date.today()
        future_meetings = [m for m in by_meeting.keys() if m >= today]
        if not future_meetings:
            latest_available_meeting = sorted(by_meeting.keys())[-1]
            if (today - latest_available_meeting).days > 45:
                logger.warning(
                    f"FedWatch Excel data is outdated (latest meeting {latest_available_meeting} is > 45 days old). Using fallbacks."
                )
                raise ValueError(
                    f"Stale meeting data: latest meeting {latest_available_meeting}"
                )
            next_meeting = latest_available_meeting
        else:
            next_meeting = min(future_meetings)

        meeting_rows = by_meeting[next_meeting]

        # 3. Parse target range and calculate maintain / hike / cut probabilities
        first_row = meeting_rows[0]
        target_range_str = str(first_row[2] or "350bps - 375bps").strip()

        m = re.search(r"(\d+)bps\s*-\s*(\d+)bps", target_range_str)
        if m:
            low_bps = int(m.group(1))
            high_bps = int(m.group(2))
            current_target = f"{low_bps / 100:.2f}%-{high_bps / 100:.2f}%"
            current_range_low_bps = low_bps
            current_range_high_bps = high_bps
        else:
            current_target = target_range_str
            current_range_low_bps = 350
            current_range_high_bps = 375

        direct_cut: float | None = None
        direct_hike: float | None = None
        maintain_bucket_val: float | None = None
        bucket_cut_sum = 0.0
        bucket_hike_sum = 0.0

        for r in meeting_rows:
            field = str(r[3] or "").strip()
            val_str = r[4]
            if not field or val_str is None:
                continue

            try:
                val = float(str(val_str).strip())
            except ValueError:
                continue

            field_lower = field.lower()
            if (
                "prob: cut" in field_lower
                or "prob: <" in field_lower
                or "probability of cut" in field_lower
            ):
                direct_cut = val
            elif (
                "prob: hike" in field_lower
                or "prob: >" in field_lower
                or "probability of hike" in field_lower
            ):
                direct_hike = val
            else:
                match_prob = re.search(r"(\d+)bps\s*-\s*(\d+)bps", field)
                if match_prob:
                    bucket_low = int(match_prob.group(1))
                    bucket_high = int(match_prob.group(2))
                    if (
                        bucket_low == current_range_low_bps
                        and bucket_high == current_range_high_bps
                    ):
                        maintain_bucket_val = val
                    elif bucket_high <= current_range_low_bps:
                        bucket_cut_sum += val
                    elif bucket_low >= current_range_high_bps:
                        bucket_hike_sum += val

        # 優先使用直接匯總欄位；若無則使用離散區間累加，嚴格杜絕重複累加
        prob_cut = direct_cut if direct_cut is not None else bucket_cut_sum
        prob_hike = direct_hike if direct_hike is not None else bucket_hike_sum

        if maintain_bucket_val is not None:
            prob_maintain = maintain_bucket_val
        elif prob_cut > 0 or prob_hike > 0:
            prob_maintain = max(0.0, round(100.0 - prob_cut - prob_hike, 2))
        else:
            prob_maintain = 50.0
            prob_cut = 50.0
            prob_hike = 0.0

        total_p = prob_cut + prob_hike + prob_maintain
        if total_p > 0 and abs(total_p - 100.0) > 0.01:
            prob_cut = round((prob_cut / total_p) * 100.0, 1)
            prob_hike = round((prob_hike / total_p) * 100.0, 1)
            prob_maintain = round(max(0.0, 100.0 - prob_cut - prob_hike), 1)

        # 精確分類與宏觀緊縮純量計算
        if prob_hike >= 50.0 or (
            prob_hike >= 30.0 and prob_hike > prob_cut and prob_hike > prob_maintain
        ):
            decision = "hike"
            prob = round(min(0.95, (50.0 + prob_hike / 2.0) / 100.0), 4)
        elif prob_cut >= 50.0 or (
            prob_cut >= 30.0 and prob_cut > prob_hike and prob_cut > prob_maintain
        ):
            decision = "cut"
            prob = round(max(0.05, (50.0 - prob_cut / 2.0) / 100.0), 4)
        elif prob_maintain >= 50.0:
            decision = "maintain"
            prob = round(
                max(0.05, min(0.95, (50.0 + (prob_hike - prob_cut) / 2.0) / 100.0)),
                4,
            )
        else:
            decision = "split"
            prob = round(
                max(0.05, min(0.95, (50.0 + (prob_hike - prob_cut) / 2.0) / 100.0)),
                4,
            )

        return {
            "probability": prob,
            "meeting_date": next_meeting.strftime("%m/%d"),
            "current_target": current_target,
            "prob_maintain": round(prob_maintain, 1),
            "prob_hike": round(prob_hike, 1),
            "prob_cut": round(prob_cut, 1),
            "decision": decision,
            "source": "Atlanta Fed Market Probability Tracker (MPT)",
        }

    # 1. Primary: CBOT 30-Day Fed Funds Futures (ZQ) 即時階梯算式
    try:
        zq_data = await asyncio.to_thread(_fetch_and_calculate_zq_futures)
        return {
            "status": "success",
            "data": zq_data,
        }
    except Exception as e:
        logger.warning(
            f"Primary CBOT ZQ Fed Funds Futures calc failed: {e}, falling back to Atlanta Fed MPT."
        )

    # 2. Secondary: Atlanta Fed MPT Excel 解析
    try:
        parsed_data = await asyncio.to_thread(_fetch_and_parse_excel)
        return {
            "status": "success",
            "data": parsed_data,
        }
    except Exception as e:
        logger.warning(
            f"Secondary Atlanta FedWatch parse failed with exception: {e}, using static fallbacks."
        )
        return {"status": "success", "data": fallback}


@router.get("/api/v1/scrape/options/{symbol}/gex")
async def scrape_symbol_gex(symbol: str) -> dict[str, Any]:
    """即時抓取單一標的 GEX(每次請求各自啟動一顆短命 browser)。
    實際抓取/計算邏輯已抽至 gex_scraper.scrape_symbol_gex_core，供本端點與
    背景排程 (scheduler.py) 共用。"""
    from gex_scraper import FALLBACK_GEX

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            try:
                data = await scrape_symbol_gex_core(symbol, browser)
                return {"status": "success", "data": data}
            finally:
                await browser.close()
    except Exception as e:
        logger.warning(f"[{symbol}] Playwright GEX scrape failed: {e}, using fallback.")
        return {"status": "success", "data": dict(FALLBACK_GEX)}
