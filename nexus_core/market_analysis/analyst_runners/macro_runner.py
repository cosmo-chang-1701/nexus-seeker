"""Macro data fetching and market scan logic for the Analyst Agent."""

from __future__ import annotations
from typing import Any

import asyncio
import logging
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from services import market_data_service

logger = logging.getLogger(__name__)


# PRE_CALIBRATION：FRED 殖利率觀測日距今超過此日曆天數即視為過期（週末＋長假緩衝）
_FRED_YIELD_STALE_DAYS = 5


def _compute_fred_yields(
    dgs2: list[Any], dgs10: list[Any], today: date
) -> dict[str, Any]:
    """由已儲存的 FRED 觀測計算 2Y 殖利率與 10Y−2Y 利差（純函式，無 I/O）。

    - us2y：`available_date <= today` 的最新 DGS2 值（觀測日 us2y_date）。
    - curve_spread：DGS10 − DGS2，取兩者「同一觀測日」皆存在的最新日期（curve_date）。
    - 觀測日距 today 超過 `_FRED_YIELD_STALE_DAYS` 日者視為缺失（None）。
    """
    result: dict[str, Any] = {
        "us2y": None,
        "us2y_date": None,
        "curve_spread": None,
        "curve_date": None,
    }
    usable2 = {o.obs_date: float(o.value) for o in dgs2 if o.available_date <= today}
    usable10 = {o.obs_date: float(o.value) for o in dgs10 if o.available_date <= today}
    if usable2:
        d2 = max(usable2)
        if (today - d2).days <= _FRED_YIELD_STALE_DAYS:
            result["us2y"] = usable2[d2]
            result["us2y_date"] = d2.isoformat()
    common = sorted(set(usable2) & set(usable10))
    if common:
        dc = common[-1]
        if (today - dc).days <= _FRED_YIELD_STALE_DAYS:
            result["curve_spread"] = round(usable10[dc] - usable2[dc], 2)
            result["curve_date"] = dc.isoformat()
    return result


def _read_fred_yields_sync() -> dict[str, Any]:
    from database.macro_signal_log import load_observations

    today = datetime.now(ZoneInfo("America/New_York")).date()
    return _compute_fred_yields(
        load_observations("DGS2"), load_observations("DGS10"), today
    )


async def fetch_fred_yields() -> dict[str, Any]:
    """讀取已儲存的 FRED DGS2／DGS10（不發網路請求）；任何錯誤回傳全 None，不中斷簡報。"""
    try:
        return await asyncio.to_thread(_read_fred_yields_sync)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"讀取 FRED 2Y／10Y 殖利率失敗: {e}")
        return {
            "us2y": None,
            "us2y_date": None,
            "curve_spread": None,
            "curve_date": None,
        }


