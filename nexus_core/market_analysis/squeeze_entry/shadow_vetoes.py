"""擠壓進場「影子否決」前向紀錄：只算原始值與旗標寫入 features_json，不參與任何判定。

使用者提案把技術面進場改為「微結構／盈虧比／總經」一票否決，但既有證據（docs/strategies/10
§3.2：壓力區擋下組 20 日超額不比放行組差）不支持直接硬擋，因此比照
docs/architecture/05 §5.17／§5.18 採「呈現先行、前向紀錄」：把各項否決的輸入與旗標存下來，
日後以 forward-report 判斷哪些值得接進判定。

欄位契約（鍵名逐字固定，報表端依此讀取）見 `compute_shadow_vetoes` 的回傳。
"""

import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# 以下門檻皆為 PRE_CALIBRATION／僅紀錄，不參與判定。
# PRE_CALIBRATION／僅紀錄，不參與判定：上方牆距離 ≤ N 倍日線 ATR 視為天花板過近
SHADOW_CEILING_ATR = 1.5
# PRE_CALIBRATION／僅紀錄，不參與判定：Skew 分位 ≥ 此值視為偏態過熱
SHADOW_SKEW_PCTL = 80.0
# PRE_CALIBRATION／僅紀錄，不參與判定：Max Pain 到期日 ≤ N 個日曆天才檢查磁吸
SHADOW_MP_MAX_DTE = 2
# PRE_CALIBRATION／僅紀錄，不參與判定：現價高於 Max Pain 超過此百分比視為有下拉磁吸
SHADOW_MP_DIST_PCT = 3.0
# PRE_CALIBRATION／僅紀錄，不參與判定：牆位盈虧比低於此值視為不划算
SHADOW_MIN_RR = 2.0
# PRE_CALIBRATION／僅紀錄，不參與判定：停損寬度超過此百分比視為過寬
SHADOW_MAX_STOP_PCT = 10.0
# PRE_CALIBRATION／僅紀錄，不參與判定：高影響總經事件前 N 小時內視為事件窗
SHADOW_MACRO_EVENT_HOURS = 48.0
# PRE_CALIBRATION／僅紀錄，不參與判定：高影響總經事件公布後 N 小時內視為事件窗
SHADOW_MACRO_POST_HOURS = 2.0

# 停損緩衝：PutWall 下方 0.5 × ATR（與契約 sv_rr 定義一致）
_WALL_STOP_ATR_MULT = 0.5

SHADOW_KEYS: tuple[str, ...] = (
    "sv_cw",
    "sv_cw_atr",
    "sv_sw",
    "sv_sw_atr",
    "sv_pw",
    "sv_rr",
    "sv_wstop_pct",
    "sv_stop_pct",
    "sv_mp_dist",
    "sv_mp_dte",
    "sv_ngex",
    "sv_mev_h",
    "sv_gex_stale",
    "sv_f_ceil",
    "sv_f_skew",
    "sv_f_mp",
    "sv_f_rr",
    "sv_f_wide",
    "sv_f_mev",
    "sv_any",
)

_FLAG_KEYS: tuple[str, ...] = (
    "sv_f_ceil",
    "sv_f_skew",
    "sv_f_mp",
    "sv_f_rr",
    "sv_f_wide",
    "sv_f_mev",
)


def _fnum(value: Any) -> Optional[float]:
    """只過濾非數值／非有限值；0.0 保留（不可用 evaluation_recorder._num）。"""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def _parse_swamp(raw: Any) -> Optional[float]:
    """泥淖 strike：接受長度 2 的 tuple／list；(0.0, 0.0) 為無泥淖。"""
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        return None
    strike = _fnum(raw[0])
    if strike is None or strike <= 0:
        return None
    return strike


def _tte_hours_from_time(time_str: Any, now: datetime) -> Optional[float]:
    """依事件 `time`（ISO，可能以 Z 結尾）與當下時間重算 tte 小時。"""
    try:
        dt = datetime.fromisoformat(str(time_str).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - now).total_seconds() / 3600.0


async def nearest_macro_event_tte_hours() -> tuple[bool, Optional[float]]:
    """(日曆是否成功, 最近高影響事件的重算 tte 小時)。

    `get_high_impact_events` 失敗時吞例外回傳普通 `[]`，成功才回傳
    `EconomicEventList`；故以型別區分成功與失敗。事件物件上的 `tte_hours` 在抓取當下
    凍結（快取無 TTL），一律改用 `time` 與當下時間重算，取 tte ≥ −2 的最小值。
    """
    try:
        from services.calendar_service import EconomicEventList, calendar_service

        result = await calendar_service.get_high_impact_events(days=7)
        if not isinstance(result, EconomicEventList):
            return False, None
        now = datetime.now(timezone.utc)
        best: Optional[float] = None
        for ev in result:
            tte = _tte_hours_from_time(getattr(ev, "time", None), now)
            if tte is None or tte < -SHADOW_MACRO_POST_HOURS:
                continue
            if best is None or tte < best:
                best = tte
        return True, best
    except Exception as e:
        logger.debug(f"[ShadowVeto] 總經日曆讀取失敗: {e}")
        return False, None


