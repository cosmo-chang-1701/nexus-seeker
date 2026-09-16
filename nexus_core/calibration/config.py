"""校準工具組態 (不可變)。"""

from dataclasses import dataclass, field
from pathlib import Path


def _default_root(container_path: str, local_name: str) -> Path:
    """容器內 (/app 為 bind-mount 的 repo) 用固定路徑；本機執行則相對於 cwd。"""
    app = Path("/app")
    if app.is_dir() and (app / "config.py").exists():
        return Path(container_path)
    return Path.cwd() / local_name


@dataclass(frozen=True)
class CalibrationConfig:
    # 磁碟快取與報告輸出。刻意不放在 /app/data (production 的 named volume)。
    cache_dir: Path = field(
        default_factory=lambda: _default_root(
            "/app/.calibration_cache", ".calibration_cache"
        )
    )
    out_dir: Path = field(
        default_factory=lambda: _default_root("/app/reports", "reports")
    )
    # 資料範圍：日線自 2005 起 (涵蓋 2008 / 2020 / 2022)；1h 取 729 天 (yfinance 上限 730)。
    daily_start: str = "2005-01-01"
    hourly_period: str = "729d"
    max_symbols: int = 40
    offline: bool = False
    fetch_sleep_seconds: tuple[float, float] = (1.5, 3.0)
    fetch_retries: int = 3
    # 統計
    seed: int = 7
    n_boot: int = 2000
    min_events: int = 100
    min_dates: int = 30
    shrinkage_n0: int = 200
    oos_split: str = "2024-01-01"
    # 標註
    k_grid: tuple[float, ...] = (1.0, 1.5, 2.0)
    primary_k: float = 1.5
    max_sessions: int = 5
    # Regime V RSI 門檻掃描格點
    rsi_grid: tuple[float, ...] = (
        30.0,
        32.5,
        35.0,
        37.5,
        40.0,
        42.5,
        45.0,
        47.5,
        50.0,
        52.5,
        55.0,
    )
    # 空間倍數掃描格點 (ATR₁D 倍數)
    room_grid: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
