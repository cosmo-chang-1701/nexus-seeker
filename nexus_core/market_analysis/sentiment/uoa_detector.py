from .history_storage import INDEX_SYMBOLS
import logging
import pandas as pd
import sqlite3  # noqa: F401
import asyncio
from datetime import date, datetime
from typing import Dict, Any, List, Tuple
from services import market_data_service
from market_analysis.uoa_telemetry import UOATradeInput, classify_uoa_trade
from market_analysis.greeks import calculate_greeks


logger = logging.getLogger(__name__)


async def _fetch_and_combine_chains(
    symbol: str, max_expiries: int = 4, force_live: bool = False
) -> Tuple[float, List[Tuple[str, "pd.DataFrame", float]]]:
    """抓取現價與多個到期日的完整合併期權鏈（calls+puts 標記 option_type）。

    供 `detect_uoa()` 與 `detect_uoa_with_physical_caps()` 共用，確保兩者對
    同一 symbol 只發動一次期權鏈網路請求（`get_option_chain` 本身有 20 分鐘
    BoundedCache，但共用同一次 gather 可避免重複的 DataFrame 組裝與潛在的
    快取未命中窗口）。

    回傳 (spot_price, [(expiry, df_combined, total_chain_volume), ...])。
    若現價無效或無到期日資料，回傳 (0.0, [])。
    """
    expiries = await market_data_service.get_all_option_expiries(symbol)
    if not expiries:
        return 0.0, []

    spot_price = 0.0
    try:
        quote = await market_data_service.get_quote(symbol)
        spot_price = quote.get("c", 0.0) if quote else 0.0
    except Exception as e:
        logger.warning(f"[{symbol}] 取得現價失敗: {e}")

    if spot_price <= 0:
        logger.error(
            f"[{symbol}] 現價異常或為零 ({spot_price})，熔斷期權鏈處理以防 Greeks 誤判。"
        )
        return 0.0, []

    target_expiries = expiries[:max_expiries]
    tasks = [
        market_data_service.get_option_chain(symbol, exp, force_live=force_live)
        for exp in target_expiries
    ]
    chains = await asyncio.gather(*tasks, return_exceptions=True)

    results: List[Tuple[str, pd.DataFrame, float]] = []
    for exp, chain in zip(target_expiries, chains):
        if isinstance(chain, BaseException) or not chain:
            if isinstance(chain, BaseException):
                logger.error(f"[{symbol}] 獲取到期日 {exp} 期權鏈失敗: {chain}")
            continue

        dfs = []
        total_chain_volume = 0.0

        if chain.calls is not None and not chain.calls.empty:
            df_calls = chain.calls.copy()
            df_calls["option_type"] = "CALL"
            dfs.append(df_calls)
            total_chain_volume += float(df_calls["volume"].sum())

        if chain.puts is not None and not chain.puts.empty:
            df_puts = chain.puts.copy()
            df_puts["option_type"] = "PUT"
            dfs.append(df_puts)
            total_chain_volume += float(df_puts["volume"].sum())

        if not dfs:
            continue

        df_combined = pd.concat(dfs, ignore_index=True)
        results.append((exp, df_combined, total_chain_volume))

    return spot_price, results


def _select_uoa_candidate_rows(
    df_combined: "pd.DataFrame", vol_oi_ratio: float, min_volume: int
) -> "pd.DataFrame":
    """雙軌 UOA 候選篩選：(1) Sweep 異動 Volume/OI 比值, (2) Whale Block 巨額名義價值。"""
    price_col = (
        df_combined["lastPrice"].fillna(0.0)
        if "lastPrice" in df_combined.columns
        else pd.Series(0.0, index=df_combined.index)
    )
    nominal_proxy = df_combined["volume"] * price_col * 100.0

    sweep_mask = (
        (df_combined["openInterest"] > 0)
        & (df_combined["volume"] > vol_oi_ratio * df_combined["openInterest"])
        & (df_combined["volume"] >= min_volume)
    )
    whale_block_mask = (
        (df_combined["openInterest"] > 0)
        & (df_combined["volume"] >= 500)
        & (nominal_proxy >= 250_000.0)
    )
    return df_combined[sweep_mask | whale_block_mask]


