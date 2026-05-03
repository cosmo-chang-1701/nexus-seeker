import asyncio
import json
import logging
import datetime
import websockets
import httpx
from typing import Dict, Any, List

import database
from database.user_settings import get_full_user_context, get_all_user_ids
from services.llm_service import generate_polymarket_summary

logger = logging.getLogger(__name__)

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLY_API_BASE = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class PolymarketService:
    def __init__(self, bot):
        self.bot = bot
        self.running = False
        self._market_cache = {}
        self._active_markets = []  # 儲存目前活躍市場的詳細資訊
        self._monitor_task = None
        self._ping_task = None
        
        # 狀態追蹤
        self.last_message_at = None
        self.asset_count = 0
        self.error_count = 0
        self.is_connected = False

    def get_status(self) -> Dict[str, Any]:
        """獲獲目前服務狀態摘要"""
        if self.last_message_at:
            ts = int(self.last_message_at.timestamp())
            last_msg = f"<t:{ts}:F>"
        else:
            last_msg = "從未收到"

        return {
            "running": self.running,
            "connected": self.is_connected,
            "asset_count": self.asset_count,
            "last_message": last_msg,
            "errors": self.error_count
        }

    def get_active_markets(self, limit: int = 20) -> List[Dict[str, Any]]:
        """獲取目前監控中的活躍市場清單"""
        return self._active_markets[:limit]

    def start(self):
        if self.running:
            return
        self.running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("🐋 Polymarket Whale Monitor Service started.")

    def stop(self):
        self.running = False
        self.is_connected = False
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._ping_task:
            self._ping_task.cancel()
        logger.info("🛑 Polymarket Whale Monitor Service stopped.")

    async def _monitor_loop(self):
        while self.running:
            try:
                # 1. 獲取所有活躍市場以取得 asset_id 列表
                asset_ids = await self._fetch_all_active_asset_ids()
                if not asset_ids:
                    logger.warning("No active asset IDs found. Retrying in 30s...")
                    self.error_count += 1
                    await asyncio.sleep(30)
                    continue

                self.asset_count = len(asset_ids)

                # 修正：websockets.connect 不直接支援 timeout 參數，使用 asyncio.wait_for
                try:
                    ws = await asyncio.wait_for(websockets.connect(POLY_WS_URL), timeout=10)
                except asyncio.TimeoutError:
                    logger.error("Polymarket WS connection timed out.")
                    self.error_count += 1
                    await asyncio.sleep(10)
                    continue

                async with ws:
                    self.is_connected = True
                    # Subscribe to trades (MARKET channel)
                    sub_msg = {
                        "type": "market",
                        "assets_ids": asset_ids,
                        "custom_feature_enabled": True  # 文件建議啟用以獲得更完整的資訊
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info(f"Successfully subscribed to Polymarket 'market' channel for {len(asset_ids)} assets.")

                    # 啟動 PING 任務
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))

                    async for message in ws:
                        if not self.running:
                            break
                        
                        # 記錄最後收到訊息的時間 (UTC)
                        self.last_message_at = datetime.datetime.now(datetime.timezone.utc)
                        
                        try:
                            data = json.loads(message)
                            
                            # 處理多種格式的訊息 (嚴格限制在真正的成交事件)
                            # 1. 直接是交易物件 (由 event_type 識別)
                            if isinstance(data, dict) and data.get("event_type") in ["trade", "last_trade_price"]:
                                await self._handle_trade(data)
                            
                            # 2. 陣列格式 (處理批次訊息)
                            elif isinstance(data, list):
                                for item in data:
                                    if item.get("event_type") in ["trade", "last_trade_price"]:
                                        await self._handle_trade(item)
                            
                            # 3. 錯誤訊息
                            elif isinstance(data, dict) and data.get("type") == "error":
                                self.error_count += 1
                                logger.error(f"Polymarket WS error message: {data.get('message')}")
                                
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            self.error_count += 1
                            logger.error(f"Error processing Polymarket WS message: {e}")

            except websockets.exceptions.ConnectionClosed:
                self.is_connected = False
                logger.warning("Polymarket WS connection closed. Reconnecting...")
            except Exception as e:
                self.is_connected = False
                self.error_count += 1
                logger.error(f"Polymarket WS monitor loop encountered error: {e}")
            finally:
                if self._ping_task:
                    self._ping_task.cancel()
            
            if self.running:
                await asyncio.sleep(10)  # Wait before reconnecting

    async def _ping_loop(self, ws):
        """保持 WebSocket 連線的心跳 (Polymarket 要求每 10 秒發送 PING)"""
        try:
            while self.running and self.is_connected:
                await ws.send("PING")
                await asyncio.sleep(10)
        except Exception:
            pass

    async def _fetch_all_active_asset_ids(self) -> List[str]:
        """
        透過 Gamma API 獲取目前的活躍市場資產 ID，並預熱快取。
        """
        asset_ids = []
        active_markets_data = []
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # 抓取 Gamma API 的活躍市場 (active=true, closed=false)
                # 這比 CLOB API 的過濾更準確
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": 100
                }
                resp = await client.get(f"{GAMMA_API_BASE}/markets", params=params)
                
                if resp.status_code == 200:
                    markets = resp.json()
                    for m in markets:
                        q = m.get("question")
                        # 獲取 CLOB Token IDs
                        clob_tokens_raw = m.get("clobTokenIds")
                        if not clob_tokens_raw:
                            continue
                            
                        try:
                            # clobTokenIds 有時是字串化的 JSON 陣列
                            if isinstance(clob_tokens_raw, str):
                                t_ids = json.loads(clob_tokens_raw)
                            else:
                                t_ids = clob_tokens_raw
                            
                            if not t_ids:
                                continue
                                
                            # 獲取 Outcome 列表 (例如 ["Yes", "No"]) 與價格
                            outcomes = m.get("outcomes", [])
                            outcome_prices = m.get("outcomePrices", [])
                            
                            current_market_tokens = []
                            for i, tid in enumerate(t_ids):
                                asset_ids.append(tid)
                                
                                # 嘗試獲取 Event Slug (用於正確的 URL)
                                event_slug = None
                                if "events" in m and m["events"] and isinstance(m["events"], list):
                                    event_slug = m["events"][0].get("slug")
                                elif "event" in m and m["event"]:
                                    event_slug = m["event"].get("slug")

                                # 獲取該 Token 對應的 Outcome
                                outcome_name = outcomes[i] if i < len(outcomes) else "未知選項"
                                # 獲取該 Token 對應的價格
                                current_price = outcome_prices[i] if i < len(outcome_prices) else 0

                                # 預熱快取：將 asset_id 對應到市場資訊
                                self._market_cache[tid] = {
                                    "question": q,
                                    "description": m.get("description"),
                                    "end_date": m.get("endDate"),
                                    "condition_id": m.get("conditionId"),
                                    "slug": m.get("slug"),
                                    "event_slug": event_slug,
                                    "outcome": outcome_name
                                }
                                current_market_tokens.append({
                                    "token_id": tid,
                                    "outcome": outcome_name,
                                    "price": current_price
                                })
                            
                            active_markets_data.append({
                                "question": q,
                                "description": m.get("description"),
                                "end_date": m.get("endDate"),
                                "tokens": current_market_tokens
                            })
                            
                            # 同時也用 conditionId 快取
                            cond_id = m.get("conditionId")
                            if cond_id:
                                self._market_cache[cond_id] = self._market_cache[t_ids[0]]
                                
                        except Exception as e:
                            logger.error(f"Error parsing tokens for market '{q}': {e}")

            # 儲存活躍市場資訊供 UI 顯示
            self._active_markets = active_markets_data
            # 確保不重複
            return list(set(asset_ids))
        except Exception as e:
            logger.error(f"Failed to fetch active asset IDs via Gamma API: {e}")
        return []

    async def _handle_trade(self, trade: Dict[str, Any]):
        """
        處理單筆交易：過濾門檻 -> 獲取背景 -> LLM 總結 -> 推播
        """
        try:
            asset_id = trade.get("asset_id")
            price = float(trade.get("price", 0))
            size = float(trade.get("size", 0))
            usd_value = price * size
            
            if usd_value <= 0:
                return

            # 1. 篩選符合門檻的使用者
            user_ids = get_all_user_ids()
            target_users = []
            for uid in user_ids:
                context = get_full_user_context(uid)
                # 門檻為 0 代表關閉
                if context.polymarket_threshold > 0 and usd_value >= context.polymarket_threshold:
                    target_users.append(uid)
            
            if not target_users:
                return

            logger.info(f"🐋 Whale trade detected: {trade.get('side')} ${usd_value:,.2f} on asset {asset_id}")

            # 2. 獲取市場背景資訊
            market_info = await self._get_market_info(asset_id, trade.get("condition_id"))
            if not market_info:
                market_info = {"question": "未知市場", "description": "無法獲取市場詳細資訊"}

            # 3. 推播通知 (針對每個使用者個別處理)
            summary_cache = None
            
            for uid in target_users:
                context = get_full_user_context(uid)
                
                current_summary = "（未啟用 AI 分析）"
                if context.polymarket_use_llm:
                    if summary_cache is None:
                        summary_cache = await generate_polymarket_summary(market_info, trade, usd_value)
                    current_summary = summary_cache
                
                await self._push_notification(uid, current_summary, market_info, trade, usd_value)

        except Exception as e:
            logger.error(f"Failed to handle Polymarket trade: {e}")

    async def _get_market_info(self, asset_id: str, condition_id: str = None) -> Dict[str, Any]:
        """
        透過快取或 API 獲取市場元數據。
        """
        # 1. 優先從快取獲取
        if asset_id in self._market_cache:
            return self._market_cache[asset_id]
        if condition_id and condition_id in self._market_cache:
            return self._market_cache[condition_id]

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 如果有 condition_id，直接查 Gamma API
                if condition_id:
                    resp = await client.get(f"{GAMMA_API_BASE}/markets", params={"condition_id": condition_id})
                    if resp.status_code == 200:
                        data = resp.json()
                        market = data[0] if isinstance(data, list) and data else data
                        if market:
                            self._market_cache[condition_id] = market
                            return market

                # 如果只有 asset_id (token_id)，嘗試透過 Gamma API 搜尋
                # 注意：Gamma API 可能不直接支援以 token_id 查詢單一市場，
                # 但我們可以在 _fetch 時盡可能抓取更多
                pass

        except Exception as e:
            logger.error(f"Error fetching market info for {asset_id}/{condition_id}: {e}")
        
        return None

    async def _push_notification(self, user_id: int, summary: str, market_info: Dict[str, Any], trade: Dict[str, Any], usd_value: float):
        """
        封裝 Discord Embed 並排入私訊佇列
        """
        import discord
        import json
        
        # 取得原始數據
        side_raw = trade.get("side", "BUY")
        asset_id = trade.get("asset_id")
        base_price = float(trade.get("price", 0))
        
        # 1. 嘗試解析該 Token 的正確選項名稱 (Outcome)
        base_outcome = market_info.get("outcome")
        if not base_outcome:
            # 如果是從 API 直接抓取的 Market 對象，需手動對應 Token ID 與 Outcomes 列表
            try:
                clob_tokens = market_info.get("clobTokenIds", [])
                if isinstance(clob_tokens, str):
                    clob_tokens = json.loads(clob_tokens)
                
                if asset_id in clob_tokens:
                    idx = clob_tokens.index(asset_id)
                    outcomes_list = market_info.get("outcomes", [])
                    if idx < len(outcomes_list):
                        base_outcome = outcomes_list[idx]
            except Exception as e:
                logger.debug(f"解析動態市場選項失敗: {e}")
        
        # 終極退路
        if not base_outcome or str(base_outcome).strip() == "":
            base_outcome = "Yes"

        # 2. 邏輯轉換：確保顯示的價格與「買入」的方向一致
        action_text = "買入"
        final_outcome = ""
        final_price = base_price
        
        # 處理標準二元市場 (Yes/No)
        if str(base_outcome).lower() == "yes":
            if side_raw == "BUY":
                final_outcome = "Yes"
                final_price = base_price
            else:
                final_outcome = "No"
                final_price = 1 - base_price
        elif str(base_outcome).lower() == "no":
            if side_raw == "BUY":
                final_outcome = "No"
                final_price = base_price
            else:
                final_outcome = "Yes"
                final_price = 1 - base_price
        # 處理命名市場
        else:
            if side_raw == "BUY":
                final_outcome = base_outcome
                final_price = base_price
            else:
                # 買入「非該選項」
                final_outcome = f"非 {base_outcome}"
                final_price = 1 - base_price

        # 再次檢查確保 final_outcome 不為空 (防止 "非 " 狀況)
        if not final_outcome or str(final_outcome).strip() in ["", "非"]:
            final_outcome = "No" if side_raw == "SELL" else "Yes"

        side_emoji = "🟢" if "yes" in str(final_outcome).lower() else "🔴"
        # 針對「非 XXX」的狀況，如果是看淡 Yes 則用紅色
        if "非" in str(final_outcome) and str(base_outcome).lower() == "yes":
            side_emoji = "🔴"
            
        direction_text = f"{action_text} {final_outcome}"
        
        embed = discord.Embed(
            title=f"🐋 Polymarket 巨鯨交易偵測 ({direction_text})",
            description=summary,
            color=discord.Color.blue() if "yes" in str(final_outcome).lower() else discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        
        embed.add_field(name="🎯 市場問題", value=market_info.get("question", "未知"), inline=False)
        
        # 建立交易連結
        event_slug = market_info.get("event_slug")
        market_slug = market_info.get("slug")
        
        if event_slug:
            market_url = f"https://polymarket.com/event/{event_slug}"
            embed.add_field(name="🔗 市場連結", value=f"[點擊前往 Polymarket (活動頁)]({market_url})", inline=False)
        elif market_slug:
            market_url = f"https://polymarket.com/market/{market_slug}"
            embed.add_field(name="🔗 市場連結", value=f"[點擊前往 Polymarket (市場頁)]({market_url})", inline=False)
        
        embed.add_field(name="💰 成交金額", value=f"`${usd_value:,.2f}`", inline=True)
        embed.add_field(name="📊 成交價格", value=f"`{final_price:.3f}`", inline=True)
        embed.add_field(name="🎲 押注方向", value=f"{side_emoji} {direction_text}", inline=True)
        
        embed.set_footer(text="Nexus Seeker 巨鯨監測系統 | Powered by Polymarket CLOB")
        
        # 使用 bot 的佇列發送私訊
        await self.bot.queue_dm(user_id, embed=embed)
