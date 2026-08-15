from typing import Dict, Any, List, Optional
import logging
from pydantic import BaseModel, Field
from services.llm_service import client, is_memory_safe
from config import LLM_MODEL_NAME

from market_analysis.gamma_cliff_confirmation import is_gamma_cliff_confirmed
from market_analysis.ivr_strategy_gate import is_selling_locked_by_ivr
from database.user_settings import get_full_user_context
from market_analysis.sentiment.history_storage import get_indicator_percentile
import sqlite3
import config

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
        self,
        symbol: str,
        metrics: dict,
        system_action: str,
        target: str = "VOO",
        strategy_override: str = "",
        asset_class: str = "SPOT",
        is_take_profit: bool = False,
        active_orders: Optional[list[dict]] = None,
        position_shares: float = 0.0,
        current_value: float = 0.0,
    ) -> dict:
        """
        Evaluates rebalancing rules under Gray-Scale Quantitative Framework
        and generates the strict 4-part markdown report.
        Returns a dict containing the final action, target asset, and markdown string.
        """
        spot = float(metrics.get("spot_price", 0.0))
        price_15m_close = float(metrics.get("price_15m_close", spot))
        ivr = float(metrics.get("ivr", 0.0))
        put_wall = float(metrics.get("put_wall", 0.0))
        call_wall = float(metrics.get("call_wall", 0.0))
        max_pain = float(metrics.get("max_pain", 0.0))
        is_uoa_sweep = bool(metrics.get("is_uoa_sweep", False))
        sqz_mom = float(metrics.get("sqz_mom", 0.0))
        skew = float(metrics.get("skew", 0.0))
        support_wall = float(metrics.get("support_wall", 0.0))
        resistance_wall = float(metrics.get("resistance_wall", 0.0))
        atr_15m = float(metrics.get("atr_15m", metrics.get("atr_14", 0.0)))
        hvn = float(metrics.get("hvn", 0.0))
        lvn = float(metrics.get("lvn", 0.0))
        dte = int(metrics.get("dte", 99))

        # ━━━ 期權拓撲微結構校正 ━━━
        # 若 put_wall 與 call_wall 顛倒，或是已有 GEX 提取之 support_wall / resistance_wall
        if support_wall > 0:
            anchor_wall = support_wall
        elif put_wall > 0 and call_wall > 0 and put_wall > call_wall:
            # 拓撲逆轉修復：較低價為做市商支撐底牆，較高價為上方阻力天花板
            anchor_wall = min(put_wall, call_wall)
        elif put_wall > 0:
            anchor_wall = put_wall
        else:
            anchor_wall = hvn if hvn > 0 else spot

        if resistance_wall > 0:
            effective_res_wall = resistance_wall
        elif put_wall > 0 and call_wall > 0 and put_wall > call_wall:
            effective_res_wall = max(put_wall, call_wall)
        elif call_wall > 0:
            effective_res_wall = call_wall
        else:
            effective_res_wall = spot * 1.05

        # ━━━ 防洗盤四大機制：計算精確防守位與掛單限價 ━━━
        # 機制 2: 1.5x ATR 防護墊片
        if anchor_wall > 0:
            raw_stop_loss = anchor_wall - (1.5 * atr_15m)
        else:
            raw_stop_loss = spot * 0.96 if spot > 0 else 0.0

        base_stop_loss = raw_stop_loss

        # 機制 1: 避開 LVN 陷阱 (量價拓撲吸附演算法：絕對吸附至次級 HVN 上緣 + 0.2*ATR_15m，禁止固定 % 平移)
        if lvn > 0 and base_stop_loss > 0 and abs(base_stop_loss - lvn) / lvn <= 0.015:
            secondary_hvn = float(metrics.get("secondary_hvn", 0.0))
            target_hvn = 0.0
            if secondary_hvn > 0 and secondary_hvn < lvn:
                target_hvn = secondary_hvn
            elif hvn > 0 and hvn < lvn:
                target_hvn = hvn
            elif anchor_wall > 0 and anchor_wall < lvn:
                target_hvn = anchor_wall

            if target_hvn > 0:
                base_stop_loss = target_hvn + (0.2 * atr_15m)
            else:
                base_stop_loss = lvn - (1.0 * atr_15m)

        # 機制 4: 末日結算容忍度 (DTE 0/1) 與風險平價口數縮放 (Risk-Parity Sizing)
        is_01dte_expanded = dte <= 1 and base_stop_loss > 0
        dte_risk_parity_scale = 1.0
        if is_01dte_expanded:
            base_stop_loss = base_stop_loss - 1.5 * atr_15m
            # Risk-Parity 縮放因子: Base Distance / (Base Distance + 1.5 * ATR_15m) = 1.5 / 3.0 = 0.5
            dte_risk_parity_scale = 0.5

        stop_loss = round(base_stop_loss, 2)
        limit_price = round(
            max(stop_loss - (0.5 * atr_15m if atr_15m > 0 else 0.6), stop_loss * 0.995),
            2,
        )

        # ━━━ 委託單聯動 (Active Orders) ━━━
        matching_order: Optional[dict] = None
        if active_orders:
            for ord_entry in active_orders:
                if (
                    ord_entry.get("symbol", "").upper() == symbol.upper()
                    and ord_entry.get("side", "SELL").upper() == "SELL"
                ):
                    matching_order = ord_entry
                    break

        if matching_order:
            order_id = matching_order.get("id", "")
            ord_stop = float(matching_order.get("stop_price") or stop_loss)
            ord_limit = float(matching_order.get("limit_price") or limit_price)
            order_defense_str = f"**委託單 #{order_id} 有效**\n\n**停損: ${ord_stop:.2f} | 限價: ${ord_limit:.2f}**"
        else:
            order_defense_str = f"**建議設置防守委託單**\n\n**停損: ${stop_loss:.2f} | 限價: ${limit_price:.2f}**"

        # ━━━ 灰階思考量化裁決 (決策矩陣 - 雙軌裁決機制 Dual-Track Exit) ━━━
        final_target = target if target else "VOO"
        final_action = system_action
        system_conflict_note = ""

        ivr_drop = float(metrics.get("ivr_drop", metrics.get("ivr_change", 0.0)))
        is_options_fast_exit = asset_class == "OPTIONS" and (
            (spot < stop_loss if (stop_loss > 0 and spot > 0) else False)
            or (ivr_drop >= 20.0)
        )

        # 機制 3: 15 分鐘實體 K 線過濾 (非瞬時破位，針對現貨 SPOT)
        is_15m_close_broken = (
            (price_15m_close < stop_loss)
            if (stop_loss > 0 and price_15m_close > 0)
            else False
        )

        if is_take_profit:
            final_action = "LIQUIDATE"
            final_target = target
            system_conflict_note = (
                "🎯 **獲利解鎖達成**：觸及阻力目標位，按計劃獲利了結轉倉。"
            )
            options_strategy = f"100% LIQUIDATE (轉入 {final_target})"
        elif is_options_fast_exit:
            final_action = "LIQUIDATE"
            final_target = target if target else "VOO"
            system_conflict_note = (
                f"🚨 **期權雙軌快速通道觸發**：標的現價 (${spot:.2f}) 貫穿防守位 (${stop_loss:.2f}) 或 IV 驟降 (>20%)，"
                f"啟動 3-5m 快速通道平倉 (拒絕等待 15m 實體收盤以規避 Delta/Vega 雙殺)。"
            )
            options_strategy = f"100% LIQUIDATE / STC (快速平倉轉入 {final_target})"
        elif is_15m_close_broken:
            final_action = "LIQUIDATE"
            final_target = target if target else "VOO"
            system_conflict_note = (
                f"🚨 **15m 實體破位確認**：15 分鐘實體收盤價 (${price_15m_close:.2f}) 跌破防守線 (${stop_loss:.2f})，"
                f"做市商底牆徹底崩塌，負 Gamma 助跌啟動，強制啟動 100% 轉入 {final_target} 防禦。"
            )
            options_strategy = f"100% LIQUIDATE (轉入 {final_target})"
        elif system_action == "REDUCE":
            final_action = "REDUCE"
            final_target = target
            system_conflict_note = "⚖️ **持倉比例再平衡**：衛星部位超過風險上限，執行常規部分減倉以平衡資產權重。"
            options_strategy = "REDUCE (部分獲利了結/降低持倉比重)"
        else:
            # 未跌破防守線 -> 一律維持 HOLD
            final_action = "HOLD"
            final_target = symbol
            system_conflict_note = (
                f"🛡️ **灰階量化裁決**：${anchor_wall:.2f} 正 Gamma 護城河完好，"
                f"動能（SQZ MOM {sqz_mom:+.2f}）維持多頭，未觸發轉倉條件，維持現狀續抱。"
            )
            options_strategy = "HOLD (維持現狀續抱)"

        # ━━━ IVR 策略防禦與微調 ━━━
        if strategy_override:
            options_strategy = strategy_override
        elif is_selling_locked_by_ivr(ivr):
            options_strategy += f" | ⚠️ IVR 極低位 ({ivr:.1f}%): 賣方策略已鎖死。"
        elif ivr > 50.0:
            options_strategy += " | 嚴禁買方 (IV 過高，規避 Gamma 陷阱)"

        # 停損數值字串格式化 (嚴禁輸出 N/A)
        stop_loss_str = f"${stop_loss:.2f}"

        # 數據異常註記
        data_note = ""
        if ivr == 0.0 or spot == 0.0:
            data_note = " (⚠️ 數據失真或快取未更新，請留意風險)"

        # ━━━ 資金回收與目標核心資產買入預估 (結合風險平價口數縮放) ━━━
        if current_value > 0:
            recovered_cash = current_value
        elif position_shares > 0 and spot > 0:
            recovered_cash = position_shares * spot
        else:
            recovered_cash = 0.0

        target_core_name = target if target else "VOO"
        if recovered_cash > 0:
            cash_str = f"${recovered_cash:,.0f}"
            target_est_price = (
                560.0
                if ("VOO" in target_core_name or "SPY" in target_core_name)
                else (spot if spot > 0 else 500.0)
            )
            target_shares_est = int(recovered_cash / target_est_price)
            if is_01dte_expanded:
                target_shares_est = max(
                    1, int(target_shares_est * dte_risk_parity_scale)
                )
                target_shares_low = max(1, target_shares_est - 1)
                target_shares_high = max(1, target_shares_est + 1)
                shares_guidance_str = f"{target_core_name}（0/1 DTE 風險平價縮放: 約 {target_shares_low}–{target_shares_high} 股 / 削減 50% 部位）"
            else:
                target_shares_low = max(1, target_shares_est - 1)
                target_shares_high = max(1, target_shares_est + 1)
                shares_guidance_str = f"{target_core_name}（約 {target_shares_low}–{target_shares_high} 股）"
        else:
            cash_str = "全數部位資金"
            shares_guidance_str = f"{target_core_name}（全額買入）"

        # GEX 數值描述格式化
        supp_gex = metrics.get("support_gex")
        res_gex = metrics.get("resistance_gex")
        if supp_gex is not None and supp_gex != 0:
            gex_support_desc = (
                f"{supp_gex/1e6:+.0f}M"
                if abs(supp_gex) >= 1e6
                else f"{supp_gex/1e3:+.0f}k"
            )
        else:
            gex_support_desc = "做市商強正 Gamma 支撐"

        if res_gex is not None and res_gex != 0:
            gex_res_desc = (
                f"{res_gex/1e6:+.0f}M"
                if abs(res_gex) >= 1e6
                else f"{res_gex/1e3:+.0f}k"
            )
        else:
            gex_res_desc = "做市商阻力天花板"

        dte_scale_note = ""
        if is_01dte_expanded:
            dte_scale_note = "\n   - ⚡ **0/1 DTE 風險平價口數縮放**：已啟動 3.0× ATR 緩衝墊片 (停損拉寬)，強制削減 50% 轉倉/開倉部位規模以維持 Dollar Risk 恆定。"

        dual_track_note = (
            "**3-5m 快速通道監控** (期權合約拒絕等待 15m 實體收盤以規避 Delta/Vega 雙殺)"
            if asset_class == "OPTIONS"
            else f"**15m 實體 K 線過濾** (盤中插針至 ${spot:.2f} 屬做市商正常洗盤，未跌破 ${stop_loss_str} 實體收盤前絕不手動干預)"
        )

        # 建構標準 4 段式 Markdown
        markdown_report = f"""
1. **盤勢定調**
   - 現價: ${spot:.2f} | IV 位階: {ivr:.1f}%{data_note}
   - 相對位置: Max Pain ${max_pain:.2f}
2. **主力意圖拆解 (UOA/GEX 微結構)**
   - 做市商護盤牆: GEX Wall: ${anchor_wall:.2f} ({gex_support_desc}) (強支撐彈簧床)
   - 阻力天花板: ${effective_res_wall:.2f} ({gex_res_desc})
   - 巨鯨掃貨: {'✅ 偵測到 UOA Sweep' if is_uoa_sweep else '❌ 無明顯 UOA'}
3. **動能與擠壓狀態**
   - SQZ MOM: {sqz_mom:+.2f} | Skew: {skew:.2f} ({'多頭動能延續' if sqz_mom > 0 else '動能中性/趨緩'})
4. **具體的動態轉倉建議**
   - {system_conflict_note if system_conflict_note else '常規執行：依系統建議比例調節'}{dte_scale_note}
   - 轉倉決策: **{final_action} ({'維持現狀續抱' if final_action == 'HOLD' else '轉入 ' + final_target})**
   - 微結構判定: GEX Wall ${anchor_wall:.2f} 護城河完好，阻力天花板 ${effective_res_wall:.2f}
   - 防守機制: {order_defense_str}
     *(避開真空區，依據公式：`Stop = ${anchor_wall:.2f} - ({'3.0' if is_01dte_expanded else '1.5'} × ATR_15m) = ${stop_loss_str}`)*
   - 出場裁決軌道: {dual_track_note}

---
## 🚨 動態資金輪動觸發條件（何時才真正轉倉 {target_core_name}？）
只有在以下**硬性量化條件觸發**時，才允許執行 100% 轉入 {target_core_name}：
1. **實體破位觸發**：
   - {'3-5m 快速通道跌破或 IV 崩塌' if asset_class == 'OPTIONS' else f'15 分鐘 K 線**實體收盤跌破 ${stop_loss_str}**'}，或委託單自動觸發成交。
   - **量化含義**：宣告 ${anchor_wall:.2f} 做市商底牆徹底崩塌，負 Gamma 助跌啟動，價格將下探 ${max_pain:.2f} 痛點。
2. **轉倉執行動作**：
   - 回收資金約 **{cash_str}**。
   - **唯一指令**：立即市價全數買入 **{shares_guidance_str}**，使組合轉為 100% {target_core_name} 大盤防禦模式。
"""
        return {
            "final_action": final_action,
            "final_target": final_target,
            "options_strategy": options_strategy,
            "markdown_report": markdown_report.strip(),
        }

    def _find_best_rollover_target(self) -> str:
        """掃描快取尋找下一個高 EV 衛星標的，若無則回傳 VOO"""
        conn = None
        try:
            conn = sqlite3.connect(config.DB_NAME)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT symbol, max_pain, expected_move_lower, expected_move_upper FROM market_cache"
            )
            rows = cursor.fetchall()

            for row in rows:
                sym = row["symbol"]
                if sym in ["QQQ", "SPY", "VOO", "VXX"]:
                    continue
                # 此處應整合各項指標掃描 (IVR < 30%, EV > 0.05 等)，
                # 簡化起見，實作上可與 SentimentEngine 或 intraday_pipeline 的 radar cache 連動。
                # 目前假設若資料齊全且符合初步過濾則視為標的：
                # (此為架構骨架，後續可接上真實的 Screener)
                # 假設有抓到，回傳該 sym
                pass

            return "VOO"
        except Exception as e:
            logger.error(f"尋找 Rollover Target 失敗: {e}")
            return "VOO"
        finally:
            if conn:
                conn.close()

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

    async def check_satellite_rebalancing(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        total_account_value: float,
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (3): 核心與衛星比例再平衡 + 深度微觀結構與選擇權籌碼驅動
        包含勝率傾斜與雜訊避險等高階戰術。
        """
        rebalance_instructions: List[Dict[str, Any]] = []

        # 取得使用者待成交委託單以供防守機制關聯
        user_orders: list[dict] = []
        try:
            from database.orders import get_user_active_orders

            user_orders = get_user_active_orders(user_id)
        except Exception as e:
            logger.debug(f"無法取得 user {user_id} active_orders: {e}")
            user_orders = []

        for asset in portfolio_assets:
            if asset.get("asset_class") == "SATELLITE":
                symbol: str = str(asset.get("symbol", ""))
                current_value: float = float(asset.get("current_value", 0.0))
                quantity: float = float(asset.get("quantity", 0.0))
                max_alloc: float = float(asset.get("max_allocation_pct", 0.3))

                # --- 新增：深度量化數據 (Fallback = None/0.0) ---
                spot: float = float(asset.get("spot_price", 0.0))
                call_wall: float = float(asset.get("call_wall", 0.0))
                max_pain: float = float(asset.get("max_pain", 0.0))
                ivr: float = float(asset.get("ivr", 0.0))
                put_wall: float = float(asset.get("put_wall", 0.0))
                is_uoa_sweep: bool = bool(asset.get("is_uoa_sweep", False))
                sqz_mom: float = float(asset.get("sqz_mom", 0.0))
                skew: float = float(asset.get("skew", 0.0))

                raw_skew_perc = asset.get("skew_percentile", None)
                skew_percentile: float
                if raw_skew_perc is not None:
                    skew_percentile = float(raw_skew_perc)
                else:
                    skew_percentile = get_indicator_percentile(symbol, "SKEW", skew)

                gamma_flip: float = float(asset.get("gamma_flip", 0.0))
                atr_14: float = float(asset.get("atr_14", 0.0))
                hvn: float = float(asset.get("hvn", 0.0))
                lvn: float = float(asset.get("lvn", 0.0))
                dte: int = int(asset.get("dte", 99))
                price_15m_close: float = float(asset.get("price_15m_close", spot))
                atr_15m: float = float(asset.get("atr_15m", atr_14))

                # 計算比例
                current_alloc: float = (
                    current_value / total_account_value
                    if total_account_value > 0
                    else 0.0
                )

                asset_class = str(
                    asset.get("instrument_type", asset.get("asset_type", "SPOT"))
                ).upper()
                if "OPT" in asset_class or "CONTRACT" in asset_class:
                    asset_class = "OPTIONS"
                else:
                    asset_class = "SPOT"

                from market_analysis.index_microstructure import classify_gex_wall

                gex_profile_data = asset.get("gex_profile_data", {})
                support_wall: float = 0.0
                resistance_wall: float = 0.0
                support_gex: float = 0.0
                resistance_gex: float = 0.0
                if (
                    gex_profile_data
                    and "gex_profile" in gex_profile_data
                    and isinstance(gex_profile_data["gex_profile"], dict)
                ):
                    gex_prof = gex_profile_data["gex_profile"]
                    max_positive: float = 0.0
                    for k, v in gex_prof.items():
                        try:
                            val = float(v)
                            if val > max_positive:
                                max_positive = val
                        except Exception:
                            pass
                    for k, v in gex_prof.items():
                        try:
                            val = float(v)
                            strike = float(k)
                            wall_type = classify_gex_wall(
                                val, max_positive, is_heavy_otm_call=False
                            )
                            if (
                                wall_type == "SUPPORT_GEX_WALL"
                                and strike > support_wall
                            ):
                                support_wall = strike
                                support_gex = val
                            elif (
                                wall_type == "RESISTANCE_CALL_WALL"
                                and strike > resistance_wall
                            ):
                                resistance_wall = strike
                                resistance_gex = val
                        except Exception:
                            pass

                metrics: Dict[str, Any] = {
                    "spot_price": spot,
                    "call_wall": call_wall,
                    "max_pain": max_pain,
                    "ivr": ivr,
                    "put_wall": put_wall,
                    "is_uoa_sweep": is_uoa_sweep,
                    "sqz_mom": sqz_mom,
                    "skew": skew,
                    "skew_percentile": skew_percentile,
                    "gamma_flip": gamma_flip,
                    "atr_14": atr_14,
                    "hvn": hvn,
                    "lvn": lvn,
                    "dte": dte,
                    "price_15m_close": price_15m_close,
                    "atr_15m": atr_15m,
                    "support_wall": support_wall,
                    "resistance_wall": resistance_wall,
                    "support_gex": support_gex,
                    "resistance_gex": resistance_gex,
                }

                # ----------------------------------------------------
                # 條件一：現有持倉結構劣化（護衛牆破位 / 主力物理蓋頂 / 目標區獲利解鎖完成）
                # ----------------------------------------------------
                # 1. 做市商 GEX 防線失守
                # 灰階思考防洗盤：若多頭動能充足且未破 15m 收盤價防守位，不預先標記破位
                anchor_base: float = (
                    support_wall
                    if support_wall > 0
                    else (
                        min(put_wall, gamma_flip)
                        if (put_wall > 0 and gamma_flip > 0)
                        else (put_wall if put_wall > 0 else gamma_flip)
                    )
                )
                gamma_cliff_level: float = (
                    anchor_base - (1.5 * atr_14) if anchor_base > 0 else 0.0
                )

                is_structural_breakdown_pending: bool = (
                    anchor_base > 0
                    and spot < anchor_base
                    and (price_15m_close < gamma_cliff_level or sqz_mom <= 0)
                )

                # Phase 2: 負 Gamma 懸崖連續 15 分鐘實體 K 線貫穿確認 (現貨 SPOT) 或 3-5m 快速通道 (期權 OPTIONS)
                is_structural_breakdown = False
                if asset_class == "OPTIONS":
                    # 期權快速通道：現價貫穿 anchor_base 或 stop_loss 即時判定破位，拒絕等待 15m 實體收盤
                    if anchor_base > 0 and spot < anchor_base:
                        is_structural_breakdown = True
                else:
                    if is_structural_breakdown_pending and gamma_cliff_level > 0:
                        is_structural_breakdown = await is_gamma_cliff_confirmed(
                            symbol, gamma_cliff_level
                        )
                # 2. 主力巨量 STO 實體蓋頂
                is_whale_sto_block = (sqz_mom < 0.0) and (skew < -0.3)
                # 3. 目標區獲利解鎖完成
                is_profit_unlocked = (call_wall > 0 and spot > 0) and (
                    spot >= call_wall or abs(spot - call_wall) / call_wall < 0.015
                )

                # 目標解鎖與極端亢奮 (Euphoria)
                is_euphoria_skew = skew < 0 and skew_percentile <= 20.0
                is_euphoria = is_profit_unlocked or is_euphoria_skew

                # 條件三 (部分)：擺脫高波洗籌泥淖 (IV Crush 威脅)
                is_iv_bubble = ivr > 80.0

                if (
                    is_structural_breakdown
                    or is_whale_sto_block
                    or is_euphoria
                    or is_iv_bubble
                ):
                    next_target = self._find_best_rollover_target()

                    if is_euphoria:
                        user_ctx = get_full_user_context(user_id)
                        # 雙重動能衰竭確認制：
                        # 1. 15m SQZ MOM 由正轉負 (動能拐頭)
                        # 2. Skew 脫離極端狂熱 (Percentile 回升至 30% 以上)
                        is_exhaustion_confirmed = (sqz_mom < 0.0) and (
                            skew_percentile >= 30.0
                        )

                        if user_ctx.can_trade_spreads and is_exhaustion_confirmed:
                            # 90/10 權限資金拆分 - 衰竭確認，建立 Bear Call Spread 反向收租
                            # 90% 轉入新標的
                            report_90 = self._generate_rule_based_rebalance_report(
                                symbol,
                                metrics,
                                system_action="LIQUIDATE",
                                target=next_target,
                                asset_class=asset_class,
                                is_take_profit=True,
                                active_orders=user_orders,
                                position_shares=quantity,
                                current_value=current_value,
                            )
                            rebalance_instructions.append(
                                {
                                    "symbol": symbol,
                                    "action": report_90["final_action"],
                                    "sell_ratio": 0.9
                                    if report_90["final_action"] == "LIQUIDATE"
                                    else (
                                        0.5
                                        if report_90["final_action"] == "REDUCE"
                                        else 0.0
                                    ),
                                    "target_core": report_90["final_target"],
                                    "reason": report_90["markdown_report"],
                                    "suggested_strategy": report_90["options_strategy"],
                                }
                            )
                            # 10% 留存原標的做 Bear Call Spread 反向收租
                            report_10 = self._generate_rule_based_rebalance_report(
                                symbol,
                                metrics,
                                system_action="LIQUIDATE",
                                target=symbol,
                                strategy_override=f"Bear Call Spread (Short Call @ ${call_wall * 1.02:.2f})",
                                asset_class=asset_class,
                                is_take_profit=True,
                                active_orders=user_orders,
                                position_shares=quantity,
                                current_value=current_value,
                            )
                            rebalance_instructions.append(
                                {
                                    "symbol": symbol,
                                    "action": "REDUCE"
                                    if report_10["final_action"]
                                    in ["LIQUIDATE", "REDUCE"]
                                    else "HOLD",
                                    "sell_ratio": 0.1
                                    if report_10["final_action"]
                                    in ["LIQUIDATE", "REDUCE"]
                                    else 0.0,
                                    "target_core": symbol,
                                    "reason": report_10["markdown_report"]
                                    + "\n⚠️ **【動能衰竭確認】SQZ MOM 拐頭且 Skew 降溫，觸發 Bear Call Spread 反向收租 (手動防滑價)**",
                                    "suggested_strategy": report_10["options_strategy"],
                                    "is_manual_override_required": True,
                                }
                            )
                            continue
                        elif user_ctx.can_trade_spreads and not is_exhaustion_confirmed:
                            # 未衰竭 (多頭動能強勁或 Skew 極端狂熱)，嚴禁做空以防 Gamma Squeeze！
                            # 90% 獲利了結轉入新標的，剩餘 10% 啟動 Trailing Stop 移動止盈
                            trailing_stop_level = round(
                                max(call_wall - (0.5 * atr_15m), spot * 0.98), 2
                            )
                            report_90 = self._generate_rule_based_rebalance_report(
                                symbol,
                                metrics,
                                system_action="LIQUIDATE",
                                target=next_target,
                                asset_class=asset_class,
                                is_take_profit=True,
                                active_orders=user_orders,
                                position_shares=quantity,
                                current_value=current_value,
                            )
                            rebalance_instructions.append(
                                {
                                    "symbol": symbol,
                                    "action": report_90["final_action"],
                                    "sell_ratio": 0.9
                                    if report_90["final_action"] == "LIQUIDATE"
                                    else (
                                        0.5
                                        if report_90["final_action"] == "REDUCE"
                                        else 0.0
                                    ),
                                    "target_core": report_90["final_target"],
                                    "reason": report_90["markdown_report"],
                                    "suggested_strategy": report_90["options_strategy"],
                                }
                            )
                            report_10 = self._generate_rule_based_rebalance_report(
                                symbol,
                                metrics,
                                system_action="HOLD",
                                target=symbol,
                                strategy_override=f"Trailing Stop 移動止盈 (防守位: ${trailing_stop_level:.2f})",
                                asset_class=asset_class,
                                is_take_profit=False,
                                active_orders=user_orders,
                                position_shares=quantity,
                                current_value=current_value,
                            )
                            rebalance_instructions.append(
                                {
                                    "symbol": symbol,
                                    "action": "HOLD",
                                    "sell_ratio": 0.0,
                                    "target_core": symbol,
                                    "reason": report_10["markdown_report"]
                                    + f"\n🚀 **【動能延續・移動止盈】**突破 Call Wall 但動能未衰竭 (SQZ MOM {sqz_mom:+.2f} / Skew {skew_percentile:.0f}%)，嚴禁以身擋車做空！剩餘 10% 部位啟動 Trailing Stop (${trailing_stop_level:.2f}) 讓獲利奔馳。",
                                    "suggested_strategy": report_10["options_strategy"],
                                    "is_manual_override_required": False,
                                }
                            )
                            continue

                    # 一般清倉 / 灰階判定
                    report = self._generate_rule_based_rebalance_report(
                        symbol,
                        metrics,
                        system_action="LIQUIDATE"
                        if is_structural_breakdown
                        else "HOLD",
                        target=next_target,
                        asset_class=asset_class,
                        is_take_profit=is_euphoria,
                        active_orders=user_orders,
                        position_shares=quantity,
                        current_value=current_value,
                    )

                    rebalance_instructions.append(
                        {
                            "symbol": symbol,
                            "action": report["final_action"],
                            "sell_ratio": 1.0
                            if report["final_action"] == "LIQUIDATE"
                            else (0.5 if report["final_action"] == "REDUCE" else 0.0),
                            "target_core": report["final_target"],
                            "reason": report["markdown_report"],
                            "suggested_strategy": report["options_strategy"],
                        }
                    )
                    continue  # 已經處理，不需進行後續常規再平衡

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
                        symbol,
                        metrics,
                        system_action="REDUCE",
                        asset_class=asset_class,
                        active_orders=user_orders,
                        position_shares=quantity,
                        current_value=current_value,
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
