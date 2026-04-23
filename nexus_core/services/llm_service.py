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