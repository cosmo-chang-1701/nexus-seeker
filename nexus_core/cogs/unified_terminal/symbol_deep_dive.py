"""單一標的深度分析（/x symbol: 互動指令與批次分析警示標的共用資料來源）。"""

import asyncio
import math
import logging
import re
from typing import TYPE_CHECKING, Any, List, Optional

import discord

from services import market_data_service, reddit_service
from services.market_data_service import _sanitize_ticker
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.psq_engine import analyze_psq
from market_analysis.atr_utils import (
    compute_atr_14_from_daily_df,
    fetch_atr_15m,
    fetch_atr_1d,
)
from market_analysis.vwap_utils import fetch_session_stats
from market_analysis.price_volume_alert import get_confirmed_15m_bar
from market_analysis.sentiment.max_pain import find_settlement_gravity
import market_math

from cogs.embed_builder import create_error_embed, create_tactical_symbol_embed
from .symbol_view import SymbolBatchHubView, SymbolHubPage, SymbolHubView

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# /x 多標的（逗號分隔）批次分析的上限與逾時。
_MAX_BATCH_SYMBOLS = 10  # 單次最多標的數（一則訊息逐檔換頁）
_BATCH_CONCURRENCY = 2  # 同時分析的標的數；CPU／記憶體併發上限，非對外 API 限流
# 整批期限：互動 token 壽命 15 分鐘，結果訊息的 View 逾時（300 秒）移除按鈕仍要用
# 原 token，故 540 + 300 < 900。不設單檔逾時：SingleFlight 以 shield 包住抓取，
# 取消等待端並不會停止抓取，單檔逾時後釋放併發名額只會讓背景抓取越疊越多。
_BATCH_DEADLINE_S = 540.0
_PROGRESS_EDIT_TIMEOUT_S = 5.0  # 進度訊息編輯的等待上限（撞 webhook 限流時不拖慢整批）

# 合法代號不含這些分隔字元（見 quote.py `_TICKER_PATTERN`）
_SYMBOL_SPLIT_RE = re.compile(r"[,，、;；\s]+")


