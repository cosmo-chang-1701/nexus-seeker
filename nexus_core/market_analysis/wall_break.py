"""GEX 牆突破／跌破分類器（純邏輯，無網路、無 DB）。

錨點為前一交易日收盤牆（edge 的牆位相對當下現價重算，當日牆永遠不會被收盤突破）。
三值邏輯（Kleene）：PASS=True、FAIL=False、UNKNOWN=None；資料不足不等於失敗。
所有門檻皆為 PRE_CALIBRATION，尚未校準。
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Optional, Sequence

Direction = Literal["BREAKOUT", "BREAKDOWN"]
TriggerKind = Literal["CLOSE_BEYOND", "WICK_REJECT"]
GateStatus = Literal["PASS", "FAIL", "UNKNOWN"]
IntradayVerdict = Literal["CANDIDATE", "FAKE", "UNDETERMINED"]
FinalVerdict = Literal["CONFIRMED", "FAKE", "UNDETERMINED"]

# PRE_CALIBRATION
PENETRATION_ATR_MULT = 0.5
PENETRATION_SPOT_PCT_FALLBACK = 0.005
RVOL_PASS_MIN = 1.5
DAY_VOLUME_SMA_MULT = 1.3
SKEW_BULLISH_MAX_PCT = 50.0
SKEW_BEARISH_MIN_PCT = 80.0
GEX_HYSTERESIS_RATIO = 1.10
RETEST_STOP_PCT_MIN = 0.006
RETEST_STOP_PCT_MAX = 0.010


@dataclass(frozen=True)
class WallAnchor:
    trade_date: str
    call_wall: Optional[float]
    put_wall: Optional[float]
    gex_expiry: Optional[str] = None


@dataclass(frozen=True)
class BarSnapshot:
    bar_time: datetime
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: float
    volume: float
    avg_volume_20: Optional[float]
    tod_avg_volume: Optional[float] = None
    atr_15m: Optional[float] = None
    projected_day_volume: Optional[float] = None
    daily_volume_sma20: Optional[float] = None


@dataclass(frozen=True)
class OptionsSnapshot:
    gex_profile: Mapping[float, float]
    skew_percentile: Optional[float]
    skew_is_fallback: bool
    uoa: Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class WallBreakTrigger:
    direction: Direction
    trigger: TriggerKind
    old_wall: float
    buffer: float
    penetration: float
    anchor_trade_date: str
    bar_time: datetime
    close: float


@dataclass(frozen=True)
class GateResult:
    key: str
    label: str
    status: GateStatus
    detail: str


def _label_for(direction: Direction, verdict: str) -> str:
    if verdict in ("CANDIDATE", "CONFIRMED"):
        prefix = "TRUE"
    elif verdict == "FAKE":
        prefix = "FAKE"
    else:
        prefix = "UNDETERMINED"
    return f"{prefix}_{direction}"


@dataclass(frozen=True)
class WallBreakResult:
    trigger: WallBreakTrigger
    gates: tuple[GateResult, ...]
    verdict: IntradayVerdict

    @property
    def label(self) -> str:
        return _label_for(self.trigger.direction, self.verdict)


@dataclass(frozen=True)
class T1EventInput:
    direction: Direction
    old_wall: float
    intraday_verdict: IntradayVerdict
    old_wall_oi: Optional[float]
    gex_expiry: Optional[str]


@dataclass(frozen=True)
class T1Snapshot:
    trade_date: str
    spot: Optional[float]
    call_wall: Optional[float]
    put_wall: Optional[float]
    gex_expiry: Optional[str]
    gex_profile: Mapping[float, float]
    old_wall_oi_now: Optional[float]


@dataclass(frozen=True)
class T1Result:
    checks: tuple[GateResult, ...]
    verdict: FinalVerdict
    direction: Optional[Direction] = None

    @property
    def label(self) -> str:
        return _label_for(self.direction or "BREAKOUT", self.verdict)


# ---------------------------------------------------------------- Kleene 三值邏輯


def _to_tri(status: GateStatus) -> Optional[bool]:
    if status == "PASS":
        return True
    if status == "FAIL":
        return False
    return None


def _tri_and(vals: Sequence[Optional[bool]]) -> Optional[bool]:
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals):
        return None
    return True


def _tri_or(vals: Sequence[Optional[bool]]) -> Optional[bool]:
    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals):
        return None
    return False


def _status(v: Optional[bool]) -> GateStatus:
    if v is None:
        return "UNKNOWN"
    return "PASS" if v else "FAIL"


# ---------------------------------------------------------------- 觸發


def penetration_buffer(wall: float, atr_15m: Optional[float]) -> float:
    if atr_15m is not None and atr_15m > 0:
        return PENETRATION_ATR_MULT * atr_15m
    return PENETRATION_SPOT_PCT_FALLBACK * wall


def detect_wall_break_triggers(
    anchor: WallAnchor, bar: BarSnapshot
) -> list[WallBreakTrigger]:
    out: list[WallBreakTrigger] = []
    cw = anchor.call_wall
    if cw is not None and cw > 0:
        buf = penetration_buffer(cw, bar.atr_15m)
        if bar.close >= cw + buf:
            out.append(
                WallBreakTrigger(
                    "BREAKOUT",
                    "CLOSE_BEYOND",
                    cw,
                    buf,
                    bar.close - cw,
                    anchor.trade_date,
                    bar.bar_time,
                    bar.close,
                )
            )
        elif bar.high is not None and bar.high >= cw + buf and bar.close < cw:
            out.append(
                WallBreakTrigger(
                    "BREAKOUT",
                    "WICK_REJECT",
                    cw,
                    buf,
                    bar.high - cw,
                    anchor.trade_date,
                    bar.bar_time,
                    bar.close,
                )
            )
    pw = anchor.put_wall
    if pw is not None and pw > 0:
        buf = penetration_buffer(pw, bar.atr_15m)
        if bar.close <= pw - buf:
            out.append(
                WallBreakTrigger(
                    "BREAKDOWN",
                    "CLOSE_BEYOND",
                    pw,
                    buf,
                    pw - bar.close,
                    anchor.trade_date,
                    bar.bar_time,
                    bar.close,
                )
            )
        elif bar.low is not None and bar.low <= pw - buf and bar.close > pw:
            out.append(
                WallBreakTrigger(
                    "BREAKDOWN",
                    "WICK_REJECT",
                    pw,
                    buf,
                    pw - bar.low,
                    anchor.trade_date,
                    bar.bar_time,
                    bar.close,
                )
            )
    return out


# ---------------------------------------------------------------- 閘門


def _gate_penetration(t: WallBreakTrigger, bar: BarSnapshot) -> GateResult:
    if t.trigger == "WICK_REJECT":
        detail = "上影線收回牆內" if t.direction == "BREAKOUT" else "下影線收回牆上"
        return GateResult("wick", "影線假突破", "FAIL", detail)
    basis = (
        f"{PENETRATION_ATR_MULT}×ATR15m"
        if bar.atr_15m is not None and bar.atr_15m > 0
        else f"{PENETRATION_SPOT_PCT_FALLBACK * 100:.1f}%×牆位"
    )
    return GateResult(
        "penetration",
        "穿透幅度",
        "PASS",
        f"穿透 ${t.penetration:.2f} ≥ 緩衝 ${t.buffer:.2f}（{basis}）",
    )


def _gate_body(t: WallBreakTrigger, bar: BarSnapshot) -> GateResult:
    if bar.open is None:
        return GateResult("body", "K 棒實體", "UNKNOWN", "缺開盤價")
    if t.direction == "BREAKOUT":
        ok = bar.close > bar.open
        detail = "收紅 K（收盤高於開盤）" if ok else "收盤未高於開盤"
    else:
        ok = bar.close < bar.open
        detail = "收黑 K（收盤低於開盤）" if ok else "收盤未低於開盤"
    return GateResult("body", "K 棒實體", _status(ok), detail)


def _gate_rvol(bar: BarSnapshot) -> GateResult:
    if bar.tod_avg_volume is not None and bar.tod_avg_volume > 0:
        base, src = bar.tod_avg_volume, "同時段"
    elif bar.avg_volume_20 is not None and bar.avg_volume_20 > 0:
        base, src = bar.avg_volume_20, "20 根"
    else:
        return GateResult("rvol", "相對量能", "UNKNOWN", "無量能基準")
    r = bar.volume / base
    return GateResult(
        "rvol", "相對量能", _status(r >= RVOL_PASS_MIN), f"RVOL {r:.2f}x（{src}）"
    )


def _gate_day_volume(bar: BarSnapshot) -> GateResult:
    p, s = bar.projected_day_volume, bar.daily_volume_sma20
    if p is None or s is None or s <= 0:
        return GateResult("day_volume", "全日量推估", "UNKNOWN", "全日量資料不足")
    r = p / s
    return GateResult(
        "day_volume",
        "全日量推估",
        _status(p >= DAY_VOLUME_SMA_MULT * s),
        f"推估全日量 {r:.2f}x 20 日均量",
    )


def _eligible_uoa(
    uoa: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for u in uoa:
        if u.get("structure") == "STRADDLE":
            continue
        if u.get("spread_role") == "SHORT_LEG" and not u.get("spread_credit"):
            continue
        out.append(u)
    return out


def _gate_flow(
    t: WallBreakTrigger, bar: BarSnapshot, opt: OptionsSnapshot
) -> GateResult:
    entries = _eligible_uoa(opt.uoa)
    close = bar.close
    if t.direction == "BREAKOUT":
        for u in entries:
            strike = u.get("strike")
            if (
                u.get("type") == "CALL"
                and u.get("action") == "STO"
                and strike is not None
                and float(strike) > close
                and not u.get("level_breached")
            ):
                return GateResult(
                    "flow", "選擇權流向", "FAIL", f"STO Call ${float(strike):g} 封頂"
                )
        for u in entries:
            if u.get("type") == "CALL" and u.get("action") == "BTO":
                return GateResult("flow", "選擇權流向", "PASS", "有 BTO Call 大單支持")
        return GateResult(
            "flow", "選擇權流向", "UNKNOWN", "無可判定方向的 Call 大單（資料不足）"
        )
    for u in entries:
        strike = u.get("strike")
        if (
            u.get("type") == "PUT"
            and u.get("action") == "STO"
            and strike is not None
            and float(strike) < close
            and not u.get("level_breached")
        ):
            return GateResult(
                "flow", "選擇權流向", "FAIL", f"STO Put ${float(strike):g} 承接"
            )
    for u in entries:
        if (
            u.get("type") == "PUT"
            and u.get("action") == "BTO"
            and "Whale_Hedge" not in str(u.get("intent") or "")
        ):
            return GateResult("flow", "選擇權流向", "PASS", "有 BTO Put 恐慌買盤")
    return GateResult(
        "flow", "選擇權流向", "UNKNOWN", "無可判定方向的 Put 大單（資料不足）"
    )


def _gate_skew(t: WallBreakTrigger, opt: OptionsSnapshot) -> GateResult:
    pct = opt.skew_percentile
    if pct is None or opt.skew_is_fallback:
        return GateResult("skew", "偏斜百分位", "UNKNOWN", "Skew 百分位資料不足")
    detail = f"Skew 百分位 {pct:.0f}"
    bull = pct <= SKEW_BULLISH_MAX_PCT
    bear = pct > SKEW_BEARISH_MIN_PCT
    if t.direction == "BREAKOUT":
        v: Optional[bool] = True if bull else (False if bear else None)
    else:
        v = True if bear else (False if bull else None)
    if v is None:
        detail += "（中性）"
    return GateResult("skew", "偏斜百分位", _status(v), detail)


def _profile_at(profile: Mapping[float, float], strike: float) -> Optional[float]:
    for k, v in profile.items():
        if abs(float(k) - strike) < 1e-6:
            return v
    return None


def _gate_local_gex(t: WallBreakTrigger, opt: OptionsSnapshot) -> GateResult:
    g = _profile_at(opt.gex_profile, t.old_wall)
    if g is None:
        return GateResult("local_gex", "舊牆淨 GEX", "UNKNOWN", "舊牆履約價無 GEX 資料")
    if t.direction == "BREAKOUT":
        ok = g > 0
        detail = f"舊牆淨 GEX {g:,.0f}" + ("" if ok else "（非正，缺乏阻力轉支撐）")
    else:
        ok = g < 0
        detail = f"舊牆淨 GEX {g:,.0f}" + (
            "（負 GEX 助跌）" if ok else "（正 GEX 托盤）"
        )
    return GateResult("local_gex", "舊牆淨 GEX", _status(ok), detail)


def classify_wall_break(
    trigger: WallBreakTrigger, bar: BarSnapshot, options: OptionsSnapshot
) -> WallBreakResult:
    pen = _gate_penetration(trigger, bar)
    body = _gate_body(trigger, bar)
    rvol = _gate_rvol(bar)
    dayv = _gate_day_volume(bar)
    flow = _gate_flow(trigger, bar, options)
    skew = _gate_skew(trigger, options)
    lgex = _gate_local_gex(trigger, options)
    gates = (pen, body, rvol, dayv, flow, skew, lgex)

    verdict: IntradayVerdict
    if trigger.trigger == "WICK_REJECT":
        verdict = "FAKE"
    else:
        tb, tr, td = _to_tri(body.status), _to_tri(rvol.status), _to_tri(dayv.status)
        tf, ts, tg = _to_tri(flow.status), _to_tri(skew.status), _to_tri(lgex.status)
        if trigger.direction == "BREAKOUT":
            ok = _tri_and([tb, tr, td, tf, ts, tg])
        else:
            ok = _tri_and([_tri_or([_tri_and([tb, tr, td]), tg]), tf, ts])
        verdict = (
            "CANDIDATE" if ok is True else ("FAKE" if ok is False else "UNDETERMINED")
        )
    return WallBreakResult(trigger, gates, verdict)


# ---------------------------------------------------------------- T+1


def evaluate_t1(event: T1EventInput, snapshot: T1Snapshot) -> T1Result:
    checks: list[GateResult] = []

    if event.old_wall_oi is None or snapshot.old_wall_oi_now is None:
        checks.append(GateResult("delta_oi", "舊牆 OI 變化", "UNKNOWN", "OI 資料不足"))
    else:
        d = snapshot.old_wall_oi_now - event.old_wall_oi
        checks.append(
            GateResult(
                "delta_oi",
                "舊牆 OI 變化",
                _status(d <= 0),
                f"舊牆 OI {event.old_wall_oi:,.0f} → "
                f"{snapshot.old_wall_oi_now:,.0f}（{d:+,.0f}）",
            )
        )

    if event.direction == "BREAKOUT":
        if (
            event.gex_expiry is not None
            and snapshot.gex_expiry is not None
            and event.gex_expiry != snapshot.gex_expiry
        ):
            checks.append(
                GateResult(
                    "hysteresis",
                    "新牆 GEX 遲滯",
                    "UNKNOWN",
                    "GEX 到期日已換檔，牆位不可比",
                )
            )
        else:
            nw = snapshot.call_wall
            g_new = _profile_at(snapshot.gex_profile, nw) if nw is not None else None
            if nw is None or g_new is None:
                checks.append(
                    GateResult(
                        "hysteresis", "新牆 GEX 遲滯", "UNKNOWN", "新牆 GEX 資料不足"
                    )
                )
            else:
                g_old = _profile_at(snapshot.gex_profile, event.old_wall)
                base = max(g_old or 0.0, 0.0)
                ok = g_new > 0 and g_new >= GEX_HYSTERESIS_RATIO * base
                checks.append(
                    GateResult(
                        "hysteresis",
                        "新牆 GEX 遲滯",
                        _status(ok),
                        f"新牆 ${nw:g} GEX {g_new:,.0f} vs 舊牆 {base:,.0f}"
                        f"（需 ≥ {GEX_HYSTERESIS_RATIO:.2f}x）",
                    )
                )

    spot = snapshot.spot
    if spot is None:
        checks.append(GateResult("hold", "收盤站穩", "UNKNOWN", "缺現價"))
    elif event.direction == "BREAKOUT":
        ok = spot >= event.old_wall
        checks.append(
            GateResult(
                "hold",
                "收盤站穩",
                _status(ok),
                f"現價 ${spot:.2f} ≥ 舊牆 ${event.old_wall:.2f}"
                if ok
                else "已收回牆內",
            )
        )
    else:
        ok = spot <= event.old_wall
        checks.append(
            GateResult(
                "hold",
                "收盤站穩",
                _status(ok),
                f"現價 ${spot:.2f} ≤ 舊牆 ${event.old_wall:.2f}"
                if ok
                else "已收復牆上",
            )
        )

    verdict: FinalVerdict
    if event.intraday_verdict == "FAKE":
        verdict = "FAKE"
    else:
        ok_all = _tri_and([_to_tri(c.status) for c in checks])
        verdict = (
            "CONFIRMED"
            if ok_all is True
            else ("FAKE" if ok_all is False else "UNDETERMINED")
        )
    return T1Result(tuple(checks), verdict, event.direction)


def gates_to_json(gates: Sequence[GateResult]) -> str:
    return json.dumps(
        [
            {"key": g.key, "label": g.label, "status": g.status, "detail": g.detail}
            for g in gates
        ],
        ensure_ascii=False,
    )
