import httpx
import logging
from typing import Optional, Dict, Any

import config

logger = logging.getLogger(__name__)


async def get_fundamental_context(
    symbol: str, enable_tunnel: bool = True, accession_number: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """透過 Cloudflare Tunnel 呼叫本地端爬取 10-K/10-Q/8-K 基本面文本。

    Returns:
        若成功則回傳包含 'text', 'source_url', 'form_type', 'sections' 的字典
        (form_type/sections 可能為空字串/空字典，尤其是讀取舊版快取時)；
        若失敗或關閉 Tunnel 則回傳 None 或包含錯誤訊息的字典 (例如 {'error': '...'})。
    """

    if not enable_tunnel:
        logger.info(
            f"⏭️ [{symbol}] 呼叫端明確跳過本地 Tunnel (Fundamental Scraper) 呼叫。"
        )
        return None

    # ======= 檢查快取 =======
    from database.cache import get_kv_cache, save_kv_cache

    cache_key = (
        f"fundamental_report_{symbol.upper()}_{accession_number}"
        if accession_number
        else f"fundamental_report_{symbol.upper()}"
    )
    cached_data = get_kv_cache(cache_key)

    if not getattr(config, "TUNNEL_URL", ""):
        return {"error": "尚未配置本地 Tunnel URL，無法抓取基本面財報。"}

    tunnel_url = config.TUNNEL_URL

    # 若有快取，進行輕量級心跳驗證 (Strategy 2)
    if cached_data and isinstance(cached_data, dict):
        cached_accession = cached_data.get("accession_number")
        if cached_accession:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    meta_res = await client.get(
                        f"{tunnel_url}/api/v1/scrape/fundamental/{symbol}/metadata"
                    )
                    if meta_res.status_code == 200:
                        meta_json = meta_res.json()
                        if meta_json.get("status") == "success":
                            live_accession = meta_json.get("data", {}).get(
                                "accession_number"
                            )
                            if live_accession == cached_accession:
                                logger.info(
                                    f"[{symbol}] 輕量級心跳驗證通過，直接讀取本地快取 ({live_accession})"
                                )
                                # .get() 帶預設值：舊版快取資料可能沒有 form_type/sections 這兩個 key
                                return {
                                    "text": cached_data.get("text", ""),
                                    "source_url": cached_data.get("source_url", ""),
                                    "form_type": cached_data.get("form_type", ""),
                                    "sections": cached_data.get("sections", {}),
                                }
                            else:
                                logger.info(
                                    f"[{symbol}] 偵測到新財報 ({live_accession} != {cached_accession})，準備重新下載。"
                                )
                        else:
                            logger.info(
                                f"[{symbol}] 輕量級心跳驗證失敗 (Edge API 錯誤)，將重新下載全文本。"
                            )
            except Exception as e:
                logger.warning(
                    f"[{symbol}] 輕量級心跳驗證連線失敗 ({e})，安全起見，直接使用現有快取。"
                )
                return {
                    "text": cached_data.get("text", ""),
                    "source_url": cached_data.get("source_url", ""),
                    "form_type": cached_data.get("form_type", ""),
                    "sections": cached_data.get("sections", {}),
                }
    # ========================

    try:
        logger.info(f"[{symbol}] 啟動邊緣運算呼叫，透過 Tunnel 抓取基本面財報全文...")
        async with httpx.AsyncClient(timeout=30.0) as client:
            req_url = f"{tunnel_url}/api/v1/scrape/fundamental/{symbol}"
            if accession_number:
                req_url += f"?accession_number={accession_number}"
            res = await client.get(req_url)
            res.raise_for_status()

            response_json = res.json()
            if response_json.get("status") == "success":
                data = response_json.get("data", {})
                result_data = {
                    "text": data.get("text", "無財報文字"),
                    "source_url": data.get("source_url", ""),
                    "form_type": data.get("form_type", ""),
                    "sections": data.get("sections", {}),
                }

                cache_payload = {
                    **result_data,
                    "accession_number": data.get("accession_number", ""),
                }

                # 非同步寫入快取 (save_kv_cache)
                await save_kv_cache(cache_key, cache_payload)

                return result_data
            else:
                err_msg = response_json.get("data")
                logger.warning(f"[{symbol}] 本地端爬取財報發生錯誤: {err_msg}")
                return {"error": f"本地備援節點發生錯誤，暫無財報資料: {err_msg}"}

    except httpx.ReadTimeout:
        logger.error(f"[{symbol}] Tunnel 財報請求超時。")
        return {"error": "本地節點財報連線超時。"}
    except Exception as e:
        logger.error(f"[{symbol}] 呼叫本地 Tunnel 財報失敗: {e}")
        return {"error": "邊緣運算節點連線異常。"}


async def get_fundamental_reports_list(
    symbol: str, enable_tunnel: bool = True
) -> Optional[list[Dict[str, Any]]]:
    """獲取近期財報清單"""
    if not enable_tunnel:
        return None

    if not getattr(config, "TUNNEL_URL", ""):
        return None

    tunnel_url = config.TUNNEL_URL
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(
                f"{tunnel_url}/api/v1/scrape/fundamental/{symbol}/list"
            )
            res.raise_for_status()

            response_json = res.json()
            if response_json.get("status") == "success":
                return response_json.get("data", [])  # type: ignore
            else:
                return None
    except Exception as e:
        logger.error(f"[{symbol}] 取得財報清單失敗: {e}")
        return None
