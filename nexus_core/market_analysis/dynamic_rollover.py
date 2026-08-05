from typing import Dict, Any, List, Optional
import logging
from pydantic import BaseModel, Field
from services.llm_service import client, is_memory_safe
from config import LLM_MODEL_NAME

logger = logging.getLogger(__name__)


class FundamentalThesisResult(BaseModel):
    # 讓模型先進行思考與文字輸出
    reasoning: str = Field(description="Step-by-step reasoning in Traditional Chinese")
    # 思考完後再給出最終判斷
    is_broken: bool = Field(
        description="True if structural thesis is broken, False if just macro/temporary"
    )
    confidence: float = Field(description="Confidence score from 0.0 to 1.0")


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
            f"You are a senior Wall Street quantitative analyst and fundamental research director.\n"
            f"Please analyze the following latest earnings report and conference call highlights for {symbol}.\n\n"
            f"Your objective is to determine whether the company's long-term 'growth moat' has been lost or if its original bullish fundamental thesis is structurally broken.\n\n"
            f"### 🧠 Analytical Framework (Think step-by-step before finalizing fields):\n"
            f"Evaluate based on these four strict criteria:\n"
            f"1. Forward Guidance: Are there significant downward revisions or withdrawal of future guidance?\n"
            f"2. Margin Compression: Is there a structural contraction in gross/operating margins indicating lost pricing power?\n"
            f"3. Market Share & Competition: Is there clear evidence of the company losing core market share to rivals?\n"
            f"4. Core Strategy: Has management pivoted away from their primary growth engine due to failure?\n\n"
            f"### ⚠️ STRICT EXCLUSION RULE (Crucial for `is_broken` decision):\n"
            f"DO NOT classify the thesis as broken (is_broken = false) if the weakness is primarily driven by:\n"
            f"- Cyclical / Macroeconomic headwinds (e.g., interest rates, inflation).\n"
            f"- Foreign exchange (FX) fluctuations.\n"
            f"- General industry downturns.\n"
            f"- A minor single-quarter EPS/Revenue miss where the long-term structural advantage remains intact.\n"
            f"A thesis is ONLY broken (is_broken = true) due to company-specific structural degradation (e.g., lost pricing power, technological obsolescence, permanent market share loss).\n\n"
            f"### 📝 Output Field Instructions:\n"
            f"You must strictly populate the required structured output fields based on the following logic:\n"
            f"- `reasoning`: (CRITICAL) You must perform a Chain-of-Thought analysis here BEFORE concluding. Explicitly state the evidence extracted, categorize if the headwinds are macro (A) or structural (B), and explain how it triggers or avoids the strict exclusion rule. This field MUST be highly analytical, actionable, and written in Traditional Chinese (繁體中文).\n"
            f"- `is_broken`: Set to `true` ONLY IF the thesis is structurally broken based on the exclusion rule. Otherwise, `false`.\n"
            f"- `confidence`: Provide a float from 0.0 to 1.0 reflecting your confidence in this assessment based on the density and clarity of the provided text.\n\n"
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
        target_ivr: float = 0.0,
        target_uoa_sweep: bool = False,
        target_spot: float = 0.0,
        target_put_wall: float = 0.0,
    ) -> Dict[str, Any]:
        """
        邏輯 (2): 機會成本與期望值比對 (包含勝率傾斜)
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
        strategy = "Buy Shares"

        if holding_momentum_decaying and target_breakout_ready and ev_spread > 0.05:
            should_rollover = True
            if current_holding_profit_pct > 0.3:
                # 獲利豐厚，可轉換 50%
                rollover_ratio = 0.5
            else:
                # 獲利一般或虧損，轉換 30% 或全轉，視風險偏好而定
                rollover_ratio = 0.3

            # ----------------------------------------------------
            # [ 勝率傾斜 ] (Win-rate skew)
            # 條件: 低 IVR (< 30) + 巨量 GEX 防線 (靠近 PutWall) + UOA 巨鯨掃貨
            # 動作: 現貨打底 + ITM Call 槓桿
            # ----------------------------------------------------
            is_near_put_wall = (target_put_wall > 0 and target_spot > 0) and (
                abs(target_spot - target_put_wall) / target_put_wall < 0.015
            )
            is_low_ivr = 0 < target_ivr < 30.0

            if is_low_ivr and is_near_put_wall and target_uoa_sweep:
                strategy = "Shares + ITM Call"
                reason_suffix = f" (🎯 勝率傾斜觸發: 低IVR({target_ivr:.1f}%) + PutWall防線 + UOA掃貨，建議 ITM Call 槓桿)"
            else:
                reason_suffix = ""

            return {
                "should_rollover": should_rollover,
                "rollover_ratio": rollover_ratio,
                "strategy": strategy,
                "reason": (
                    f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                    f"Target {target_watchlist_symbol} showing breakout potential (PSQ={target_power_squeeze}) "
                    f"with EV spread +{ev_spread*100:.1f}%." + reason_suffix
                ),
            }

        return {
            "should_rollover": False,
            "rollover_ratio": 0.0,
            "strategy": "N/A",
            "reason": "No action required.",
        }

    def check_satellite_rebalancing(
        self, portfolio_assets: List[Dict[str, Any]], total_account_value: float
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (3): 核心與衛星比例再平衡 + 深度微觀結構與選擇權籌碼驅動
        包含勝率傾斜與雜訊避險等高階戰術。
        """
        rebalance_instructions = []

        for asset in portfolio_assets:
            if asset.get("asset_class") == "SATELLITE":
                symbol = asset["symbol"]
                current_value = asset.get("current_value", 0.0)
                max_alloc = asset.get("max_allocation_pct", 0.3)

                # --- 新增：深度量化數據 (Fallback = None/0.0) ---
                spot = asset.get("spot_price", 0.0)
                call_wall = asset.get("call_wall", 0.0)
                max_pain = asset.get("max_pain", 0.0)
                ivr = asset.get("ivr", 0.0)

                # 計算比例
                current_alloc = (
                    current_value / total_account_value
                    if total_account_value > 0
                    else 0.0
                )

                # ----------------------------------------------------
                # [ 雜訊避險 ] (Noise hedge)
                # 條件: 高波泡沫 (IVR > 80)、碰觸 CallWall，或大幅偏離 Max Pain (>10%)
                # 動作: 100% 撤退回 VOO
                # ----------------------------------------------------
                is_iv_bubble = ivr > 80.0
                touch_call_wall = (call_wall > 0 and spot > 0) and (
                    abs(spot - call_wall) / call_wall < 0.015 or spot >= call_wall
                )
                deviate_max_pain = (max_pain > 0 and spot > 0) and (
                    abs(spot - max_pain) / max_pain > 0.1
                )

                if is_iv_bubble or touch_call_wall or deviate_max_pain:
                    reason = []
                    if is_iv_bubble:
                        reason.append(f"高波動泡沫 (IVR={ivr:.1f}%)")
                    if touch_call_wall:
                        reason.append(f"碰觸或突破 CallWall 天花板 (${call_wall})")
                    if deviate_max_pain:
                        reason.append(f"大幅偏離 Max Pain (${max_pain})")

                    rebalance_instructions.append(
                        {
                            "symbol": symbol,
                            "action": "LIQUIDATE",
                            "sell_ratio": 1.0,
                            "target_core": "VOO",
                            "reason": "🚨 雜訊避險觸發: "
                            + "、".join(reason)
                            + "。強烈建議 100% 資金回流 VOO 避風港。",
                            "suggested_strategy": "N/A",
                        }
                    )
                    continue  # 已經100%撤退，不需進行後續常規再平衡

                # ----------------------------------------------------
                # [ 常規比例控管 ]
                # ----------------------------------------------------
                if (
                    max_alloc > 0.0
                    and total_account_value > 0.0
                    and current_alloc > max_alloc
                ):
                    excess_alloc = current_alloc - asset.get(
                        "target_allocation_pct", max_alloc
                    )
                    excess_value = excess_alloc * total_account_value
                    sell_ratio = excess_value / current_value

                    rebalance_instructions.append(
                        {
                            "symbol": symbol,
                            "action": "REDUCE",
                            "sell_ratio": round(sell_ratio, 2),
                            "target_core": "VOO",
                            "reason": f"SATELLITE asset {symbol} allocation ({current_alloc*100:.1f}%) exceeds max ({max_alloc*100:.1f}%).",
                            "suggested_strategy": "N/A",
                        }
                    )

        return rebalance_instructions
