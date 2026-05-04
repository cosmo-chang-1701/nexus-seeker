import os
import json
import logging
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from typing import Literal

from config import LLM_API_BASE, LLM_MODEL_NAME, API_KEY

logger = logging.getLogger(__name__)

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
    report_content: str = Field(description="完整的分析報告內容 (Markdown 格式)，必須維持原本的標題與分隔線")

class PolymarketAnalysis(BaseModel):
    event_background: str = Field(description="簡短的事件背景，說明該預測市場在賭什麼")
    whale_logic: str = Field(description="分析此筆大額交易可能的動機、對沖行為或內線情報推測")
    market_sentiment: str = Field(description="目前市場的整體情緒、賠率分布與預期偏差")
    one_line_verdict: str = Field(description="一句話的核心總結，必須包含看多/看空的方向性結論")

async def evaluate_trade_risk(symbol: str, strategy: str, news_context: str, reddit_context: str) -> dict:
    """
    呼叫 LLM 進行 NLP 新聞毒性分析與風控審查
    """
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
        response = await client.responses.parse(
            model=LLM_MODEL_NAME,
            instructions=system_prompt, 
            input=user_prompt,
            text_format=RiskAssessment
        )
        
        result = response.output_parsed
        tags_str = " ".join([f"[{tag}]" for tag in result.tags])
        formatted_reasoning = f"🏷️ 標籤：{tags_str}\n📝 理由：{result.reasoning}"
        return {
            "decision": result.decision,
            "reasoning": formatted_reasoning
        }

    except Exception as e:
        logger.error(f"[{symbol}] LLM 伺服器連線或推論失敗: {e}")
        # Fail-Open 策略
        return {"decision": "APPROVE", "reasoning": f"AI 伺服器離線或異常，預設放行: {str(e)}"}

async def generate_analyst_report(report_type: str, raw_data: dict) -> str:
    """
    將量化資料餵給 LLM，生成口語化且專業的分析報告。
    """
    system_prompt = """
    You are a Wall Street Quantitative Analyst Agent for Nexus Seeker.
    Your task is to take raw quantitative data and output a concise, professional, and insightful market report in Traditional Chinese (繁體中文).
    The report should be formatted in Markdown, strictly keeping the specified header format for the given report type.
    Do not invent numbers, only use the provided raw_data.
    Keep the tone extremely cold, objective, and analytical.
    """

    user_prompt = f"Report Type: {report_type}\nRaw Data: {json.dumps(raw_data, ensure_ascii=False)}\nGenerate the report."

    try:
        response = await client.responses.parse(
            model=LLM_MODEL_NAME,
            instructions=system_prompt,
            input=user_prompt,
            text_format=AnalystReport
        )
        return response.output_parsed.report_content
    except Exception as e:
        logger.error(f"Failed to generate analyst report: {e}")
        return f"**{report_type}**\n--------------------------------------------------\n⚠️ LLM 生成失敗或伺服器離線: {str(e)}"

async def generate_polymarket_summary(market_info: dict, trade_data: dict, usd_value: float, trade_details: dict = None) -> str:
    """
    針對 Polymarket 巨鯨交易生成高度結構化的 Markdown 分析報告。
    """
    intent_desc = trade_details.get('intent', '未知意圖') if trade_details else '未知意圖'
    sentiment_tag = "看多 (Bullish)" if (trade_details and trade_details.get('is_bullish')) else "看空 (Bearish)"
    
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
        response = await client.responses.parse(
            model=LLM_MODEL_NAME,
            instructions=system_prompt,
            input=user_prompt,
            text_format=PolymarketAnalysis
        )
        
        # 使用 Markdown 優化輸出格式
        res = response.output_parsed
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
