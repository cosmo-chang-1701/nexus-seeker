import httpx
import logging
import config

logger = logging.getLogger(__name__)


async def get_reddit_context(
    symbol: str, limit: int = 5, *, enable_tunnel: bool = True
) -> str:
    """透過 Cloudflare Tunnel 呼叫本地端爬取 Reddit。

    注意：若 enable_tunnel=False，必須直接跳過呼叫（避免任何本地 Tunnel I/O）。
    """
    if not enable_tunnel:
        return "本地 Tunnel 已關閉（可於 /settings 開啟），暫不抓取 Reddit 情緒。"
    if not getattr(config, "TUNNEL_URL", ""):
        return "尚未配置本地 Tunnel URL，暫不抓取 Reddit 情緒。"

    try:
        logger.info(
            f"[{symbol}] 啟動邊緣運算呼叫，透過 Tunnel 要求本地端爬取 Reddit..."
        )

        # 設定 25 秒超時，給予本地端足夠的渲染時間
        async with httpx.AsyncClient(timeout=25.0) as client:
            res = await client.get(
                f"{config.TUNNEL_URL}/scrape/reddit/{symbol}?limit={limit}"
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
