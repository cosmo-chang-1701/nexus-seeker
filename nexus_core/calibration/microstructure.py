"""微結構校準資料：GEX 牆體深度 (D-04) 與週預期波幅到期日選擇 (D-03)。

為什麼需要自己蒐集：production 的 GEX 快取全是 upsert，歷史期權鏈也拿不到，
所以「牆體深度 vs 事後是否守住」無法回測，只能從今天起逐日累積。

- `micro-snapshot`：經 `market_data_service`（edge 代理 → 本地 yfinance）對標的池抓取期權鏈，用與 `nexus_edge_scraper/gex_scraper.py`
  相同的公式重算 GEX 剖面（Yahoo 預設頁面＝最近到期日、t 下限 2 天、|Δ|<0.02
  雜訊過濾、`OI × 100 × Γ × S²`），並記錄 20 日平均成交額、各到期日推算的週 EM。
  每天一個 JSONL 檔，建議每個交易日收盤後執行一次。
- `micro-report`：彙整所有快照，輸出牆體深度的橫斷面分布、現行 500k 絕對門檻
  在不同規模標的上的通過率，以及以後續日線標註的「支撐牆守住率 × 深度分組」。

只寫 `cache_dir` 與報告目錄，不寫資料庫、不改程式碼。
"""

import asyncio
import logging
import math
import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from calibration.data_store import read_jsonl_dir, write_jsonl

logger = logging.getLogger(__name__)

SNAPSHOT_SUBDIR = "microstructure"

# 與 nexus_edge_scraper/gex_scraper.py 相同的常數
_GEX_MIN_DELTA_THRESHOLD = 0.02
_RISK_FREE_RATE = 0.04
_MIN_T_DAYS = 2.0

# 週 EM 候選到期日範圍（日曆日）
_EM_MAX_DTE = 14


