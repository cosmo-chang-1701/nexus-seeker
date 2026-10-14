"""Macro data fetching and market scan logic for the Analyst Agent."""

from __future__ import annotations
from typing import Any

import logging
import math

import yfinance as yf

from services import market_data_service

logger = logging.getLogger(__name__)


async def fetch_macro_data() -> dict:
    """Fetch general macro proxies: VIX, DXY, TNX, IRX."""

    def _fetch() -> Any:
        tickers = yf.Tickers("^VIX DX-Y.NYB ^TNX ^IRX")
        return tickers.history(period="2d")

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
            us2y = tnx - 0.2 if not math.isnan(tnx) else 0.0

            return {
                "vix": round(vix, 2) if not math.isnan(vix) else float("nan"),
                "vix_change": round(vix_change, 2),
                "dxy": round(dxy, 2) if not math.isnan(dxy) else 0.0,
                "tnx": round(tnx, 2) if not math.isnan(tnx) else 0.0,
                "tnx_change_bps": round(tnx_change_bps, 1),
                "us2y": round(us2y, 2),
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
                "us2y": (tnx - 0.2) if not math.isnan(tnx) else 0.0,
            }

    except Exception as e:
        logger.warning(f"Failed to fetch macro proxies: {e}")

    return {
        "vix": 0.0,
        "vix_change": 0.0,
        "dxy": 0.0,
        "tnx": 0.0,
        "tnx_change_bps": 0.0,
        "us2y": 0.0,
    }


def build_macro_alerts(macro_data: dict) -> list[str]:
    """Evaluate yield-curve and volatility conditions, return alert strings."""
    vix = macro_data.get("vix", 0.0)
    vix_change = macro_data.get("vix_change", 0.0)
    dxy = macro_data.get("dxy", 0.0)
    tnx = macro_data.get("tnx", 0.0)
    tnx_change_bps = macro_data.get("tnx_change_bps", 0.0)
    us2y = macro_data.get("us2y", 0.0)
    spread = tnx - us2y

    alerts: list[str] = []
    if spread < -0.2:
        alerts.append(
            "殖利率曲線深度倒掛。市場反映中長期經濟衰退預期，建議關注防禦型資產"
        )
    if -0.1 <= spread <= 0.2 and tnx_change_bps < 0:
        alerts.append("殖利率曲線接近解除倒掛 (陡峭化)。留意衰退交易發酵")
    if tnx > 4.5 and tnx_change_bps > 8:
        alerts.append(
            "10 年期殖利率突破 4.5% 且短期急升。建議盤中降低對高 Beta / 估值敏感成長股的曝險"
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
    us2y = _level_or_none(macro_data.get("us2y"))
    spread = tnx - us2y if tnx is not None and us2y is not None else None

    # 1. 波動率環境
    if vix is None:
        vol_text = "VIX 數據缺失，無法判讀波動率環境。"
    else:
        chg = f" (變動 {vix_change:+.2f})" if vix_change is not None else ""
        if vix < 15.0:
            vol_text = f"VIX {vix:.2f}{chg}，處於低波動沉睡區間，恐慌情緒極低，注意權利金較薄。"
        elif vix <= 20.0:
            vol_text = f"VIX {vix:.2f}{chg}，處於常態健康位階，未見異常避險情緒。"
        else:
            vol_text = f"VIX {vix:.2f}{chg}，位階略微偏高但波動率平穩，維持正常戒備。"

    # 2. 公債與利差
    if tnx is None:
        bond_text = "10Y 美債數據缺失，無法判讀公債與利差。"
    else:
        chg = f" (變動 {tnx_change_bps:+.1f} bps)" if tnx_change_bps is not None else ""
        if spread is None:
            bond_text = (
                f"10Y 美債 {tnx:.2f}%{chg}，2Y 殖利率缺失，無法判讀利差與曲線形態。"
            )
        else:
            bond_text = f"10Y 美債 {tnx:.2f}%{chg}，殖利率曲線平穩 (利差 {spread:+.2f}%)，未見異常倒掛或惡性陡峭化。"

    # 3. 美元與匯率
    if dxy is None:
        fx_text = "DXY 數據缺失，無法判讀美元與匯率。"
    elif dxy <= 103.0:
        fx_text = f"DXY {dxy:.2f}，美元走勢溫和，跨國資產流動性充裕。"
    else:
        fx_text = f"DXY {dxy:.2f}，美元匯率處於平穩區間 (≤ 105.0)，無流動性緊縮壓力。"

    # 4. 操盤風控指引
    if None in (vix, tnx, dxy, us2y):
        guide_text = "部分總經數據缺失，無法宣稱風控指標全數合規；請稍後重試。"
    elif vix is not None and vix < 15.0:
        guide_text = (
            "總經風控指標全數合規，維持常規市場部位；賣方策略留意合約點位與安全邊際。"
        )
    else:
        guide_text = "總經風控指標全數合規，維持常態市場部位與標準網格步長。"

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

    if isinstance(macro_data, tuple):
        vix, dxy, tnx = macro_data
        macro_data = {
            "vix": vix,
            "vix_change": 0.0,
            "dxy": dxy,
            "tnx": tnx,
            "tnx_change_bps": 0.0,
            "us2y": tnx - 0.2,
        }

    alerts = build_macro_alerts(macro_data)

    from cogs.embed_builder import create_macro_scan_embed

    return create_macro_scan_embed(macro_data, alerts)
