"""校準流程編排：fetch → 逐標的 (特徵 → 事件 → 標註) → 校準器 → 報告。

記憶體：逐標的處理，每檔處理完只保留標註後的事件列並 gc，K 線 frame 以 float32
從快取載入。目標峰值 < 300 MB。
"""

import gc
import logging
from typing import Any, Optional

import pandas as pd

from calibration.calibrators import kelly_priors, room_atr, rsi_threshold, vix_short
from calibration.config import CalibrationConfig
from calibration.data_store import DataStore
from calibration.events import (
    apply_default_thresholds,
    detect_regime_proxy_events,
    detect_scanner_events,
    random_control_events,
)
from calibration.features import daily_features, hourly_features
from calibration.fetcher import HistoryFetcher
from calibration.labeling import label_events
from calibration.parameter_registry import ParameterResult
from calibration.stats import summarize

logger = logging.getLogger(__name__)

VIX_SYMBOL = "^VIX"
VIX3M_SYMBOL = "^VIX3M"


async def fetch_all(
    cfg: CalibrationConfig,
    store: DataStore,
    fetcher: HistoryFetcher,
    symbols: list[str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for sym in [VIX_SYMBOL, VIX3M_SYMBOL]:
        df = await fetcher.fetch(sym, "max", "1d")
        summary[f"1d/{sym}"] = store.save("1d", sym, df)
    for sym in symbols:
        daily = await fetcher.fetch(sym, "max", "1d")
        if not daily.empty:
            idx = pd.to_datetime(daily.index, utc=True)
            daily = daily[idx >= pd.Timestamp(cfg.daily_start, tz="UTC")]
        summary[f"1d/{sym}"] = store.save("1d", sym, daily)
        hourly = await fetcher.fetch(sym, cfg.hourly_period, "1h")
        if hourly.empty:
            # 上市日早於 730 天的標的，yfinance 會把 "729d" 的起點夾到上市日，
            # 反而超出 1h 資料的 730 天上限而整批拒絕；退回 "2y" 重試。
            hourly = await fetcher.fetch(sym, "2y", "1h")
        summary[f"1h/{sym}"] = store.save("1h", sym, hourly)
    return summary


def _load(
    store: DataStore, cfg: CalibrationConfig, interval: str, symbol: str
) -> Optional[pd.DataFrame]:
    return (
        store.require(interval, symbol) if cfg.offline else store.load(interval, symbol)
    )


def process_symbol(
    symbol: str, store: DataStore, cfg: CalibrationConfig, vix: Optional[pd.Series]
) -> pd.DataFrame:
    daily = _load(store, cfg, "1d", symbol)
    if daily is None or len(daily) < 300:
        return pd.DataFrame()
    dfeat = daily_features(daily)
    frames = []
    scanner = detect_scanner_events(symbol, dfeat, vix)
    frames.append(
        label_events(
            scanner, daily, cfg.k_grid, cfg.primary_k, cfg.max_sessions, cfg.room_grid
        )
    )
    hourly = _load(store, cfg, "1h", symbol)
    if hourly is not None and len(hourly) > 200:
        hfeat = hourly_features(hourly, dfeat)
        proxies = detect_regime_proxy_events(symbol, hfeat, vix)
        control = random_control_events(
            symbol, hfeat, apply_default_thresholds(proxies), vix, cfg.seed
        )
        for ev in (proxies, control):
            frames.append(
                label_events(
                    ev,
                    hourly,
                    cfg.k_grid,
                    cfg.primary_k,
                    cfg.max_sessions,
                    cfg.room_grid,
                )
            )
        del hfeat, hourly
    del dfeat, daily
    gc.collect()
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def event_tables(labeled: pd.DataFrame, cfg: CalibrationConfig) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    if labeled.empty:
        return tables
    for (etype, side), grp in labeled.groupby(["event_type", "side"]):
        st = summarize(grp, cfg.n_boot, cfg.seed, cfg.oos_split)
        tables.append(
            {
                "event_type": etype,
                "side": side,
                "n": st.n,
                "n_dates": st.n_dates,
                "win_rate": st.win_rate,
                "wilson": [st.wilson_lo, st.wilson_hi],
                "expectancy": st.expectancy,
                "expectancy_ci": [st.exp_lo, st.exp_hi],
                "oos_expectancy": st.oos_expectancy,
            }
        )
    return tables


def run_calibrators(
    labeled_loose: pd.DataFrame, cfg: CalibrationConfig
) -> list[ParameterResult]:
    labeled = (
        apply_default_thresholds(labeled_loose)
        if not labeled_loose.empty
        else labeled_loose
    )
    results: list[ParameterResult] = []
    results += vix_short.calibrate(labeled, cfg)
    results += kelly_priors.calibrate(labeled, cfg)
    results += rsi_threshold.calibrate(labeled_loose, cfg)
    results += room_atr.calibrate(labeled, cfg)
    return results


def run_event_study(
    cfg: CalibrationConfig, store: DataStore, symbols: list[str]
) -> tuple[pd.DataFrame, list[ParameterResult], list[dict[str, Any]]]:
    vix_df = _load(store, cfg, "1d", VIX_SYMBOL)
    vix = vix_df["Close"] if vix_df is not None and not vix_df.empty else None
    labeled_frames = []
    for sym in symbols:
        try:
            frame = process_symbol(sym, store, cfg, vix)
        except Exception as e:
            if cfg.offline and e.__class__.__name__ == "CacheMissError":
                raise
            logger.warning(f"[calibration] {sym} 處理失敗: {e}")
            continue
        if not frame.empty:
            labeled_frames.append(frame)
    labeled = (
        pd.concat(labeled_frames, ignore_index=True)
        if labeled_frames
        else pd.DataFrame()
    )
    params = run_calibrators(labeled, cfg)
    tables = event_tables(
        apply_default_thresholds(labeled) if not labeled.empty else labeled, cfg
    )
    return labeled, params, tables
