"""總經狀態判定（離線回測用，純函式 + FRED 抓取）：信用利差 + Sahm 衰退指標（+ 升息循環）。

用途：`scripts/run_macro_switch_backtest.py`，見
`docs/strategies/09_static_allocation_rebalance.md` 的「總經切換」一節。兩種用法：

- **三態配置（主）**：依亮起的警訊數決定整個投組——0 個 → 好（100% 科技池）、恰好 1 個 →
  轉差（100% VOO）、≥ `worst_min_warnings` 個 → 最差（100% BOXX）。`macro_states3()`。
- **股票 40% 固定、股票內切換（額外對照）**：任一／兩者皆亮 → 不好（股票改 VOO），
  BOXX 60% 不動。`macro_states()`。

指標與其**可用時點**（無前視的關鍵）：

1. 信用利差（FRED `BAA10Y`：Moody's Baa 公司債殖利率 − 10 年公債殖利率，每日、不修正）。
   原規格優先使用 ICE BofA 高收益債 OAS（`BAMLH0A0HYM2`），但 FRED 目前只提供該序列近 3 年
   （2023-09 起），不足以涵蓋 2007 年前的暖機期，因此改用 `BAA10Y`（1986 起）。
   警訊：利差 ≥ 其過去 `spread_ma_days` 筆觀測平均的 `spread_mult` 倍。
   可用時點：某日的數值在**次一日（嚴格晚於觀測日）**才可用——H.15 殖利率於次一營業日公布。

2. Sahm 衰退指標（FRED `SAHMREALTIME`：以當時公布的失業率計算的即時版，避免事後修正偏差）。
   警訊：數值 ≥ `sahm_threshold`。月資料，某月的數值視為在**次月 `sahm_release_day` 日**
   （遇非交易日順延到下一個交易日）才可用。

3. 聯準會升息循環（變體才使用；FRED `DFF`：聯邦基金有效利率，每日含週末、不修正）。
   先依可用時點（嚴格晚於觀測日）對齊到交易日曆，警訊：當日值較 `hike_lookback` 個交易日前
   上升 ≥ `hike_threshold` 個百分點。2022 年是升息造成、沒有衰退的空頭，只用前兩個指標可能
   無法辨識，故另設此變體。

判定（`combine`）：ANY = 任一警訊亮即「不好」；ALL = 兩個都亮才「不好」。任一指標尚無可用
數值時為未知（NaN）。狀態切換需連續 `confirm_days` 個交易日成立（沿用
`regime_momentum_backtest.confirm_regime`）；以當日收盤後已知的資訊判定，次一交易日開盤
執行（`effective_state` 再 shift 1）。

只讀寫 `.calibration_cache/fred/`，不碰 production DB。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import pandas as pd

from calibration.data_store import DataStore
from calibration.regime_momentum_backtest import confirm_regime

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
SPREAD_SERIES = "BAA10Y"
SPREAD_SERIES_PREFERRED = "BAMLH0A0HYM2"  # 歷史不足（FRED 僅近 3 年），見模組說明
SAHM_SERIES = "SAHMREALTIME"
FED_SERIES = "DFF"
FRED_SERIES: tuple[str, ...] = (
    SPREAD_SERIES,
    SAHM_SERIES,
    FED_SERIES,
    SPREAD_SERIES_PREFERRED,
)

MacroState = Literal["GOOD", "BAD"]
Combine = Literal["ANY", "ALL"]
State3 = Literal["GOOD", "WEAK", "WORST"]

# 三態的目標配置（科技池、VOO、BOXX）
STATE3_WEIGHTS: dict[str, tuple[float, float, float]] = {
    "GOOD": (1.0, 0.0, 0.0),
    "WEAK": (0.0, 1.0, 0.0),
    "WORST": (0.0, 0.0, 1.0),
}


@dataclass(frozen=True)
class MacroParams:
    """所有總經判定參數集中於此；預設值即使用者規格。"""

    spread_ma_days: int = 126  # 利差均線：過去 126 筆每日觀測（約 6 個月）
    spread_mult: float = 1.2  # 利差 ≥ 均線 × 1.2 即亮警訊
    sahm_threshold: float = 0.50
    sahm_release_day: int = 10  # 某月數值於次月 10 日（遇非交易日順延）才可用
    combine: Combine = "ANY"
    confirm_days: int = 5
    good_tech_share: float = 1.0  # 總經好：股票全放科技池
    bad_tech_share: float = 0.0  # 總經不好：股票全放 VOO
    # 回測起點前尚無可判定狀態時採用的狀態（2007 年 BAA10Y 與 Sahm 都已有長歷史，實際不會用到）
    default_state: MacroState = "GOOD"
    # --- 三態配置 ---
    include_fed_hike: bool = False  # 變體：加入升息循環指標
    hike_lookback: int = 126  # 交易日
    hike_threshold: float = 1.0  # 百分點
    worst_min_warnings: int = 2  # 亮起 ≥ 此數 → 最差；恰好 1 個 → 轉差

    def label3(self) -> str:
        base = "三態＋升息" if self.include_fed_hike else "三態"
        parts = [
            f"利差 ≥ {self.spread_ma_days} 日均 ×{self.spread_mult:g}",
            f"Sahm ≥ {self.sahm_threshold:g}",
        ]
        if self.include_fed_hike:
            parts.append(f"升息 ≥ {self.hike_threshold:g}pp／{self.hike_lookback} 日")
        return f"{base}（{'、'.join(parts)}、確認 {self.confirm_days} 日）"

    def label(self) -> str:
        rule = "任一亮" if self.combine == "ANY" else "兩者皆亮"
        return (
            f"總經切換（{rule}；利差 ≥ {self.spread_ma_days} 日均 ×{self.spread_mult:g}、"
            f"Sahm ≥ {self.sahm_threshold:g}、確認 {self.confirm_days} 日）"
        )


# ---------------------------------------------------------------------------
# FRED 抓取與解析
# ---------------------------------------------------------------------------


def parse_fred_csv(text: str) -> pd.Series:
    """解析 FRED `fredgraph.csv`：第一欄為日期，第二欄為數值，`.` 為缺值（丟棄）。"""
    df = pd.read_csv(io.StringIO(text))
    if df.shape[1] < 2:
        raise ValueError("FRED CSV 欄位不足")
    dates = pd.to_datetime(df.iloc[:, 0])
    values = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s = pd.Series(values.to_numpy(dtype=float), index=pd.DatetimeIndex(dates))
    return s.dropna().sort_index()


def fetch_fred(series: str, cache_dir: Path, timeout: float = 60.0) -> int:
    """下載 FRED 序列到 `.calibration_cache/fred/<series>.csv`，回傳有效觀測筆數。

    寫檔一律經 `DataStore`（calibration 套件只允許 data_store.py／report.py 寫檔）。
    """
    import urllib.request

    url = FRED_CSV_URL.format(series=series)
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        text = resp.read().decode("utf-8")
    parsed = parse_fred_csv(text)
    DataStore(cache_dir).save_fred_csv(series, text)
    return len(parsed)


def load_fred(series: str, cache_dir: Path) -> pd.Series:
    text = DataStore(cache_dir).load_fred_csv(series)
    if text is None:
        raise FileNotFoundError(
            f"缺少 FRED 快取 {series}；請先執行 python -m calibration fetch-fred"
        )
    return parse_fred_csv(text)


# ---------------------------------------------------------------------------
# 指標與可用時點
# ---------------------------------------------------------------------------


def _as_of(
    values: pd.Series, available_on: pd.DatetimeIndex, calendar: pd.DatetimeIndex
) -> pd.Series:
    """把「在 available_on[i] 才可用的 values[i]」對齊到交易日曆：每個交易日取已可用者中最新的一筆。"""
    s = pd.Series(values.to_numpy(), index=available_on)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.reindex(calendar.union(s.index)).ffill().reindex(calendar)


def spread_warning(
    spread: pd.Series, calendar: pd.DatetimeIndex, ma_days: int, mult: float
) -> pd.Series:
    """信用利差警訊（1.0 / 0.0；均線資料不足或尚無可用觀測時 NaN），對齊到交易日曆。

    均線以觀測序列本身計算（含當日）；觀測日 t 的結果在**嚴格晚於 t** 的第一個交易日才可用。
    """
    s = spread.astype(float).sort_index()
    ma = s.rolling(ma_days, min_periods=ma_days).mean()
    warn = pd.Series(np.where(s >= mult * ma, 1.0, 0.0), index=s.index)
    warn[ma.isna()] = np.nan
    # 觀測日 + 1 天：交易日 d 可用 ⇔ 觀測日 < d
    available = pd.DatetimeIndex(s.index + pd.Timedelta(days=1))
    return _as_of(warn, available, calendar)


def sahm_available_dates(
    obs_months: pd.DatetimeIndex, calendar: pd.DatetimeIndex, release_day: int
) -> pd.DatetimeIndex:
    """某月（觀測日為該月 1 日）的數值於次月 `release_day` 日可用；遇非交易日順延到下一個交易日。"""
    nominal = pd.DatetimeIndex(
        [
            (pd.Timestamp(d.year, d.month, 1) + pd.offsets.MonthBegin(1))
            + pd.Timedelta(days=release_day - 1)
            for d in obs_months
        ]
    )
    cal = calendar.sort_values()
    pos = cal.searchsorted(nominal, side="left")
    out: list[pd.Timestamp] = []
    for p, nom in zip(pos, nominal):
        # 超出日曆末端：沿用名目日期（對回測期間內無影響）
        out.append(cal[p] if p < len(cal) else nom)
    return pd.DatetimeIndex(out)


def sahm_warning(
    sahm: pd.Series, calendar: pd.DatetimeIndex, threshold: float, release_day: int
) -> pd.Series:
    """Sahm 警訊（1.0 / 0.0；尚無可用數值時 NaN），依公布延遲對齊到交易日曆。"""
    s = sahm.astype(float).sort_index()
    warn = pd.Series(np.where(s >= threshold, 1.0, 0.0), index=s.index)
    available = sahm_available_dates(pd.DatetimeIndex(s.index), calendar, release_day)
    return _as_of(warn, available, calendar)


def fed_hike_warning(
    dff: pd.Series, calendar: pd.DatetimeIndex, lookback: int, threshold: float
) -> pd.Series:
    """升息循環警訊（1.0 / 0.0；回看資料不足時 NaN）。

    先把 DFF 依可用時點（嚴格晚於觀測日）對齊到交易日曆，再與 `lookback` 個交易日前比較。
    """
    s = dff.astype(float).sort_index()
    available = pd.DatetimeIndex(s.index + pd.Timedelta(days=1))
    aligned = _as_of(s, available, calendar)
    change = aligned - aligned.shift(lookback)
    warn = pd.Series(np.where(change >= threshold - 1e-12, 1.0, 0.0), index=calendar)
    warn[change.isna()] = np.nan
    return warn


def raw_state3(warnings: list[pd.Series], worst_min: int) -> pd.Series:
    """依亮起的警訊數決定三態；任一指標未知時為 NaN。"""
    frame = pd.concat(warnings, axis=1)
    known = frame.notna().all(axis=1)
    count = (frame.fillna(0.0) >= 0.5).sum(axis=1)
    raw = pd.Series(
        np.where(count >= worst_min, "WORST", np.where(count >= 1, "WEAK", "GOOD")),
        index=frame.index,
        dtype=object,
    )
    raw[~known] = np.nan
    return raw


def macro_states3(
    spread: pd.Series,
    sahm: pd.Series,
    calendar: pd.DatetimeIndex,
    params: MacroParams = MacroParams(),
    dff: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """三態逐日表：各警訊、count、raw、confirmed（收盤後已知）、effective（當日開盤採用）。"""
    sw = spread_warning(spread, calendar, params.spread_ma_days, params.spread_mult)
    hw = sahm_warning(sahm, calendar, params.sahm_threshold, params.sahm_release_day)
    cols: dict[str, pd.Series] = {"spread_warn": sw, "sahm_warn": hw}
    if params.include_fed_hike:
        if dff is None:
            raise ValueError("include_fed_hike 需要 DFF 序列")
        cols["fed_warn"] = fed_hike_warning(
            dff, calendar, params.hike_lookback, params.hike_threshold
        )
    raw = raw_state3(list(cols.values()), params.worst_min_warnings)
    confirmed = confirm_regime(raw, params.confirm_days)
    out = pd.DataFrame(cols, index=calendar)
    out["count"] = pd.concat(list(cols.values()), axis=1).sum(
        axis=1, min_count=len(cols)
    )
    out["raw"] = raw
    out["confirmed"] = confirmed
    out["effective"] = confirmed.shift(1)
    return out


def targets_series(effective: pd.Series, equity_scale: float = 1.0) -> pd.DataFrame:
    """把當日採用的三態轉成目標配置（TECH／VOO／BOXX 權重）；未知狀態視為「好」。

    `equity_scale` = X（取捨分析用）：好 = X 科技池 + (1−X) BOXX、轉差 = X VOO + (1−X) BOXX、
    最差仍為 100% BOXX。X = 1 即使用者規格。
    """
    x = min(max(equity_scale, 0.0), 1.0)
    state = effective.where(effective.notna(), "GOOD")
    rows: list[tuple[float, float, float]] = []
    for v in state.tolist():
        tech, core, cash = STATE3_WEIGHTS[str(v)]
        if str(v) == "WORST":
            rows.append((tech, core, cash))
        else:
            rows.append((tech * x, core * x, 1.0 - (tech + core) * x))
    return pd.DataFrame(rows, index=effective.index, columns=["TECH", "VOO", "BOXX"])


def raw_macro_state(
    spread_warn: pd.Series, sahm_warn: pd.Series, combine: Combine
) -> pd.Series:
    """逐日原始狀態（GOOD／BAD）；任一指標未知時為 NaN。"""
    both_known = spread_warn.notna() & sahm_warn.notna()
    a = spread_warn.fillna(0.0) >= 0.5
    b = sahm_warn.fillna(0.0) >= 0.5
    bad = (a | b) if combine == "ANY" else (a & b)
    raw = pd.Series(np.where(bad, "BAD", "GOOD"), index=spread_warn.index, dtype=object)
    raw[~both_known] = np.nan
    return raw


def macro_states(
    spread: pd.Series,
    sahm: pd.Series,
    calendar: pd.DatetimeIndex,
    params: MacroParams = MacroParams(),
) -> pd.DataFrame:
    """回傳逐日表：spread_warn、sahm_warn、raw、confirmed（收盤後已知）、effective（當日開盤採用）。"""
    sw = spread_warning(spread, calendar, params.spread_ma_days, params.spread_mult)
    hw = sahm_warning(sahm, calendar, params.sahm_threshold, params.sahm_release_day)
    raw = raw_macro_state(sw, hw, params.combine)
    confirmed = confirm_regime(raw, params.confirm_days)
    effective = confirmed.shift(1)
    return pd.DataFrame(
        {
            "spread_warn": sw,
            "sahm_warn": hw,
            "raw": raw,
            "confirmed": confirmed,
            "effective": effective,
        },
        index=calendar,
    )


def tech_share_series(
    effective: pd.Series, params: MacroParams = MacroParams()
) -> pd.Series:
    """把當日採用的狀態轉成股票內科技池占比（GOOD → good_tech_share、BAD → bad_tech_share）。"""
    state = effective.where(effective.notna(), params.default_state)
    return pd.Series(
        np.where(state == "BAD", params.bad_tech_share, params.good_tech_share),
        index=effective.index,
        dtype=float,
    )


# ---------------------------------------------------------------------------
# 狀態區段統計
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateEpisode:
    state: str
    start: pd.Timestamp
    end: pd.Timestamp  # 含；最後一段為期末
    days: int


def state_episodes(
    state: pd.Series, start: Optional[str] = None, end: Optional[str] = None
) -> list[StateEpisode]:
    """連續同狀態的區段（只計非 NaN）。"""
    s = state.dropna()
    if start is not None:
        s = s[s.index >= pd.Timestamp(start)]
    if end is not None:
        s = s[s.index <= pd.Timestamp(end)]
    out: list[StateEpisode] = []
    if s.empty:
        return out
    values = s.tolist()
    idx = s.index
    begin = 0
    for j in range(1, len(values) + 1):
        if j == len(values) or values[j] != values[begin]:
            out.append(
                StateEpisode(str(values[begin]), idx[begin], idx[j - 1], j - begin)
            )
            begin = j
    return out


def whipsaw_count(episodes: list[StateEpisode], window: int) -> int:
    """切換後 `window` 個交易日內又切回原狀態的次數（即中間那段長度 ≤ window）。"""
    count = 0
    for k in range(1, len(episodes) - 1):
        mid = episodes[k]
        if mid.days <= window and episodes[k - 1].state == episodes[k + 1].state:
            count += 1
    return count