def _process_uoa_candidate_rows(
    symbol: str,
    exp: str,
    today_dt: date,
    df_uoa_candidates: "pd.DataFrame",
    total_chain_volume: float,
    spot_price: float,
    max_non_index_nominal: float,
) -> List[Dict[str, Any]]:
    """對已篩選出的候選列執行風控驗證、Greeks 計算與意圖分類，回傳結果 dict 列表。"""
    results: List[Dict[str, Any]] = []

    try:
        exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
        dte = max((exp_dt - today_dt).days, 0.5)
        t_years = dte / 365.0
    except Exception as parse_err:
        logger.error(f"[{symbol}] 到期日格式解析失敗 {exp}: {parse_err}")
        return results

    for _, row in df_uoa_candidates.iterrows():
        vol = float(row["volume"])
        oi = float(row["openInterest"])
        strike = float(row["strike"])
        opt_type = row["option_type"]

        # 風控檢驗 1：總量守恆定律 (Data Cleanse)
        if vol > total_chain_volume:
            logger.warning(
                f"[{symbol}] 異常資料：單一合約成交量 ({vol}) 大於全鏈總量 ({total_chain_volume})。予以剔除。"
            )
            continue

        trade_price = (
            float(row["lastPrice"])
            if "lastPrice" in row
            and pd.notna(row["lastPrice"])
            and float(row["lastPrice"]) > 0
            else (
                (float(row["bid"]) + float(row["ask"])) / 2.0
                if "bid" in row
                and "ask" in row
                and pd.notna(row["bid"])
                and pd.notna(row["ask"])
                and float(row["ask"]) > 0
                else (
                    float(row.get("ask", 0.0) or 0.0)
                    or float(row.get("bid", 0.0) or 0.0)
                )
            )
        )

        # 風控檢驗 2：非指數虛擬名義價值過濾與 UOA 門檻 (一般標的 >= $50k，極端高波 IV > 80% 標的動態提升至 >= $250k 且成交量 >= 1000 口以過濾散戶雜訊)
        nominal_val = vol * trade_price * 100.0
        iv_val = float(row.get("impliedVolatility", 0.0) or 0.0)
        is_high_iv_noise = iv_val > 0.80
        min_nominal_threshold = 250_000.0 if is_high_iv_noise else 50_000.0
        min_vol_threshold = 1000.0 if is_high_iv_noise else 300.0

        if nominal_val < min_nominal_threshold or vol < min_vol_threshold:
            continue

        is_index = symbol in INDEX_SYMBOLS or symbol.startswith("^")
        if nominal_val > max_non_index_nominal and not is_index:
            logger.warning(
                f"[{symbol}] UOA 名義價值 ${nominal_val:,.2f} 超過限制。予以剔除。"
            )
            continue

        # 風控檢驗 3：異常 IV 熔斷與 Delta 深價內過濾

        # 報告精髓：防範非交易時段 SQLite 快取導致的 15.5% 等低 IV 異常
        if iv_val <= 0.02:  # IV 低於 2% 通常為異常快取或無流動性報價
            logger.warning(
                f"[{symbol}] 合約 {exp} {opt_type} {strike} 偵測到異常低 IV ({iv_val:.2f})，跳過 Greeks 計算。"
            )
            d_val = 0.0
        else:
            greeks = calculate_greeks(
                opt_type.lower(),
                spot_price,
                strike,
                t_years,
                iv_val,
                0.0,  # 假設無風險利率為 0 或外部傳入
            )
            d_val = greeks.get("delta", 0.0)

        # 深價內 (ITM) 排除邏輯（防止除權息或異常調整數據污染）
        # 閾值放寬至 0.95：0.70 會誤傷合法的深度避險 Put/Call (Whale_Hedge
        # 分類需要 Delta < -0.65 的深價內 Put 存活到分類邏輯，見
        # uoa_telemetry.py::classify_uoa_trade)，僅過濾真正異常值 (>0.95)。
        if abs(d_val) > 0.95:
            logger.warning(
                f"[{symbol}] 深價內合約 Delta ({d_val:.2f}) 疑似除權息未調整資料。予以剔除。"
            )
            continue

        # 6. 分類與結果封裝
        trade_type = row.get("trade_type")
        if not trade_type:
            trade_type = "BLOCK" if (vol > 1500 and int(vol) % 100 == 0) else "SWEEP"

        oi_change_net = (
            int(row.get("oi_change_net"))
            if pd.notna(row.get("oi_change_net"))
            else int(vol - oi)
        )

        trade_input = UOATradeInput(
            expiry=exp,
            strike_price=strike,
            option_type=opt_type,
            trade_price=trade_price,
            bid_price=float(row["bid"])
            if "bid" in row and pd.notna(row["bid"])
            else 0.0,
            ask_price=float(row["ask"])
            if "ask" in row and pd.notna(row["ask"])
            else 0.0,
            volume=int(vol),
            open_interest=int(oi),
            symbol=symbol,
        )

        result = classify_uoa_trade(trade_input, current_price=spot_price, delta=d_val)

        results.append(
            {
                "symbol": symbol,
                "expiry": exp,
                "strike": result.strike_price,
                "type": result.option_type,
                "volume": result.volume,
                "oi": result.open_interest,
                "ratio": result.ratio,
                "ratio_str": result.ratio_str,
                "trade_price": result.trade_price,
                "bid_price": result.bid_price,
                "ask_price": result.ask_price,
                "action": result.action,
                "intent": result.intent,
                "iv": round(iv_val, 4),
                "trade_type": trade_type,
                "oi_change_net": oi_change_net,
                "delta": result.delta,
                "dte": result.dte,
            }
        )

    return results