def compute_shadow_vetoes(
    spot: float,
    radar: Optional[Mapping[str, Any]],
    atr_1d: Any,
    stop: Any,
    skew_percentile: Any,
    macro_event_tte_h: Optional[float],
    macro_calendar_ok: bool,
    today: date,
) -> dict[str, Any]:
    """純函式：回傳全部 `sv_*` 鍵；缺輸入為 None。僅供紀錄，不得用於判定。"""
    r: Mapping[str, Any] = radar if isinstance(radar, Mapping) else {}
    spot_f = _fnum(spot)
    atr = _fnum(atr_1d)
    if atr is not None and atr <= 0:
        atr = None
    if spot_f is not None and spot_f <= 0:
        spot_f = None

    gex_raw = r.get("gex_profile_data")
    gex: Optional[Mapping[str, Any]] = gex_raw if isinstance(gex_raw, Mapping) else None

    # --- 牆位 ---
    cw: Optional[float] = None
    pw: Optional[float] = None
    ngex: Optional[int] = None
    if gex is not None:
        c = _fnum(gex.get("call_wall"))
        if c is not None and spot_f is not None and c > spot_f:
            cw = c
        p = _fnum(gex.get("put_wall"))
        if p is not None and spot_f is not None and 0 < p < spot_f:
            pw = p
        n = _fnum(gex.get("net_gex"))
        if n is not None and n != 0:
            ngex = 1 if n > 0 else -1
    sw = _parse_swamp(r.get("overhead_neg_gex_swamp"))
    if sw is not None and (spot_f is None or sw <= spot_f):
        sw = None

    stale_raw = gex.get("_is_stale_cache") if gex is not None else None
    gex_stale: Optional[bool] = stale_raw if isinstance(stale_raw, bool) else None

    cw_atr = (
        (cw - spot_f) / atr if cw is not None and spot_f is not None and atr else None
    )
    sw_atr = (
        (sw - spot_f) / atr if sw is not None and spot_f is not None and atr else None
    )

    # --- 盈虧比與停損寬度 ---
    targets = [t for t in (cw, sw) if t is not None]
    target = min(targets) if targets else None
    wall_stop = pw - _WALL_STOP_ATR_MULT * atr if pw is not None and atr else None
    rr: Optional[float] = None
    wstop_pct: Optional[float] = None
    if wall_stop is not None and spot_f is not None:
        risk = spot_f - wall_stop
        wstop_pct = risk / spot_f * 100.0
        if target is not None and risk > 0:
            rr = (target - spot_f) / risk
    stop_f = _fnum(stop)
    stop_pct = (
        (spot_f - stop_f) / spot_f * 100.0
        if stop_f is not None and spot_f is not None and stop_f < spot_f
        else None
    )

    # --- Max Pain ---
    mp_dist: Optional[float] = None
    mp_dte: Optional[int] = None
    mp_raw = r.get("max_pain")
    if isinstance(mp_raw, Mapping):
        mp = _fnum(mp_raw.get("max_pain"))
        if (
            mp is not None
            and mp > 0
            and spot_f is not None
            and not bool(mp_raw.get("is_stale"))
        ):
            mp_dist = (spot_f - mp) / mp * 100.0
        expiry = mp_raw.get("expiry")
        if expiry:
            try:
                exp_d = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
                mp_dte = (exp_d - today).days
            except ValueError:
                mp_dte = None

    mev_h = _fnum(macro_event_tte_h)

    out: dict[str, Any] = {
        "sv_cw": _round(cw, 2),
        "sv_cw_atr": _round(cw_atr, 2),
        "sv_sw": _round(sw, 2),
        "sv_sw_atr": _round(sw_atr, 2),
        "sv_pw": _round(pw, 2),
        "sv_rr": _round(rr, 2),
        "sv_wstop_pct": _round(wstop_pct, 2),
        "sv_stop_pct": _round(stop_pct, 2),
        "sv_mp_dist": _round(mp_dist, 2),
        "sv_mp_dte": mp_dte,
        "sv_ngex": ngex,
        "sv_mev_h": _round(mev_h, 1),
        "sv_gex_stale": gex_stale,
    }

    # --- 旗標（以紀錄值判斷，報表端可由原始值重現） ---
    def _any_le(a: Optional[float], b: Optional[float], limit: float) -> Optional[bool]:
        vals = [v for v in (a, b) if v is not None]
        return any(v <= limit for v in vals) if vals else None

    def _any_gt(a: Optional[float], b: Optional[float], limit: float) -> Optional[bool]:
        vals = [v for v in (a, b) if v is not None]
        return any(v > limit for v in vals) if vals else None

    skew = _fnum(skew_percentile)
    f_mp: Optional[bool] = None
    if (
        out["sv_mp_dte"] is not None
        and out["sv_ngex"] is not None
        and out["sv_mp_dist"] is not None
    ):
        f_mp = (
            out["sv_mp_dte"] <= SHADOW_MP_MAX_DTE
            and out["sv_ngex"] == 1
            and out["sv_mp_dist"] > SHADOW_MP_DIST_PCT
        )
    f_mev: Optional[bool]
    if not macro_calendar_ok:
        f_mev = None
    elif out["sv_mev_h"] is None:
        f_mev = False
    else:
        f_mev = -SHADOW_MACRO_POST_HOURS <= out["sv_mev_h"] <= SHADOW_MACRO_EVENT_HOURS

    out["sv_f_ceil"] = _any_le(out["sv_cw_atr"], out["sv_sw_atr"], SHADOW_CEILING_ATR)
    out["sv_f_skew"] = None if skew is None else skew >= SHADOW_SKEW_PCTL
    out["sv_f_mp"] = f_mp
    out["sv_f_rr"] = None if out["sv_rr"] is None else out["sv_rr"] < SHADOW_MIN_RR
    out["sv_f_wide"] = _any_gt(
        out["sv_stop_pct"], out["sv_wstop_pct"], SHADOW_MAX_STOP_PCT
    )
    out["sv_f_mev"] = f_mev

    flags = [out[k] for k in _FLAG_KEYS]
    if any(f is True for f in flags):
        out["sv_any"] = True
    elif all(f is False for f in flags):
        out["sv_any"] = False
    else:
        out["sv_any"] = None
    return out
