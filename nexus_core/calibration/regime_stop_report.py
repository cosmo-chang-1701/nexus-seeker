"""體制停損 κ 離線比較（`python -m calibration regime-stop-report`）。

問題：持有於 GEX 錨點之上的部位，「依體制調整 κ」的停損（stop = anchor − κ×ATR15m）
是否優於現行固定 κ=0.5？只產出報告，不修改任何正式參數。純離線：只讀 production
快照 DB 的事件表（唯讀）、edge GEX 歷史與日線路徑，不寫任何資料庫。
定義與限制見 docs/architecture/05_calibration_harness_and_forward_collection.md §5.19。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from calibration.microstructure import (
    _MIN_LABELED_DATES,
    _MIN_TESTED_PER_DEFINITION,
    _mean_stats,
    _rate_stats,
    net_gex_at_strike,
)
from market_analysis.dynamic_rollover.constants import (
    _MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT,
)
from market_analysis.dynamic_rollover.structural_signals import (
    _resolve_canonical_anchor_base,
)
from market_analysis.room_threshold import _BARS_PER_SESSION

logger = logging.getLogger(__name__)

KAPPA_GRID: tuple[float, ...] = (0.0, 0.4, 0.5, 1.0, 1.75, 3.0)
CURRENT_KAPPA: float = float(_MICROSTRUCTURE_SL_STRUCTURAL_ATR_MULT)
SPEC_KAPPA: dict[str, float] = {
    "LONG_GAMMA": 1.75,
    "SHORT_GAMMA": 0.40,
    "EVENT_RISK": 0.0,
}
EVENT_WINDOW_HOURS: float = 48.0
HORIZONS: tuple[int, ...] = (5, 10)
MAJOR_MACRO_KEYWORDS: tuple[str, ...] = (
    "FOMC",
    "Interest Rate",
    "利率決策",
    "CPI",
    "Inflation Rate",
    "Non Farm",
    "Nonfarm",
    "Non-Farm",
    "非農",
)
MACRO_EXCLUDE_KEYWORDS: tuple[str, ...] = ("Minutes", "Revision")

REASON_NET_GEX = "個股淨GEX<=0"
REASON_PUTWALL = "PutWall處淨GEX<0"
GROUP_ALL = "全部"
GROUP_NET = f"SHORT_GAMMA｜{REASON_NET_GEX}"
GROUP_PW = f"SHORT_GAMMA｜{REASON_PUTWALL}"
GROUPS: tuple[str, ...] = (
    "LONG_GAMMA",
    "SHORT_GAMMA",
    GROUP_NET,
    GROUP_PW,
    "EVENT_RISK",
    GROUP_ALL,
)
INSUFFICIENT = "樣本不足，不判讀，維持現行 0.5×ATR15m"

PathLoader = Callable[[str, date], Any]
# 一個交易日的 (open, low, close)
Bar = tuple[float, float, float]


# ---------------------------------------------------------------- 事件載入
def _parse_ts(raw: Any) -> Optional[datetime]:
    """ISO 時間字串轉 UTC-aware datetime；無時區視為 UTC；無法解析回傳 None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_major_macro(name: str) -> bool:
    low = name.lower()
    if any(x.lower() in low for x in MACRO_EXCLUDE_KEYWORDS):
        return False
    return any(k.lower() in low for k in MAJOR_MACRO_KEYWORDS)


def load_event_times(
    snapshot_db: Path,
) -> tuple[dict[str, list[datetime]], list[datetime]]:
    """(財報 8-K Item 2.02 的 UTC 時間 by 標的, 重大總經事件 UTC 時間)。缺表只警告。"""
    from database.connection import connect_external_readonly

    earnings: dict[str, list[datetime]] = defaultdict(list)
    macro: list[datetime] = []
    conn = connect_external_readonly(str(snapshot_db))
    try:
        try:
            rows = conn.execute(
                "SELECT symbol, items, accepted_at FROM sec_filing_event "
                "WHERE form LIKE ?",
                ("8-K%",),
            ).fetchall()
            for sym, items, accepted in rows:
                parts = {p.strip() for p in str(items or "").split(",")}
                ts = _parse_ts(accepted)
                if "2.02" in parts and ts is not None and sym:
                    earnings[str(sym).upper()].append(ts)
        except Exception as e:
            logger.warning(f"[regime_stop] 讀取 sec_filing_event 失敗: {e}")
        try:
            rows = conn.execute(
                "SELECT event, event_time FROM economic_calendar_events"
            ).fetchall()
            for name, event_time in rows:
                ts = _parse_ts(event_time)
                if ts is not None and is_major_macro(str(name or "")):
                    macro.append(ts)
        except Exception as e:
            logger.warning(f"[regime_stop] 讀取 economic_calendar_events 失敗: {e}")
    finally:
        conn.close()
    return {k: sorted(v) for k, v in earnings.items()}, sorted(macro)


