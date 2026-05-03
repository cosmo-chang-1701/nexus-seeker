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
                        "assets_ids": asset_ids
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
                            # 處理訊息
                            if isinstance(data, list):
                                for item in data:
                                    if item.get("event_type") in ["trade", "last_trade_price"]:
                                        await self._handle_trade(item)
                            elif data.get("event_type") in ["trade", "last_trade_price"]:
                                await self._handle_trade(data)
                            elif data.get("type") == "error":
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
        """保持 WebSocket 連線的心跳"""
        try:
            while self.running and self.is_connected:
                # 發送 PING 或是簡單的字串，視伺服器要求
                await ws.send("PING")
                await asyncio.sleep(20)
        except Exception:
            pass

    async def _fetch_all_active_asset_ids(self) -> List[str]:
        """
        透過 API 獲取所有活躍市場的資產 ID，並儲存市場資訊
        """
        asset_ids = []
        active_markets_data = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # 嘗試多抓幾頁或增加 limit 以確保抓到活躍市場
                url = f"{POLY_API_BASE}/markets?active=true&closed=false&limit=500"
                resp = await client.get(url)
                
                if resp.status_code == 200:
                    raw_data = resp.json()
                    markets = raw_data.get("data", []) if isinstance(raw_data, dict) else raw_data
                    
                    for m in markets:
                        # 再次驗證狀態
                        if m.get("active") and not m.get("closed"):
                            # 只有具有 token_id 的 CLOB 市場才能被 WebSocket 監控
                            current_market_tokens = []
                            has_clob_tokens = False
                            
                            if "tokens" in m:
                                for token in m["tokens"]:
                                    if token.get("token_id"):  # 確保 token_id 存在且非空字串
                                        asset_ids.append(token["token_id"])
                                        current_market_tokens.append(token)
                                        has_clob_tokens = True
                            
                            if has_clob_tokens:
                                active_markets_data.append({
                                    "question": m.get("question"),
                                    "description": m.get("description"),
                                    "end_date": m.get("end_date_iso"),
                                    "tokens": current_market_tokens
                                })
                
                # 如果還是沒抓到，嘗試不帶過濾條件抓取最新的市場
                if not asset_ids:
                    resp = await client.get(f"{POLY_API_BASE}/markets?limit=500")
                    if resp.status_code == 200:
                        raw_data = resp.json()
                        markets = raw_data.get("data", []) if isinstance(raw_data, dict) else raw_data
                        for m in markets:
                            if not m.get("closed"):
                                current_market_tokens = []
                                has_clob_tokens = False
                                for token in m.get("tokens", []):
                                    if token.get("token_id"):  # 確保 token_id 存在且非空字串
                                        asset_ids.append(token["token_id"])
                                        current_market_tokens.append(token)
                                        has_clob_tokens = True
                                
                                if has_clob_tokens:
                                    active_markets_data.append({
                                        "question": m.get("question"),
                                        "description": m.get("description"),
                                        "end_date": m.get("end_date_iso"),
                                        "tokens": current_market_tokens
                                    })

            # 儲存活躍市場資訊
            self._active_markets = active_markets_data
            # 確保不重複
            return list(set(asset_ids))
        except Exception as e:
            logger.error(f"Failed to fetch active asset IDs: {e}")
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
            market_info = await self._get_market_info(asset_id)
            if not market_info:
                market_info = {"question": "未知市場", "description": "無法獲取市場詳細資訊"}

            # 3. LLM 總結分析
            summary = await generate_polymarket_summary(market_info, trade, usd_value)

            # 4. 推播通知
            for uid in target_users:
                await self._push_notification(uid, summary, market_info, trade, usd_value)

        except Exception as e:
            logger.error(f"Failed to handle Polymarket trade: {e}")

    async def _get_market_info(self, asset_id: str) -> Dict[str, Any]:
        """
        透過 API 獲取市場元數據，並快取以減少請求
        """
        if asset_id in self._market_cache:
            return self._market_cache[asset_id]

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 獲取單一資產/市場的詳細資訊
                resp = await client.get(f"{POLY_API_BASE}/markets/{asset_id}")
                if resp.status_code == 200:
                    data = resp.json()
                    self._market_cache[asset_id] = data
                    return data
                else:
                    logger.warning(f"Failed to fetch market info for {asset_id}, status: {resp.status_code}")
        except Exception as e:
            logger.error(f"Error fetching market info for {asset_id}: {e}")
        
        return None

    async def _push_notification(self, user_id: int, summary: str, market_info: Dict[str, Any], trade: Dict[str, Any], usd_value: float):
        """
        封裝 Discord Embed 並排入私訊佇列
        """
        import discord
        
        side_emoji = "🟢" if trade.get("side") == "BUY" else "🔴"
        side_text = "買入 (BUY)" if trade.get("side") == "BUY" else "賣出 (SELL)"
        
        embed = discord.Embed(
            title=f"🐋 Polymarket 巨鯨交易偵測 ({side_text})",
            description=summary,
            color=discord.Color.blue() if trade.get("side") == "BUY" else discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        
        embed.add_field(name="🎯 市場問題", value=market_info.get("question", "未知"), inline=False)
        embed.add_field(name="💰 成交金額", value=f"`${usd_value:,.2f}`", inline=True)
        embed.add_field(name="📊 成交價格", value=f"`{trade.get('price')}`", inline=True)
        embed.add_field(name="🎲 押注方向", value=f"{side_emoji} {trade.get('side')}", inline=True)
        
        embed.set_footer(text="Nexus Seeker 巨鯨監測系統 | Powered by Polymarket CLOB")
        
        # 使用 bot 的佇列發送私訊
        await self.bot.queue_dm(user_id, embed=embed)