# ---------------------------------------------------------------------------
# Black-Scholes（與 edge 相同）
# ---------------------------------------------------------------------------


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(s: float, k: float, t: float, sigma: float, r: float, q: float) -> float:
    return (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (
        sigma * math.sqrt(t)
    )


def bs_gamma(
    s: float, k: float, t: float, sigma: float, r: float, q: float = 0.0
) -> float:
    if s <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        return 0.0
    return (
        math.exp(-q * t) * _npdf(_d1(s, k, t, sigma, r, q)) / (s * sigma * math.sqrt(t))
    )


def bs_delta(
    s: float, k: float, t: float, sigma: float, r: float, is_call: bool, q: float = 0.0
) -> float:
    if s <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        return 0.0
    n = _ncdf(_d1(s, k, t, sigma, r, q))
    return math.exp(-q * t) * (n if is_call else n - 1.0)


@dataclass(frozen=True)
class Contract:
    strike: float
    oi: float
    iv: Optional[float]
    t: float
    is_call: bool


def compute_gex_profile(contracts: list[Contract], spot: float) -> dict[str, Any]:
    """edge `scrape_symbol_gex_core` 的計算部分（不含抓取）。"""
    valid_ivs = [c.iv for c in contracts if c.iv is not None and c.iv > 0.01]
    adaptive_iv = float(statistics.median(valid_ivs)) if valid_ivs else 0.30
    chain = [
        Contract(
            c.strike,
            c.oi,
            c.iv if c.iv and c.iv > 0.01 else adaptive_iv,
            c.t,
            c.is_call,
        )
        for c in contracts
        if c.oi > 0
    ]

    def _delta_ok(c: Contract) -> bool:
        d = bs_delta(
            spot, c.strike, c.t, float(c.iv or adaptive_iv), _RISK_FREE_RATE, c.is_call
        )
        return abs(d) >= _GEX_MIN_DELTA_THRESHOLD

    calls = [c for c in chain if c.is_call]
    puts = [c for c in chain if not c.is_call]
    chain = ([c for c in calls if _delta_ok(c)] or calls) + (
        [c for c in puts if _delta_ok(c)] or puts
    )

    net_by_strike: dict[float, float] = {}
    call_by_strike: dict[float, float] = {}
    put_by_strike: dict[float, float] = {}
    net_gex = 0.0
    for c in chain:
        g = bs_gamma(spot, c.strike, c.t, float(c.iv or adaptive_iv), _RISK_FREE_RATE)
        raw = c.oi * 100.0 * g * spot * spot
        if c.is_call:
            call_by_strike[c.strike] = call_by_strike.get(c.strike, 0.0) + raw
            signed = raw
        else:
            put_by_strike[c.strike] = put_by_strike.get(c.strike, 0.0) + raw
            signed = -raw
        net_gex += signed
        net_by_strike[c.strike] = net_by_strike.get(c.strike, 0.0) + signed

    def _argmax(d: dict[float, float], below: bool) -> tuple[float, float]:
        cands = {
            k: v
            for k, v in d.items()
            if v > 0
            and not math.isclose(k, spot, abs_tol=1e-4)
            and ((k < spot) if below else (k > spot))
        }
        if not cands:
            return 0.0, 0.0
        k = max(cands, key=lambda x: cands[x])
        return k, cands[k]

    # 核心端的「支撐牆」定義：現價下方淨 GEX 最大正值（structural_signals.py）
    support_strike, support_gex = _argmax(net_by_strike, below=True)
    # edge 的 put_wall／call_wall 定義：各邊單側 GEX 最大值
    put_wall, put_wall_gex = _argmax(put_by_strike, below=True)
    call_wall, call_wall_gex = _argmax(call_by_strike, below=False)
    return {
        "net_gex": net_gex,
        "total_abs_gex": sum(abs(v) for v in net_by_strike.values()),
        "support_strike": support_strike,
        "support_gex": support_gex,
        "put_wall": put_wall,
        "put_wall_gex": put_wall_gex,
        "call_wall": call_wall,
        "call_wall_gex": call_wall_gex,
        "n_contracts": len(chain),
    }


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


def _mid(row: Any) -> float:
    bid = float(row.get("bid", 0.0) or 0.0)
    ask = float(row.get("ask", 0.0) or 0.0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return float(row.get("lastPrice", 0.0) or 0.0)


def _chain_contracts(chain: Any, dte: int) -> list[Contract]:
    t = max(float(dte), _MIN_T_DAYS) / 365.0
    out: list[Contract] = []
    for frame, is_call in ((chain.calls, True), (chain.puts, False)):
        if frame is None or frame.empty:
            continue
        for _, r in frame.iterrows():
            try:
                oi = float(r.get("openInterest", 0) or 0)
                if math.isnan(oi):
                    oi = 0.0
                iv_raw = r.get("impliedVolatility")
                iv = (
                    float(iv_raw)
                    if iv_raw is not None and not math.isnan(float(iv_raw))
                    else None
                )
                out.append(Contract(float(r["strike"]), oi, iv, t, is_call))
            except (TypeError, ValueError, KeyError):
                continue
    return out


def _straddle_em(chain: Any, spot: float, dte: int) -> Optional[dict[str, Any]]:
    calls, puts = chain.calls, chain.puts
    if calls is None or puts is None:
        return None
    calls = calls.dropna(subset=["strike"])
    puts = puts.dropna(subset=["strike"])
    if calls.empty or puts.empty:
        return None
    c = calls.loc[(calls["strike"] - spot).abs().idxmin()]
    p = puts.loc[(puts["strike"] - spot).abs().idxmin()]
    straddle = _mid(c) + _mid(p)
    if straddle <= 0:
        return None
    one_sigma = straddle * math.sqrt(math.pi / 2.0)
    return {
        "dte": float(dte),
        "straddle": straddle,
        # 現行 iv_metrics._calculate_straddle_implied_em 的縮放（日曆日 √(7/max(1,dte))）
        "em_weekly_calendar": one_sigma * math.sqrt(7.0 / max(1.0, float(dte))),
    }


async def snapshot_symbol(symbol: str, today: date) -> Optional[dict[str, Any]]:
    """抓取一檔標的的日線與期權鏈並計算快照。

    經 `services.market_data_service` 抓取，與 bot 同樣走三階降級（edge 快照 →
    edge 即時抓取 → 本地 yfinance），因此可以在資料中心 IP 被 Yahoo 封鎖的
    VPS 上執行。期權鏈刻意不裁減履約價 (`prune_pct=None`)：GEX 牆與雜訊過濾
    需要完整期權鏈。
    """
    from services import market_data_service as mds

    hist = await mds.get_history_df(symbol, period="3mo", interval="1d")
    if hist is not None and not hist.empty:
        # 盤中或剛收盤時最後一列可能尚未完成 (Close/Volume 為 NaN)
        hist = hist.dropna(subset=["Close", "High", "Low", "Volume"])
    if hist is None or hist.empty or len(hist) < 21:
        return None
    spot = float(hist["Close"].iloc[-1])
    last20 = hist.iloc[-20:]
    adv_dollar = float((last20["Close"] * last20["Volume"]).mean())
    tr = (
        (hist["High"] - hist["Low"])
        .combine((hist["High"] - hist["Close"].shift()).abs(), max)
        .combine((hist["Low"] - hist["Close"].shift()).abs(), max)
    )
    atr_1d = float(tr.iloc[-14:].mean())

    expiries = list(await mds.get_all_option_expiries(symbol) or [])
    if not expiries:
        return None

    em_rows: list[dict[str, Any]] = []
    gex: Optional[dict[str, Any]] = None
    nearest_dte: Optional[int] = None
    for exp in sorted(expiries):
        try:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        # 快照在收盤後執行：當天到期 (DTE=0) 的合約已經結算，未平倉量歸零或
        # 失真，拿它算 GEX 牆沒有意義，一律從下一檔開始。
        if dte < 1:
            continue
        if dte > _EM_MAX_DTE and gex is not None:
            break
        chain = await mds.get_option_chain(symbol, exp, prune_pct=None)
        if chain is None:
            continue
        if gex is None:
            # 最近一檔未到期的到期日，對應 edge 抓取 Yahoo 期權頁預設表格的行為
            gex = compute_gex_profile(_chain_contracts(chain, dte), spot)
            nearest_dte = dte
        if dte <= _EM_MAX_DTE:
            em = _straddle_em(chain, spot, dte)
            if em:
                em["expiry"] = exp
                em_rows.append(em)
        await asyncio.sleep(0.3)

    if gex is None:
        return None
    if gex["n_contracts"] == 0:
        # Yahoo 約在美東午夜到開盤前重置期權鏈 (未平倉量歸 0、IV 1e-5)；
        # 這個時段抓到的資料不能當成當日收盤的牆體結構，整檔不寫入。
        logger.warning(
            f"[micro-snapshot] {symbol} 期權鏈未平倉量全為 0 (Yahoo 夜間重置時段？)，略過"
        )
        return None
    return {
        "date": today.isoformat(),
        "symbol": symbol,
        "spot": spot,
        "adv_dollar_20d": adv_dollar,
        "atr_1d": atr_1d,
        "nearest_dte": nearest_dte,
        "gex": gex,
        "em": em_rows,
    }


def _today_ny() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date()


def snapshot_skip_reason(
    cache_dir: Path, now_ny: Optional[datetime] = None
) -> Optional[str]:
    """排程執行前的保護：回傳略過原因，可以執行時回傳 None。

    - 美東當天不是 NYSE 交易日（週末、國定假日）
    - 當天尚未收盤（半日市以 13:00 ET 為界）：盤中的期權鏈與日線都還沒定案
    - 當天的快照已存在：cron 重試或手動重跑時不重複抓取
    """
    import market_time

    now = now_ny or datetime.now(market_time.ny_tz)
    day = now.date()
    bounds = market_time.get_session_bounds_utc(day, day)
    if day.isoformat() not in bounds:
        return f"{day.isoformat()} 不是交易日"
    close_utc = datetime.strptime(bounds[day.isoformat()][1], "%Y-%m-%d %H:%M:%S")
    if now.astimezone(timezone.utc).replace(tzinfo=None) < close_utc:
        return f"{day.isoformat()} 尚未收盤"
    target = Path(cache_dir) / SNAPSHOT_SUBDIR / f"snapshot_{day.isoformat()}.jsonl"
    if target.exists():
        return f"{target.name} 已存在"
    return None


async def run_snapshot(
    symbols: list[str], cache_dir: Path, today: Optional[date] = None
) -> Path:
    # DTE 以美東日期計算；開發機若在其他時區，date.today() 會多算或少算一天
    today = today or _today_ny()
    out_dir = Path(cache_dir) / SNAPSHOT_SUBDIR
    target = out_dir / f"snapshot_{today.isoformat()}.jsonl"
    records: list[dict[str, Any]] = []
    for sym in symbols:
        try:
            rec = await snapshot_symbol(sym, today)
        except Exception as e:
            logger.warning(f"[micro-snapshot] {sym} 失敗: {e}")
            rec = None
        if rec:
            records.append(rec)
        await asyncio.sleep(0.5)
    ok = write_jsonl(target, records)
    logger.info(f"[micro-snapshot] {ok}/{len(symbols)} 檔寫入 {target}")
    return target


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------

CURRENT_THIN_WALL_THRESHOLD = 500_000.0


def load_snapshots(cache_dir: Path) -> list[dict[str, Any]]:
    return read_jsonl_dir(Path(cache_dir) / SNAPSHOT_SUBDIR, "snapshot_*.jsonl")


def depth_ratio(gex_value: float, adv_dollar: float) -> Optional[float]:
    """牆體深度 = 股價每變動 1% 時做市商的避險名目 ÷ 20 日平均成交額。

    edge 的 `OI × 100 × Γ × S²` 是「每 100% 變動」的尺度，乘 0.01 才是每 1%。
    """
    if adv_dollar <= 0 or gex_value <= 0:
        return None
    return gex_value * 0.01 / adv_dollar


def _quantiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    v = sorted(values)

    def q(p: float) -> float:
        idx = min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))
        return v[idx]

    return {
        "p10": q(0.10),
        "p25": q(0.25),
        "p50": q(0.50),
        "p75": q(0.75),
        "p90": q(0.90),
    }