def parse_symbol_list(raw: str) -> list[str]:
    """將 /x symbol 參數切成代號清單：支援半／全形逗號、頓號、分號與空白分隔，
    逐段清洗（去 `$`、轉大寫）、丟棄空段，並保序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for part in _SYMBOL_SPLIT_RE.split(raw or ""):
        sym = _sanitize_ticker(part)
        if sym and sym not in seen:
            seen.add(sym)
            result.append(sym)
    return result


# /x 的 Kelly 欄位以「賣出 16Δ Put」為參考單位（單口 Delta 再乘 β×S/SPY×100 換成 SPY 等值股數）
_KELLY_REF_SHORT_PUT_DELTA = 0.16


async def _evaluate_squeeze_for_panel(
    symbol: str, quote_task: "asyncio.Task[Any]"
) -> Optional[Any]:
    """/x 用的多時間框架擠壓評估；沿用 entry_advisor._evaluate_squeeze_long 的否決／降級，
    但刻意不呼叫 evaluation_recorder（互動指令不寫 DB）。失敗回傳 None。"""
    try:
        from market_analysis.squeeze_entry import evaluate_symbol
        from market_analysis.squeeze_entry.vetoes import resolve_long_entry_vetoes

        quote = await quote_task
        spot = _safe_float((quote or {}).get("c"), 0.0)
        if spot <= 0:
            return None
        vetoes, downgrade = await resolve_long_entry_vetoes(symbol)
        return await evaluate_symbol(symbol, spot, vetoes, downgrade)
    except Exception as e:
        logger.warning(f"[{symbol}] /x 擠壓評估失敗: {e}")
        return None


def _compute_adv_dollar_20d(df_hist_1d: Any, price: float) -> Optional[float]:
    """20 日平均成交額（avg_vol_20d × 現價）；缺資料、NaN 或 ≤0 時回傳 None。"""
    try:
        if df_hist_1d is None or df_hist_1d.empty or "Volume" not in df_hist_1d:
            return None
        px = price
        if not (px > 0) and "Close" in df_hist_1d:
            px = float(df_hist_1d["Close"].iloc[-1])
        avg_vol = float(df_hist_1d["Volume"].tail(20).mean())
        adv = avg_vol * px
        if math.isfinite(adv) and adv > 0:
            return adv
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    return None


def _with_adv(gex_profile_data: Any, df_hist_1d: Any, price: float) -> Any:
    """為 GEX profile 補上 `adv_dollar_20d`（與雷達同公式 avg_vol_20d × 現價）。

    僅在 profile 為 dict 且尚無 ADV、且能算出有效值時回傳**淺拷貝**；
    其餘情況原物件原樣回傳（不覆寫既有 ADV、不修改原 dict）。
    ADV 與雷達一樣含當日未完成 K 棒，盤中會偏低。
    """
    if not isinstance(gex_profile_data, dict) or gex_profile_data.get("adv_dollar_20d"):
        return gex_profile_data
    adv = _compute_adv_dollar_20d(df_hist_1d, price)
    if adv is None:
        return gex_profile_data
    return {**gex_profile_data, "adv_dollar_20d": adv}


async def _throttled_volume_profile(symbol: str) -> Any:
    """Volume Profile 為同步 yfinance 直連：套 Yahoo 預算閘門；冷卻／排隊逾時
    時回 None（面板略過該欄），不得讓例外中斷整個 gather。"""
    from market_analysis.volume_profile import calculate_volume_profile

    try:
        async with market_data_service.yahoo_slot():
            return await asyncio.to_thread(calculate_volume_profile, symbol)
    except (
        market_data_service.YahooRateLimitedError,
        market_data_service.YahooEdgeBusyError,
    ):
        return None


class SymbolDeepDiveMixin:
    if TYPE_CHECKING:
        bot: Any

    async def _fetch_single_symbol_data_raw(self, symbol: str) -> dict:
        """
        獲取單一標的所需的所有重型量化數據與外部情緒分析。
        供 SingleFlightManager 調度使用。

        這是 `/x symbol:` 互動指令、批次掃描「⚡ 批次分析警示標的」按鈕，以及
        SymbolHubView 分頁切換共用的唯一深度分析資料來源，呼叫端一律已透過
        Discord `interaction.response.defer()` 取得最長 15 分鐘的 followup
        視窗，不再受 3 秒互動逾時限制。因此期權鏈/GEX/IV/Max Pain/Skew/PCR/UOA
        等量化數據一律以 force_live=True 或等效的 force_refresh=True 抓取，
        略過 Edge Snapshot（最舊可能 30 分鐘）與各自的記憶體/SQLite 快取層，
        保證回傳即時資料。標的本身的 1y 日線同樣以 force_refresh=True 抓取：
        面板的「動能與擠壓狀態」以最後一根（盤中為今日成型中）日線計算，
        走 6 小時 `_history_cache` 會讓盤中一直停在盤前預熱寫入的前一日收盤。
        現價/SPY 歷史/總經/Reddit/Polymarket/基本面論點等其餘數據維持既有
        快取策略不變。
        """
        from market_analysis.ddp_inspector import DDPInspector
        from market_time import ny_tz
        from datetime import datetime
        from market_analysis.index_microstructure import fetch_symbol_gex_metrics

        ddp_inspector = DDPInspector(self.bot)
        poly_service = getattr(self.bot, "polymarket_service", None)

        async def _safe_get_poly_markets() -> list:
            if poly_service:
                return await poly_service.get_market_snapshot(limit=0)  # type: ignore
            return []

        # 1. 啟動基礎行情與總經數據任務 (t=0 並行)
        expiries_task = asyncio.create_task(
            market_data_service.get_all_option_expiries(symbol)
        )
        spy_task = asyncio.create_task(market_data_service.get_spy_history_df("1y"))
        macro_task = asyncio.create_task(market_data_service.get_macro_environment())
        quote_task = asyncio.create_task(market_data_service.get_quote(symbol))
        squeeze_task = asyncio.create_task(
            _evaluate_squeeze_for_panel(symbol, quote_task)
        )

        async def _get_daily_history() -> tuple[Any, datetime]:
            # 抓取時間在日線回來當下記錄，而非等整個 gather（Reddit 等慢任務）結束
            df = await market_data_service.get_history_df(
                symbol, period="1y", interval="1d", force_refresh=True
            )
            return df, datetime.now(ny_tz)

        df_hist_task = asyncio.create_task(_get_daily_history())
        gex_profile_task = asyncio.create_task(
            fetch_symbol_gex_metrics(symbol, force_live=True)
        )

        vp_task = asyncio.create_task(_throttled_volume_profile(symbol))
        # 刻意不在此並行 fetch_atr_1d：它內部走的是 get_history_df(symbol, "1y",
        # "1d")——與上方 df_hist_task 同一個 cache key，而 get_history_df 沒有
        # single-flight 去重（快取寫入在 await 之後），兩者同時於 t=0 查快取會在
        # 冷啟動時發出兩次相同的 yfinance/Edge 請求。日線 ATR 改由
        # _process_symbol_hub_data 以 compute_atr_14_from_daily_df 純記憶體計算，
        # 真的算不出來時才在那裡惰性 await（此時快取已被 df_hist_task 寫暖）。
        # atr_15m_task 走 force_refresh=True + 5d/15m 不同 key，不是冗餘，保留。
        atr_15m_task = asyncio.create_task(fetch_atr_15m(symbol))
        # VWAP 與當日區間極值出自同一份 K 線（見 intraday_consistency.py）
        vwap_task = asyncio.create_task(fetch_session_stats(symbol))
        bar_15m_task = asyncio.create_task(get_confirmed_15m_bar(symbol))
        from services.calendar_service import calendar_service

        catalysts_task = asyncio.create_task(
            calendar_service.get_symbol_catalysts(symbol, days=14)
        )
        reddit_task = asyncio.create_task(reddit_service.get_reddit_details(symbol))
        poly_task = asyncio.create_task(_safe_get_poly_markets())
        ddp_task = asyncio.create_task(ddp_inspector.inspect_symbol(symbol))

        # 2. 啟動期權結構分析任務 (SingleFlight 自動合併相同到期日)
        # 深度分析路徑：一律 force_live/force_refresh=True，保證即時性。
        skew_task = asyncio.create_task(
            SentimentEngine.calculate_skew(symbol, force_live=True)
        )
        pcr_task = asyncio.create_task(
            SentimentEngine.calculate_pcr(symbol, force_live=True)
        )

        async def _get_uoa_with_physical_caps() -> tuple[list, list]:
            try:
                return await SentimentEngine.detect_uoa_with_physical_caps(  # type: ignore[no-any-return]
                    symbol, force_live=True
                )
            except Exception as e:
                logger.warning(f"[{symbol}] 獲取 UOA/物理封頂 STO 失敗: {e}")
                return [], []

        uoa_task = asyncio.create_task(_get_uoa_with_physical_caps())
        mp_task = asyncio.create_task(
            SentimentEngine.calculate_max_pain(symbol, _retry=True)
        )
        iv_task = asyncio.create_task(
            SentimentEngine.fetch_and_calculate_iv_metrics(symbol, force_refresh=True)
        )

        # 3. 取得 30 天內到期日之 Max Pain
        async def _fetch_month_max_pains() -> list[dict[str, Any]]:
            try:
                expiries = await expiries_task
            except Exception as e:
                logger.warning(f"[{symbol}] Failed to fetch expiries: {e}")
                return []

            if not expiries:
                return []

            today = datetime.now(ny_tz).date()
            valid_expiries: list[str] = []
            for exp in expiries:
                try:
                    exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                    if 0 <= (exp_dt - today).days <= 30:
                        valid_expiries.append(exp)
                except ValueError:
                    continue

            if not valid_expiries:
                return []

            mp_tasks = [
                SentimentEngine.get_unified_max_pain(
                    symbol, expiry=exp, force_refresh=True
                )
                for exp in valid_expiries
            ]
            mp_results = await asyncio.gather(*mp_tasks, return_exceptions=True)

            month_max_pains: list[dict[str, Any]] = []
            for exp, res in zip(valid_expiries, mp_results):
                if isinstance(res, dict) and "error" not in res:
                    month_max_pains.append(
                        {
                            "expiry": exp,
                            "max_pain": res.get("max_pain"),
                            "distance_pct": res.get("distance_pct", 0.0),
                            "is_degraded": bool(res.get("is_degraded", 0)),
                            "calculation_mode": res.get("calculation_mode", "OI"),
                        }
                    )
            return month_max_pains

        month_mp_task = asyncio.create_task(_fetch_month_max_pains())

        # 4. 全量 Gather
        (
            df_spy,
            macro_raw,
            quote,
            (df_hist_1d, df_hist_fetched_at),
            gex_profile_data,
            vp_data,
            atr_15m_data,
            session_stats_data,
            bar_15m_data,
            catalysts,
            reddit_details,
            poly_markets,
            ddp_report,
            skew_data,
            pcr_data,
            (uoa_data, sto_physical_cap_strikes),
            max_pain_data,
            iv_metrics,
            month_max_pains,
            squeeze_eval,
        ) = await asyncio.gather(
            spy_task,
            macro_task,
            quote_task,
            df_hist_task,
            gex_profile_task,
            vp_task,
            atr_15m_task,
            vwap_task,
            bar_15m_task,
            catalysts_task,
            reddit_task,
            poly_task,
            ddp_task,
            skew_task,
            pcr_task,
            uoa_task,
            mp_task,
            iv_task,
            month_mp_task,
            squeeze_task,
        )

        # 完整期權到期日清單（任務已完成，僅取結果）：供 /x 推定 GEX 涵蓋的到期日，
        # 不可用 month_max_pains——它會漏掉 Max Pain 計算失敗的到期日。
        try:
            option_expiries = [str(e) for e in (await expiries_task or [])]
        except Exception:
            option_expiries = []

        safe_reddit_text = (
            reddit_details[0] if isinstance(reddit_details, tuple) else reddit_details
        )
        safe_reddit_posts = (
            reddit_details[1] if isinstance(reddit_details, tuple) else []
        )

        return {
            "df_spy": df_spy,
            "macro_raw": macro_raw,
            "quote": quote,
            "skew_data": skew_data,
            "pcr_data": pcr_data,
            "uoa_data": uoa_data,
            "sto_physical_cap_strikes": sto_physical_cap_strikes,
            "max_pain_data": max_pain_data,
            "iv_metrics": iv_metrics,
            "reddit_text": safe_reddit_text,
            "reddit_posts": safe_reddit_posts,
            "poly_markets": poly_markets,
            "ddp_report": ddp_report,
            "ddp_reason": ddp_inspector.last_fail_reason.get(symbol),
            "squeeze_eval": squeeze_eval,
            "df_hist_1d": df_hist_1d,
            "df_hist_fetched_at": df_hist_fetched_at,
            "month_max_pains": month_max_pains,
            "option_expiries": option_expiries,
            "gex_profile_data": gex_profile_data,
            "volume_profile": vp_data,
            "atr_15m": atr_15m_data,
            "session_vwap": (
                session_stats_data.vwap if session_stats_data is not None else 0.0
            ),
            "session_stats": session_stats_data,
            "bar_15m": bar_15m_data,
            "catalysts": catalysts,
        }

    async def _process_symbol_hub_data(
        self, symbol: str, user_id: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """將原始抓取的量化與社群數據轉換為標的深度分析 (Tactical Deep-Dive) 統一資料模型。"""
        from services.asset_manager import AssetManager
        from models.asset import ContextType
        from market_analysis.risk_engine import optimize_position_risk
        from cogs.unified_terminal.utils import (
            find_matching_polymarket_odds,
            calculate_polymarket_weighted_odds,
        )
        import database

        manager = AssetManager()
        assets = manager.get_assets(user_id, ContextType.HOLDING)
        stock_cost_raw = next(
            (a.metadata.get("avg_cost", 0.0) for a in assets if a.symbol == symbol),
            0.0,
        )
        stock_cost = _safe_float(stock_cost_raw, 0.0)

        df_spy = data["df_spy"]
        macro_raw = data["macro_raw"]
        quote = data["quote"]
        skew_data = data["skew_data"]
        pcr_data = data["pcr_data"]
        uoa_data = data["uoa_data"]
        sto_physical_cap_strikes = data.get("sto_physical_cap_strikes", [])
        max_pain_data = data["max_pain_data"]
        iv_metrics = data["iv_metrics"]
        reddit_text = data["reddit_text"]
        poly_markets = data["poly_markets"]
        ddp_report = data["ddp_report"]
        df_hist_1d = data["df_hist_1d"]
        gex_profile_data = data.get("gex_profile_data")
        vp_data = data.get("volume_profile")
        catalysts = data.get("catalysts", [])

        # SPY / VIX 未知時為 None（不補 670 / 18.0 / 75.0 備援常數）。
        spy_price: Optional[float] = (
            _safe_float(df_spy["Close"].iloc[-1], 0.0) or None
            if df_spy is not None and not df_spy.empty
            else None
        )
        from market_analysis.risk_engine import build_macro_context

        macro_data = build_macro_context(macro_raw or {})
        vix_now = macro_data.vix if macro_data is not None else None

        # 並行執行技術指標分析與 Polymarket 機率解析
        math_task = market_math.analyze_symbol(
            symbol, stock_cost, df_spy, spy_price, vix_spot=vix_now
        )
        poly_task = find_matching_polymarket_odds(symbol, poly_markets, bot=self.bot)
        poly_summary_task = calculate_polymarket_weighted_odds(
            symbol, poly_markets, bot=self.bot
        )

        result_math, poly_odds, poly_summary = await asyncio.gather(
            math_task, poly_task, poly_summary_task
        )
        result: dict[str, Any] = (
            result_math
            if isinstance(result_math, dict) and result_math
            else {"symbol": symbol, "stock_cost": stock_cost, "price": 0.0}
        )

        from market_analysis.squeeze_entry.timeframes import GREEN_DOT_LOOKBACK

        psq_result = analyze_psq(
            df_hist_1d, vix_spot=vix_now, green_dot_lookback=GREEN_DOT_LOOKBACK["D"]
        )
        if psq_result:
            result["psq_result"] = psq_result
            is_df_valid = df_hist_1d is not None and not df_hist_1d.empty
            result["price"] = (
                _safe_float(df_hist_1d["Close"].iloc[-1], 0.0)
                if is_df_valid
                else _safe_float(result.get("price"), 0.0)
            )

        # 日高低點與 VWAP／15m K 棒必須出自可相容的資料源：Tier 0 報價是 IEX
        # 單一交易所成交，極值比全市場窄，這裡以同一份全市場 K 線放寬之。
        from datetime import datetime as _dt

        from market_analysis.intraday_consistency import (
            assess_15m_bar,
            is_vwap_within_range,
            reconcile_daily_range,
        )
        from market_time import is_market_open, ny_tz

        now_ny = _dt.now(ny_tz)

        # 擠壓欄位的資料時間：最後一根日線（tz-naive US/Eastern）與抓取時刻。
        # 今日那一根在盤中仍在成型，呈現層須標明，避免誤讀為已收盤定案。
        if psq_result and df_hist_1d is not None and not df_hist_1d.empty:
            last_bar_ts = df_hist_1d.index[-1]
            if hasattr(last_bar_ts, "date"):
                psq_bar_date = last_bar_ts.date()
                result["psq_bar_date"] = psq_bar_date
                result["psq_bar_is_live"] = psq_bar_date == now_ny.date() and bool(
                    is_market_open()
                )
            result["psq_fetched_at"] = data.get("df_hist_fetched_at")

        session_stats = data.get("session_stats")
        if isinstance(quote, dict) and session_stats is not None:
            quote, range_fixed = reconcile_daily_range(
                quote,
                session_stats.high,
                session_stats.low,
                session_stats.session_date,
                now_ny.date(),
            )
            if range_fixed:
                logger.info(
                    f"[{symbol}] 報價日高低點以全市場 15m K 線校正: "
                    f"H={quote.get('h')} L={quote.get('l')}"
                )
        result["quote"] = quote

        safe_skew = skew_data if isinstance(skew_data, dict) else {}
        result["skew"] = _safe_float(safe_skew.get("skew"), 0.0)
        # calculate_skew() 已在同一個 dict 內回傳分位（與它寫入歷史的那筆值同源）。
        # 舊實作拿 _safe_float(..., 0.0) 的結果再查一次 DB 重算，缺資料時等於用
        # 假的 0.0 去排名；直接沿用上游回傳值即可，順帶省下一次查詢。
        result["skew_percentile"] = safe_skew.get("skew_percentile")
        result["skew_sample_size"] = safe_skew.get("skew_sample_size")

        result["pcr"] = pcr_data if pcr_data is not None else {}
        result["uoa"] = uoa_data if uoa_data is not None else []
        result["sto_physical_cap_strikes"] = (
            sto_physical_cap_strikes if sto_physical_cap_strikes is not None else []
        )

        result["iv_data"] = iv_metrics
        iv_rank_raw = (
            iv_metrics.get("iv_rank")
            if isinstance(iv_metrics, dict)
            else getattr(iv_metrics, "iv_rank", None)
        )
        # 樣本不足時保留 None（未知）；補 0.0 會讓下游把它當成「極低 IVR」
        result["iv_rank"] = (
            _safe_float(iv_rank_raw, 0.0) if iv_rank_raw is not None else None
        )
        raw_em_context = await SentimentEngine.get_expected_move(
            symbol, quote=quote, iv_metrics=iv_metrics
        )
        em_context: dict[str, Any] = (
            raw_em_context if isinstance(raw_em_context, dict) else {}
        )
        # 跨式以定價當下的現價（盤後為最新收盤）為中心，而非昨日前收。
        if isinstance(iv_metrics, dict):
            _ref_spot = _safe_float(iv_metrics.get("reference_spot_price"), 0.0)
        else:
            _ref_spot = _safe_float(
                getattr(iv_metrics, "reference_spot_price", None), 0.0
            )
        if (
            em_context
            and _ref_spot > 0
            and _safe_float(em_context.get("expected_move_weekly"), 0.0) > 0
        ):
            from market_analysis.sentiment.iv_metrics import IVContext

            em_context = IVContext.build_expected_move(
                symbol,
                expected_move_weekly=_safe_float(
                    em_context.get("expected_move_weekly")
                ),
                reference_price=_ref_spot,
                current_price=_safe_float(em_context.get("current_price"), 0.0),
            )
            em_context["reference_label"] = "現價" if is_market_open() else "最新收盤"
        elif em_context:
            em_context["reference_label"] = "前收"
        result["expected_move_context"] = em_context

        safe_mp = max_pain_data if isinstance(max_pain_data, dict) else {}
        result["max_pain"] = _safe_float(safe_mp.get("max_pain"), 0.0)
        # 頭條 Max Pain 實際鎖定的到期日：結算前 1σ 必須用同一檔的 DTE
        result["max_pain_expiry"] = safe_mp.get("expiry")
        result["month_max_pains"] = data.get("month_max_pains", [])
        result["option_expiries"] = data.get("option_expiries", [])
        # 與雷達同源注入 20 日平均成交額（淺拷貝，不改共用快取；見 _with_adv）。
        gex_profile_data = _with_adv(
            gex_profile_data, df_hist_1d, _safe_float(result.get("price"), 0.0)
        )
        result["gex_profile_data"] = gex_profile_data
        result["catalysts"] = catalysts

        safe_ddp = ddp_report if isinstance(ddp_report, dict) else {}
        result["is_ddp"] = bool(safe_ddp.get("is_ddp", False))
        result["ddp_reason"] = data.get("ddp_reason")
        result["squeeze_eval"] = data.get("squeeze_eval")
        result["vix"] = vix_now
        result["spy_price"] = spy_price

        # Reddit sentiment score
        safe_reddit_text = reddit_text or ""
        if any(err in safe_reddit_text for err in ["錯誤", "異常", "超時", "尚未配置"]):
            result["reddit_sentiment_score"] = "⚠️ 抓取失敗 (邊緣節點異常)"
        elif "看多" in safe_reddit_text or "Bullish" in safe_reddit_text:
            result["reddit_sentiment_score"] = "🚀 樂觀 (Bullish)"
        elif "看空" in safe_reddit_text or "Bearish" in safe_reddit_text:
            result["reddit_sentiment_score"] = "💀 恐慌 (Bearish)"
        else:
            result["reddit_sentiment_score"] = "⚖️ 中性"

        result["reddit_posts"] = data.get("reddit_posts", [])
        result["polymarket_odds"] = poly_odds
        result["polymarket_summary"] = poly_summary

        safe_vp = vp_data if isinstance(vp_data, dict) else {}
        result["volume_profile"] = safe_vp
        result["atr_15m"] = _safe_float(data.get("atr_15m"), 0.0)
        session_vwap_val = _safe_float(data.get("session_vwap"), 0.0)
        safe_quote = quote if isinstance(quote, dict) else {}
        day_high = _safe_float(safe_quote.get("h"), 0.0)
        day_low = _safe_float(safe_quote.get("l"), 0.0)
        if (
            session_vwap_val > 0
            and day_high > 0
            and day_low > 0
            and not is_vwap_within_range(session_vwap_val, day_high, day_low)
        ):
            # 放寬區間後仍在區間外 → 兩源不屬於同一時段，寧可顯示缺失也不給假錨點
            logger.warning(
                f"[{symbol}] Session VWAP {session_vwap_val:.2f} 超出當日區間 "
                f"[{day_low:.2f}, {day_high:.2f}]，視為資料異常"
            )
            session_vwap_val = 0.0
        result["session_vwap"] = session_vwap_val

        # ATR₁D 取數階梯（刻意「先用手上已有的，最後才發網路請求」）：
        #   1. 已 gather 到的日線 frame 就地純記憶體計算
        #   2. 呼叫端顯式帶入的 atr_1d / atr_14（0.01 為 EnhancedWatchlistMetrics
        #      的佔位值，須視為缺失）
        #   3. 惰性 await fetch_atr_1d()——此時 df_hist_task 已把 1y/1d 快取寫暖，
        #      快樂路徑是快取命中零網路；df_hist 真的抓失敗時這層提供一次重試。
        # 少了第 3 層，日線抓取失敗就會讓下游「上檔壓力」欄位回到
        # ⚠ 數據缺失（ATR₁D）的降級揭露。
        atr_14_val = compute_atr_14_from_daily_df(df_hist_1d)
        if atr_14_val <= 0.0 or abs(atr_14_val - 0.01) < 1e-6:
            c_1d = _safe_float(data.get("atr_1d"), 0.0)
            c_14 = _safe_float(data.get("atr_14"), 0.0)
            candidate = c_1d if (c_1d > 0.0 and abs(c_1d - 0.01) >= 1e-6) else c_14
            if candidate > 0.0 and abs(candidate - 0.01) >= 1e-6:
                atr_14_val = candidate
        if atr_14_val <= 0.0 or abs(atr_14_val - 0.01) < 1e-6:
            try:
                lazy_atr_1d = await fetch_atr_1d(symbol)
            except Exception as e:  # pragma: no cover - fetch 端已 fail-safe
                logger.warning(f"[{symbol}] ATR₁D 惰性回抓失敗: {e}")
                lazy_atr_1d = 0.0
            if lazy_atr_1d > 0.0 and abs(lazy_atr_1d - 0.01) >= 1e-6:
                atr_14_val = lazy_atr_1d
        result["atr_14"] = atr_14_val
        result["atr_1d"] = atr_14_val

        bar_15m = data.get("bar_15m")
        result["bar_15m"] = bar_15m
        if bar_15m is not None:

            def _extract_val(k: str) -> Any:
                if hasattr(bar_15m, k):
                    return getattr(bar_15m, k, None)
                if isinstance(bar_15m, dict):
                    return bar_15m.get(k)
                return None

            import math

            def _clean_float(v: Any) -> Optional[float]:
                if v is None:
                    return None
                try:
                    f = float(v)
                    return None if math.isnan(f) else f
                except (TypeError, ValueError):
                    return None

            c_15m = _clean_float(_extract_val("close"))
            o_15m = _clean_float(_extract_val("open"))
            h_15m = _clean_float(_extract_val("high"))
            l_15m = _clean_float(_extract_val("low"))
            v_15m = _clean_float(_extract_val("volume"))
            sma_15m = _clean_float(
                _extract_val("avg_volume")
                if _extract_val("avg_volume") is not None
                else _extract_val("volume_15m_sma20")
            )
            rvol = (
                (v_15m / sma_15m)
                if (v_15m is not None and sma_15m is not None and sma_15m > 0)
                else None
            )

            bar_time_raw = _extract_val("bar_time")
            bar_time = bar_time_raw if isinstance(bar_time_raw, _dt) else None
            assessment = assess_15m_bar(
                bar_time,
                h_15m,
                l_15m,
                v_15m,
                now_ny=now_ny,
                market_open=bool(is_market_open()),
                day_high=day_high,
                day_low=day_low,
                session_date=(
                    session_stats.session_date if session_stats is not None else None
                ),
                session_volume=(
                    session_stats.volume if session_stats is not None else None
                ),
                session_bar_count=(
                    session_stats.bar_count if session_stats is not None else 0
                ),
            )
            result["bar_15m_time"] = bar_time
            result["bar_15m_notes"] = assessment.notes
            tod_avg = _clean_float(_extract_val("tod_avg_volume"))
            # /x 以同時段中位數為基準（抗單日離群）；沒有時退回平均。
            tod_median = _clean_float(_extract_val("tod_median_volume"))
            tod_base = tod_median if tod_median is not None else tod_avg
            # 面板標籤必須如實標示實際採用的統計（中位數缺失而退回平均時不得標中位數）
            result["tod_stat"] = "median" if tod_median is not None else "mean"
            rvol_tod = (
                (v_15m / tod_base)
                if (v_15m is not None and tod_base is not None and tod_base > 0)
                else None
            )
            if assessment.is_stale or assessment.is_anomalous:
                # 凍結或合併的 K 棒量比沒有意義，不得當成「放量」呈現
                rvol = None
                rvol_tod = None
                logger.warning(
                    f"[{symbol}] 15m K 棒一致性檢查未通過: {assessment.notes}"
                )

            result["open_15m"] = o_15m
            result["high_15m"] = h_15m
            result["low_15m"] = l_15m
            result["close_15m"] = c_15m
            result["volume_15m"] = v_15m
            result["volume_15m_sma20"] = sma_15m
            result["rvol_15m"] = rvol
            result["volume_15m_tod_avg"] = tod_avg
            result["rvol_15m_tod"] = rvol_tod
            result["tod_sample_count"] = _extract_val("tod_sample_count") or 0
            # 盤前／休市時最近一根已收盤 K 棒屬於前一交易日，呈現層須標明日期
            result["bar_15m_is_prior_session"] = (
                bar_time is not None and bar_time.date() != now_ny.date()
            )

        result["session_vwap_date"] = (
            session_stats.session_date if session_stats is not None else None
        )

        # TDP 估值三擊判斷: 現價 < EMA 21 且 現價 < Max Pain 且 現價 < V-POC
        ema_21 = (
            df_hist_1d["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
            if df_hist_1d is not None and not df_hist_1d.empty
            else 0.0
        )
        vpoc = _safe_float(safe_vp.get("hvn"), 0.0)
        max_pain = _safe_float(result.get("max_pain"), 0.0)
        price = _safe_float(result.get("price"), 0.0)

        if result.get("is_ddp"):
            if price > 0 and ema_21 > 0 and max_pain > 0 and vpoc > 0:
                if price < ema_21 and price < max_pain and price < vpoc:
                    result["is_ddp"] = True
                    result["tdp_activated"] = True

                    psq_res = result.get("psq_result", {})
                    is_sqz = (
                        psq_res.get("is_squeezing", False)
                        if isinstance(psq_res, dict)
                        else getattr(psq_res, "is_squeezing", False)
                    )
                    if is_sqz:
                        result["tdpq_activated"] = True
        ctx = database.get_full_user_context(user_id)

        try:
            user_capital = _safe_float(getattr(ctx, "capital", 100000.0), 100000.0)
            # risk_limit 是百分比單位 (DB 預設 15.0)；舊備援值 0.05 會被當成 0.05%。
            risk_limit = _safe_float(getattr(ctx, "risk_limit", 15.0), 15.0)
            raw_stock_iv = (
                iv_metrics.get("current_iv")
                if isinstance(iv_metrics, dict)
                else getattr(iv_metrics, "current_iv", None)
            )
            stock_iv_val = _safe_float(raw_stock_iv, 0.0)
            # 0.40 只是讓公式可算的佔位值；真實 IV 缺失以 stock_iv_unknown 交給引擎 fail-closed。
            stock_iv_unknown = stock_iv_val <= 0
            stock_iv = stock_iv_val if stock_iv_val > 0 else 0.40
            # PCR 缺值為 None (NRO 不做 PCR 修正)，不補 0.8。
            vol_pcr: Optional[float] = (
                _safe_float(pcr_data.get("volume_pcr"))
                if isinstance(pcr_data, dict) and pcr_data.get("volume_pcr") is not None
                else None
            )
            skew_val = _safe_float(safe_skew.get("skew"), 0.0)

            degraded_reasons: list[str] = []
            if result.get("iv_rank") is None:
                degraded_reasons.append("IV Rank 缺失")
            if vol_pcr is None:
                degraded_reasons.append("Volume PCR 缺失")
            # 與呈現層 (portfolio_embeds) 相同的取值與缺值定義
            oi_pcr_raw = (
                pcr_data.get("oi_pcr", pcr_data.get("pcr"))
                if isinstance(pcr_data, dict)
                else None
            )
            if oi_pcr_raw is None or _safe_float(oi_pcr_raw, 0.0) <= 0:
                degraded_reasons.append("OI PCR 缺失")
            if isinstance(gex_profile_data, dict) and gex_profile_data.get(
                "_is_stale_cache"
            ):
                degraded_reasons.append("GEX 快取降級")
            gravity = find_settlement_gravity(result.get("month_max_pains"), now_ny)
            if gravity is not None:
                degraded_reasons.append(
                    f"結算日引力 DTE {gravity['dte']} 偏離痛點 "
                    f"{gravity['distance_pct']:+.1f}%"
                )

            # 單口 β 加權 Delta（SPY 等值股數），與 strategy/analyze.py 同定義：
            # Δ × β × (股價/SPY) × 100。舊版寫死 0.16 少乘了後三項。
            from market_analysis.risk_engine import calculate_beta_strict

            beta = (
                calculate_beta_strict(df_hist_1d, df_spy)
                if df_hist_1d is not None and df_spy is not None
                else None
            )
            if beta is None:
                degraded_reasons.append("Beta 資料不足（以 1.0 計）")
            beta_eff = beta if beta is not None else 1.0
            px = _safe_float(safe_quote.get("c"), 0.0) or _safe_float(
                result.get("price"), 0.0
            )
            unit_wd = (
                _KELLY_REF_SHORT_PUT_DELTA * beta_eff * (px / spy_price) * 100.0
                if spy_price and px > 0
                else 0.0
            )
            result["kelly_beta"] = beta
            result["kelly_unit_weighted_delta"] = unit_wd

            opt_result = optimize_position_risk(
                current_delta=0.0,
                unit_weighted_delta=unit_wd,
                user_capital=user_capital,
                spy_price=spy_price if spy_price is not None else 0.0,
                stock_iv=stock_iv,
                strategy="STO",
                macro_data=macro_data,
                risk_limit=risk_limit,
                vix_spot=vix_now,
                pcr=vol_pcr,
                skew=skew_val,
                vix_unknown=macro_data is None,
                stock_iv_unknown=stock_iv_unknown,
                data_degraded_reasons=degraded_reasons,
            )
            result["kelly_sizing"] = opt_result
        except Exception as e:
            logger.warning(f"[{symbol}] Kelly sizing calculation skipped: {e}")

        return result

    async def _build_symbol_hub_page(self, symbol: str, user_id: int) -> SymbolHubPage:
        """建構單一標的的分析頁；任何失敗皆回傳錯誤頁（base_data 為 None），不拋例外。

        單檔與多檔流程共用。例外細節只寫入日誌，不顯示給使用者。
        """
        # 與 market_data_service 各抓取函式同一套清洗規則（去空白、去 `$` 前後綴、
        # 轉大寫，保留 BRK.B 的 `.`）。只做 upper() 會讓 " nvda" 以 " NVDA" 通過
        # validate_symbol（其內部自行 strip），卻以帶空白的代號組 SingleFlight 鍵、
        # 比對持倉成本、傳給不自行清洗的下游；"$NVDA" 則會被誤判為無效代號。
        symbol = _sanitize_ticker(symbol)
        try:
            if not symbol or not await market_data_service.validate_symbol(symbol):
                error_emb = create_error_embed(
                    (
                        f"無效的標的代號: `{symbol}`"
                        if symbol
                        else "請輸入有效的股票代號（例如 NVDA、BRK.B）。"
                    ),
                    title="輸入錯誤",
                )
                return SymbolHubPage(symbol, None, error_emb)

            # 🚀 Task 2 Hook: Coalesced fetch using SingleFlightManager
            from services.single_flight import SingleFlightManager

            data = await SingleFlightManager.run(
                f"single_hub_{symbol}",
                self._fetch_single_symbol_data_raw,
                symbol,
            )

            result = await self._process_symbol_hub_data(symbol, user_id, data)

            main_embed = create_tactical_symbol_embed(result)
            return SymbolHubPage(symbol, result, main_embed)

        except Exception as e:
            logger.exception(f"Symbol Hub Error for {symbol}: {e}")
            error_emb = create_error_embed(
                f"載入 `{symbol}` 資料時發生錯誤，請稍後再試。"
            )
            return SymbolHubPage(symbol, None, error_emb)

    @market_data_service.interactive
    async def _run_single_symbol_hub(
        self,
        interaction: discord.Interaction,
        symbol: str,
        user_id: int,
        embeds_accumulator: Optional[List[discord.Embed]] = None,
    ) -> Any:
        page = await self._build_symbol_hub_page(symbol, user_id)

        if embeds_accumulator is not None:
            embeds_accumulator.append(page.embed)
            return

        if page.base_data is None:
            return await interaction.followup.send(embed=page.embed, ephemeral=True)

        view = SymbolHubView(page.symbol, user_id, self.bot)
        view.base_data = page.base_data
        # wait=True：讓 View 以 message id 登記。未帶時會以 message_id=None 登記，
        # 而 SymbolHubView 的 custom_id 固定，後送出的 /x 會覆蓋前一個 View，
        # 點舊訊息的按鈕會作用在最新那個 View 上。
        await interaction.followup.send(
            embed=page.embed, view=view, ephemeral=True, wait=True
        )

    @market_data_service.interactive
    async def _run_multi_symbol_hub(
        self,
        interaction: discord.Interaction,
        symbols: list[str],
        user_id: int,
    ) -> Any:
        """多標的深度分析：全程不使用 followup，結果以單則訊息逐檔換頁呈現。"""
        from services.llm_service import is_memory_safe

        total = len(symbols)
        # CPU／記憶體併發上限，不是對外 API 限流（對外限流仍由 rate_gate 負責）。
        # 名額持有到該檔抓取真正結束（不設單檔逾時），背景抓取數因此不超過此值。
        sem = asyncio.Semaphore(_BATCH_CONCURRENCY)

        def _error_page(sym: str, message: str, title: str) -> SymbolHubPage:
            return SymbolHubPage(sym, None, create_error_embed(message, title=title))

        async def _analyze(sym: str) -> SymbolHubPage:
            async with sem:
                if not is_memory_safe():
                    return _error_page(
                        sym, f"記憶體水位過高，已略過 `{sym}`。", "資源不足"
                    )
                return await self._build_symbol_hub_page(sym, user_id)

        tasks = [asyncio.create_task(_analyze(sym)) for sym in symbols]

        # 進度更新由此處依序送出（數字單調遞增），不佔用各檔任務的時間
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BATCH_DEADLINE_S
        pending: set[asyncio.Task[SymbolHubPage]] = set(tasks)
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            _, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if pending:
                try:
                    await asyncio.wait_for(
                        interaction.edit_original_response(
                            content=f"⏳ 分析中 {total - len(pending)}/{total}…"
                        ),
                        _PROGRESS_EDIT_TIMEOUT_S,
                    )
                except Exception as e:
                    logger.debug(f"/x 多檔進度更新失敗（忽略）: {e}")
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # 依輸入順序重組；未完成者為逾時頁，例外者為一般錯誤頁
        pages: list[SymbolHubPage] = []
        for sym, t in zip(symbols, tasks):
            if t.cancelled():
                pages.append(
                    _error_page(sym, f"⏱️ `{sym}` 分析逾時，請改為單獨查詢。", "逾時")
                )
            elif t.exception() is not None:
                logger.error(f"/x 多檔分析任務例外 {sym}: {t.exception()!r}")
                pages.append(
                    _error_page(
                        sym, f"載入 `{sym}` 資料時發生錯誤，請稍後再試。", "錯誤"
                    )
                )
            else:
                pages.append(t.result())

        first_ok = next(
            (i for i, p in enumerate(pages) if p.base_data is not None), None
        )
        if first_ok is None:
            # 逐檔列出原因（無效代號／資源不足／逾時），避免使用者誤以為系統故障
            reasons = "\n".join(
                f"• {p.symbol or '（空白）'}：{p.embed.description or '載入失敗'}"
                for p in pages
            )
            await interaction.edit_original_response(
                content=None,
                embed=create_error_embed(
                    f"以下標的皆無法載入：\n{reasons}", title="載入失敗"
                ),
            )
            return

        view = SymbolBatchHubView(pages, user_id, self.bot, start_index=first_ok)
        view.last_interaction = interaction
        await interaction.edit_original_response(
            content=None, embed=view.current_embed(), view=view
        )