async def detect_uoa(
    symbol: str,
    max_expiries: int = 4,
    vol_oi_ratio: float = 3.0,
    min_volume: int = 300,
    max_non_index_nominal: float = 500_000_000.0,
    force_live: bool = False,
) -> List[Dict[str, Any]]:
    """
    偵測異常期權活動 (Unusual Options Activity)。
    支援雙軌判定：(1) 高量與未平倉比 (Sweep 異動), (2) 巨額名義價值機構大單 (Whale Block)。
    經過高並發 I/O 與 Pandas 向量化優化，並加入異常數據風控機制。
    """
    try:
        spot_price, chain_data = await _fetch_and_combine_chains(
            symbol, max_expiries, force_live=force_live
        )
        if spot_price <= 0 or not chain_data:
            return []

        uoa_list: List[Dict[str, Any]] = []
        today_dt = datetime.now().date()

        for exp, df_combined, total_chain_volume in chain_data:
            df_uoa_candidates = _select_uoa_candidate_rows(
                df_combined, vol_oi_ratio, min_volume
            )
            if df_uoa_candidates.empty:
                continue

            uoa_list.extend(
                _process_uoa_candidate_rows(
                    symbol,
                    exp,
                    today_dt,
                    df_uoa_candidates,
                    total_chain_volume,
                    spot_price,
                    max_non_index_nominal,
                )
            )

        # 依成交量降序排列，取前 5 大
        return sorted(uoa_list, key=lambda x: x["volume"], reverse=True)[:5]

    except Exception as e:
        logger.error(f"[{symbol}] UOA 偵測嚴重失敗: {e}", exc_info=True)
        return []


