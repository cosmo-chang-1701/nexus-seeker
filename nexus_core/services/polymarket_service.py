import asyncio
import json
import logging
import datetime
import websockets
import httpx
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

import database
from database.user_settings import get_full_user_context, get_all_user_ids
from services.llm_service import generate_polymarket_summary

logger = logging.getLogger(__name__)

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLY_API_BASE = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"


@dataclass
class OrderBook:
    token_id: str
    bids: Dict[float, float] = field(default_factory=dict)  # price -> size
    asks: Dict[float, float] = field(default_factory=dict)  # price -> size
    last_update_at: datetime.datetime = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    def update(self, side: str, price: float, size: float):
        target = self.bids if side.lower() in ["buy", "bid"] else self.asks
        if size <= 0:
            target.pop(price, None)
        else:
            target[price] = size
        self.last_update_at = datetime.datetime.now(datetime.timezone.utc)

    def get_mid_price(self) -> float:
        if not self.bids or not self.asks:
            return 0.5
        best_bid = max(self.bids.keys())
        best_ask = min(self.asks.keys())
        return (best_bid + best_ask) / 2

    def calculate_slippage_threshold(self, target_percent: float = 0.02) -> float:
        """
        計算在指定滑價百分比 (預設 2%) 下，所需的累積交易金額 (USD)。
        這代表了市場目前的流動性深度。
        """
        mid = self.get_mid_price()
        if mid <= 0:
            return 5000.0  # 安全回退值

        # 計算買入側 (推升價格) 的流動性
        target_price_buy = mid * (1 + target_percent)
        cumulative_usd_buy = 0.0
        for p in sorted(self.asks.keys()):
            if p > target_price_buy:
                break
            cumulative_usd_buy += p * self.asks[p]

        # 計算賣出側 (壓低價格) 的流動性
        target_price_sell = mid * (1 - target_percent)
        cumulative_usd_sell = 0.0
        for p in sorted(self.bids.keys(), reverse=True):
            if p < target_price_sell:
                break
            cumulative_usd_sell += p * self.bids[p]

        # 取兩側流動性較高者作為巨鯨門檻，或設定最低保底
        return max(cumulative_usd_buy, cumulative_usd_sell, 1000.0)