async def fetch_macro_data() -> dict:
    """Fetch general macro proxies: VIX, DXY, TNX (yfinance) + 2Y／利差 (stored FRED)."""

    def _fetch() -> Any:
        tickers = yf.Tickers("^VIX DX-Y.NYB ^TNX")
        return tickers.history(period="2d")

    fred = await fetch_fred_yields()
    try:
        # 改走 call_yf：與其他 yfinance 呼叫共用節流並計入 API 配額摘要
        hist = await market_data_service.call_yf(_fetch, _endpoint="tickers_history")
        if not hist.empty and len(hist) >= 2:
            vix = float(hist["Close"]["^VIX"].iloc[-1])
            dxy = float(hist["Close"]["DX-Y.NYB"].iloc[-1])
            tnx = float(hist["Close"]["^TNX"].iloc[-1])
            vix_prev = float(hist["Close"]["^VIX"].iloc[-2])
            tnx_prev = float(hist["Close"]["^TNX"].iloc[-2])

            if math.isnan(vix_prev):
                vix_prev = vix
            vix_change = (
                vix - vix_prev if not (math.isnan(vix) or math.isnan(vix_prev)) else 0.0
            )
            tnx_change_bps = (
                (tnx - tnx_prev) * 100
                if not (math.isnan(tnx) or math.isnan(tnx_prev))
                else 0.0
            )

            return {
                "vix": round(vix, 2) if not math.isnan(vix) else float("nan"),
                "vix_change": round(vix_change, 2),
                "dxy": round(dxy, 2) if not math.isnan(dxy) else 0.0,
                "tnx": round(tnx, 2) if not math.isnan(tnx) else 0.0,
                "tnx_change_bps": round(tnx_change_bps, 1),
                **fred,
            }

        if not hist.empty:
            vix = float(hist["Close"]["^VIX"].iloc[-1])
            dxy = float(hist["Close"]["DX-Y.NYB"].iloc[-1])
            tnx = float(hist["Close"]["^TNX"].iloc[-1])
            return {
                "vix": float("nan") if math.isnan(vix) else vix,
                "vix_change": 0.0,
                "dxy": 0.0 if math.isnan(dxy) else dxy,
                "tnx": 0.0 if math.isnan(tnx) else tnx,
                "tnx_change_bps": 0.0,
                **fred,
            }

    except Exception as e:
        logger.warning(f"Failed to fetch macro proxies: {e}")

    return {
        "vix": 0.0,
        "vix_change": 0.0,
        "dxy": 0.0,
        "tnx": 0.0,
        "tnx_change_bps": 0.0,
        **fred,
    }


def format_obs_date(value: Any) -> str:
    """ISO 日期字串／date 轉 MM/DD；無法解析回傳空字串。"""
    try:
        if isinstance(value, date):
            return value.strftime("%m/%d")
        if isinstance(value, str) and value:
            return date.fromisoformat(value[:10]).strftime("%m/%d")
    except ValueError:
        pass
    return ""


def build_macro_alerts(macro_data: dict) -> list[str]:
    """Evaluate yield-curve and volatility conditions, return alert strings.

    警示文字僅陳述事實與風險，不提供減碼／轉進防禦的操作建議。
    曲線警示使用 FRED 的 10Y−2Y 利差（curve_spread）；缺值時略過，不以 tnx 推算。
    """
    vix = macro_data.get("vix", 0.0)
    vix_change = macro_data.get("vix_change", 0.0)
    dxy = macro_data.get("dxy", 0.0)
    tnx = macro_data.get("tnx", 0.0)
    tnx_change_bps = macro_data.get("tnx_change_bps", 0.0)
    spread = _num_or_none(macro_data.get("curve_spread"))
    curve_date = format_obs_date(macro_data.get("curve_date"))
    date_note = f"，{curve_date}" if curve_date else ""

    alerts: list[str] = []
    if spread is not None and spread < -0.2:
        alerts.append(
            f"殖利率曲線深度倒掛 (10Y−2Y {spread:+.2f}%{date_note})。市場反映中長期經濟衰退預期"
        )
    if spread is not None and -0.1 <= spread <= 0.2 and tnx_change_bps < 0:
        alerts.append(
            f"殖利率曲線接近解除倒掛 (10Y−2Y {spread:+.2f}%{date_note}，10Y 當日 {tnx_change_bps:+.1f} bps)"
        )
    if tnx > 4.5 and tnx_change_bps > 8:
        alerts.append(
            "10 年期殖利率突破 4.5% 且短期急升。高 Beta／高估值成長股波動可能放大"
        )
    if vix > 20 and vix_change > 2.0:
        alerts.append("恐慌指數急遽上升，市場避險情緒發酵，注意流動性風險")
    if dxy > 105:
        alerts.append("美元指數處於強勢區間，可能壓抑跨國企業獲利與大宗商品表現")
    return alerts


def _num_or_none(value: Any) -> float | None:
    """安全轉 float；None／NaN／Inf／無法轉換回傳 None。"""
    try:
        if value is None:
            return None
        val = float(value)
        return None if (math.isnan(val) or math.isinf(val)) else val
    except (TypeError, ValueError):
        return None


