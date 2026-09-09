from .history_storage import (
    get_last_stored_sentiment,
    get_indicator_percentile_with_sample_size,
    save_sentiment_history,
)
from .skew_taxonomy import SKEW_INDICATOR, classify_skew_state
import asyncio
import logging
import sqlite3  # noqa: F401
from datetime import date, datetime
from typing import Any, Dict, Optional
from services import market_data_service


logger = logging.getLogger(__name__)


# 目標 Delta 與可接受區間。選出的合約若 |δ| 落在區間外，代表該期權鏈根本沒有
# 涵蓋 25-Delta 區域，寧可降級也不要拿一個不像 25-Delta 的合約充數。
_SKEW_TARGET_DELTA = 0.25
_SKEW_DELTA_MIN = 0.10
_SKEW_DELTA_MAX = 0.40

# 到期日選擇：在 DTE >= _SKEW_MIN_DTE 的到期日中取最接近 _SKEW_TARGET_DTE 的一檔。
# 舊實作在找不到 20-45 DTE 時直接回退 expiries[0]（可能是 0DTE），且無任何標記，
# 0DTE 的偏斜值會混進同一條百分位序列。
_SKEW_MIN_DTE = 7
_SKEW_TARGET_DTE = 30


def _select_contract_near_target_delta(
    frame: Any, spot_price: float, t_years: float, flag: str
) -> tuple[Any, float]:
    """從單邊期權鏈挑出 |δ| 最接近 25-Delta 的價外合約。

    回傳 (合約列, |δ|)；找不到合格合約時回傳 (None, 0.0)。

    複用 `greeks.calculate_contract_delta`（Merton 模型 + RISK_FREE_RATE），
    它對 NaN / <= MIN_IV_THRESHOLD 的無效 IV 會回傳 0.0，這裡再顯式濾掉這些列，
    避免一堆 δ=0 的雜訊合約參與「最接近 0.25」的比較。
    """
    from market_analysis.greeks import MIN_IV_THRESHOLD, calculate_contract_delta

    if frame is None or getattr(frame, "empty", True):
        return None, 0.0

    # Call 取現價之上、Put 取現價之下（純價外側）。
    otm = (
        frame[frame["strike"] > spot_price]
        if flag == "c"
        else frame[frame["strike"] < spot_price]
    )
    if otm.empty:
        return None, 0.0

    best_row: Any = None
    best_abs_delta = 0.0
    best_distance = float("inf")

    for _, row in otm.iterrows():
        iv = row.get("impliedVolatility")
        if iv is None or not (float(iv) > MIN_IV_THRESHOLD):
            continue
        abs_delta = abs(calculate_contract_delta(row, spot_price, t_years, flag))
        if abs_delta <= 0.0:
            continue
        distance = abs(abs_delta - _SKEW_TARGET_DELTA)
        if distance < best_distance:
            best_row = row
            best_abs_delta = abs_delta
            best_distance = distance

    if best_row is None:
        return None, 0.0

    # 涵蓋度守衛：期權鏈若根本沒觸及 25-Delta 區域就降級，不硬湊。
    if not (_SKEW_DELTA_MIN <= best_abs_delta <= _SKEW_DELTA_MAX):
        logger.debug(
            "最接近的 %s 合約 |δ|=%.4f 落在 25-Delta 區間 [%.2f, %.2f] 之外，判定涵蓋不足",
            flag,
            best_abs_delta,
            _SKEW_DELTA_MIN,
            _SKEW_DELTA_MAX,
        )
        return None, 0.0

    return best_row, best_abs_delta