def label_wall_holds(
    snapshots: list[dict[str, Any]], horizon_days: int = 5
) -> list[dict[str, Any]]:
    """以快照日之後的日線標註支撐牆是否守住（只用快照日之後的資料，無前視偏差）。

    - tested：後續 `horizon_days` 個交易日內，最低價曾進入牆上方 0.5 × ATR 以內
    - held：tested 且期間收盤價從未跌破牆
    """
    import yfinance as yf

    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for s in snapshots:
        if s["gex"].get("support_strike", 0) > 0:
            by_symbol.setdefault(s["symbol"], []).append(s)

    labeled: list[dict[str, Any]] = []
    for sym, snaps in by_symbol.items():
        first = min(s["date"] for s in snaps)
        try:
            hist = yf.Ticker(sym).history(start=first, interval="1d", auto_adjust=False)
        except Exception as e:
            logger.warning(f"[micro-report] {sym} 日線抓取失敗: {e}")
            continue
        if hist is None or hist.empty:
            continue
        idx_dates = [ts.date().isoformat() for ts in hist.index]
        for s in snaps:
            after = [i for i, d in enumerate(idx_dates) if d > s["date"]][:horizon_days]
            if len(after) < horizon_days:
                continue  # 尚未走完觀察期
            wall = float(s["gex"]["support_strike"])
            band = wall + 0.5 * float(s.get("atr_1d") or 0.0)
            lows = [float(hist["Low"].iloc[i]) for i in after]
            closes = [float(hist["Close"].iloc[i]) for i in after]
            tested = min(lows) <= band
            labeled.append(
                {
                    "symbol": sym,
                    "date": s["date"],
                    "support_gex": s["gex"]["support_gex"],
                    "depth_ratio": depth_ratio(
                        s["gex"]["support_gex"], s["adv_dollar_20d"]
                    ),
                    "tested": tested,
                    "held": tested and min(closes) >= wall,
                }
            )
        time.sleep(0.3)
    return labeled


