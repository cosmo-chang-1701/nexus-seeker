"""call_yf 的 `_endpoint` 參數：指定計數端點名且不得傳給 func。"""

import pytest

from services import api_budget
from services.market_data_service import _core


@pytest.fixture(autouse=True)
def _reset() -> None:
    api_budget.reset_for_tests()


@pytest.mark.asyncio
async def test_endpoint_override_used_and_not_forwarded() -> None:
    received: dict[str, object] = {}

    def _fn(x: int, **kw: object) -> int:
        received.update(kw)
        return x + 1

    assert await _core.call_yf(lambda: 1, _endpoint="splits") == 1
    assert await _core.call_yf(_fn, 1, y=2, _endpoint="dividends") == 2
    assert received == {"y": 2}  # _endpoint 不得洩漏給 func
    snap = api_budget.snapshot()
    assert snap["yahoo/splits/background"] == 1
    assert snap["yahoo/dividends/background"] == 1
    assert not any("<lambda>" in k for k in snap)


@pytest.mark.asyncio
async def test_endpoint_defaults_to_func_name() -> None:
    def history_like() -> int:
        return 0

    await _core.call_yf(history_like)
    assert api_budget.snapshot() == {"yahoo/history_like/background": 1}
