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

    def _generate_rule_based_rebalance_report(
        self, symbol: str, metrics: dict, system_action: str
    ) -> dict:
        """
        Evaluates rebalancing rules and generates the strict 4-part markdown report.
        Returns a dict containing the final action, target asset, and markdown string.
        """
        spot = metrics.get("spot_price", 0.0)
        ivr = metrics.get("ivr", 0.0)
        put_wall = metrics.get("put_wall", 0.0)
        call_wall = metrics.get("call_wall", 0.0)
        max_pain = metrics.get("max_pain", 0.0)
        is_uoa_sweep = metrics.get("is_uoa_sweep", False)
        sqz_mom = metrics.get("sqz_mom", 0.0)
        skew = metrics.get("skew", 0.0)

        final_target = "VOO"
        final_action = system_action

        # 邏輯 1: 資金效率最大化 & 邏輯 6: 指示不相符處置
        system_conflict_note = ""
        if (
            final_action in ["BUY", "REDUCE", "HOLD"]
            and not is_uoa_sweep
            and (put_wall > 0 and spot < put_wall)
        ):
            final_action = "LIQUIDATE"
            final_target = "VOO"
            system_conflict_note = "⚠️ **系統算式瑕疵攔截**：原建議留倉或加碼，但缺乏 UOA 支持且跌穿 PutWall，已強制推翻並要求撤退至 VOO。"

        # 邏輯 2 & 3: IV Context 與 試水溫
        options_strategy = "N/A (觀望現股)"
        if ivr < 25.0 and put_wall > 0 and spot >= put_wall and is_uoa_sweep:
            options_strategy = "Buy OTM Call (低 IV 佈局 Vega 擴張)"
        elif ivr > 50.0:
            options_strategy = "嚴禁買方 (IV 過高，規避 Gamma 陷阱)"

        # 邏輯 4: 動態停損 (依託 GEX)
        stop_loss = put_wall * 0.995 if put_wall > 0 else spot * 0.95

        # 邏輯 5: 數據真實性校正
        data_note = ""
        if ivr == 0.0 or spot == 0.0:
            data_note = " (⚠️ 數據失真或快取未更新，請留意風險)"

        # 建構 4 段式 Markdown
        markdown_report = f"""
1. **盤勢定調**
   - 現價: ${spot:.2f} | IV 位階: {ivr:.1f}%{data_note}
   - 相對位置: Max Pain ${max_pain}
2. **主力意圖拆解 (UOA/GEX)**
   - 做市商護盤牆: PutWall @ ${put_wall} | 壓制區: CallWall @ ${call_wall}
   - 巨鯨掃貨: {'✅ 偵測到 UOA Sweep' if is_uoa_sweep else '❌ 無明顯 UOA'}
3. **動能與擠壓狀態**
   - SQZ MOM: {sqz_mom:.2f} | Skew: {skew:.2f}
4. **具體的動態轉倉建議**
   - {system_conflict_note if system_conflict_note else '常規執行：依系統建議比例調節'}
   - 處置計畫: 轉入 `{final_target}` ({final_action})
   - 期權策略: {options_strategy}
   - 精確停損: 跌破 ${stop_loss:.2f} 絕對停損
"""
        return {
            "final_action": final_action,
            "final_target": final_target,
            "options_strategy": options_strategy,
            "markdown_report": markdown_report.strip(),
        }

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

        system_prompt = (
            "You are a senior Wall Street quantitative analyst and fundamental research director.\n"
            "Your objective is to determine whether a company's long-term 'growth moat' has been lost or if its original bullish fundamental thesis is structurally broken.\n\n"
            "### 🧠 Analytical Framework (Think step-by-step before finalizing fields):\n"
            "Evaluate based on these four strict criteria:\n"
            "1. Forward Guidance: Are there significant downward revisions or withdrawal of future guidance?\n"
            "2. Margin Compression: Is there a structural contraction in gross/operating margins indicating lost pricing power?\n"
            "3. Market Share & Competition: Is there clear evidence of the company losing core market share to rivals?\n"
            "4. Core Strategy: Has management pivoted away from their primary growth engine due to failure?\n\n"
            "### ⚠️ STRICT EXCLUSION RULE (Crucial for `is_broken` decision):\n"
            "DO NOT classify the thesis as broken (is_broken = false) if the weakness is primarily driven by:\n"
            "- Cyclical / Macroeconomic headwinds (e.g., interest rates, inflation).\n"
            "- Foreign exchange (FX) fluctuations.\n"
            "- General industry downturns.\n"
            "- A minor single-quarter EPS/Revenue miss where the long-term structural advantage remains intact.\n"
            "A thesis is ONLY broken (is_broken = true) due to company-specific structural degradation (e.g., lost pricing power, technological obsolescence, permanent market share loss).\n\n"
            "### 📝 Output Field Instructions:\n"
            "You must strictly populate the required structured output fields based on the following logic:\n"
            "- `reasoning`: (CRITICAL) You must perform a Chain-of-Thought analysis here BEFORE concluding. Explicitly state the evidence extracted, categorize if the headwinds are macro (A) or structural (B), and explain how it triggers or avoids the strict exclusion rule. This field MUST be highly analytical, actionable, and written in Traditional Chinese (繁體中文).\n"
            "- `is_broken`: Set to `true` ONLY IF the thesis is structurally broken based on the exclusion rule. Otherwise, `false`.\n"
            "- `confidence`: Provide a float from 0.0 to 1.0 reflecting your confidence in this assessment based on the density and clarity of the provided text."
        )

        user_prompt = (
            f"Please analyze the following latest earnings report and conference call highlights for {symbol}.\n\n"
            f"Context:\n{fundamental_text}"
        )

        try:
            response = await client.beta.chat.completions.parse(
                model=LLM_MODEL_NAME,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {"role": "user", "content": user_prompt},
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
            # 條件二：新標的出現「極致不對稱勝率」
            # ----------------------------------------------------
            is_low_ivr = 0 < target_ivr < 30.0
            is_near_put_wall = (target_put_wall > 0 and target_spot > 0) and (
                abs(target_spot - target_put_wall) / target_put_wall <= 0.01
            )
            is_extreme_asymmetric = is_low_ivr and is_near_put_wall and target_uoa_sweep

            if is_extreme_asymmetric:
                strategy = "Shares + ITM Call"
                reason_suffix = f" (🎯 條件二極致勝率觸發: 低IVR({target_ivr:.1f}%) + 鋼鐵牆築底 + 巨鯨掃貨，強制啟動轉倉)"
            else:
                reason_suffix = ""

            # 強制優先採用極致不對稱勝率條件
            if holding_momentum_decaying and is_extreme_asymmetric:
                should_rollover = True
                rollover_ratio = 1.0  # 條件三要求 100% 滿載運算 / 不留戀
                return {
                    "should_rollover": should_rollover,
                    "rollover_ratio": rollover_ratio,
                    "strategy": strategy,
                    "reason": (
                        f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                        f"Target {target_watchlist_symbol} hit asymmetric win-rate. "
                        + reason_suffix
                    ),
                }

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
                metrics = {
                    "spot_price": asset.get("spot_price", 0.0),
                    "call_wall": asset.get("call_wall", 0.0),
                    "max_pain": asset.get("max_pain", 0.0),
                    "ivr": asset.get("ivr", 0.0),
                    "put_wall": asset.get("put_wall", 0.0),
                    "is_uoa_sweep": asset.get("is_uoa_sweep", False),
                    "sqz_mom": asset.get("sqz_mom", 0.0),
                    "skew": asset.get("skew", 0.0),
                    "gamma_flip": asset.get("gamma_flip", 0.0),
                }

                spot = metrics["spot_price"]
                call_wall = metrics["call_wall"]
                ivr = metrics["ivr"]
                put_wall = metrics["put_wall"]
                gamma_flip = metrics["gamma_flip"]
                sqz_mom = metrics["sqz_mom"]
                skew = metrics["skew"]

                # 計算比例
                current_alloc = (
                    current_value / total_account_value
                    if total_account_value > 0
                    else 0.0
                )

                # ----------------------------------------------------
                # 條件一：現有持倉結構劣化（護衛牆破位 / 主力物理蓋頂 / 目標區獲利解鎖完成）
                # ----------------------------------------------------
                # 1. 做市商 GEX 防線失守
                is_structural_breakdown = (put_wall > 0 and gamma_flip > 0) and (
                    spot < put_wall and spot < gamma_flip
                )
                # 2. 主力巨量 STO 實體蓋頂
                is_whale_sto_block = (sqz_mom < 0.0) and (skew < -0.3)
                # 3. 目標區獲利解鎖完成
                is_profit_unlocked = (call_wall > 0 and spot > 0) and (
                    spot >= call_wall or abs(spot - call_wall) / call_wall < 0.015
                )

                # 條件三 (部分)：擺脫高波洗籌泥淖 (IV Crush 威脅)
                is_iv_bubble = ivr > 80.0

                if (
                    is_structural_breakdown
                    or is_whale_sto_block
                    or is_profit_unlocked
                    or is_iv_bubble
                ):
                    report = self._generate_rule_based_rebalance_report(
                        symbol, metrics, system_action="LIQUIDATE"
                    )

                    rebalance_instructions.append(
                        {
                            "symbol": symbol,
                            "action": report["final_action"],
                            "sell_ratio": 1.0,
                            "target_core": report["final_target"],
                            "reason": report["markdown_report"],
                            "suggested_strategy": report["options_strategy"],
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

                    report = self._generate_rule_based_rebalance_report(
                        symbol, metrics, system_action="REDUCE"
                    )

                    rebalance_instructions.append(
                        {
                            "symbol": symbol,
                            "action": report["final_action"],
                            "sell_ratio": round(sell_ratio, 2)
                            if report["final_action"] != "LIQUIDATE"
                            else 1.0,
                            "target_core": report["final_target"],
                            "reason": report["markdown_report"],
                            "suggested_strategy": report["options_strategy"],
                        }
                    )

        return rebalance_instructions
