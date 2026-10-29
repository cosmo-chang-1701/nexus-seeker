"""盤後綜合風險報告 system prompt 內容檢查（不連網）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.llm_service import generate_analyst_report

HEADERS = (
    "📊 多空大盤交叉驗證解讀",
    "⚠️ 潛在陷阱與風險提示",
    "🛡️ 高勝率交易策略推薦",
)


@pytest.fixture
async def prompt() -> str:
    parsed = SimpleNamespace(report_content="ok")
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
    )
    mock = AsyncMock(return_value=resp)
    with patch("services.llm_service.llm_parse", mock):
        await generate_analyst_report(
            "2026-10-29 盤後綜合風險與 AI 策略報告", {"report_status": "ok"}
        )
    return str(mock.call_args.kwargs["messages"][0]["content"])


def _post_market_block(p: str) -> str:
    start = p.index('Specific Instructions for "盤後綜合風險與 AI 策略報告"')
    end = p.index('Specific Instructions for "盤前財報與估值調整"')
    return p[start:end]


async def test_new_fields_present(prompt: str) -> None:
    for key in (
        "pnl_attribution_since_entry",
        "margin_heat_pct",
        "net_delta_exposure_pct",
        "positions",
    ):
        assert key in prompt


async def test_headers_kept(prompt: str) -> None:
    for h in HEADERS:
        assert h in prompt


async def test_conditional_skip_before_rule_one(prompt: str) -> None:
    skip = prompt.index("skip that rule silently")
    rule1 = prompt.index("1. **IV Bubble Validation**")
    assert skip < rule1
    assert "data-limitation" in prompt
    assert "do not write any 資料限制" in prompt


async def test_no_unconditional_must(prompt: str) -> None:
    for marker in ("you MUST explicitly flag", "you MUST explain"):
        idx = prompt.index(marker)
        assert "present" in prompt[max(0, idx - 120) : idx]


async def test_post_market_block_cleaned(prompt: str) -> None:
    block = _post_market_block(prompt)
    assert "margin utilization" not in block
    assert "當日盈虧歸因 (PnL Attribution): Use brinson_attribution_proxy" not in block
    assert "PnL Since Entry" in block
