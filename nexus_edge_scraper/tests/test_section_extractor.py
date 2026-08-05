"""Unit tests for section_extractor — uses real SPCX (SpaceX) 10-Q snippets."""

from section_extractor import extract_sections, ExtractedSections, SECTION_CHAR_LIMIT


# Real text snippets from SPCX 10-Q (spcx-20260630.htm)
SPCX_FORWARD_GUIDANCE_SNIPPET = (
    "This Quarterly Report on Form 10-Q contains forward-looking statements "
    "within the meaning of the Private Securities Litigation Reform Act of 1995. "
    "Forward-looking statements are based on assumptions with respect to "
    "management's future and current expectations, involve certain risks and "
    "uncertainties, are not guarantees. These forward-looking statements include, "
    "but are not limited to, statements concerning the development and deployment "
    "of Starship, the size and growth of our various existing and future markets."
)

SPCX_MARGIN_SNIPPET = (
    "Revenue $ 7,814 $ 4,071 $ 12,508 $ 8,138 "
    "Costs and expenses Cost of revenue 3,495 2,282 5,883 4,244 "
    "Research and development 3,548 1,958 7,062 3,515 "
    "Selling, general, and administrative 912 606 1,658 1,099"
)

SPCX_MARKET_SHARE_SNIPPET = (
    "the competitive landscape in the industries in which we operate, "
    "the implementation. A significant portion of our AI infrastructure revenue "
    "is concentrated in a small number of customers. The loss of a significant "
    "customer, the termination or non-renewal of one or more of these agreements, "
    "our inability to replace lost business on comparable terms."
)

SPCX_QUARTERLY_FINANCIALS_SNIPPET = (
    "Revenue $ 7,814 $ 4,071 $ 12,508 $ 8,138 "
    "Loss from operations ( 143 ) ( 970 ) ( 2,086 ) ( 943 ) "
    "Net loss $ ( 541 ) $ ( 1,008 ) $ ( 4,817 ) $ ( 1,536 ) "
    "Net loss per share of common stock attributable to common shareholders Basic"
)

SPCX_OPERATIONAL_DISRUPTION_SNIPPET = (
    "higher costs in our AI segment of $1,056 million driven by higher "
    "infrastructure and cloud computing costs as a result of our AI data center "
    "expansions. Supply chain disruptions, equipment shortages, regulatory "
    "restrictions, or permit delays could impair our ability to deploy capacity. "
    "Capital expenditures $ 1,174 $ 1,367 $ 15,828 $ 18,369"
)


class TestExtractSections:
    def test_forward_guidance_from_spcx(self) -> None:
        result = extract_sections(SPCX_FORWARD_GUIDANCE_SNIPPET)
        assert result.forward_guidance, "Should extract forward-looking content"
        assert "forward-looking" in result.forward_guidance.lower()

    def test_margin_data_from_spcx(self) -> None:
        result = extract_sections(SPCX_MARGIN_SNIPPET)
        assert result.margin_data, "Should extract cost/margin content"
        assert "cost of revenue" in result.margin_data.lower()

    def test_market_share_from_spcx(self) -> None:
        result = extract_sections(SPCX_MARKET_SHARE_SNIPPET)
        assert (
            result.market_share_and_customer
        ), "Should extract customer/competitive content"
        assert "competitive landscape" in result.market_share_and_customer.lower()

    def test_quarterly_financials_from_spcx(self) -> None:
        result = extract_sections(SPCX_QUARTERLY_FINANCIALS_SNIPPET)
        assert result.quarterly_financials, "Should extract financial results"
        assert "loss from operations" in result.quarterly_financials.lower()

    def test_operational_disruption_from_spcx(self) -> None:
        result = extract_sections(SPCX_OPERATIONAL_DISRUPTION_SNIPPET)
        assert result.operational_disruption, "Should extract operational risk content"
        assert "data center" in result.operational_disruption.lower()

    def test_empty_text_returns_empty_sections(self) -> None:
        result = extract_sections("Completely irrelevant boilerplate text.")
        assert result.forward_guidance == ""
        assert result.margin_data == ""
        assert result.to_dict() == {}

    def test_section_char_limit_respected(self) -> None:
        chunk = "Revenue growth was 15% year-over-year. " * 500
        result = extract_sections(chunk)
        assert len(result.quarterly_financials) <= SECTION_CHAR_LIMIT + 100

    def test_to_dict_omits_empty(self) -> None:
        sections = ExtractedSections(forward_guidance="some text")
        d = sections.to_dict()
        assert "forward_guidance" in d
        assert "margin_data" not in d


class TestContextWindowMerging:
    """Verify that overlapping keyword matches are correctly merged."""

    def test_adjacent_keywords_produce_single_window(self) -> None:
        """Two keywords within 800 chars should merge into one window."""
        text = (
            "A" * 100
            + " cost of revenue was $500M. "
            + "A" * 50
            + " operating margin declined to 12%. "
            + "B" * 100
        )
        result = extract_sections(text)
        assert "---" not in result.margin_data, "Should be one merged window"

    def test_distant_keywords_produce_separate_windows(self) -> None:
        """Two keywords >1600 chars apart should be separate windows."""
        text = (
            "Cost of revenue was $500M. "
            + "X" * 3000
            + " Operating margin declined to 12%."
        )
        result = extract_sections(text)
        assert (
            "---" in result.margin_data
        ), "Should have separator between distant windows"
