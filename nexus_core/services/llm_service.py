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

class AnalystReport(BaseModel):
    report_content: str = Field(description="完整的分析報告內容 (Markdown 格式)，必須維持原本的標題與分隔線")

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
    針對 Polymarket 巨鯨交易生成背景總結與情緒分析。
    """
    intent_desc = trade_details.get('intent', '未知意圖') if trade_details else '未知意圖'
    sentiment_tag = "看多 (Bullish)" if (trade_details and trade_details.get('is_bullish')) else "看空 (Bearish)"
    
    system_prompt = f"""
    你是一位專業的預測市場分析師。請針對 Polymarket 的大額交易（巨鯨交易）提供簡短、專業的背景總結與市場情緒分析。
    請使用繁體中文 (Traditional Chinese)。
    
    **重要規則：**
    1. 預測市場的邏輯與一般股市不同：買入 "NO" 代幣代表押注該事件「不會發生」，因此是「看空」該事件。
    2. 價格越接近 1.0 代表該選項發生的機率越高。
    3. 此筆交易的量化判定為：{intent_desc}，市場情緒：{sentiment_tag}。請務必基於此判定進行分析。
    
    報告應包含：
    1. 事件背景簡述。
    2. 此筆大額交易可能的動機或市場意義。
    3. 當前的市場情緒評估。
    總字數請控制在 150 字以內，語氣專業且客觀。
    """

    user_prompt = f"""
    ### Polymarket Trade Detected
    - **Market Question**: {market_info.get('question', 'Unknown')}
    - **Market Description**: {market_info.get('description', 'No description available')}
    - **Trade Side**: {trade_data.get('side', 'Unknown')} (已對應至選項: {market_info.get('outcome', 'YES/NO')})
    - **Trade Price**: {trade_data.get('price', 0)} (代表該選項發生的市場機率)
    - **Total USD Value**: ${usd_value:,.2f}
    - **Quant Engine Intent**: {intent_desc}
    ---
    請根據以上資訊生成分析報告，請確保分析內容與量化引擎判定的「{sentiment_tag}」方向一致。
    """

    try:
        response = await client.responses.parse(
            model=LLM_MODEL_NAME,
            instructions=system_prompt,
            input=user_prompt,
            text_format=AnalystReport
        )
        return response.output_parsed.report_content
    except Exception as e:
        logger.error(f"Failed to generate polymarket summary: {e}")
        return "⚠️ 無法生成 AI 總結，請參考原始交易數據。"