def _level_or_none(value: Any) -> float | None:
    """水位值（VIX／DXY／殖利率）：缺失或 <= 0（舊上游以 0.0 代表缺值）視為缺失。"""
    v = _num_or_none(value)
    return v if v is not None and v > 0 else None


def build_macro_healthy_status(macro_data: dict) -> str:
    """生成動態四維度宏觀狀態結構化判讀 (4-Dimensional Macro Healthy Status Interpretation)

    缺值（None／NaN／<=0 的水位）不會被當成 0.0 判讀：對應子判讀改為「數據缺失」。
    """
    vix = _level_or_none(macro_data.get("vix"))
    vix_change = _num_or_none(macro_data.get("vix_change"))
    dxy = _level_or_none(macro_data.get("dxy"))
    tnx = _level_or_none(macro_data.get("tnx"))
    tnx_change_bps = _num_or_none(macro_data.get("tnx_change_bps"))
    spread = _num_or_none(macro_data.get("curve_spread"))
    curve_date = format_obs_date(macro_data.get("curve_date"))

    # 1. 波動率環境
    if vix is None:
        vol_text = "VIX 數據缺失，無法判讀波動率環境。"
    else:
        chg = f" (變動 {vix_change:+.2f})" if vix_change is not None else ""
        if vix < 15.0:
            vol_text = f"VIX {vix:.2f}{chg}，低於 15，注意權利金較薄。"
        elif vix <= 20.0:
            vol_text = f"VIX {vix:.2f}{chg}，介於 15–20 常態區間。"
        else:
            vol_text = f"VIX {vix:.2f}{chg}，高於 20，位階偏高。"

    # 2. 公債與利差
    if tnx is None:
        bond_text = "10Y 美債數據缺失，無法判讀公債與利差。"
    else:
        chg = f" (變動 {tnx_change_bps:+.1f} bps)" if tnx_change_bps is not None else ""
        if spread is None:
            if _level_or_none(macro_data.get("us2y")) is None:
                bond_text = f"10Y 美債 {tnx:.2f}%{chg}，10Y−2Y 利差 2Y 無資料。"
            else:
                bond_text = f"10Y 美債 {tnx:.2f}%{chg}，利差無資料（10Y 未入庫或與 2Y 無同日資料）。"
        else:
            on = f"，{curve_date}" if curve_date else ""
            bond_text = f"10Y 美債 {tnx:.2f}%{chg}，10Y−2Y 利差 {spread:+.2f}%{on}。"

    # 3. 美元與匯率
    if dxy is None:
        fx_text = "DXY 數據缺失，無法判讀美元與匯率。"
    elif dxy <= 103.0:
        fx_text = f"DXY {dxy:.2f}，≤ 103 溫和。"
    else:
        fx_text = f"DXY {dxy:.2f}，103–105 偏強。"

    # 4. 操盤指引（僅依已檢查的 VIX／10Y／DXY 是否齊備判定；2Y 缺失不算缺值）
    if None in (vix, tnx, dxy):
        guide_text = "部分總經數據缺失，無法判定是否觸發總經警示；請稍後重試。"
    elif vix is not None and vix < 15.0:
        guide_text = (
            "未觸發總經警示，維持既有持有計畫；賣方策略留意權利金較薄與合約安全邊際。"
        )
    else:
        guide_text = "未觸發總經警示，維持既有持有計畫。"

    lines = [
        f"• 📈 **波動率環境**：{vol_text}",
        f"• 🏦 **公債與利差**：{bond_text}",
        f"• 💵 **美元與匯率**：{fx_text}",
        f"• 🛡️ **操盤風控指引**：{guide_text}",
    ]
    return "\n".join(lines)


async def run_macro_scan() -> Any:
    """Fetch macro data, evaluate alerts, and return a styled Embed."""
    macro_data = await fetch_macro_data()

    alerts = build_macro_alerts(macro_data)

    from cogs.embed_builder import create_macro_scan_embed

    return create_macro_scan_embed(macro_data, alerts)
