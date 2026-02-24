from fastapi import FastAPI, Query
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
import logging

app = FastAPI()
logger = logging.getLogger(__name__)

@app.get("/scrape/reddit/{symbol}")
async def scrape_reddit(symbol: str, limit: int = Query(5, description="回傳的貼文數量上限")):
    symbol_clean = symbol.replace("$", "")
    url = (
        f"https://old.reddit.com/r/wallstreetbets+stocks+options/search"
        f"?q=%22{symbol_clean}%22"
        f"&restrict_sr=on"
        f"&sort=new"
        f"&t=day"
    )
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                java_script_enabled=False
            )
            
            await context.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "script"] else route.continue_())
            
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