import json
import logging
import psutil
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal

from config import LLM_API_BASE, LLM_MODEL_NAME, API_KEY

logger = logging.getLogger(__name__)

# 記憶體安全閾值 (85%)
MEMORY_SAFETY_THRESHOLD = 85.0


def is_memory_safe() -> bool:
    """檢查系統記憶體是否高於安全閾值。"""
    mem = psutil.virtual_memory()
    return mem.percent < MEMORY_SAFETY_THRESHOLD


# ==========================================
# ⚙️ LLM Inference Server 連線設定
# ==========================================
client_args = {}

if API_KEY:
    client_args["api_key"] = API_KEY
if LLM_API_BASE:
    client_args["base_url"] = LLM_API_BASE
client = AsyncOpenAI(**client_args)


# ==========================================
# 📊 Pydantic Schema 定義 (Structured Output)
# ==========================================
class RiskAssessment(BaseModel):
    model_config = ConfigDict()
    decision: Literal["APPROVE", "VETO"] = Field(
        description="風控裁決結果：APPROVE (批准) 或 VETO (否決)"
    )
    tags: list[str] = Field(
        description="萃取 2 到 3 個極度精簡的風控關鍵字標籤，例如：['常規雜音', 'Long Gamma', '無黑天鵝風險']"
    )
    reasoning: str = Field(
        description="一句話的終極風控結論 (請控制在 30 字以內，極度冷靜客觀)"
    )


class AnalystReport(BaseModel):
    model_config = ConfigDict()
    report_content: str = Field(
        description="完整的分析報告內容 (Markdown 格式)，必須維持原本的標題與分隔線"
    )


class PolymarketAnalysis(BaseModel):
    model_config = ConfigDict()
    event_background: str = Field(description="簡短的事件背景，說明該預測市場在賭什麼")
    whale_logic: str = Field(
        description="分析此筆大額交易可能的動機、對沖行為或內線情報推測"
    )
    market_sentiment: str = Field(description="目前市場的整體情緒、賠率分布與預期偏差")
    one_line_verdict: str = Field(
        description="一句話的核心總結，必須包含看多/看空的方向性結論"
    )


class UOAIntentMapping(BaseModel):
    model_config = ConfigDict()
    classification: Literal[
        "Institutional Hedging",
        "Speculative Directional Betting",
        "Arbitrage",
        "Unknown",
    ] = Field(description="活動分類")
    confidence: float = Field(description="信心指數 (0.0 - 1.0)")
    explanation: str = Field(description="簡短解釋分類理由 (繁體中文)")


class WatchlistRoundupCommentary(BaseModel):
    model_config = ConfigDict()
    commentary: str = Field(
        description="針對本輪 watchlist 多標的重點的簡短總覽，必須使用繁體中文並控制在 180 字內"
    )


