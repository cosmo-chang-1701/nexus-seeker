import logging
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

async def get_reddit_context(symbol: str, limit: int = 5) -> str:
    """
    使用 Playwright 無頭瀏覽器訪問 old.reddit.com
    獲取絕對即時 (Real-time) 的散戶情緒數據。
    """
    # 移除 $ 符號，準備用於 URL
    symbol_clean = symbol.replace("$", "")
    
    # 鎖定這三個板塊
    subreddits = "wallstreetbets+stocks+options"
    
    # 建構 old.reddit 的搜尋 URL
    # sort=top, t=day: 鎖定過去 24 小時最高分
    url = f"https://old.reddit.com/r/{subreddits}/search?q={symbol_clean}&restrict_sr=on&sort=hot"

    logger.info(f"[{symbol}] 啟動無頭瀏覽器抓取: {url}")

    async with async_playwright() as p:
        # 啟動 Chromium 瀏覽器 (headless=True 表示在背景執行，不跳出視窗)
        # args=['--no-sandbox'] 是在 Docker 環境中運行的必要參數
        browser = await p.chromium.launch(
            headless=True, 
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        
        try:
            # 禁用 JavaScript，防護腳本與廣告完全失效
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                java_script_enabled=False
            )
            # 網路請求攔截，封殺所有圖片、CSS 與字型
            await context.route(
                "**/*", 
                lambda route: route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "media", "script"] else route.continue_()
            )
            page = await context.new_page()

            # 設定導航超時為 20 秒
            page.set_default_timeout(20000)

            # 前往目標頁面
            await page.goto(url, wait_until="domcontentloaded")

            # ⚠️ 關鍵戰術：等待搜尋結果的容器出現
            # 這確保了頁面已經載入完成，不是空白頁
            # old.reddit 的搜尋結果都包在 div.search-result-link 裡面
            try:
                # 等待第一個結果出現，最多等 5 秒。如果沒出現代表可能沒資料。
                await page.wait_for_selector("div.search-result-link", timeout=5000)
            except PlaywrightTimeoutError:
                logger.warning(f"[{symbol}] 頁面載入完成，但在超時內未發現搜尋結果元素。可能無資料。")
                return "Reddit 目前無相關即時討論。"

            # 取得渲染完成的 HTML 原碼
            html_content = await page.content()
            
            # --- 進入 BeautifulSoup 解析階段 ---
            soup = BeautifulSoup(html_content, "lxml")
            
            # 找到所有搜尋結果區塊
            results = soup.select("div.search-result-link")[:limit]

            posts_text = ""
            for res in results:
                # 解析標題
                title_tag = res.select_one("a.search-title")
                title = title_tag.text.strip() if title_tag else "N/A"
                
                # 解析看板名稱
                sub_tag = res.select_one("a.search-subreddit-link")
                # 移除原本文字中的 "r/" 前綴
                sub = sub_tag.text.strip().replace("r/", "") if sub_tag else "unknown"

                # 解析分數 (共識分數)
                score_tag = res.select_one("span.search-score")
                score_text = score_tag.text.strip() if score_tag else "0"
                # 處理分數顯示為 "•" (隱藏) 或 "hide" 的情況，或是帶有 "points" 文字
                score = "".join(filter(str.isdigit, score_text))
                score = int(score) if score else 0
                
                posts_text += f"[{sub} | 共識分數:{score}] {title}\n"
            
            logger.info(f"[{symbol}] 成功抓取 {len(results)} 筆即時資料。")
            return posts_text if posts_text else "Reddit 目前無相關即時討論。"

        except PlaywrightTimeoutError:
            logger.error(f"[{symbol}] 瀏覽器導航或元素等待超時。")
            return "Reddit 連線超時，暫無即時資料。"
        except Exception as e:
            logger.error(f"[{symbol}] Playwright 抓取發生未預期錯誤: {e}")
            return "無法獲取 Reddit 即時情緒。"
        finally:
            # 無論成功失敗，務必關閉瀏覽器釋放記憶體資源
            await browser.close()