from typing import Any, Dict, Optional

from . import logger
from .models import FundamentalThesisResult

# Per-form-type prompt supplement, appended AFTER the shared 4-criteria
# framework + exclusion rule. Empty/unknown form_type -> no supplement
# (byte-identical to the original filing-type-agnostic prompt).
_FORM_TYPE_PROMPT_NOTES: Dict[str, str] = {
    "10-K": (
        "\n### 📄 Filing Context: Annual Report (10-K)\n"
        "This is a comprehensive ANNUAL filing. Weight full-year trends, "
        "year-over-year structural shifts, and management's full-year "
        "outlook commentary more heavily than any single quarter's noise. "
        "A 10-K's Risk Factors and MD&A sections typically reflect "
        "management's most deliberate, board-reviewed disclosure — treat "
        "explicit risk-factor additions/removals as strong signal.\n"
    ),
    "10-Q": (
        "\n### 📄 Filing Context: Quarterly Report (10-Q)\n"
        "This is a QUARTERLY filing. Quarterly results carry more noise "
        "than annual ones — apply the STRICT EXCLUSION RULE with extra "
        "rigor here. A single quarter's miss, seasonal softness, or "
        "one-off charges should NOT alone trigger is_broken=true. Only "
        "flag the thesis as broken if the quarterly data reveals a "
        "*trend* (e.g., sequential margin compression across quarters, "
        "repeated guidance cuts) rather than an isolated data point.\n"
    ),
    "8-K": (
        "\n### 📄 Filing Context: Current Report (8-K — Event-Driven)\n"
        "This is an EVENT-DRIVEN filing triggered by a specific material "
        "event, NOT a periodic financial report. It will NOT contain "
        "MD&A or comprehensive financial narrative. Judge materiality "
        "primarily from WHICH Item(s) are present (see 'Key Events' "
        "appendix below):\n"
        "- HIGH SIGNAL for structural thesis-break: Item 2.05 (exit/impairment "
        "of a business), Item 2.02 combined with a guidance cut, Item 4.02 "
        "(non-reliance on prior financials / restatement), Item 5.02 "
        "(abrupt departure of CEO/CFO — especially if 'for cause' or "
        "unplanned), Item 1.01/1.02 (loss/termination of a material "
        "contract).\n"
        "- LOW SIGNAL / usually NOT thesis-breaking on its own: Item 7.01 "
        "(Reg FD disclosure, often routine investor materials), Item 8.01 "
        "(other events, often administrative), routine Item 5.02 "
        "(planned retirement, board refresh).\n"
        "If the 8-K reports a single event without broader corroborating "
        "context, lean toward LOWER confidence unless the event itself is "
        "self-evidently structural (e.g., a restatement or a core-business "
        "divestiture).\n"
    ),
}

_SECTION_LABELS: Dict[str, str] = {
    "forward_guidance": "Forward Guidance",
    "margin_data": "Margin & Cost Structure",
    "market_share_and_customer": "Market Share & Customer Dynamics",
    "quarterly_financials": "Quarterly Financial Results",
    "operational_disruption": "Operational Disruption",
    "key_events": "Key Events (8-K Item Triggers)",
}


def _format_sections_appendix(sections: Optional[Dict[str, str]]) -> str:
    """Render the structured `sections` dict as a labeled appendix for the
    user prompt. Returns "" if sections is empty/None, keeping the user
    prompt byte-identical to before when no structured data is available
    (e.g. the news_context-only /verify_thesis path)."""
    if not sections:
        return ""

    blocks = [
        f"#### {label}\n{sections[key]}"
        for key, label in _SECTION_LABELS.items()
        if sections.get(key)
    ]
    if not blocks:
        return ""

    return (
        "\n\n### 📎 Structured Filing Appendix (auto-extracted, may be partial):\n"
        + "\n\n".join(blocks)
    )


async def evaluate_fundamental_thesis_impl(
    client: Any,
    is_memory_safe: Any,
    llm_model_name: str,
    symbol: str,
    fundamental_text: str,
    form_type: str = "",
    sections: Optional[Dict[str, str]] = None,
) -> Optional[FundamentalThesisResult]:
    """
    邏輯 (1): 原型假設破滅
    傳入 FastAPI 爬取的法說會或財報文本，使用 LLM 判定基本面護城河是否流失。
    `form_type` (10-K/10-Q/8-K) 客製化分析框架補充說明；`sections` 為
    edge scraper 結構化擷取的段落，會以附錄形式併入 user prompt。兩者皆為
    選填，留空時 prompt 與未區分格式前完全一致。
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
    ) + _FORM_TYPE_PROMPT_NOTES.get(form_type, "")

    user_prompt = (
        f"Please analyze the following latest earnings report and conference call highlights for {symbol}.\n\n"
        f"Context:\n{fundamental_text}"
        f"{_format_sections_appendix(sections)}"
    )

    try:
        response = await client.beta.chat.completions.parse(
            model=llm_model_name,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {"role": "user", "content": user_prompt},
            ],
            response_format=FundamentalThesisResult,
        )
        parsed = response.choices[0].message.parsed

        # 寫入 SQLite 全域防禦閘門
        if parsed:
            from database.market_cache import save_fundamental_cache

            save_fundamental_cache(
                symbol, parsed.is_broken, parsed.confidence, parsed.reasoning
            )

        if isinstance(parsed, FundamentalThesisResult):
            return parsed
        return None
    except Exception as e:
        logger.error(f"[{symbol}] Fundamental thesis evaluation failed: {e}")
        return None