async def classify_uoa_intent(
    symbol: str, uoa_data: dict, whale_intent: str = None
) -> dict:
    """
    結合 UOA 數據與 Polymarket 巨鯨意圖，判定異常活動性質。
    """
    if not is_memory_safe():
        logger.warning("🚨 [記憶體警報] 系統資源不足，跳過 UOA LLM 分析。")
        return {
            "classification": "Unknown",
            "confidence": 0,
            "explanation": "系統記憶體負載過高，已自動降級。",
        }

    system_prompt = """
    你是 Nexus Seeker 的異常期權活動分析專家。
    請分析給定的 UOA (Unusual Option Activity) 數據，並結合 Polymarket 巨鯨的意圖 (若有提供)，判定該交易的性質。
    你必須使用繁體中文 (Traditional Chinese) 填寫所有非枚舉的回傳欄位 (例如 explanation)，並遵循台灣期權交易術語。
    """

    user_prompt = f"標的: {symbol}\nUOA 數據: {json.dumps(uoa_data, ensure_ascii=False)}\n巨鯨意圖: {whale_intent or '無'}"

    try:
        response = await client.beta.chat.completions.parse(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=UOAIntentMapping,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError("Parsed result is None")
        return parsed.model_dump()
    except Exception as e:
        logger.error(f"UOA 分類失敗: {e}")
        return {
            "classification": "Unknown",
            "confidence": 0,
            "explanation": "AI 分析不可用",
        }


async def evaluate_trade_risk(
    symbol: str, strategy: str, news_context: str, reddit_context: str
) -> dict:
    """
    呼叫 LLM 進行 NLP 新聞毒性分析與風控審查
    """
    if not is_memory_safe():
        logger.warning("🚨 [記憶體警報] 系統資源不足，跳過風控 LLM 分析。")
        return {
            "decision": "APPROVE",
            "tags": ["資源受限"],
            "reasoning": "系統記憶體高負載，自動通過風控審查以確保核心運行。",
        }
    system_prompt = """
    ## Role & Objective
    You are a Quant Hedge Fund CRO. Evaluate option proposals by cross-referencing official news and Reddit sentiment (titles + consensus scores) to prevent tail risks.

    ## Decision Logic
    1. **VETO (Reject)**:
       - **Black Swans**: Fraud, SEC probes, bankruptcy, or executive departures.
       - **Retail Mania**: High Reddit consensus scores indicating FOMO or Short Squeezes. Strictly VETO Seller strategies (STO/Short Gamma) due to explosive IV risk.
    2. **APPROVE (Pass)**:
       - **Market Noise**: Macro data, routine product news, analyst ratings.
       - **Buyer Strategies (BTO/Long Gamma)**: Can tolerate or benefit from high Reddit volatility.

    ## Output Constraints
    - Strictly follow the JSON schema.
    - `reasoning` MUST be in Traditional Chinese (繁體中文), max 50 words. Focus strictly on core risks.
    - Use Taiwan options terminology: Call = "買權", Put = "賣權" (Never use 認購/認沽).
    """

    user_prompt = f"""
    ### Trade Proposal for Review
    - **Underlying**: {symbol}
    - **Strategy**: {strategy}
    ---

    - **Recent News**:
    {news_context}

    ---

    - **Reddit Context**:
    {reddit_context}
    ---

    **Instruction**: Perform a risk audit based on the CRO guidelines and return the adjudication in the required structural format.
    """

    try:
        response = await client.beta.chat.completions.parse(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=RiskAssessment,
        )

        result = response.choices[0].message.parsed
        if result is None:
            raise ValueError("Parsed result is None")
        tags_str = " ".join([f"[{tag}]" for tag in result.tags])
        formatted_reasoning = f"🏷️ 標籤：{tags_str}\n📝 理由：{result.reasoning}"
        return {"decision": result.decision, "reasoning": formatted_reasoning}

    except Exception as e:
        logger.error(f"[{symbol}] LLM 伺服器連線或推論失敗: {e}")
        # Fail-Open 策略
        return {
            "decision": "APPROVE",
            "reasoning": f"AI 伺服器離線或異常，預設放行: {str(e)}",
        }


async def generate_analyst_report(report_type: str, raw_data: dict) -> str:
    """
    將量化資料餵給 LLM，生成口語化且專業的分析報告。
    """
    system_prompt = """
    You are a Wall Street Quantitative Analyst Agent for Nexus Seeker.
    Your task is to take raw quantitative data and output a concise, professional, and insightful market report in 100% fluent, finance-grade Traditional Chinese (繁體中文) using Taiwanese market terminology.

    ### ⚠️ MANDATORY LANGUAGE & TERMINOLOGY CONSTRAINT
    No matter how the raw data or inputs are labeled, you MUST use the following Traditional Chinese (Taiwanese style) options terminology:
    - Use "選擇權" (Options)
    - Use "履約價" (Strike)
    - Use "權利金" (Premium)
    - Use "價差期權/價差策略" (Spreads)
    - Use "隱含波動率" (Implied Volatility)
    - Use "乖離率" (Deviation)
    - Do not use simplified terms like "期權", "執行價", "期權費", etc.

    ### 📐 REQUIRED FORMAT & HEADERS
    The report MUST be structured using the following exact Markdown headers and formatting:
    1. 📊 多空大盤交叉驗證解讀
    2. ⚠️ 潛在陷阱與風險提示
    3. 🛡️ 高勝率交易策略推薦

    ### 🧮 MATHEMATICAL CROSS-VALIDATION RULES
    You must mathematically cross-reference the input data:
    1. **IV Bubble Validation**: If Technical Overheating occurs (e.g. Price/MA20 Deviation/乖離率 > 10% or RSI > 65) while `IV Rank > 90%` AND `days_to_earnings > 20`, you MUST explicitly flag an artificial IV bubble ("人工隱含波動率泡沫") and strictly avoid recommending single-leg long options ("單邊買入選擇權" e.g., 買入買權/賣權). Recommend defined-risk spreads or defensive actions instead.
    2. **Market Divergence Validation**: If `Option Skew` is negative (meaning Calls are expensive, skew < 0, showing speculative retail/momentum buying) but `Put/Call Ratio (PCR) > 1.5` (Heavy Put volume, indicating institutional hedging), you MUST explain this as retail momentum vs. institutional hedging ("散戶動能與機構避險的背離").

    ### Specific Instructions for "盤後交易與每日總結" (Post-market Summary):
    If the report type contains "盤後交易與每日總結", you MUST include the following in your analysis under the headers above:
    - **🏁 財務生存跑道 (Financial Runway)**: Use aggregate_risk_metrics.avg_financial_runway_days. If >= 9999, describe as "無限 (收益已覆蓋支出)".
    - **📦 當日盈虧歸因 (PnL Attribution)**: Use brinson_attribution_proxy data.
    - **🛡️ 風控管線評估與對沖決策**: Analyze macro_snapshot (VIX) and aggregate_risk_metrics (Delta, Heat).
    - **🧬 系統狀態與 STHE 優化**: Brief status of the system based on sector_correlation and volatility.

    ### Specific Instructions for "盤後綜合風險與 AI 策略報告" (Post-market Intelligence):
    If the report type contains "盤後綜合風險與 AI 策略報告", you MUST include the following in your analysis under the headers above:
    - **🏁 財務生存跑道 (Financial Runway)**: Use aggregate_risk_metrics.avg_financial_runway_days. If >= 9999, describe as "無限 (收益已覆蓋支出)".
    - **📦 當日盈虧歸因 (PnL Attribution)**: Use brinson_attribution_proxy data.
    - **⚙️ 行業板塊資金輪動 (Sector Rotation)**: Analyze the sectors data (pct_change, rel_vol, skew).
    - **🛡️ 盤後風險對沖決策**: Analyze Delta/Theta exposure and margin utilization.
    - **🎯 次日交易策略模板 (Next Day Strategy)**: Generate specific tactical guidelines for tomorrow, including defense/resistance zones (防線區間) and execution trigger conditions (觸發條件).

    ### Specific Instructions for "盤前財報與估值調整" (Pre-market Earnings):
    If the report type contains "盤前財報與估值調整", you MUST include the following in your analysis under the headers above:
    - **🧬 財報影響力評估 (Impact Assessment)**: 根據即將發布財報的標的，分析其對所屬板塊的潛在波動傳導。
    - **🧪 估值調整與期望值 (Valuation & Expectation)**: 討論市場目前的預期是否過高或過低，以及隱含波動率 (IV) 的合理性。
    - **🎯 戰術建議 (Tactical Advice)**: 給出具體的交易策略建議 (例如：跨式、勒式或中性對沖)。

    ### ⚠️ OUTPUT FORMATTING & LAYOUT RULES (CRITICAL)
    1. **No ANSI Escape Code Residuals**: Do not include any Linux terminal formatting codes like "[0;31m", "[0;32m", "[0m", etc. Under no circumstances should these color codes appear in your response. Use standard Markdown or Discord Emojis (e.g. 🚨, 🟢) to highlight data or alerts.
    2. **Strict tree structure limitation**: Do not use tree branch symbols like "├─", "└─", "│", "──" in your analysis, commentary, or recommended strategies. Standard Markdown bullet points ("*" or "-") must be used for listings to ensure proper mobile rendering.

    Do not invent numbers, only use the provided raw_data.
    Keep the tone extremely cold, objective, and analytical.
    """

    user_prompt = f"Report Type: {report_type}\nRaw Data: {json.dumps(raw_data, ensure_ascii=False)}\nGenerate the report."

    try:
        response = await client.beta.chat.completions.parse(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=AnalystReport,
        )
        result = response.choices[0].message.parsed
        if result is None:
            raise ValueError("Parsed result is None")
        return result.report_content
    except Exception as e:
        logger.error(f"Failed to generate analyst report: {e}")
        return f"**{report_type}**\n--------------------------------------------------\n⚠️ LLM 生成失敗或伺服器離線: {str(e)}"


async def generate_watchlist_roundup_commentary(raw_data: dict) -> str:
    """
    程式化（規則引擎）替代自選股組合本輪全局總覽，0 延遲，100% 穩定。
    """
    symbols_data = raw_data.get("symbols", [])
    total = len(symbols_data)
    if total == 0:
        return "本輪無可用 watchlist 評估結果。"

    red_items = [item for item in symbols_data if item.get("alert_level") == "red"]
    yellow_items = [
        item for item in symbols_data if item.get("alert_level") == "yellow"
    ]

    roundup_parts = []
    roundup_parts.append(f"📡 【本輪自選股全局診斷】共掃描 `{total}` 檔標的。")

    if red_items:
        red_syms = ", ".join([str(item.get("symbol")) for item in red_items])
        roundup_parts.append(
            f"🔴 **高危警報 (紅燈 {len(red_items)} 檔)**：`{red_syms}` 已擊穿支撐防線！系統已啟動 Hard-Hedge 與緊急 SPY 對沖。請「優先處置」這些標的，做好現貨鎖利或保護性 Put。"
        )

    if yellow_items:
        yellow_syms = ", ".join([str(item.get("symbol")) for item in yellow_items])
        roundup_parts.append(
            f"🟡 **收租機會 (黃燈 {len(yellow_items)} 檔)**：`{yellow_syms}` 處於 Phase 1 支撐且 IV 偏高。推薦在買點賣出 CSP 收集溢價，勝率高於現股追高。"
        )

    if not red_items and not yellow_items:
        roundup_parts.append(
            "🟢 **全局安全**：所有標的均處於常規防禦區間，多頭大後方安全穩固，無須任何緊急對沖，耐心等待買點陷阱觸發。"
        )

    # 檢測是否有重大事件
    event_symbols = []
    for item in symbols_data:
        sym = item.get("symbol", "")
        ev = item.get("event_risk_summary", "")
        if ev and "未偵測到" not in ev and "無重大事件" not in ev:
            # 取得前段事件名稱簡化呈現
            ev_name = ev.split("｜")[0] if "｜" in ev else ev
            event_symbols.append(f"{sym}({ev_name})")

    if event_symbols:
        roundup_parts.append(
            f"🗓️ **重大事件追蹤**：`{', '.join(event_symbols)}`，注意事件前波動率 Crush 與跳空風險。"
        )

    return "\n\n".join(roundup_parts)[:250]


async def generate_polymarket_summary(
    market_info: dict, trade_data: dict, usd_value: float, trade_details: dict = None
) -> str:
    """
    針對 Polymarket 巨鯨交易生成高度結構化的 Markdown 分析報告。
    """
    intent_desc = (
        trade_details.get("intent", "未知意圖") if trade_details else "未知意圖"
    )
    sentiment_tag = (
        "看多 (Bullish)"
        if (trade_details and trade_details.get("is_bullish"))
        else "看空 (Bearish)"
    )

    system_prompt = f"""
    你是一位華爾街頂尖預測市場策略師。
    請針對 Polymarket 的大額交易提供深度、結構化的 Markdown 分析。

    ## 核心邏輯架構：
    1. **背景 (Background)**：用一句話點出該事件的當前核心矛盾。
    2. **巨鯨邏輯 (Logic)**：分析該資金是在「下注方向」還是在「套利/對沖」。大額買入 "NO" 代表該巨鯨認為事件「極高機率不會發生」。
    3. **情緒與偏離 (Sentiment)**：目前的市場價格是否過度樂觀或悲觀？
    4. **結論 (Verdict)**：給出明確的方向性定論。

    ## 寫作風格：
    - 使用**繁體中文 (Traditional Chinese)**。
    - 語氣必須冷靜、專業、避免廢話。
    - **嚴格遵守量化引擎判定**：此筆交易判定為「{sentiment_tag}」，你的分析必須支持此結論。
    """

    user_prompt = f"""
    ### 原始交易數據
    - **事件標題**: {market_info.get('question', 'Unknown')}
    - **事件描述**: {market_info.get('description', 'No description available')}
    - **交易動作**: {trade_data.get('side', 'Unknown')} {market_info.get('outcome', 'YES/NO')}
    - **成交價格**: {trade_data.get('price', 0)} (隱含機率)
    - **交易總值**: ${usd_value:,.2f}
    - **量化意圖**: {intent_desc}
    """

    try:
        response = await client.beta.chat.completions.parse(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=PolymarketAnalysis,
        )

        # 使用 Markdown 優化輸出格式
        res = response.choices[0].message.parsed
        if res is None:
            return "⚠️ 無法解析 AI 結構化總結，請參考原始交易數據。"

        formatted_md = (
            f"> **背景摘要**\n> {res.event_background}\n\n"
            f"💡 **巨鯨動機分析**\n- {res.whale_logic}\n\n"
            f"📊 **市場情緒評估**\n- {res.market_sentiment}\n\n"
            f"📌 **核心結論**\n**{res.one_line_verdict}**"
        )
        return formatted_md

    except Exception as e:
        logger.error(f"Failed to generate structured polymarket summary: {e}")
        return "⚠️ 無法生成 AI 結構化總結，請參考原始交易數據。"
