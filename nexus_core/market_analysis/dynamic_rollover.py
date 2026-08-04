from typing import Dict, Any, List, Optional
import logging
from pydantic import BaseModel, Field
from services.llm_service import client, is_memory_safe
from config import LLM_MODEL_NAME

logger = logging.getLogger(__name__)


class FundamentalThesisResult(BaseModel):
    is_broken: bool = Field(description="護城河是否流失或基本面假設已破滅")
    confidence: float = Field(description="判斷信心指數 (0.0 ~ 1.0)")
    reasoning: str = Field(description="簡短的判斷理由，以繁體中文說明")


class DynamicRolloverEngine:
    def __init__(self) -> None:
        pass

    async def evaluate_fundamental_thesis(
        self, symbol: str, fundamental_text: str
    ) -> Optional[FundamentalThesisResult]:
        """
        邏輯 (1): 原型假設破滅
        傳入 FastAPI 爬取的法說會或財報文本，使用 LLM 判定基本面護城河是否流失。
        """
        if not is_memory_safe():
            logger.warning("記憶體水位過高，跳過 vLLM 基本面護城河判定")
            return None

        prompt = (
            f"You are a senior Wall Street quantitative analyst and fundamental researcher.\n"
            f"Please analyze the following latest earnings report and conference call highlights for {symbol}.\n"
            f"Determine whether the company's 'growth moat' has been lost, or if its original bullish fundamental thesis is broken.\n"
            f"IMPORTANT: You must provide your reasoning strictly in Traditional Chinese (繁體中文).\n\n"
            f"Context:\n{fundamental_text}"
        )

        try:
            response = await client.beta.chat.completions.parse(
                model=LLM_MODEL_NAME,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a quantitative fundamentals analyst.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=FundamentalThesisResult,
            )
            parsed = response.choices[0].message.parsed  # type: ignore

            # 寫入 SQLite 全域防禦閘門
            if parsed:
                from database.market_cache import save_fundamental_cache

                save_fundamental_cache(
                    symbol, parsed.is_broken, parsed.confidence, parsed.reasoning
                )

            return parsed  # type: ignore
        except Exception as e:
            logger.error(f"[{symbol}] Fundamental thesis evaluation failed: {e}")
            return None

    def evaluate_opportunity_cost(
        self,
        current_holding_symbol: str,
        current_holding_power_squeeze: float,
        current_holding_profit_pct: float,
        target_watchlist_symbol: str,
        target_power_squeeze: float,
        target_expected_value: float,
        current_holding_expected_value: float,
    ) -> Dict[str, Any]:
        """
        邏輯 (2): 機會成本與期望值比對
        結合 PowerSqueeze 動能指標，當持倉動能衰退且 Watchlist 具備突破條件時，
        計算期望值並給出轉倉建議。
        """
        # 假設 PowerSqueeze 指標中，數值越低代表動能越弱，越高代表突破動能強烈
        holding_momentum_decaying = current_holding_power_squeeze < 20.0
        target_breakout_ready = target_power_squeeze > 80.0

        # 期望值差距
        ev_spread = target_expected_value - current_holding_expected_value

        should_rollover = False
        rollover_ratio = 0.0

        if holding_momentum_decaying and target_breakout_ready and ev_spread > 0.05:
            should_rollover = True
            if current_holding_profit_pct > 0.3:
                # 獲利豐厚，可轉換 50%
                rollover_ratio = 0.5
            else:
                # 獲利一般或虧損，轉換 30% 或全轉，視風險偏好而定
                rollover_ratio = 0.3

        return {
            "should_rollover": should_rollover,
            "rollover_ratio": rollover_ratio,
            "reason": (
                f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                f"Target {target_watchlist_symbol} showing breakout potential (PSQ={target_power_squeeze}) "
                f"with EV spread +{ev_spread*100:.1f}%."
            )
            if should_rollover
            else "No action required.",
        }

    def check_satellite_rebalancing(
        self, portfolio_assets: List[Dict[str, Any]], total_account_value: float
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (3): 核心與衛星比例再平衡
        監控 SQLite 中的持倉配比，當高 Beta 衛星部位超過最大佔比上限時，
        觸發轉倉至核心資產 (如 SPY/QQQ) 的指令。

        portfolio_assets 每個元素應包含:
        - symbol: str
        - asset_class: str ('CORE' 或 'SATELLITE')
        - current_value: float
        - target_allocation_pct: float
        - max_allocation_pct: float
        """
        rebalance_instructions = []

        # 這裡的邏輯可以是單一資產檢查，也可以是整體 Satellite 比例檢查
        # 依照 Step 1 的 Scenario 3，這是一個單一資產上限或 Satellite 總上限的問題
        # 此處實作：單一衛星資產超過 max_allocation_pct，即建議賣出超額部分

        for asset in portfolio_assets:
            if asset.get("asset_class") == "SATELLITE":
                current_value = asset.get("current_value", 0.0)
                max_alloc = asset.get("max_allocation_pct", 0.0)

                if max_alloc > 0.0 and total_account_value > 0.0:
                    current_alloc = current_value / total_account_value
                    if current_alloc > max_alloc:
                        excess_alloc = current_alloc - asset.get(
                            "target_allocation_pct", max_alloc
                        )
                        excess_value = excess_alloc * total_account_value

                        # 建議賣出比例 = 超出金額 / 當前部位總額
                        sell_ratio = excess_value / current_value

                        rebalance_instructions.append(
                            {
                                "symbol": asset["symbol"],
                                "action": "REDUCE",
                                "sell_ratio": round(sell_ratio, 2),
                                "target_core": "VOO",  # 預設核心資產
                                "reason": f"SATELLITE asset {asset['symbol']} allocation ({current_alloc*100:.1f}%) exceeds max ({max_alloc*100:.1f}%).",
                            }
                        )

        return rebalance_instructions
