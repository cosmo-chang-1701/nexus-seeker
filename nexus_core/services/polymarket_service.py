import asyncio
import json
import logging
import websockets
import httpx
from typing import Dict, Any, List

import database
from database.user_settings import get_full_user_context, get_all_user_ids
from services.llm_service import generate_polymarket_summary

logger = logging.getLogger(__name__)

POLY_WS_URL = "wss://clob.polymarket.com/ws/market"
POLY_API_BASE = "https://clob.polymarket.com"

class PolymarketService:
    def __init__(self, bot):
        self.bot = bot
        self.running = False
        self._market_cache = {}
        self._monitor_task = None

    def start(self):
        if self.running:
            return
        self.running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("🐋 Polymarket Whale Monitor Service started.")

    def stop(self):
        self.running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        logger.info("🛑 Polymarket Whale Monitor Service stopped.")

    async def _monitor_loop(self):
        while self.running:
            try:
                # 1. 獲取所有活躍市場以取得 asset_id 列表
                asset_ids = await self._fetch_all_active_asset_ids()
                if not asset_ids:
                    logger.warning("No active asset IDs found. Retrying in 30s...")
                    await asyncio.sleep(30)
                    continue

                async with websockets.connect(POLY_WS_URL) as ws:
                    # Subscribe to trades for all active assets
                    sub_msg = {
                        "type": "subscribe",
                        "assets_ids": asset_ids,
                        "channels": ["trades"]
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info(f"Successfully subscribed to Polymarket 'trades' channel for {len(asset_ids)} assets.")

                    async for message in ws:
                        if not self.running:
                            break
                        try:
                            data = json.loads(message)
                            if isinstance(data, list):
                                for item in data:
                                    if item.get("event_type") == "trade":
                                        await self._handle_trade(item)
                            elif data.get("event_type") == "trade":
                                await self._handle_trade(data)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            logger.error(f"Error processing Polymarket WS message: {e}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("Polymarket WS connection closed. Reconnecting...")
            except Exception as e:
                logger.error(f"Polymarket WS monitor loop encountered error: {e}")
            
            if self.running:
                await asyncio.sleep(10)  # Wait before reconnecting

    async def _fetch_all_active_asset_ids(self) -> List[str]:
        """
        透過 API 獲取所有活躍市場的資產 ID
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 獲取所有活躍市場
                resp = await client.get(f"{POLY_API_BASE}/markets")
                if resp.status_code == 200:
                    markets = resp.json()
                    asset_ids = []
                    for m in markets:
                        if "tokens" in m:
                            for token in m["tokens"]:
                                if "token_id" in token:
                                    asset_ids.append(token["token_id"])
                    return asset_ids
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