def build_micro_report(
    cache_dir: Path,
    horizon_days: int = 5,
    snapshots: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """`snapshots` 未提供時讀取 micro-snapshot 的快取；edge 來源由呼叫端先轉好傳入。"""
    snaps = snapshots if snapshots is not None else load_snapshots(cache_dir)
    if not snaps:
        return {"status": "尚無快照資料", "n_snapshots": 0}

    from market_analysis.gex_wall_depth import thin_wall_threshold

    ratios: list[float] = []
    abs_pass = 0
    new_pass = 0
    with_wall = 0
    by_size: dict[str, dict[str, Any]] = {}
    for s in snaps:
        g = s["gex"]
        if g.get("support_gex", 0) <= 0:
            continue
        with_wall += 1
        r = depth_ratio(g["support_gex"], s["adv_dollar_20d"])
        if r is not None:
            ratios.append(r)
        passed = g["support_gex"] >= CURRENT_THIN_WALL_THRESHOLD
        passed_new = g["support_gex"] >= thin_wall_threshold(s["adv_dollar_20d"])
        abs_pass += int(passed)
        new_pass += int(passed_new)
        adv = s["adv_dollar_20d"]
        bucket = (
            "ADV<$50M"
            if adv < 5e7
            else "ADV $50M-$500M"
            if adv < 5e8
            else "ADV $500M-$5B"
            if adv < 5e9
            else "ADV>$5B"
        )
        b = by_size.setdefault(
            bucket, {"n": 0, "abs_pass": 0, "new_pass": 0, "ratios": []}
        )
        b["n"] += 1
        b["abs_pass"] += int(passed)
        b["new_pass"] += int(passed_new)
        if r is not None:
            b["ratios"].append(r)

    size_table = {
        k: {
            "n": v["n"],
            "現行500k通過率": round(v["abs_pass"] / v["n"], 3) if v["n"] else None,
            "成交額正規化門檻通過率": round(v["new_pass"] / v["n"], 3)
            if v["n"]
            else None,
            "深度比分位": _quantiles(v["ratios"]),
        }
        for k, v in sorted(by_size.items())
    }

    # D-03：各到期日推算的週 EM 相對於「最接近 7 DTE 的直接量測」的偏差
    em_bias: dict[str, list[float]] = {}
    for s in snaps:
        em_rows: list[dict[str, Any]] = s.get("em") or []
        ref = min(em_rows, key=lambda e: abs(e["dte"] - 7), default=None)
        if not ref or abs(ref["dte"] - 7) > 2:
            continue
        for em in em_rows:
            if em is ref or ref["em_weekly_calendar"] <= 0:
                continue
            key = f"DTE={int(em['dte'])}" if em["dte"] <= 3 else "DTE 4-14"
            em_bias.setdefault(key, []).append(
                em["em_weekly_calendar"] / ref["em_weekly_calendar"]
            )

    labeled = label_wall_holds(snaps, horizon_days)
    hold_table: dict[str, Any] = {}
    if labeled:
        valid = sorted(
            x["depth_ratio"] for x in labeled if x["depth_ratio"] is not None
        )
        if len(valid) >= 4:
            cuts = [valid[len(valid) * i // 4] for i in (1, 2, 3)]
            for x in labeled:
                if x["depth_ratio"] is None or not x["tested"]:
                    continue
                qi = sum(1 for c in cuts if x["depth_ratio"] >= c)
                h = hold_table.setdefault(f"Q{qi + 1}", {"tested": 0, "held": 0})
                h["tested"] += 1
                h["held"] += int(x["held"])
            for h in hold_table.values():
                h["hold_rate"] = (
                    round(h["held"] / h["tested"], 3) if h["tested"] else None
                )
            hold_table["quartile_cuts"] = cuts

    return {
        "n_snapshots": len(snaps),
        "snapshot_dates": sorted({s["date"] for s in snaps}),
        "n_with_support_wall": with_wall,
        "現行500k絕對門檻通過率": round(abs_pass / with_wall, 3) if with_wall else None,
        "成交額正規化門檻通過率": round(new_pass / with_wall, 3) if with_wall else None,
        "深度比全體分位": _quantiles(ratios),
        "依成交額分組": size_table,
        "週EM相對7DTE直接量測之比值": {
            k: {"n": len(v), **_quantiles(v)} for k, v in sorted(em_bias.items())
        },
        "支撐牆守住率_依深度四分位": hold_table
        or f"尚無走完 {horizon_days} 個交易日觀察期的快照",
        "n_labeled": len(labeled),
    }