class PolymarketService:
    def __init__(self, bot):
        self.bot = bot
        self.running = False
        self._market_cache = {}
        self._active_markets = []  # 儲存目前活躍市場的詳細資訊
        self._order_books: Dict[str, OrderBook] = {}
        self._monitor_task = None
        self._ping_task = None
        
        # 狀態追蹤
        self.last_message_at = None
        self.asset_count = 0
        self.error_count = 0
        self.is_connected = False

    def get_status(self) -> Dict[str, Any]:
        """獲取目前服務狀態摘要"""
        if self.last_message_at:
            ts = int(self.last_message_at.timestamp())
            last_msg = f"<t:{ts}:F>"
        else:
            last_msg = "從未收到"

        return {
            "running": self.running,
            "connected": self.is_connected,
            "asset_count": self.asset_count,
            "order_book_count": len(self._order_books),
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
        logger.info("🐋 Polymarket Whale Monitor Service started with Dynamic Slippage Engine.")

    def stop(self):
        self.running = False
        self.is_connected = False
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._ping_task:
            self._ping_task.cancel()
        logger.info("🛑 Polymarket Whale Monitor Service stopped.")

    async def _monitor_loop(self):
        retry_delay = 5
        max_delay = 60
        
        while self.running:
            try:
                # 1. 獲取所有活躍市場以取得 asset_id 列表
                asset_ids = await self._fetch_all_active_asset_ids()
                if not asset_ids:
                    logger.warning(f"No active asset IDs found. Retrying in {retry_delay}s...")
                    self.error_count += 1
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_delay)
                    continue

                self.asset_count = len(asset_ids)
                retry_delay = 5 # 重設延遲

                # 預熱 Order Books
                await self._initialize_order_books(asset_ids[:50])  # 先預熱前 50 個熱門資產

                try:
                    ws = await asyncio.wait_for(websockets.connect(POLY_WS_URL), timeout=10)
                except asyncio.TimeoutError:
                    logger.error(f"Polymarket WS connection timed out. Retrying in {retry_delay}s...")
                    self.error_count += 1
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_delay)
                    continue

                async with ws:
                    self.is_connected = True
                    # Subscribe to trades and order book updates
                    sub_msg = {
                        "type": "market",
                        "assets_ids": asset_ids,
                        "custom_feature_enabled": True
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info(f"Subscribed to 'market' channel for {len(asset_ids)} assets (Trades + L2 updates).")

                    # 啟動 PING 任務
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))

                    async for message in ws:
                        if not self.running:
                            break
                        
                        self.last_message_at = datetime.datetime.now(datetime.timezone.utc)
                        retry_delay = 5 # 成功通訊後重設延遲
                        
                        try:
                            data = json.loads(message)
                            
                            # 處理訊息
                            if isinstance(data, dict):
                                event_type = data.get("event_type")
                                if event_type in ["trade", "last_trade_price"]:
                                    await self._handle_trade(data)
                                elif event_type == "order_book_update":
                                    self._handle_order_book_update(data)
                            
                            elif isinstance(data, list):
                                for item in data:
                                    event_type = item.get("event_type")
                                    if event_type in ["trade", "last_trade_price"]:
                                        await self._handle_trade(item)
                                    elif event_type == "order_book_update":
                                        self._handle_order_book_update(item)
                                
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            self.error_count += 1
                            logger.error(f"Error processing Polymarket WS message: {e}")

            except websockets.exceptions.ConnectionClosed:
                self.is_connected = False
                logger.warning(f"Polymarket WS connection closed. Reconnecting in {retry_delay}s...")
            except Exception as e:
                self.is_connected = False
                self.error_count += 1
                logger.error(f"Polymarket WS monitor loop encountered error: {e}. Reconnecting in {retry_delay}s...")
            finally:
                if self._ping_task:
                    self._ping_task.cancel()
            
            if self.running:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)

    async def _initialize_order_books(self, asset_ids: List[str]):
        """從 CLOB API 抓取初始 Order Book 快照"""
        async with httpx.AsyncClient(timeout=10.0) as client:
            for aid in asset_ids:
                try:
                    resp = await client.get(f"{POLY_API_BASE}/book", params={"token_id": aid})
                    if resp.status_code == 200:
                        data = resp.json()
                        ob = OrderBook(token_id=aid)
                        for bid in data.get("bids", []):
                            ob.update("buy", float(bid["price"]), float(bid["size"]))
                        for ask in data.get("asks", []):
                            ob.update("sell", float(ask["price"]), float(ask["size"]))
                        self._order_books[aid] = ob
                except Exception as e:
                    logger.debug(f"Failed to fetch initial book for {aid}: {e}")
                # 避免過快請求
                await asyncio.sleep(0.1)

    def _handle_order_book_update(self, data: Dict[str, Any]):
        """處理增量 Order Book 更新"""
        asset_id = data.get("asset_id")
        if not asset_id:
            return
            
        if asset_id not in self._order_books:
            self._order_books[asset_id] = OrderBook(token_id=asset_id)
            
        ob = self._order_books[asset_id]
        side = data.get("side")
        price = float(data.get("price", 0))
        size = float(data.get("size", 0))
        
        if side and price > 0:
            ob.update(side, price, size)

    async def _handle_trade(self, trade: Dict[str, Any]):
        """
        處理單筆交易：動態滑價門檻判定 -> 獲取背景 -> LLM 總結 -> 推播
        """
        try:
            asset_id = trade.get("asset_id")
            price = float(trade.get("price", 0))
            size = float(trade.get("size", 0))
            usd_value = price * size
            
            if usd_value <= 0:
                return

            # 1. 計算動態門檻 (基於 2% 滑價所需金額)
            ob = self._order_books.get(asset_id)
            if not ob:
                # 如果沒有快取，嘗試同步抓取一次
                await self._initialize_order_books([asset_id])
                ob = self._order_books.get(asset_id)
            
            # 動態門檻判定
            dynamic_threshold = 10000.0 # 預設回退值
            if ob:
                dynamic_threshold = ob.calculate_slippage_threshold(target_percent=0.02)
            
            # 2. 篩選符合門檻的使用者
            user_ids = get_all_user_ids()
            target_users = []
            for uid in user_ids:
                context = get_full_user_context(uid)
                # 如果使用者設定了手動門檻，則取兩者較小值（更靈敏）或使用者設定值？
                # 這裡採用的邏輯是：如果交易金額大於動態門檻，即視為巨鯨交易
                if usd_value >= dynamic_threshold:
                    target_users.append(uid)
                # 或者：如果交易金額大於使用者設定的靜態門檻，也推播 (保留原本功能)
                elif context.polymarket_threshold > 0 and usd_value >= context.polymarket_threshold:
                    target_users.append(uid)
            
            if not target_users:
                return

            logger.info(f"🐋 Whale trade detected: {trade.get('side')} ${usd_value:,.2f} on asset {asset_id} (Dynamic Threshold: ${dynamic_threshold:,.2f})")

            # 3. 獲取市場背景資訊
            market_info = await self._get_market_info(asset_id, trade.get("condition_id"))
            if not market_info:
                market_info = {"question": "未知市場", "description": "無法獲取市場詳細資訊"}

            # 4. 推播通知
            summary_cache = None
            # 獲取基礎交易詳情用於快取
            base_details = self._resolve_trade_details(trade, market_info)
            
            for uid in target_users:
                context = get_full_user_context(uid)
                
                # 重新為該使用者計算其偏好的動態門檻 (如果滑價設定不同)
                user_dynamic_threshold = dynamic_threshold
                if context.polymarket_slippage != 2.0 and ob:
                    user_dynamic_threshold = ob.calculate_slippage_threshold(target_percent=context.polymarket_slippage / 100.0)
                
                # 再次確認是否符合該使用者的門檻
                if usd_value < user_dynamic_threshold and (context.polymarket_threshold <= 0 or usd_value < context.polymarket_threshold):
                    continue

                current_summary = "（未啟用 AI 分析）"
                if context.polymarket_use_llm:
                    if summary_cache is None:
                        summary_cache = await generate_polymarket_summary(market_info, trade, usd_value, base_details)
                    current_summary = summary_cache
                
                await self._push_notification(uid, current_summary, market_info, trade, usd_value, user_dynamic_threshold)

        except Exception as e:
            logger.error(f"Failed to handle Polymarket trade: {e}")

    async def _push_notification(self, user_id: int, summary: str, market_info: Dict[str, Any], trade: Dict[str, Any], usd_value: float, dynamic_threshold: float):
        """
        封裝 Discord Embed (專業分析師格式) 並排入私訊佇列
        """
        import discord
        
        context = get_full_user_context(user_id)
        details = self._resolve_trade_details(trade, market_info)
        size = float(trade.get("size", 0))
        win_rate = details['p_yes'] * 100
        
        # 計算 Whale Multiplier (交易金額是動態門檻的幾倍)
        whale_multiplier = usd_value / dynamic_threshold if dynamic_threshold > 0 else 1.0
        multiplier_str = f"{whale_multiplier:.2f}x" if whale_multiplier >= 0.01 else ("< 0.01x" if whale_multiplier > 0 else "0.00x")
        
        # 估計價格衝擊 (Price Impact)
        user_slip = context.polymarket_slippage
        price_impact = (usd_value / dynamic_threshold) * user_slip if dynamic_threshold > 0 else 0.0
        impact_str = f"{price_impact:.2f}%" if price_impact >= 0.01 else ("< 0.01%" if price_impact > 0 else "0.00%")
            
        embed = discord.Embed(
            title=f"{details['emoji']} 巨鯨買入：{details['intent'].split(']')[0][1:]}",
            color=discord.Color.blue() if details['is_bullish'] else discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        
        content = [
            "---",
            f"**市場問題：** **{market_info.get('question', '未知市場')}**",
            f"**交易金額：** `${usd_value:,.2f}`",
            f"**流動性倍數：** `{multiplier_str}` (相對於 {user_slip}% 滑價深度)",
            f"**估計價格衝擊：** `{impact_str}`",
            f"**當前勝率：** {win_rate:.1f}% (Yes: {details['p_yes']:.3f} | No: {details['p_no']:.3f})",
            "**交易屬性：** 主動買入 (Market Taker)"
        ]
        
        if summary and summary != "（未啟用 AI 分析）":
            content.append("---")
            content.append(f"**🤖 AI 總結分析**\n{summary}")
            
        content.append("---")
        
        # 連結處理
        event_slug = market_info.get("event_slug")
        market_slug = market_info.get("slug")
        market_url = f"https://polymarket.com/event/{event_slug}" if event_slug else (f"https://polymarket.com/market/{market_slug}" if market_slug else "https://polymarket.com")
            
        content.append(f"[🔗 點擊前往 Polymarket 市場]({market_url})")
        
        embed.description = "\n".join(content)
        embed.set_footer(text=f"Nexus Seeker 監測系統 | 動態門檻: ${dynamic_threshold:,.0f}")
        
        await self.bot.queue_dm(user_id, embed=embed)

    async def _fetch_all_active_asset_ids(self) -> List[str]:
        """
        透過 Gamma API 獲取目前的活躍市場資產 ID，並預熱快取。
        """
        asset_ids = []
        active_markets_data = []
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
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
                        clob_tokens_raw = m.get("clobTokenIds")
                        if not clob_tokens_raw:
                            continue
                            
                        try:
                            if isinstance(clob_tokens_raw, str):
                                t_ids = json.loads(clob_tokens_raw)
                            else:
                                t_ids = clob_tokens_raw
                            
                            if not t_ids:
                                continue
                                
                            outcomes_raw = m.get("outcomes", [])
                            prices_raw = m.get("outcomePrices", [])
                            
                            try:
                                if isinstance(outcomes_raw, str):
                                    outcomes = json.loads(outcomes_raw)
                                else:
                                    outcomes = outcomes_raw
                                
                                if isinstance(prices_raw, str):
                                    outcome_prices = json.loads(prices_raw)
                                else:
                                    outcome_prices = prices_raw
                                    
                                outcomes = outcomes if isinstance(outcomes, list) else []
                                outcome_prices = outcome_prices if isinstance(outcome_prices, list) else []
                            except Exception:
                                outcomes = []
                                outcome_prices = []

                            current_market_tokens = []
                            for i, tid in enumerate(t_ids):
                                asset_ids.append(tid)
                                
                                event_slug = None
                                if "events" in m and m["events"] and isinstance(m["events"], list):
                                    event_slug = m["events"][0].get("slug")
                                elif "event" in m and m["event"]:
                                    event_slug = m["event"].get("slug")

                                outcome_name = str(outcomes[i]).strip().strip('"') if i < len(outcomes) else "未知選項"
                                current_price = outcome_prices[i] if i < len(outcome_prices) else 0

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
                            
                            cond_id = m.get("conditionId")
                            if cond_id:
                                self._market_cache[cond_id] = self._market_cache[t_ids[0]]
                                
                        except Exception as e:
                            logger.error(f"Error parsing tokens for market '{q}': {e}")

            self._active_markets = active_markets_data
            return list(set(asset_ids))
        except Exception as e:
            logger.error(f"Failed to fetch active asset IDs via Gamma API: {e}")
        return []

    async def _ping_loop(self, ws):
        try:
            while self.running and self.is_connected:
                await ws.send("PING")
                await asyncio.sleep(10)
        except Exception:
            pass

    async def _get_market_info(self, asset_id: str, condition_id: str = None) -> Dict[str, Any]:
        if asset_id in self._market_cache:
            return self._market_cache[asset_id]
        if condition_id and condition_id in self._market_cache:
            return self._market_cache[condition_id]

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if condition_id:
                    resp = await client.get(f"{GAMMA_API_BASE}/markets", params={"condition_id": condition_id})
                    if resp.status_code == 200:
                        data = resp.json()
                        market = data[0] if isinstance(data, list) and data else data
                        if market:
                            self._market_cache[condition_id] = market
                            return market
        except Exception as e:
            logger.error(f"Error fetching market info for {asset_id}/{condition_id}: {e}")
        return None

    def _resolve_trade_details(self, trade: Dict[str, Any], market_info: Dict[str, Any]) -> Dict[str, Any]:
        side_raw = trade.get("side", "BUY")
        base_price = float(trade.get("price", 0))
        outcome = str(market_info.get("outcome", "Yes")).upper()
        
        if outcome == "YES":
            if side_raw == "BUY":
                intent = "[強力看多] (主動買入 YES)"
                p_yes = base_price
                p_no = 1 - base_price
                is_bullish = True
            else:
                intent = "[獲利了結] (主動賣出 YES / 平倉)"
                p_yes = base_price
                p_no = 1 - base_price
                is_bullish = False
        elif outcome == "NO":
            if side_raw == "BUY":
                intent = "[強力看空] (主動買入 NO)"
                p_no = base_price
                p_yes = 1 - base_price
                is_bullish = False
            else:
                intent = "[獲利了結] (主動賣出 NO / 平倉)"
                p_no = base_price
                p_yes = 1 - base_price
                is_bullish = True
        else:
            if side_raw == "BUY":
                intent = f"[主動選取] (主動買入 {outcome})"
                p_yes = base_price
                p_no = 1 - base_price
                is_bullish = True
            else:
                intent = f"[獲利了結] (主動賣出 {outcome} / 平倉)"
                p_yes = base_price
                p_no = 1 - base_price
                is_bullish = False

        return {
            "intent": intent,
            "p_yes": p_yes,
            "p_no": p_no,
            "emoji": "🟢" if is_bullish else "🔴",
            "is_bullish": is_bullish
        }
