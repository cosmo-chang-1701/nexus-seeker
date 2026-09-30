"""產生提領跑道的歷史壓力路徑靜態資料 `market_analysis/data/stress_paths.csv`。

兩條路徑（docs/risk_portfolio/05 §1.3、§2.3）：
  GFC     — SPY 自 2007-10-09 高點起 10 年
  DOTCOM  — QQQ 自 2000-03-10 高點起 10 年
欄位：path, k（自高點起第 k 個交易日，0 起算）, ret（當日報酬，auto_adjust 含股息）,
cpi_growth（CPIAUCSL 相對高點日的累積比值，只用當時已公布值：日期前 45 天可得）。
一次性離線產生後提交；正式環境只讀 CSV，不連網。

用法（dev 機器，需網路）：
  docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python scripts/build_stress_paths.py
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

from calibration.macro_regime import fetch_fred, load_fred  # noqa: E402

PATHS: dict[str, tuple[str, str]] = {
    "GFC": ("SPY", "2007-10-09"),
    "DOTCOM": ("QQQ", "2000-03-10"),
}
TRADING_DAYS = 252
YEARS = 10
CPI_LAG_DAYS = 45
OUT = Path(__file__).resolve().parent.parent / "market_analysis/data/stress_paths.csv"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        fetch_fred("CPIAUCSL", Path(tmp))
        cpi = load_fred("CPIAUCSL", Path(tmp))
    cpi.index = pd.DatetimeIndex(cpi.index)

    rows: list[tuple[str, int, float, float]] = []
    for name, (sym, peak) in PATHS.items():
        px = yf.download(
            sym, start=peak, auto_adjust=True, progress=False, multi_level_index=False
        )["Close"].squeeze()
        px = px.iloc[: TRADING_DAYS * YEARS + 1]
        if len(px) < TRADING_DAYS * YEARS or str(px.index[0].date()) != peak:
            raise RuntimeError(f"{name}: 資料不足或起點不是 {peak}")
        ret = px.pct_change().fillna(0.0)
        base = float(cpi.asof(px.index[0] - pd.Timedelta(days=CPI_LAG_DAYS)))
        for k, (d, r) in enumerate(ret.items()):
            growth = float(cpi.asof(d - pd.Timedelta(days=CPI_LAG_DAYS))) / base
            rows.append((name, k, round(float(r), 6), round(growth, 6)))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "k", "ret", "cpi_growth"])
        w.writerows(rows)
    print(f"已輸出 {len(rows)} 列 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