async def calculate_skew(symbol: str, force_live: bool = False) -> Dict[str, Any]:
    """計算期權偏斜 (Option Skew)。

    Skew = IV(25-Delta Put) - IV(25-Delta Call)，單位為百分點（正 = Put 昂貴）。

    Delta 為真實計算值（`greeks.calculate_contract_delta`，Merton 模型），
    非舊版的固定 ±5% 履約價代理——後者對高 IV 標的幾乎貼著價平（Skew 被壓平）、
    對低 IV 標的已是深度價外（Skew 被放大），導致跨標的與跨 IV 週期都不可比，
    而百分位排名正是拿同一標的的歷史值互比。
    """

    def _get_skew_fallback(reason: str) -> Dict[str, Any]:
        last_skew = get_last_stored_sentiment(symbol, SKEW_INDICATOR)
        if last_skew is not None:
            skew_percentile, sample_size = get_indicator_percentile_with_sample_size(
                symbol, SKEW_INDICATOR, last_skew
            )
            state = f"{classify_skew_state(last_skew, skew_percentile)} [歷史快取]"
            percentile_text = (
                f"{skew_percentile:.1f}%" if skew_percentile is not None else "N/A"
            )
            logger.warning(
                f"[{symbol}] Skew 計算降級 (原因: {reason})，使用歷史快取值: "
                f"{last_skew:.2f}% (分位點 {percentile_text}, 樣本 {sample_size} 筆)"
            )
            return {
                "symbol": symbol,
                "skew": round(last_skew, 2),
                "skew_percentile": (
                    float(round(skew_percentile, 2))
                    if skew_percentile is not None
                    else None
                ),
                "skew_sample_size": sample_size,
                "state": state,
                "expiry": "CACHE",
                "is_fallback": True,
            }
        logger.error(
            f"[{symbol}] Skew 計算失敗且無歷史快取 (原因: {reason})，回傳降級空數據"
        )
        return {
            "symbol": symbol,
            "skew": None,
            "skew_percentile": None,
            "skew_sample_size": 0,
            "state": "數據不足"
            if "Insufficient" in reason or "數據不足" in reason
            else "N/A",
            "is_fallback": False,
            "error": reason,
        }

    try:
        expiries = await market_data_service.get_all_option_expiries(symbol)
        if not expiries:
            return _get_skew_fallback("No option expiries returned")

        # 在 DTE >= _SKEW_MIN_DTE 的到期日中，取 DTE 最接近 _SKEW_TARGET_DTE 的一檔。
        # 用 date 相減而非 naive datetime.now()，避免時分秒造成的 off-by-one。
        today = date.today()
        target_expiry: Optional[str] = None
        target_dte: int = 0
        for exp in expiries:
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            dte = (exp_date - today).days
            if dte < _SKEW_MIN_DTE:
                continue
            if target_expiry is None or abs(dte - _SKEW_TARGET_DTE) < abs(
                target_dte - _SKEW_TARGET_DTE
            ):
                target_expiry = exp
                target_dte = dte

        if target_expiry is None:
            return _get_skew_fallback(
                f"Insufficient DTE coverage (no expiry with DTE >= {_SKEW_MIN_DTE})"
            )

        chain = await market_data_service.get_option_chain(
            symbol, target_expiry, force_live=force_live
        )
        if not chain:
            return _get_skew_fallback(
                f"No option chain returned for expiry {target_expiry}"
            )

        quote = await market_data_service.get_quote(symbol)
        spot_price = quote.get("c", 0) if quote else 0
        if spot_price == 0:
            return _get_skew_fallback("Spot price is 0")

        t_years = target_dte / 365.0
        otm_call, call_delta = _select_contract_near_target_delta(
            chain.calls, spot_price, t_years, "c"
        )
        otm_put, put_delta = _select_contract_near_target_delta(
            chain.puts, spot_price, t_years, "p"
        )

        if otm_call is None or otm_put is None:
            return _get_skew_fallback(
                "Insufficient OTM Call/Put options to compute Skew"
            )

        iv_call = float(otm_call["impliedVolatility"])
        iv_put = float(otm_put["impliedVolatility"])

        # --- Rigid definition (must not drift) ---
        # Option Skew = IV(OTM Put) - IV(OTM Call)
        skew_val = (iv_put - iv_call) * 100  # percentage points

        # 儲存到資料庫以便後續計算百分位
        await save_sentiment_history(symbol, SKEW_INDICATOR, skew_val)
        skew_percentile, sample_size = get_indicator_percentile_with_sample_size(
            symbol, SKEW_INDICATOR, skew_val
        )

        return {
            "symbol": symbol,
            "skew": round(skew_val, 2),
            "skew_percentile": (
                float(round(skew_percentile, 2))
                if skew_percentile is not None
                else None
            ),
            "skew_sample_size": sample_size,
            "iv_put": round(iv_put, 4),
            "iv_call": round(iv_call, 4),
            "call_delta": round(call_delta, 4),
            "put_delta": round(put_delta, 4),
            "dte": target_dte,
            "state": classify_skew_state(skew_val, skew_percentile),
            "expiry": target_expiry,
            "is_fallback": False,
        }

    except Exception as e:
        return _get_skew_fallback(f"Exception during skew calculation: {str(e)}")


