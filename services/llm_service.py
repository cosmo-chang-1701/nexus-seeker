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
    reasoning: str = Field(
        description="用繁體中文簡要說明判斷理由 (50字以內)"
    )

async def evaluate_trade_risk(symbol: str, strategy: str, news_context: str) -> dict:
    """
    呼叫 LLM 進行 NLP 新聞毒性分析與風控審查
    """
    system_prompt = """
    ## Role
    You are the Chief Risk Officer (CRO) of a premier Wall Street quantitative hedge fund. Your expertise lies in identifying "Structural Breaks" and "Tail Risks" that traditional statistical models fail to capture.

    ## Objective
    Review option position proposals submitted by quantitative models. Your primary task is to determine if the current news environment renders the model’s historical volatility assumptions invalid.

    ## Risk Decision Logic
    1.  **VETO (Immediate Rejection)**:
        * **Trigger**: Non-linear "Black Swan" events. This includes accounting fraud, SEC investigations, bankruptcy/default risks, major litigation, or the abrupt resignation of key executives (CEO/CFO).
        * **Logic**: These events cause price gaps and extreme volatility spikes that invalidate historical statistical distributions. The model's risk parameters are likely compromised.

    2.  **APPROVE (Permission to Trade)**:
        * **Trigger**: Standard market noise. This includes macro data releases (CPI, Non-farm Payrolls), routine product launches, general industry competition, or standard analyst rating changes.
        * **Logic**: These risks are considered "priced-in" or within the model's expected volatility regime.

    3.  **Strategy-Specific Sensitivity**:
        * **Buyer (BTO/Long Gamma)**: Higher tolerance for volatility. Veto only if the event poses a fundamental threat to the company’s existence or market liquidity.
        * **Seller (STO/Short Gamma)**: Extreme sensitivity to tail risk. Veto if there is any sign of unpredictable non-linear volatility.

    ## Output Constraints
    - You must strictly adhere to the provided JSON schema.
    - **Field `reasoning` must be written in Traditional Chinese (繁體中文)** and limited to 50 words, focusing on the core risk factor.
    """

    user_prompt = f"""
    ### Trade Proposal for Review
    - **Underlying**: {symbol}
    - **Strategy**: {strategy}
    - **Market Context / Recent News**:
    ---
    {news_context}
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
        return result.model_dump()

    except Exception as e:
        logger.error(f"[{symbol}] LLM 伺服器連線或推論失敗: {e}")
        # Fail-Open 策略
        return {"decision": "APPROVE", "reasoning": f"AI 伺服器離線或異常，預設放行: {str(e)}"}