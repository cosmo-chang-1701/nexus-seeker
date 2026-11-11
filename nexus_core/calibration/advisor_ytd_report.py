"""「總經量化選擇權顧問」(MQA) 2026 年初至今離線報告（`python -m calibration advisor-ytd-report`）。

把機械化的 MQA 策略（`calibration/advisor_backtest.py`）與專案自己的策略
（`static_allocation_backtest.simulate_static`，100% 科技池／偏離 5 個百分點才再平衡）放在
同一個科技標的池、同一段期間下比較，輸出 `results.json` 與繁體中文 `report.md`。
只做離線計算：讀寫 `.calibration_cache/`，`--snapshot-db` 以唯讀模式開啟（帳戶 NAV 只輸出正規化後的百分比），絕不寫任何資料庫。
規格與限制見 docs/architecture/05_calibration_harness_and_forward_collection.md §5.20。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from calibration.advisor_backtest import (
    FOMC_DATES_2026,
    AdvisorParams,
    AdvisorResult,
    iv_proxy_frame,
    simulate_advisor,
)
from calibration.daily_panel import load_panel, prepare_panel
from calibration.data_store import DataStore
from calibration.regime_momentum_backtest import (
    CASH_PROXY,
    CASH_SYMBOL,
    CORE_SYMBOL,
    DEFAULT_UNIVERSE,
    MARKET_SYMBOL,
    buy_and_hold,
    compute_metrics,
    equal_weight_pit,
    slice_nav,
    window_drawdown,
)
from calibration.static_allocation_backtest import (
    BUCKET_CORE,
    BUCKET_TECH,
    StaticAllocationParams,
    simulate_static,
)
from market_analysis.downside_risk import max_drawdown

YTD_START = "2026-01-02"
FETCH_FROM = "2024-01-01"
QQQ_SYMBOL = "QQQ"
VIX_SYMBOL = "^VIX"
MAR_ANNUAL = 0.045
INITIAL_CAPITAL = 100_000.0
FALLBACK_CASH_RATE = 0.045
EQUITY_COST_RATE = 0.0015  # 科技池等權對照組的單邊成本（比照既有回測）

ROW_BASE = "MQA 基準"
ROW_INVESTED = "MQA 期初滿倉"
ROW_EARN_OPT = "MQA 財報只平期權"
ROW_NO_OPT = "MQA 無期權"
ROW_NO_CC = "MQA 右側不賣 CC"
ROW_D = "專案 D（100% 科技池）"
ROW_80 = "專案 80%"
ROW_VOO = "VOO B&H"
ROW_QQQ = "QQQ B&H"
ROW_BOXX = "BOXX"
ROW_EW = "科技池等權"

MQA_VARIANTS: tuple[tuple[str, dict[str, Any]], ...] = (
    (ROW_BASE, {}),
    (ROW_INVESTED, {"initial_state": "INVESTED"}),
    (ROW_EARN_OPT, {"earnings_exit_spot": False}),
    (ROW_NO_OPT, {"options_enabled": False, "hedge_enabled": False}),
    (ROW_NO_CC, {"cc_on_right": False}),
)

LIMITATIONS: tuple[str, ...] = (
    "GEX／Put Wall／PCR／Max Pain 沒有歷史資料，**未建模**；顧問依這些訊號做的判斷無法重播。",
    "CPI／NFP／PCE 公布前後 4–8 小時的鎖定，在「日線開盤執行」的設定下**永遠不會觸發**；只有 FOMC 決議日與財報日被建模。",
    "基本面失效判斷屬於投資人自己的工作，機械化版本沒有任何基本面濾網。",
    "允許小數口數：以模擬初始資金 10 萬計，每檔預算僅數千美元，實際上無法對每檔各持有一口 CSP，選擇權損益偏樂觀。",
    "選擇權以 Black-Scholes 搭配代理 IV（已實現波動 + VIX 溢價）定價，不含股息、不含真實價差與偏斜；代理 IV 與真實 IV 的差距見驗證表。",
    "沒有出入金：全程以時間加權報酬計算；實際帳戶若有出入金，與本表不可直接比較。",
    "只有單一年度樣本，結論不應外推到其他年度或體制。",
    "存活者偏差與事後偏誤：選股池是事後已知的強勢科技股（雖然刻意納入落後標的）。",
    "現貨市價成交一律計單邊 0.15%（MQA、專案策略、科技池等權；VOO／QQQ／BOXX 買進持有不計），指派與被買走以履約價成交不計；選擇權另計 3% 價差。",
)

# ---------------------------------------------------------------------------
# 財報日：抓取（可注入 fetcher）與快取
# ---------------------------------------------------------------------------

EarningsFetcher = Callable[[str], Sequence[date]]


def default_earnings_fetcher(symbol: str) -> list[date]:
    """yfinance `get_earnings_dates(limit=12)`；index 為美東時區的時間戳。"""
    import yfinance as yf

    df = yf.Ticker(symbol).get_earnings_dates(limit=12)
    if df is None or len(df) == 0:
        return []
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("America/New_York")
    idx = idx.tz_convert("America/New_York")
    return sorted({ts.date() for ts in idx})


def _earnings_path(cache_dir: Path, symbol: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in symbol.upper())
    return Path(cache_dir) / "earnings" / f"{safe}.json"


def save_earnings(cache_dir: Path, symbol: str, dates: Sequence[date]) -> Path:
    path = _earnings_path(cache_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol.upper(),
        "dates": sorted({d.isoformat() for d in dates}),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


async def fetch_earnings(
    symbols: Sequence[str],
    cache_dir: Path,
    fetcher: Optional[EarningsFetcher] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    sleep_range: tuple[float, float] = (1.5, 3.0),
) -> dict[str, int]:
    """逐檔抓財報日並寫入快取；每檔的任何例外只略過該檔。回傳 {標的: 日期數}。"""
    fetch = fetcher or default_earnings_fetcher
    nap = sleep or asyncio.sleep
    counts: dict[str, int] = {}
    for k, sym in enumerate(symbols):
        if k > 0:
            await nap(random.uniform(*sleep_range))
        try:
            dates = await asyncio.to_thread(fetch, sym)
        except Exception as e:  # yfinance 例外型別不穩定
            print(f"  {sym}: 財報日抓取失敗（{e}）", flush=True)
            continue
        save_earnings(cache_dir, sym, list(dates))
        counts[sym] = len(dates)
        print(f"  {sym}: {len(dates)} 個財報日", flush=True)
    return counts


def load_earnings(
    cache_dir: Path, symbols: Sequence[str]
) -> tuple[dict[str, list[date]], list[str]]:
    """讀取財報日快取；回傳 (各標的日期, 缺漏標的)。無檔或日期為空皆視為缺漏。"""
    out: dict[str, list[date]] = {}
    missing: list[str] = []
    for sym in symbols:
        path = _earnings_path(cache_dir, sym)
        dates: list[date] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                dates = sorted(date.fromisoformat(str(d)) for d in raw.get("dates", []))
            except (ValueError, OSError, AttributeError):
                dates = []
        if dates:
            out[sym] = dates
        else:
            missing.append(sym)
    return out, missing


# ---------------------------------------------------------------------------
# 資料檢查
# ---------------------------------------------------------------------------


def required_daily_symbols(universe: Sequence[str]) -> list[str]:
    """`--offline` 必備的日線快取：選股池、SPY、QQQ、^VIX（VOO／BOXX／BIL 有代理，可缺）。"""
    out = list(dict.fromkeys([*universe, MARKET_SYMBOL, QQQ_SYMBOL, VIX_SYMBOL]))
    return out


def fetch_symbols(universe: Sequence[str]) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *universe,
                MARKET_SYMBOL,
                CORE_SYMBOL,
                QQQ_SYMBOL,
                CASH_SYMBOL,
                CASH_PROXY,
                VIX_SYMBOL,
            ]
        )
    )


def missing_daily_cache(store: DataStore, universe: Sequence[str]) -> list[str]:
    return [s for s in required_daily_symbols(universe) if not store.has("1d", s)]


# ---------------------------------------------------------------------------
# snapshot.db（唯讀）
# ---------------------------------------------------------------------------


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_account_nav(db_path: Path) -> pd.Series:
    """帳戶 NAV 序列（單一使用者；多使用者時取筆數最多者）。

    隱私：只讀 date 與 nav 兩欄（絕不讀持倉內容），並在首日正規化為 `INITIAL_CAPITAL`，
    之後任何輸出都只有相對報酬，不會出現帳戶的絕對金額。"""
    conn = _connect_ro(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT user_id, date, nav FROM portfolio_nav_daily ORDER BY date", conn
        )
    finally:
        conn.close()
    if df.empty:
        return pd.Series(dtype=float)
    top = df["user_id"].value_counts().idxmax()
    df = df[df["user_id"] == top].drop_duplicates("date", keep="last")
    s = pd.Series(
        df["nav"].astype(float).to_numpy(),
        index=pd.DatetimeIndex(pd.to_datetime(df["date"])).normalize(),
    )
    s = s.sort_index()
    if len(s) and s.iloc[0] > 0:
        s = s / float(s.iloc[0]) * INITIAL_CAPITAL
    return s


def load_real_iv(db_path: Path) -> pd.DataFrame:
    """`historical_iv` 的真實 IV（小數）；欄位 symbol、iv、date（Timestamp）。"""
    conn = _connect_ro(db_path)
    try:
        df = pd.read_sql_query("SELECT symbol, iv, date FROM historical_iv", conn)
    finally:
        conn.close()
    if df.empty:
        return df
    df["date"] = pd.DatetimeIndex(pd.to_datetime(df["date"])).normalize()
    df["iv"] = df["iv"].astype(float)
    return df


def iv_validation(proxy: pd.DataFrame, real: pd.DataFrame) -> dict[str, Any]:
    """代理 IV 對真實 IV：重疊 (symbol, date) 的筆數、proxy/real 中位數、MAE（波動點）、相關係數。"""
    pairs: list[tuple[float, float]] = []
    for row in real.itertuples(index=False):
        sym = str(row.symbol)
        day = pd.Timestamp(row.date)
        if sym in proxy.columns and day in proxy.index:
            p = float(proxy.at[day, sym])
            r = float(row.iv)
            if math.isfinite(p) and math.isfinite(r) and r > 0:
                pairs.append((p, r))
    if not pairs:
        return {"n": 0, "median_ratio": None, "mae_vol_points": None, "pearson": None}
    p_arr = np.array([a for a, _ in pairs])
    r_arr = np.array([b for _, b in pairs])
    corr: Optional[float] = None
    if len(pairs) >= 3 and p_arr.std() > 0 and r_arr.std() > 0:
        corr = float(np.corrcoef(p_arr, r_arr)[0, 1])
    return {
        "n": len(pairs),
        "median_ratio": float(np.median(p_arr / r_arr)),
        "mae_vol_points": float(np.mean(np.abs(p_arr - r_arr)) * 100.0),
        "pearson": corr,
    }


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def _row(
    name: str, nav: pd.Series, exposure: float, d_return: Optional[float] = None
) -> dict[str, Any]:
    m = compute_metrics(nav, MAR_ANNUAL)
    return {
        "name": name,
        "total_return": m.total_return,
        "sortino": m.sortino,
        "max_drawdown": m.max_drawdown,
        "var_95": m.var_95,
        "cvar_95": m.cvar_95,
        "avg_equity_exposure": exposure,
        "excess_vs_d": None if d_return is None else m.total_return - d_return,
    }


def _window_stats(nav: pd.Series, start: str, end: str) -> dict[str, Any]:
    s = slice_nav(nav, start, end)
    if len(s) < 2:
        return {"total_return": None, "max_drawdown": None}
    return {
        "total_return": float(s.iloc[-1] / s.iloc[0] - 1.0),
        "max_drawdown": window_drawdown(nav, start, end),
    }


def build_advisor_ytd_report(
    store: DataStore,
    ytd_start: str = YTD_START,
    ytd_end: str = "",
    cache_dir: Optional[Path] = None,
    snapshot_db: Optional[Path] = None,
    universe: Sequence[str] = DEFAULT_UNIVERSE,
    initial_capital: float = INITIAL_CAPITAL,
) -> tuple[dict[str, Any], str]:
    """計算全部列並渲染 Markdown。`store` 需已含日線快取（本函式不連網）。"""
    uni = tuple(universe)
    cdir = Path(cache_dir) if cache_dir is not None else store.cache_dir
    opens, closes, cash_index = _load(store, uni)
    start = ytd_start
    end = ytd_end or str(pd.Timestamp(closes.index[-1]).date())
    vix = closes[VIX_SYMBOL]
    earnings, missing_earn = load_earnings(cdir, uni)

    variants: dict[str, AdvisorResult] = {}
    base_params = AdvisorParams(universe=uni)
    for name, overrides in MQA_VARIANTS:
        variants[name] = simulate_advisor(
            opens,
            closes,
            cash_index,
            vix,
            start,
            end,
            replace(base_params, **overrides),
            earnings,
            FOMC_DATES_2026,
            initial_capital,
        )
    navs: dict[str, pd.Series] = {n: r.nav for n, r in variants.items()}
    exposure: dict[str, float] = {
        n: float(r.equity_exposure.mean()) for n, r in variants.items()
    }
    for name, share in ((ROW_D, 1.0), (ROW_80, 0.8)):
        res = simulate_static(
            opens,
            closes,
            cash_index,
            start,
            end,
            StaticAllocationParams(
                equity_share=share,
                tech_share=1.0,
                rebalance="THRESHOLD",
                universe=uni,
            ),
            initial_capital,
        )
        navs[name] = res.nav
        exposure[name] = float(
            (res.bucket_weights[BUCKET_TECH] + res.bucket_weights[BUCKET_CORE]).mean()
        )
    navs[ROW_VOO] = buy_and_hold(closes[CORE_SYMBOL], start, end, initial_capital)
    navs[ROW_QQQ] = buy_and_hold(closes[QQQ_SYMBOL], start, end, initial_capital)
    navs[ROW_BOXX] = buy_and_hold(cash_index, start, end, initial_capital)
    navs[ROW_EW] = equal_weight_pit(
        opens, closes, uni, start, end, EQUITY_COST_RATE, initial_capital
    )
    exposure.update({ROW_VOO: 1.0, ROW_QQQ: 1.0, ROW_BOXX: 0.0, ROW_EW: 1.0})

    order = [n for n, _ in MQA_VARIANTS] + [
        ROW_D,
        ROW_80,
        ROW_VOO,
        ROW_QQQ,
        ROW_BOXX,
        ROW_EW,
    ]
    d_return = compute_metrics(navs[ROW_D], MAR_ANNUAL).total_return
    rows = [_row(n, navs[n], exposure[n], d_return) for n in order]

    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {
            "start": str(pd.Timestamp(navs[ROW_D].index[0]).date()),
            "end": str(pd.Timestamp(navs[ROW_D].index[-1]).date()),
            "trading_days": int(len(navs[ROW_D])),
        },
        "initial_capital": initial_capital,
        "mar_annual": MAR_ANNUAL,
        "universe": list(uni),
        "mqa_params": asdict(base_params),
        "rows": rows,
        "counters": {n: variants[n].counters for n, _ in MQA_VARIANTS},
        "option_pnl": {
            n: {"total": variants[n].option_pnl, **variants[n].option_pnl_parts}
            for n, _ in MQA_VARIANTS
        },
        "spot_costs": {n: variants[n].spot_costs for n, _ in MQA_VARIANTS},
        "min_cash": {n: variants[n].min_cash for n, _ in MQA_VARIANTS},
        "missing_earnings": missing_earn,
        "iv_validation": None,
        "same_window": None,
        "limitations": list(LIMITATIONS),
    }
    if snapshot_db is not None:
        _attach_snapshot(result, navs, closes, vix, uni, Path(snapshot_db))
    return result, render_markdown(result)


def _load(
    store: DataStore, universe: tuple[str, ...]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    if not store.has("1d", MARKET_SYMBOL):
        raise ValueError(f"缺少 {MARKET_SYMBOL} 日線快取，無法建立交易日曆")
    if not store.has("1d", VIX_SYMBOL):
        raise ValueError(f"缺少 {VIX_SYMBOL} 日線快取")
    opens, closes = load_panel(store, fetch_symbols(universe))
    for frame in (opens, closes):
        if CORE_SYMBOL not in frame.columns:
            frame[CORE_SYMBOL] = float("nan")
    required = [*universe, QQQ_SYMBOL]
    return prepare_panel(opens, closes, FALLBACK_CASH_RATE, required)


def _attach_snapshot(
    result: dict[str, Any],
    navs: dict[str, pd.Series],
    closes: pd.DataFrame,
    vix: pd.Series,
    universe: tuple[str, ...],
    db_path: Path,
) -> None:
    try:
        account = load_account_nav(db_path)
    except Exception as e:  # 表不存在、欄位不符等
        result["same_window"] = {"status": f"無法讀取帳戶 NAV 資料表：{e}"}
        account = pd.Series(dtype=float)
    if len(account) >= 2:
        w_start = str(account.index[0].date())
        w_end = str(account.index[-1].date())
        acct_dd = max_drawdown(account.to_numpy(dtype=float)).max_drawdown
        rows = [
            {
                "name": "帳戶實際 NAV",
                "total_return": float(account.iloc[-1] / account.iloc[0] - 1.0),
                "max_drawdown": float(acct_dd),
            }
        ]
        for name, nav in navs.items():
            rows.append({"name": name, **_window_stats(nav, w_start, w_end)})
        result["same_window"] = {
            "start": w_start,
            "end": w_end,
            "base": INITIAL_CAPITAL,
            "rows": rows,
        }
    elif result["same_window"] is None:
        result["same_window"] = {"status": "帳戶 NAV 資料表無資料"}
    try:
        real = load_real_iv(db_path)
    except Exception as e:
        result["iv_validation"] = {"status": f"無法讀取 historical_iv：{e}"}
        return
    if real.empty:
        result["iv_validation"] = {"status": "historical_iv 無資料"}
        return
    syms = sorted({str(s) for s in real["symbol"]} & (set(universe) | {MARKET_SYMBOL}))
    proxy = iv_proxy_frame(closes, vix, syms)
    result["iv_validation"] = iv_validation(proxy, real)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _pct(v: Any, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    return f"{float(v) * 100:.{digits}f}%"


def _num(v: Any, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    return f"{float(v):.{digits}f}"


def render_markdown(result: dict[str, Any]) -> str:
    period = result["period"]
    lines: list[str] = [
        "# MQA 年初至今回測報告",
        "",
        f"- 期間：{period['start']} → {period['end']}（{period['trading_days']} 個交易日）",
        f"- 初始資金：{result['initial_capital']:,.0f}；Sortino 的 MAR：{_pct(result['mar_annual'], 1)}",
        f"- 選股池：{len(result['universe'])} 檔（point-in-time 等權預算）",
        "- 本報告為離線機械化重播，**不改任何正式參數**；解讀前請先讀最後一節「機械化解讀與限制」。",
        "",
        "## 一、績效比較",
        "",
        "| 策略 | YTD 報酬 | Sortino | MDD | VaR95 | CVaR95 | 平均股票曝險 | 對專案 D 超額 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in result["rows"]:
        lines.append(
            f"| {r['name']} | {_pct(r['total_return'])} | {_num(r['sortino'])} "
            f"| {_pct(r['max_drawdown'])} | {_pct(r['var_95'])} | {_pct(r['cvar_95'])} "
            f"| {_pct(r['avg_equity_exposure'], 1)} | {_pct(r['excess_vs_d'])} |"
        )
    counters: dict[str, dict[str, int]] = result["counters"]
    names = list(counters)
    keys = list(next(iter(counters.values()))) if counters else []
    lines += [
        "",
        "## 二、MQA 事件計數",
        "",
        "| 計數 | " + " | ".join(names) + " |",
        "| :--- | " + " | ".join("---:" for _ in names) + " |",
    ]
    for k in keys:
        lines.append(
            f"| {k} | " + " | ".join(str(counters[n][k]) for n in names) + " |"
        )
    lines += [
        "",
        "## 三、選擇權損益",
        "",
        "權利金收入 − 買回 − 到期結算（內含價值）＋ 避險損益 ＋ 期末未平倉市值（避險 Put 價值 − 空方選擇權價值）；現貨成本為單邊 0.15% 的累計；金額為美元。",
        "",
        "| 策略 | 合計 | 權利金收入 | 買回 | 到期結算 | 避險 | 未平倉市值 | 現貨成本 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for n, p in result["option_pnl"].items():
        lines.append(
            f"| {n} | {p['total']:,.0f} | {p['premium']:,.0f} | {p['buyback']:,.0f} "
            f"| {p['settle']:,.0f} | {p['hedge']:,.0f} | {p['open_mtm']:,.0f} "
            f"| {result['spot_costs'][n]:,.0f} |"
        )
    lines += ["", "## 四、IV 代理驗證", ""]
    iv = result.get("iv_validation")
    if not iv:
        lines.append("未提供 `--snapshot-db`，略過。")
    elif "status" in iv:
        lines.append(str(iv["status"]))
    elif iv["n"] == 0:
        lines.append("與 `historical_iv` 沒有重疊的 (標的, 日期)。")
    else:
        lines += [
            "| 重疊筆數 | proxy/real 中位數 | MAE（波動點） | Pearson |",
            "| ---: | ---: | ---: | ---: |",
            f"| {iv['n']} | {_num(iv['median_ratio'])} | {_num(iv['mae_vol_points'], 1)} "
            f"| {_num(iv['pearson'])} |",
        ]
    lines += ["", "## 五、同窗比較（帳戶實際 NAV 期間）", ""]
    sw = result.get("same_window")
    if not sw:
        lines.append("未提供 `--snapshot-db`，略過。")
    elif "status" in sw:
        lines.append(str(sw["status"]))
    else:
        lines += [
            f"窗口：{sw['start']} → {sw['end']}（各策略以窗口首日收盤重新起算）。",
            "",
            "| 策略 | 窗口報酬 | 窗口 MDD |",
            "| :--- | ---: | ---: |",
        ]
        for r in sw["rows"]:
            lines.append(
                f"| {r['name']} | {_pct(r['total_return'])} | {_pct(r['max_drawdown'])} |"
            )
    lines += ["", "## 六、財報日缺漏", ""]
    missing = result["missing_earnings"]
    if missing:
        lines.append(
            "`財報日缺漏`（以下標的沒有財報日資料，不套用財報封鎖）："
            + "、".join(missing)
        )
    else:
        lines.append("`財報日缺漏`：無。")
    lines += ["", "## 七、機械化解讀與限制", ""]
    lines += [f"- {item}" for item in result["limitations"]]
    lines.append("")
    return "\n".join(lines)
