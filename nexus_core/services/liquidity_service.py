"""央行淨流動性與體制分類服務 (Liquidity Service)。

職責：
1. 抓取聯準會資產負債表 (WALCL, WTREGEN, WRESBAL)、逆回購 (RRPONTSYD)、金融狀況指數 (NFCI, ANFCI) 與美債 (DGS10)。
2. 寫入 macro_series_observation 保留首次所見值。
3. 計算央行淨流動性 (Net Liquidity) 與 13 週季化動態變更率。
4. 判定流動性體制 (EASY, NEUTRAL, TIGHT, UNKNOWN) 與動態股權風險溢價 (ERP)。
5. 寫入 liquidity_regime_log 資料表。
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta

from database.fundamental_pipeline import save_liquidity_regime
from database.macro_signal_log import store_observations
from market_analysis.fundamental_pipeline.liquidity_regime import (
    calculate_13w_change,
    calculate_dynamic_erp,
    calculate_net_liquidity,
    classify_liquidity_regime,
)
from market_analysis.fundamental_pipeline.models import (
    LiquidityReading,
)
from market_analysis.macro_signals import Observation, SeriesKind, usable
from services.macro_signal_service import fetch_fred_series

logger = logging.getLogger(__name__)

# 流動性相關 FRED 序列與發布節奏
LIQUIDITY_FRED_SERIES: dict[str, SeriesKind] = {
    "NFCI": "weekly_nfci",
    "ANFCI": "weekly_nfci",
    "WALCL": "weekly_h41",
    "WTREGEN": "weekly_h41",
    "WRESBAL": "weekly_h41",
    "RRPONTSYD": "daily",
    "DGS10": "daily",
}


def _latest_val(obs: list[Observation]) -> float | None:
    """取得觀測序列中最新的數值。"""
    return obs[-1].value if obs else None


def _find_prior_obs(
    obs: list[Observation],
    target_date: date,
    window_days: int = 21,
) -> float | None:
    """在 target_date 附近尋找最貼近的歷史觀測值。"""
    candidates = [
        o
        for o in obs
        if abs((o.obs_date - target_date).days) <= window_days
        and not math.isnan(o.value)
        and not math.isinf(o.value)
    ]
    if not candidates:
        return None
    # 選擇與 target_date 差距最小者
    best = min(candidates, key=lambda o: abs((o.obs_date - target_date).days))
    return best.value


def compute_net_liquidity_series(
    walcl_obs: list[Observation],
    wtregen_obs: list[Observation],
    rrp_obs: list[Observation],
) -> list[Observation]:
    """依 WALCL 觀測日期對齊 WTREGEN 與 RRPONTSYD，計算淨流動性時間序列。"""
    if not walcl_obs or not wtregen_obs or not rrp_obs:
        return []

    # 排序各觀測序列以保證時間單調性並過濾非有限數值
    walcl_sorted = sorted(
        [o for o in walcl_obs if not math.isnan(o.value) and not math.isinf(o.value)],
        key=lambda o: o.obs_date,
    )
    wtregen_sorted = sorted(
        [o for o in wtregen_obs if not math.isnan(o.value) and not math.isinf(o.value)],
        key=lambda o: o.obs_date,
    )
    rrp_sorted = sorted(
        [o for o in rrp_obs if not math.isnan(o.value) and not math.isinf(o.value)],
        key=lambda o: o.obs_date,
    )

    if not walcl_sorted or not wtregen_sorted or not rrp_sorted:
        return []

    # 建立日期索引字典
    wtregen_dict: dict[date, Observation] = {o.obs_date: o for o in wtregen_sorted}

    net_liq_series: list[Observation] = []
    for w in walcl_sorted:
        # 1. 尋找當日或最近可用的 WTREGEN
        tga_obs = wtregen_dict.get(w.obs_date)
        if tga_obs is None:
            # 尋找前一個可用的 TGA
            prev_tgas = [o for o in wtregen_sorted if o.obs_date <= w.obs_date]
            if not prev_tgas:
                continue
            tga_obs = prev_tgas[-1]

        # 2. 尋找當日或最近可用的 RRPONTSYD
        prev_rrps = [o for o in rrp_sorted if o.obs_date <= w.obs_date]
        if not prev_rrps:
            continue
        rrp_obs_match = prev_rrps[-1]

        liq_bn = calculate_net_liquidity(w.value, tga_obs.value, rrp_obs_match.value)
        # 淨流動性可用日取 WALCL、TGA、RRP 之最大可用日以防範前視偏差
        eff_avail = max(
            w.available_date, tga_obs.available_date, rrp_obs_match.available_date
        )
        net_liq_series.append(Observation(w.obs_date, liq_bn, eff_avail))

    return sorted(net_liq_series, key=lambda o: o.obs_date)


async def run_liquidity_pipeline(today: date) -> LiquidityReading:
    """執行流動性分析管線：抓取、精算、判定體制並寫入資料庫。"""
    logger.info(f"[LiquidityService] 啟動流動性分析管線 (交易日: {today})")

    series_data: dict[str, list[Observation]] = {}

    # 1. 抓取並存儲各序列觀測值
    for sid, kind in LIQUIDITY_FRED_SERIES.items():
        try:
            obs = await fetch_fred_series(sid, today, kind=kind)
            await store_observations(sid, obs, today)
            # 僅保留 today 當天已可用的觀測
            series_data[sid] = usable(obs, today)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LiquidityService] 抓取/寫入 FRED 序列 {sid} 失敗: {e}")
            series_data[sid] = []

    # 2. 提取最新宏觀數值
    latest_nfci = _latest_val(series_data.get("NFCI", []))
    latest_anfci = _latest_val(series_data.get("ANFCI", []))
    latest_us10y = _latest_val(series_data.get("DGS10", []))

    # 3. 計算淨流動性歷史序列
    walcl_usable = series_data.get("WALCL", [])
    wtregen_usable = series_data.get("WTREGEN", [])
    rrp_usable = series_data.get("RRPONTSYD", [])

    net_liq_series = compute_net_liquidity_series(
        walcl_usable, wtregen_usable, rrp_usable
    )

    latest_net_liq: float | None = None
    net_liq_chg_13w: float | None = None

    if net_liq_series:
        latest_obs = net_liq_series[-1]
        latest_net_liq = round(latest_obs.value, 3)
        # 13 週 (91 天) 前之基準日
        target_13w_date = latest_obs.obs_date - timedelta(days=91)
        base_13w_liq = _find_prior_obs(net_liq_series, target_13w_date)
        if base_13w_liq is not None:
            raw_chg = calculate_13w_change(latest_net_liq, base_13w_liq)
            if raw_chg is not None:
                net_liq_chg_13w = round(raw_chg, 3)

    # 4. 計算銀行準備金 (WRESBAL) 13 週變更率
    reserves_usable = series_data.get("WRESBAL", [])
    reserves_chg_13w: float | None = None
    if reserves_usable:
        latest_res = reserves_usable[-1]
        target_13w_res = latest_res.obs_date - timedelta(days=91)
        base_res = _find_prior_obs(reserves_usable, target_13w_res)
        if base_res is not None:
            raw_res_chg = calculate_13w_change(latest_res.value, base_res)
            if raw_res_chg is not None:
                reserves_chg_13w = round(raw_res_chg, 3)

    # 5. 體制判定與動態 ERP
    regime = classify_liquidity_regime(latest_nfci, net_liq_chg_13w)
    erp = calculate_dynamic_erp(latest_nfci)
    if erp is not None:
        erp = round(erp, 5)

    reading = LiquidityReading(
        trading_date=today,
        nfci=latest_nfci,
        anfci=latest_anfci,
        net_liquidity_bn=latest_net_liq,
        net_liquidity_chg_13w_pct=net_liq_chg_13w,
        reserves_chg_13w_pct=reserves_chg_13w,
        us10y=latest_us10y,
        regime=regime,
        equity_risk_premium=erp,
    )

    # 6. 持久化至資料庫
    try:
        await save_liquidity_regime(reading)
        logger.info(
            f"[LiquidityService] 成功更新流動性體制: 日期={today}, 體制={regime}, "
            f"淨流動性=${latest_net_liq}B (13w變更: {net_liq_chg_13w}%), "
            f"NFCI={latest_nfci}, ERP={erp}"
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"[LiquidityService] 寫入 liquidity_regime_log 失敗: {e}")

    return reading