async def detect_uoa_with_physical_caps(
    symbol: str,
    max_expiries: int = 4,
    vol_oi_ratio: float = 3.0,
    min_volume: int = 300,
    max_non_index_nominal: float = 500_000_000.0,
    physical_cap_ratio: float = 0.8,
    physical_cap_min_volume: int = 500,
    force_live: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """偵測異常期權活動，並額外對完整期權鏈（非僅前 5 大 UOA）掃描 STO 物理封頂。

    回傳 (top5_uoa_list, physical_cap_strikes)：
    - top5_uoa_list：與 `detect_uoa()` 完全相同的前 5 大 UOA 清單。
    - physical_cap_strikes：全期權鏈中 Bid-side Volume/OI 比例 >= physical_cap_ratio
      (預設 0.8x) 且成交量 >= physical_cap_min_volume (預設 500 口)，且啟發式分類
      (`classify_uoa_trade`) 判定為 STO(Bid) 的履約價清單。此為「Bid-side 成交量」
      的啟發式代理指標（yfinance/edge scraper 未提供真實買方分邊成交量 tape），
      供「現貨重砲」等進場建議的一票否決旗標使用。

    與 `detect_uoa()` 共用同一次期權鏈抓取 (`_fetch_and_combine_chains`)，
    不發動額外網路請求。
    """
    try:
        spot_price, chain_data = await _fetch_and_combine_chains(
            symbol, max_expiries, force_live=force_live
        )
        if spot_price <= 0 or not chain_data:
            return [], []

        uoa_list: List[Dict[str, Any]] = []
        physical_cap_strikes: List[Dict[str, Any]] = []
        today_dt = datetime.now().date()

        for exp, df_combined, total_chain_volume in chain_data:
            df_uoa_candidates = _select_uoa_candidate_rows(
                df_combined, vol_oi_ratio, min_volume
            )
            if not df_uoa_candidates.empty:
                uoa_list.extend(
                    _process_uoa_candidate_rows(
                        symbol,
                        exp,
                        today_dt,
                        df_uoa_candidates,
                        total_chain_volume,
                        spot_price,
                        max_non_index_nominal,
                    )
                )

            # 全鏈 STO 物理封頂掃描（非僅前 5 大候選）：
            # ratio = volume/OI >= physical_cap_ratio 改寫為乘法比較，避免除以 0。
            cap_mask = (
                (df_combined["openInterest"] > 0)
                & (df_combined["volume"] >= physical_cap_min_volume)
                & (
                    df_combined["volume"]
                    >= physical_cap_ratio * df_combined["openInterest"]
                )
            )
            df_cap_candidates = df_combined[cap_mask]
            for _, row in df_cap_candidates.iterrows():
                vol = float(row["volume"])
                oi = float(row["openInterest"])
                strike = float(row["strike"])
                opt_type = row["option_type"]
                bid_p = (
                    float(row["bid"]) if "bid" in row and pd.notna(row["bid"]) else 0.0
                )
                ask_p = (
                    float(row["ask"]) if "ask" in row and pd.notna(row["ask"]) else 0.0
                )
                trade_price = (
                    float(row["lastPrice"])
                    if "lastPrice" in row
                    and pd.notna(row["lastPrice"])
                    and float(row["lastPrice"]) > 0
                    else ((bid_p + ask_p) / 2.0 if ask_p > 0 else (ask_p or bid_p))
                )

                cap_trade_input = UOATradeInput(
                    expiry=exp,
                    strike_price=strike,
                    option_type=opt_type,
                    trade_price=trade_price,
                    bid_price=bid_p,
                    ask_price=ask_p,
                    volume=int(vol),
                    open_interest=int(oi),
                    symbol=symbol,
                )
                cap_result = classify_uoa_trade(
                    cap_trade_input, current_price=spot_price
                )
                if cap_result.action == "🔴 賣出開倉 (STO - Bid)":
                    physical_cap_strikes.append(
                        {
                            "strike": strike,
                            "type": opt_type,
                            "ratio": cap_result.ratio,
                            "volume": int(vol),
                            "oi": int(oi),
                            "expiry": exp,
                            "action": "STO",
                        }
                    )

        top5_uoa_list = sorted(uoa_list, key=lambda x: x["volume"], reverse=True)[:5]
        return top5_uoa_list, physical_cap_strikes

    except Exception as e:
        logger.error(f"[{symbol}] UOA 物理封頂偵測嚴重失敗: {e}", exc_info=True)
        return [], []