def hours_to_next_event(
    close_utc: datetime,
    symbol_earnings: Sequence[datetime],
    macro: Sequence[datetime],
    window_hours: float = EVENT_WINDOW_HOURS,
) -> Optional[float]:
    """收盤後最近事件的小時數（0 < Δ ≤ 窗）；沒有則 None。"""
    best: Optional[float] = None
    for ev in list(symbol_earnings) + list(macro):
        h = (ev - close_utc).total_seconds() / 3600.0
        if 0.0 < h <= window_hours and (best is None or h < best):
            best = h
    return best


# ---------------------------------------------------------------- 體制／錨點
def classify_regime(
    gex: dict[str, Any], event_hours: Optional[float]
) -> tuple[str, Optional[str]]:
    """(體制, 原因)。體制為 EVENT_RISK / SHORT_GAMMA / LONG_GAMMA / UNKNOWN。"""
    if event_hours is not None:
        return "EVENT_RISK", None
    if not gex or gex.get("net_gex") is None:
        return "UNKNOWN", None
    if float(gex["net_gex"]) <= 0:
        return "SHORT_GAMMA", REASON_NET_GEX
    pw = net_gex_at_strike(gex.get("net_profile"), float(gex.get("put_wall") or 0.0))
    if pw is not None and pw < 0:
        return "SHORT_GAMMA", REASON_PUTWALL
    return "LONG_GAMMA", None


def resolve_anchor(
    gex: dict[str, Any], spot: float
) -> tuple[Optional[float], Optional[str]]:
    """(錨點, 排除原因)。無 flip／HVN（edge 歷史沒有）。"""
    anchor = _resolve_canonical_anchor_base(
        float(gex.get("support_strike") or 0.0),
        float(gex.get("put_wall") or 0.0),
        float(gex.get("call_wall") or 0.0),
        0.0,
        0.0,
        spot,
    )
    # _resolve_canonical_anchor_base 在無任何結構錨點時回傳 spot 作為「無結構」哨兵值
    if anchor == spot:
        return None, "無結構錨點"
    if anchor >= spot:
        return None, "錨點不低於現價"
    return anchor, None


# ---------------------------------------------------------------- 路徑／模擬
def _yf_daily_unadjusted(symbol: str, start: date) -> Any:
    import yfinance as yf

    try:
        df = yf.Ticker(symbol).history(
            start=start.isoformat(), interval="1d", auto_adjust=False
        )
    except Exception as e:
        logger.warning(f"[regime_stop] {symbol} 日線抓取失敗: {e}")
        return None
    if df is None or df.empty:
        return None
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def extract_path(df: Any, snap_date: str, horizon: int) -> Optional[list[Bar]]:
    """快照日**之後**的前 horizon 根日線 (open, low, close)；不足整段回傳 None。"""
    if df is None:
        return None
    try:
        import pandas as pd

        cut = pd.Timestamp(snap_date)
        sub = df[df.index > cut].dropna(subset=["Open", "Low", "Close"])
    except Exception:
        return None
    if len(sub) < horizon:
        return None
    out: list[Bar] = []
    for _, r in sub.iloc[:horizon].iterrows():
        o, lo, c = float(r["Open"]), float(r["Low"]), float(r["Close"])
        if not all(math.isfinite(x) for x in (o, lo, c)):
            return None
        out.append((o, lo, c))
    return out


