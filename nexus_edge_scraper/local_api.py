from fastapi import FastAPI, Query
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import logging

app = FastAPI()
logger = logging.getLogger(__name__)

@app.get("/scrape/reddit/{symbol}")
async def scrape_reddit(symbol: str, limit: int = Query(5, description="回傳的貼文數量上限")):
    symbol_clean = symbol.replace("$", "")
    url = f"https://old.reddit.com/r/wallstreetbets+stocks+options/search?q=title%3A{symbol_clean}&restrict_sr=on&sort=hot"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                java_script_enabled=False
            )
            # 實作網路請求攔截以優化 DOM 渲染效能
            await context.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "script"] else route.continue_())
            
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_selector("div.search-result-link", timeout=5000)
            
            html_content = await page.content()
            soup = BeautifulSoup(html_content, "lxml")
            
            # 套用 limit 參數進行資料截斷
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
            
            return {"status": "success", "data": posts_text if posts_text else "無相關討論。"}
            
        except Exception as e:
            logger.error(f"Playwright 執行例外: {e}")
            return {"status": "error", "data": str(e)}
        finally:
            await browser.close()