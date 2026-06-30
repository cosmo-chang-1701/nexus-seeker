import httpx
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)


async def get_reddit_context(
    symbol: str, limit: int = 5, *, enable_tunnel: bool = True
) -> Optional[str]:
    """透過 Cloudflare Tunnel 呼叫本地端爬取 Reddit。

    防禦性設計（Defense-in-Depth）：
    1. 若呼叫端明確傳入 ``enable_tunnel=False``，立即中斷並回傳 ``None``。
    2. 即使呼叫端未傳入（使用預設值 ``True``），本函式仍會自行查詢資料庫
       ``any_user_local_tunnel_enabled()``，確認是否有任何使用者啟用了本地
       Tunnel 開關。若全域均為關閉，同樣中斷，避免誤觸 530 網路穿透錯誤。
    3. 若 ``TUNNEL_URL`` 未配置，降級回傳提示字串。

    Returns:
        Reddit 情緒摘要文字，或 ``None`` 表示已跳過呼叫。
    """

    # ── Gate 1: 呼叫端明確關閉 ──────────────────────────────────
    if not enable_tunnel:
        logger.info(
            f"⏭️ [{symbol}] 根據用戶 settings 設定，已跳過本地 Tunnel (Reddit Scraper) 呼叫。"
        )
        return None

    # ── Gate 2: 資料庫全域開關防禦（即使呼叫端未傳 enable_tunnel）───
    try:
        from database.user_settings import any_user_local_tunnel_enabled

        if not any_user_local_tunnel_enabled():
            logger.info(
                f"⏭️ [{symbol}] 根據用戶 settings 設定，已跳過本地 Tunnel (Reddit Scraper) 呼叫。"
            )
            return None
    except Exception as e:
        # 若 DB 查詢失敗，保守降級：不發送外部請求
        logger.warning(
            f"[{symbol}] 無法查詢 Tunnel 開關狀態 ({e})，保守跳過 Reddit 呼叫。"
        )
        return None

    # ── Gate 3: TUNNEL_URL 配置檢查 ─────────────────────────────
    if not getattr(config, "TUNNEL_URL", ""):
        return "尚未配置本地 Tunnel URL，暫不抓取 Reddit 情緒。"

    try:
        logger.info(
            f"[{symbol}] 啟動邊緣運算呼叫，透過 Tunnel 要求本地端爬取 Reddit..."
        )

        # 設定 25 秒超時，給予本地端足夠的渲染時間
        async with httpx.AsyncClient(timeout=25.0) as client:
            res = await client.get(
                f"{config.TUNNEL_URL}/api/v1/scrape/reddit/{symbol}?limit={limit}"
            )
            res.raise_for_status()

            # 解析本地端回傳的 JSON
            response_json = res.json()
            if response_json.get("status") == "success":
                logger.info(f"[{symbol}] 成功從本地端取得 Reddit 資料！")
                return response_json.get("data")
            else:
                logger.warning(
                    f"[{symbol}] 本地端爬取發生內部錯誤: {response_json.get('data')}"
                )
                return "本地備援節點發生錯誤，暫無情緒資料。"

    except httpx.ReadTimeout:
        logger.error(f"[{symbol}] Tunnel 請求超時，本地端無回應。")
        return "本地節點連線超時。"
    except Exception as e:
        logger.error(f"[{symbol}] 呼叫本地 Tunnel 失敗: {e}")
        return "邊緣運算節點連線異常。"