async def calculate_pcr(symbol: str, force_live: bool = False) -> Dict[str, Any]:
    """
    計算買賣權比率 (Put/Call Ratio)，拆分為成交量 (Volume) 與未平倉量 (Open Interest) 比率。
    """

    def _get_pcr_fallback(reason: str) -> Dict[str, Any]:
        last_pcr = get_last_stored_sentiment(symbol, "PCR")
        if last_pcr is not None:
            volume_state = "平衡 [歷史快取]"
            if last_pcr < 0.90:
                volume_state = "中性偏多/看漲主導 [歷史快取]"
            elif last_pcr > 1.10:
                volume_state = "🐻 偏向空頭/看空主導 [歷史快取]"
            logger.warning(
                f"[{symbol}] PCR 計算降級 (原因: {reason})，使用歷史快取值: {last_pcr:.2f}"
            )
            return {
                "symbol": symbol,
                "pcr": round(last_pcr, 2),
                "volume_pcr": round(last_pcr, 2),
                "oi_pcr": None,
                "state": volume_state,
                "volume_pcr_state": volume_state,
                "oi_pcr_state": "N/A",
            }
        logger.error(
            f"[{symbol}] PCR 計算失敗且無歷史快取 (原因: {reason})，回傳降級空數據"
        )
        state_val = "ERROR" if "Exception" in reason or "Error" in reason else "N/A"
        return {
            "symbol": symbol,
            "pcr": None,
            "volume_pcr": None,
            "oi_pcr": None,
            "state": state_val,
            "volume_pcr_state": state_val,
            "oi_pcr_state": state_val,
            "error": reason,
        }

    try:
        expiries = await market_data_service.get_all_option_expiries(symbol)
        if not expiries:
            return _get_pcr_fallback("No option expiries returned")

        # 並行彙整前三個到期日的數據
        total_put_vol = 0.0
        total_call_vol = 0.0
        total_put_oi = 0.0
        total_call_oi = 0.0

        target_expiries = expiries[:3]
        chains = await asyncio.gather(
            *(
                market_data_service.get_option_chain(symbol, exp, force_live=force_live)
                for exp in target_expiries
            ),
            return_exceptions=True,
        )

        for chain in chains:
            if chain is None or isinstance(chain, BaseException):
                continue
            if getattr(chain, "puts", None) is not None and not chain.puts.empty:
                total_put_vol += float(chain.puts["volume"].sum())
                total_put_oi += float(chain.puts["openInterest"].sum())
            if getattr(chain, "calls", None) is not None and not chain.calls.empty:
                total_call_vol += float(chain.calls["volume"].sum())
                total_call_oi += float(chain.calls["openInterest"].sum())

        if (
            total_put_vol == 0.0
            and total_call_vol == 0.0
            and total_put_oi == 0.0
            and total_call_oi == 0.0
        ):
            return _get_pcr_fallback("No option chain data retrieved")

        volume_pcr = total_put_vol / total_call_vol if total_call_vol > 0 else 0.0
        oi_pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 0.0

        volume_state = "平衡"
        if volume_pcr < 0.90:
            volume_state = "中性偏多/看漲主導"
        elif volume_pcr > 1.10:
            volume_state = "🐻 偏向空頭/看空主導"

        oi_state = "結構平衡"
        if oi_pcr < 0.90:
            oi_state = "🐂 結構看漲/偏向多頭"
        elif oi_pcr > 1.10:
            oi_state = "🐻 結構防禦/偏向空頭"

        await save_sentiment_history(symbol, "PCR", volume_pcr)

        return {
            "symbol": symbol,
            "pcr": round(volume_pcr, 2),
            "volume_pcr": round(volume_pcr, 2),
            "oi_pcr": round(oi_pcr, 2),
            "put_vol": total_put_vol,
            "call_vol": total_call_vol,
            "put_oi": total_put_oi,
            "call_oi": total_call_oi,
            "state": volume_state,
            "volume_pcr_state": volume_state,
            "oi_pcr_state": oi_state,
        }
    except Exception as e:
        return _get_pcr_fallback(f"Exception during PCR calculation: {str(e)}")
