from .history_storage import (
    get_last_stored_sentiment,
    get_indicator_percentile,
    save_sentiment_history,
)
import logging
import sqlite3  # noqa: F401
from datetime import datetime
from typing import Dict, Any
from services import market_data_service


logger = logging.getLogger(__name__)


async def calculate_skew(symbol: str) -> Dict[str, Any]:
    """
    計算期權偏斜 (Option Skew)。
    邏輯：取最近一個月 (Monthly) 的 OTM Put IV 與 OTM Call IV 之差。
    Skew = IV (25-Delta Put) - IV (25-Delta Call)
    """

    def _get_skew_fallback(reason: str) -> Dict[str, Any]:
        last_skew = get_last_stored_sentiment(symbol, "SKEW")
        if last_skew is not None:
            skew_percentile = get_indicator_percentile(symbol, "SKEW", last_skew)
            state = (
                "左偏 (Put 昂貴) [歷史快取]"
                if last_skew > 0
                else "右偏 (Call 昂貴) [歷史快取]"
                if last_skew < 0
                else "平穩 [歷史快取]"
            )
            logger.warning(
                f"[{symbol}] Skew 計算降級 (原因: {reason})，使用歷史快取值: {last_skew:.2f}% (分位點 {skew_percentile:.1f}%)"
            )
            return {
                "symbol": symbol,
                "skew": round(last_skew, 2),
                "skew_percentile": float(round(skew_percentile, 2)),
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

        # 尋找最近的月期權 (假設距離今天 20-45 天)
        today = datetime.now()
        target_expiry = None
        for exp in expiries:
            exp_dt = datetime.strptime(exp, "%Y-%m-%d")
            days_to_expiry = (exp_dt - today).days
            if 20 <= days_to_expiry <= 45:
                target_expiry = exp
                break

        if not target_expiry:
            target_expiry = expiries[0]  # 回退到最近的一個

        chain = await market_data_service.get_option_chain(symbol, target_expiry)
        if not chain:
            return _get_skew_fallback(
                f"No option chain returned for expiry {target_expiry}"
            )

        quote = await market_data_service.get_quote(symbol)
        spot_price = quote.get("c", 0) if quote else 0
        if spot_price == 0:
            return _get_skew_fallback("Spot price is 0")

        calls = chain.calls
        puts = chain.puts

        # 尋找 OTM 25-Delta 附近的期權 (簡化版：使用距離現價一定比例的 Strike)
        # 實務上應使用 py_vollib 計算 Delta，此處先用 Strike 偏移量作為代理
        # 25 Delta Call 通常在現價 + 5~10%
        # 25 Delta Put 通常在現價 - 5~10%

        otm_call = (
            calls[calls["strike"] > spot_price * 1.05].iloc[0]
            if not calls[calls["strike"] > spot_price * 1.05].empty
            else None
        )
        otm_put = (
            puts[puts["strike"] < spot_price * 0.95].iloc[-1]
            if not puts[puts["strike"] < spot_price * 0.95].empty
            else None
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
        await save_sentiment_history(symbol, "SKEW", skew_val)
        skew_percentile = get_indicator_percentile(symbol, "SKEW", skew_val)

        # --- Rigid label mapping (sign + percentile must be consistent) ---
        # +Skew @ high percentile => Put expensive => downside hedging demand
        # -Skew @ low percentile  => Call expensive => upside chasing demand
        if skew_val > 0 and skew_percentile >= 80.0:
            state = "⚠️ 市場下行保護需求極高，隱含避險情緒升溫（機構大舉購入 Put 保險）"
        elif skew_val < 0 and skew_percentile <= 20.0:
            state = "🔥 市場上行看漲需求爆發，動能抄底/追高情緒極端亢奮（散戶搶購末日 Call）"
        elif skew_val > 0:
            state = "左偏 (Put 昂貴)"
        elif skew_val < 0:
            state = "右偏 (Call 昂貴)"
        else:
            state = "平穩"

        return {
            "symbol": symbol,
            "skew": round(skew_val, 2),
            "skew_percentile": float(round(skew_percentile, 2)),
            "iv_put": round(iv_put, 4),
            "iv_call": round(iv_call, 4),
            "state": state,
            "expiry": target_expiry,
        }

    except Exception as e:
        return _get_skew_fallback(f"Exception during skew calculation: {str(e)}")


async def calculate_pcr(symbol: str) -> Dict[str, Any]:
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

        # 彙整前三個到期日的數據
        total_put_vol = 0.0
        total_call_vol = 0.0
        total_put_oi = 0.0
        total_call_oi = 0.0

        for exp in expiries[:3]:
            chain = await market_data_service.get_option_chain(symbol, exp)
            if not chain:
                continue
            if chain.puts is not None and not chain.puts.empty:
                total_put_vol += float(chain.puts["volume"].sum())
                total_put_oi += float(chain.puts["openInterest"].sum())
            if chain.calls is not None and not chain.calls.empty:
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
