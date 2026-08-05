"""SEC Filing Structured Section Extractor.

Extracts 5 categories of structured data from 10-Q / 10-K / 8-K filing text:
1. Forward Guidance — revenue/earnings guidance, revisions, withdrawals
2. Margin & Cost Structure — gross/operating margin, cost of revenue, pricing power
3. Market Share & Customer Dynamics — churn, contract termination, competitive displacement
4. Quarterly Financial Results — revenue figures, earnings, YoY comparisons
5. Operational Disruption — supply chain, data center, power, deployment delays
"""

import re
from dataclasses import dataclass


# Maximum characters per extracted section to prevent token explosion
SECTION_CHAR_LIMIT = 5000


@dataclass
class ExtractedSections:
    """Container for structured sections extracted from SEC filings."""

    forward_guidance: str = ""
    margin_data: str = ""
    market_share_and_customer: str = ""
    quarterly_financials: str = ""
    operational_disruption: str = ""

    def to_dict(self) -> dict[str, str]:
        """Serialize to API response format, omitting empty sections."""
        result: dict[str, str] = {}
        for field_name in [
            "forward_guidance",
            "margin_data",
            "market_share_and_customer",
            "quarterly_financials",
            "operational_disruption",
        ]:
            value = getattr(self, field_name)
            if value:
                result[field_name] = value
        return result


# ---------------------------------------------------------------------------
# Keyword anchor patterns — tuned against real SPCX (SpaceX) 10-Q
# ---------------------------------------------------------------------------

_FORWARD_GUIDANCE_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)(forward[- ]looking|guidance|outlook|expect(?:s|ed|ation)|"
        r"forecast|project(?:s|ed|ion)|full[- ]year|next\s+(?:quarter|fiscal)|"
        r"anticipate[ds]?|rais(?:e[ds]?|ing)\s+(?:guidance|outlook)|"
        r"lower(?:ed|ing)\s+(?:guidance|outlook|forecast)|"
        r"withdraw(?:n|ing)?\s+(?:guidance|outlook)|"
        r"revis(?:e[ds]?|ing)\s+(?:guidance|outlook|estimate))"
    ),
]

_MARGIN_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)(gross\s+margin|operating\s+margin|net\s+margin|"
        r"cost\s+of\s+(?:revenue|goods\s+sold|sales)|"
        r"pricing\s+(?:power|pressure)|"
        r"margin\s+(?:compress|expan|contract|improv|declin|erosion)|"
        r"cost\s+structure|selling.{1,5}general.{1,5}admin|"
        r"research\s+and\s+development|operat(?:ing|ion)\s+expense|"
        r"expense\s+ratio)"
    ),
]

_MARKET_SHARE_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)(market\s+share|customer\s+(?:churn|loss|attrition|retention)|"
        r"contract\s+(?:non-?renewal|cancellation|termination)|"
        r"competi(?:tor|tion|tive)\s+(?:pressure|displacement|landscape)|"
        r"lost\s+(?:customer|client|account|contract)|"
        r"subscriber\s+(?:loss|decline|churn)|"
        r"(?:order|backlog)\s+(?:cancel|reduc|decline))"
    ),
]

_QUARTERLY_FINANCIALS_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)((?:total\s+)?(?:net\s+)?revenue[s]?\s+(?:were|was|of|increased|decreased|grew|\$)|"
        r"(?:net\s+)?(?:income|loss|earnings)\s+(?:were|was|of|per\s+share|\$)|"
        r"earnings\s+per\s+(?:diluted\s+)?share|"
        r"(?:year|quarter)-over-(?:year|quarter)|"
        r"compared\s+to\s+(?:the\s+)?(?:same|prior|previous)\s+(?:period|quarter|year)|"
        r"revenue\s+growth|"
        r"loss\s+from\s+operations|"
        r"(?:diluted|basic)\s+(?:eps|earnings))"
    ),
]

_OPERATIONAL_DISRUPTION_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)(supply\s+chain\s+(?:disrupt|constraint|bottleneck|shortage)|"
        r"data\s+center\s+(?:capacity|constraint|delay|expansion)|"
        r"power\s+(?:supply|constraint|shortage|capacity)|"
        r"deployment\s+(?:delay|constraint|challenge)|"
        r"(?:production|manufacturing)\s+(?:delay|disruption|constraint)|"
        r"(?:component|chip|semiconductor)\s+(?:shortage|constraint|supply)|"
        r"capital\s+expenditure[s]?|capex)"
    ),
]


def _extract_context_around_matches(
    text: str,
    patterns: list[re.Pattern[str]],
    context_chars: int = 800,
    max_total_chars: int = SECTION_CHAR_LIMIT,
) -> str:
    """Extract text snippets surrounding keyword matches.

    For each keyword match, captures `context_chars` characters before and
    after. Deduplicates overlapping windows and joins with section dividers.
    Truncates to `max_total_chars`.
    """
    match_positions: list[tuple[int, int]] = []
    for pattern in patterns:
        for m in pattern.finditer(text):
            start = max(0, m.start() - context_chars)
            end = min(len(text), m.end() + context_chars)
            match_positions.append((start, end))

    if not match_positions:
        return ""

    # Merge overlapping ranges
    match_positions.sort()
    merged: list[tuple[int, int]] = [match_positions[0]]
    for start, end in match_positions[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    # Extract and join
    snippets: list[str] = []
    total_chars = 0
    for start, end in merged:
        snippet = text[start:end].strip()
        if total_chars + len(snippet) > max_total_chars:
            remaining = max_total_chars - total_chars
            if remaining > 200:
                snippets.append(snippet[:remaining] + "…")
            break
        snippets.append(snippet)
        total_chars += len(snippet)

    return "\n---\n".join(snippets)


def extract_sections(full_text: str) -> ExtractedSections:
    """Extract structured sections from SEC filing plain text.

    Applies keyword-based contextual extraction for each of the 5
    target categories. Returns an ExtractedSections dataclass.
    """
    return ExtractedSections(
        forward_guidance=_extract_context_around_matches(
            full_text, _FORWARD_GUIDANCE_KEYWORDS
        ),
        margin_data=_extract_context_around_matches(full_text, _MARGIN_KEYWORDS),
        market_share_and_customer=_extract_context_around_matches(
            full_text, _MARKET_SHARE_KEYWORDS
        ),
        quarterly_financials=_extract_context_around_matches(
            full_text, _QUARTERLY_FINANCIALS_KEYWORDS
        ),
        operational_disruption=_extract_context_around_matches(
            full_text, _OPERATIONAL_DISRUPTION_KEYWORDS
        ),
    )
