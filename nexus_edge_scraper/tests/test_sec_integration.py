"""Integration test: Live SPCX (SpaceX) 10-Q structured extraction.

Source: https://www.sec.gov/Archives/edgar/data/1181412/000162828026052535/spcx-20260630.htm
Run: pytest tests/test_sec_integration.py -m integration -v -s
"""

import re

import pytest
import httpx
from bs4 import BeautifulSoup

from section_extractor import extract_sections

SPCX_FILING_URL = (
    "https://www.sec.gov/Archives/edgar/data/1181412/"
    "000162828026052535/spcx-20260630.htm"
)
SEC_USER_AGENT = "NexusSeekerBot (nexusseeker@example.com)"


@pytest.mark.integration
def test_spcx_10q_structured_extraction() -> None:
    """Fetch the real SPCX 10-Q and validate all 5 extraction categories."""

    # 1. Fetch the filing
    resp = httpx.get(
        SPCX_FILING_URL,
        headers={"User-Agent": SEC_USER_AGENT},
        timeout=15.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    assert len(resp.text) > 100_000, "Filing HTML should be substantial"

    # 2. Parse and clean
    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"([a-zA-Z0-9\-]+:[A-Za-z0-9]+[\n\s]+)+", "\n", text)
    assert len(text) > 50_000, f"Cleaned text too short: {len(text)} chars"

    # 3. Extract structured sections
    sections = extract_sections(text)
    sections_dict = sections.to_dict()

    # 4. Print diagnostic output
    print(f"\n{'='*60}")
    print(f"SPCX 10-Q | Full text: {len(text):,} chars")
    print(f"Sections found: {list(sections_dict.keys())}")
    for key, value in sections_dict.items():
        print(f"\n--- {key} ({len(value):,} chars) ---")
        print(value[:500] + ("…" if len(value) > 500 else ""))
    print(f"{'='*60}\n")

    # 5. Assert: all 5 categories should have content for this rich 10-Q
    assert sections.forward_guidance, "SPCX 10-Q should have forward-looking content"
    assert sections.margin_data, "SPCX 10-Q should have cost/margin data"
    assert (
        sections.market_share_and_customer
    ), "SPCX 10-Q should have market/customer data"
    assert sections.quarterly_financials, "SPCX 10-Q should have financial results"
    assert sections.operational_disruption, "SPCX 10-Q should have operational data"

    # 6. Assert: specific data points known to exist in this filing
    assert (
        "7,814" in sections.quarterly_financials
        or "12,508" in sections.quarterly_financials
    ), "Should contain SPCX Q2 2026 revenue figures"
    assert (
        "cost of revenue" in sections.margin_data.lower()
    ), "Should contain cost of revenue discussion"
    assert (
        "supply chain" in sections.operational_disruption.lower()
    ), "Should reference supply chain or operational constraints"
