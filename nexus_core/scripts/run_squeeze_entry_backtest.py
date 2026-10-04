"""多時間框架擠壓進場：日線事件研究（docs/strategies/10 §5）。

只產出報告，不改任何參數。讀取既有的日線快取（`.calibration_cache/1d/`），
ETF、槓桿 ETF 與指數不納入選股池。

用法（在 nexus_core 下）：
    docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker \\
        python scripts/run_squeeze_entry_backtest.py --start 2015-01-01

報告輸出到 gitignored 的 `reports/squeeze_entry/report.md`。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibration.data_store import DataStore  # noqa: E402
from calibration.squeeze_entry_backtest import (  # noqa: E402
    build_panel,
    render_markdown,
    summarize,
)

DEFAULT_CACHE = Path("/app/.calibration_cache")
DEFAULT_OUT = Path("reports/squeeze_entry")
# 非個股：指數、ETF、槓桿／反向 ETF、加密貨幣 ETF
_EXCLUDE = {
    "SPY",
    "GLD",
    "XLB",
    "COPX",
    "DRAM",
    "ETHA",
    "IBIT",
    "SOXL",
    "TZA",
    "_VIX",
    "_VIX3M",
    "^VIX",
    "^VIX3M",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--cooldown", type=int, default=10)
    args = ap.parse_args()

    store = DataStore(args.cache)
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols = sorted(
            p.name.replace(".csv.gz", "")
            for p in (args.cache / "1d").glob("*.csv.gz")
            if p.name.replace(".csv.gz", "") not in _EXCLUDE
        )

    dailies = {}
    for sym in symbols:
        df = store.load("1d", sym)
        if df is None or len(df) < 300:
            continue
        df = df.copy()
        df.index = df.index.tz_convert("America/New_York").tz_localize(None).normalize()
        df = df[~df.index.duplicated(keep="last")].astype("float64")
        dailies[sym] = df
        print(
            f"載入 {sym}: {len(df)} 根 ({df.index[0].date()} ~ {df.index[-1].date()})"
        )

    panel = build_panel(dailies, start=args.start)
    stats = summarize(panel, cooldown=args.cooldown)
    meta = {
        "期間": f"{args.start} 起至快取最後一日",
        "標的數": len(dailies),
        "標的": "、".join(dailies),
        "標的日數": len(panel),
        "獨立事件冷卻": f"{args.cooldown} 個交易日",
        "限制": "僅日線（W/3D/D）；T1 只能由壓力區突破觸發；未套用財報否決與逃頂降級；選股池事後挑選",
    }
    report = render_markdown(stats, meta)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
