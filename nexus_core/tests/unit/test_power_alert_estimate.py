"""邊緣節點電力警報：以實測耗電速率推估剩餘時間。"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.embed_builders.hedge_embeds import create_power_alert_embed
from services.memory_manager import MemoryManager, estimate_battery_secs_left


def _field_text(embed: Any) -> str:
    return "\n".join(f.value for f in embed.fields)


def test_estimator_insufficient_inputs() -> None:
    assert estimate_battery_secs_left([]) is None
    assert estimate_battery_secs_left([(0.0, 80.0)]) is None
    # 視窗過短
    assert estimate_battery_secs_left([(0.0, 80.0), (600.0, 70.0)]) is None
    # 降幅過小
    assert estimate_battery_secs_left([(0.0, 80.0), (3600.0, 79.0)]) is None
    # 電量回升
    assert estimate_battery_secs_left([(0.0, 70.0), (3600.0, 75.0)]) is None
    # 非有限值
    assert estimate_battery_secs_left([(0.0, float("nan")), (3600.0, 70.0)]) is None


def test_estimator_value() -> None:
    res = estimate_battery_secs_left([(0.0, 75.0), (1500.0, 72.0), (3000.0, 70.0)])
    assert res is not None
    secs, span = res
    assert span == 3000.0
    assert secs == pytest.approx(700 * 60)


def test_estimator_zero_percent_and_embed() -> None:
    res = estimate_battery_secs_left([(0.0, 5.0), (3000.0, 0.0)])
    assert res == (0.0, 3000.0)
    text = _field_text(create_power_alert_embed(0, res[0], "X", 50))
    assert "即將耗盡" in text


def test_embed_measured_and_unknown() -> None:
    text = _field_text(create_power_alert_embed(70, 700 * 60, "X", 50))
    assert "11 小時 40 分鐘" in text
    assert "依近 50 分鐘耗電速率推估" in text
    assert "1200" not in text
    short = _field_text(create_power_alert_embed(30, 45 * 60, "X", 30))
    assert "約 45 分鐘（依近 30 分鐘耗電速率推估）" in short
    none_text = _field_text(create_power_alert_embed(70, None, "X"))
    assert "估算中（未插電取樣不足）" in none_text
    assert "1200" not in none_text
    assert "未知" not in none_text


def _make_mm() -> tuple[MemoryManager, MagicMock]:
    bot = MagicMock()
    bot.queue_dm = AsyncMock()
    return MemoryManager(bot, threshold=90.0, swap_threshold=50.0), bot


def _payload(percent: float, plugged: bool) -> dict[str, Any]:
    return {
        "os_system": "Darwin",
        "memory_percent": 50.0,
        "swap_percent": 0.0,
        "swap_total_mb": 0.0,
        "process_memory_mb": 100.0,
        "battery": {"percent": percent, "power_plugged": plugged, "secsleft": 72000},
    }


class _Clock:
    def __init__(self, step: float = 300.0) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


async def _run_check(mm: MemoryManager, payload: dict[str, Any], clock: _Clock) -> None:
    with patch("psutil.virtual_memory") as mock_mem, patch(
        "psutil.swap_memory"
    ) as mock_swap, patch("psutil.Process"), patch(
        "config.TUNNEL_URL", "https://edge.example.com"
    ), patch("config.DISCORD_ADMIN_USER_ID", 123), patch(
        "httpx.AsyncClient"
    ) as mock_client_cls, patch("services.memory_manager._monotonic", clock):
        mock_mem.return_value.percent = 10.0
        mock_swap.return_value.percent = 0.0
        mock_swap.return_value.total = 0
        client = AsyncMock()
        res = MagicMock()
        res.status_code = 200
        res.json.return_value = payload
        client.get = AsyncMock(return_value=res)
        mock_client_cls.return_value.__aenter__.return_value = client
        await mm._perform_health_check()


@pytest.mark.asyncio
async def test_power_alert_threshold_sequence() -> None:
    mm, bot = _make_mm()
    clock = _Clock()

    await _run_check(mm, _payload(80, False), clock)
    assert bot.queue_dm.await_count == 0

    await _run_check(mm, _payload(74, False), clock)
    assert bot.queue_dm.await_count == 1
    assert mm._last_power_alert_level == 75

    await _run_check(mm, _payload(74, False), clock)
    assert bot.queue_dm.await_count == 1

    await _run_check(mm, _payload(49, False), clock)
    assert bot.queue_dm.await_count == 2
    assert mm._last_power_alert_level == 50

    await _run_check(mm, _payload(49, True), clock)
    assert bot.queue_dm.await_count == 2
    assert mm._last_power_alert_level == 100
    assert len(mm._battery_samples) == 0

    await _run_check(mm, _payload(74, False), clock)
    assert bot.queue_dm.await_count == 3
    assert mm._last_power_alert_level == 75


@pytest.mark.asyncio
async def test_health_check_dm_shows_measured_estimate() -> None:
    mm, bot = _make_mm()
    clock = _Clock()
    for pct in (80, 78, 76, 74):
        await _run_check(mm, _payload(pct, False), clock)
    assert bot.queue_dm.await_count == 1
    embed = bot.queue_dm.await_args.kwargs["embed"]
    text = _field_text(embed)
    assert "1200" not in text
    # 15 分鐘降 6%：0.4%/分鐘，74% 約 185 分鐘
    assert "3 小時 5 分鐘（依近 15 分鐘耗電速率推估）" in text

    await _run_check(mm, _payload(74, True), clock)
    assert len(mm._battery_samples) == 0


@pytest.mark.asyncio
async def test_health_check_dm_estimating_when_insufficient() -> None:
    mm, bot = _make_mm()
    clock = _Clock()
    await _run_check(mm, _payload(74, False), clock)
    assert bot.queue_dm.await_count == 1
    text = _field_text(bot.queue_dm.await_args.kwargs["embed"])
    assert "估算中（未插電取樣不足）" in text
    assert "1200" not in text


@pytest.mark.asyncio
async def test_rising_percent_restarts_samples() -> None:
    mm, _ = _make_mm()
    clock = _Clock()
    await _run_check(mm, _payload(80, False), clock)
    await _run_check(mm, _payload(78, False), clock)
    assert len(mm._battery_samples) == 2
    await _run_check(mm, _payload(85, False), clock)
    assert len(mm._battery_samples) == 1
    assert mm._battery_samples[-1][1] == 85.0


@pytest.mark.asyncio
async def test_nan_percent_not_sampled() -> None:
    mm, _ = _make_mm()
    clock = _Clock()
    await _run_check(mm, _payload(80, False), clock)
    await _run_check(mm, _payload(float("nan"), False), clock)
    assert [s[1] for s in mm._battery_samples] == [80.0]