def simulate_stop(path: Sequence[Bar], spot: float, stop: float) -> tuple[float, bool]:
    """(出場報酬, 是否觸發)。開盤跳空穿停損以開盤價成交，盤中觸及以停損價成交。"""
    for o, lo, _c in path:
        if o <= stop:
            return o / spot - 1.0, True
        if lo <= stop:
            return stop / spot - 1.0, True
    return path[-1][2] / spot - 1.0, False


# ---------------------------------------------------------------- 報表
def _cvar10(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean_pct": None}
    k = max(1, math.ceil(len(values) * 0.10))
    worst = sorted(values)[:k]
    return {"n": k, "mean_pct": round(sum(worst) / k * 100, 2)}


def _kkey(k: float) -> str:
    return f"{k:g}"


def _verdict(paired: dict[str, Any]) -> str:
    ci = paired.get("ci_pct")
    if (
        paired.get("n_dates", 0) < _MIN_LABELED_DATES
        or paired.get("n", 0) < _MIN_TESTED_PER_DEFINITION
        or ci is None
    ):
        return INSUFFICIENT
    if ci[0] > 0:
        return "規格κ優於現行（仍不得直接改閘門，需前向驗證）"
    if ci[1] < 0:
        return "規格κ劣於現行"
    return "無顯著差異，維持現行"


def _group_table(events: list[dict[str, Any]], group: str) -> dict[str, Any]:
    table: dict[str, Any] = {"n": len(events)}
    by_k: dict[str, Any] = {}
    for k in KAPPA_GRID:
        key = _kkey(k)
        res = [(e["date"], e["res"][key]) for e in events]
        trig = [(d, r[1]) for d, r in res]
        excess = [(d, r[0] - e["hold"]) for (d, r), e in zip(res, events)]
        swept = [(d, r[0] - e["hold"] < 0) for (d, r), e in zip(res, events) if r[1]]
        dist = [(e["spot"] - e["stops"][key]) / e["spot"] for e in events]
        by_k[key] = {
            "n": len(events),
            "觸發率": _rate_stats(trig),
            "停損相對持有超額": _mean_stats(excess),
            "掃損率": _rate_stats(swept),
            "CVaR10_出場": _cvar10([r[0] for _, r in res]),
            "CVaR10_持有": _cvar10([e["hold"] for e in events]),
            "停損距離%": round(sum(dist) / len(dist) * 100, 2) if dist else None,
        }
    table["各κ"] = by_k
    base = group.split("｜")[0]
    if base in SPEC_KAPPA:
        sk = _kkey(SPEC_KAPPA[base])
        ck = _kkey(CURRENT_KAPPA)
        paired = _mean_stats(
            [(e["date"], e["res"][sk][0] - e["res"][ck][0]) for e in events]
        )
        table["規格κ對現行κ"] = {"規格κ": SPEC_KAPPA[base], **paired}
        table["判讀"] = _verdict(paired)
    return table


def build_regime_stop_report(
    snapshots: list[dict[str, Any]],
    earnings: dict[str, list[datetime]],
    macro: list[datetime],
    path_loader: Optional[PathLoader] = None,
    horizons: Sequence[int] = HORIZONS,
    sessions: Optional[dict[str, tuple[str, str]]] = None,
) -> dict[str, Any]:
    """純計算；`sessions`（{日期: (開, 收 UTC 字串)}）未給時以交易所日曆推得。"""
    if path_loader is None:
        path_loader = _yf_daily_unadjusted
    dates = sorted({str(s["date"]) for s in snapshots})
    if sessions is None and dates:
        from market_time import get_session_bounds_utc

        sessions = get_session_bounds_utc(
            date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        )
    sessions = sessions or {}

    counts: dict[str, int] = defaultdict(int)
    cands: list[dict[str, Any]] = []
    for s in snapshots:
        counts["快照數"] += 1
        d = str(s["date"])
        sym = str(s["symbol"]).upper()
        gex = s.get("gex") or {}
        close_raw = sessions.get(d)
        close_utc = _parse_ts(close_raw[1]) if close_raw else None
        ev_h = (
            hours_to_next_event(close_utc, earnings.get(sym, []), macro)
            if close_utc is not None
            else None
        )
        regime, reason = classify_regime(gex, ev_h)
        if regime == "UNKNOWN":
            counts["UNKNOWN(無GEX)"] += 1
            continue
        spot = float(s.get("spot") or 0.0)
        atr1d = float(s.get("atr_1d") or 0.0)
        if spot <= 0 or atr1d <= 0:
            counts["排除:ATR1D或現價無效"] += 1
            continue
        anchor, why = resolve_anchor(gex, spot)
        if anchor is None:
            counts[f"排除:{why}"] += 1
            continue
        cands.append(
            {
                "date": d,
                "symbol": sym,
                "regime": regime,
                "reason": reason,
                "spot": spot,
                "anchor": anchor,
                "atr15": atr1d / math.sqrt(_BARS_PER_SESSION),
            }
        )

    frames: dict[str, Any] = {}
    for sym in {c["symbol"] for c in cands}:
        start = date.fromisoformat(min(c["date"] for c in cands if c["symbol"] == sym))
        try:
            frames[sym] = path_loader(sym, start)
        except Exception as e:
            logger.warning(f"[regime_stop] {sym} 路徑載入失敗: {e}")
            frames[sym] = None

    result: dict[str, Any] = {
        "快照日期範圍": [dates[0], dates[-1]] if dates else None,
        "事件載入": {
            "財報標的數": len(earnings),
            "財報事件數": sum(len(v) for v in earnings.values()),
            "重大總經事件數": len(macro),
        },
        "件數": dict(counts),
        "有效候選數": len(cands),
        "各觀察期": {},
    }
    for h in horizons:
        events: list[dict[str, Any]] = []
        short_path = 0
        for c in cands:
            path = extract_path(frames.get(c["symbol"]), c["date"], h)
            if path is None:
                short_path += 1
                continue
            spot = c["spot"]
            stops = {_kkey(k): c["anchor"] - k * c["atr15"] for k in KAPPA_GRID}
            events.append(
                {
                    **c,
                    "stops": stops,
                    "res": {k: simulate_stop(path, spot, v) for k, v in stops.items()},
                    "hold": path[-1][2] / spot - 1.0,
                }
            )
        tables: dict[str, Any] = {}
        for g in GROUPS:
            if g == GROUP_ALL:
                sel = events
            elif g == GROUP_NET:
                sel = [e for e in events if e["reason"] == REASON_NET_GEX]
            elif g == GROUP_PW:
                sel = [e for e in events if e["reason"] == REASON_PUTWALL]
            else:
                sel = [e for e in events if e["regime"] == g]
            tables[g] = _group_table(sel, g)
        result["各觀察期"][f"{h}日"] = {
            "已標註": len(events),
            "路徑不足排除": short_path,
            "分組": tables,
        }
    result["備註"] = [
        "SPY Gamma Flip 沒有歷史（edge 不追蹤 SPY，regime_evaluation_log.macro_regime 全為 NULL）：總經腿（SPY 低於 Flip 即短 Gamma）無法檢驗，本報告的短 Gamma 僅含個股淨 GEX<=0 與 PutWall 處淨 GEX<0。",
        "錨點不含 Gamma Flip／HVN（edge 歷史沒有），以 support_strike／PutWall／CallWall 依現行優先序解析。",
        "以日線近似盤中 1m／15m 觸發：最低價 <= 停損視為觸價；開盤跳空穿價以開盤價成交；未計滑價。",
        "ATR15m 由 ATR1D/√26 折算，非實際 15 分鐘 ATR。",
        "連續交易日的觀察期互相重疊，日期叢集 CI 只能部分修正自相關，區間偏樂觀。",
        "事件窗：快照日收盤後 48 小時內有該標的財報（8-K Item 2.02）或重大總經事件（FOMC／利率決策／CPI／非農；排除 Minutes／Revision）。",
        "本報告只輸出比較，不修改任何正式參數；任何改動須先經前向驗證。",
    ]
    return result


def run_regime_stop_report(
    snapshots: list[dict[str, Any]],
    snapshot_db: Path,
    path_loader: Optional[PathLoader] = None,
) -> dict[str, Any]:
    earnings, macro = load_event_times(snapshot_db)
    return build_regime_stop_report(snapshots, earnings, macro, path_loader)
