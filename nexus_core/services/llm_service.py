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
            # 備註：若您使用的 vLLM 版本較舊，未完整支援新版 API 或 json_schema，
            # 則需改用 vLLM 特有的 extra_body 參數
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