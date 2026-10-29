"""盤後綜合風險報告 Report status 守則置頂檢查（不連網）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.llm_service import generate_analyst_report

BULLETS = (
    "提領跑道 (Withdrawal Runway)",
    "建倉至今損益歸因",
    "行業板塊資金輪動",
    "盤後風險對沖決策",
    "持倉風險檢視",
    "次日交易策略模板",
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


def _block(p: str) -> str:
    start = p.index('Specific Instructions for "盤後綜合風險與 AI 策略報告"')
    end = p.index('Specific Instructions for "盤前財報與估值調整"')
    return p[start:end]


async def test_status_before_all_bullets(prompt: str) -> None:
    block = _block(prompt)
    status = block.index("**Report status**")
    rest = block[block.index("\n", status) :]
    assert block.index("- ") < status < block.index("\n    - ", status)
    for b in BULLETS:
        assert f"**{b}" not in block[:status] and b in rest


async def test_status_takes_precedence(prompt: str) -> None:
    block = _block(prompt)
    assert "takes precedence" in block
    assert "no_positions" in block
    assert "failed" in block


async def test_status_rule_only_once(prompt: str) -> None:
    assert prompt.count("**Report status**") == 1


async def test_bullets_kept_in_order(prompt: str) -> None:
    block = _block(prompt)
    rest = block[block.index("\n", block.index("**Report status**")) :]
    idx = [rest.index(b) for b in BULLETS]
    assert idx == sorted(idx)
