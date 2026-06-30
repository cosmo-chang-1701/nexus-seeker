from .history_storage import INDEX_SYMBOLS
import logging
import pandas as pd
import sqlite3  # noqa: F401
import asyncio
from datetime import datetime
from typing import Dict, Any, List
from services import market_data_service
from market_analysis.uoa_telemetry import UOATradeInput, classify_uoa_trade
from market_analysis.greeks import calculate_greeks


logger = logging.getLogger(__name__)


async def detect_uoa(
    symbol: str,
    max_expiries: int = 4,
    vol_oi_ratio: float = 5.0,
    min_volume: int = 500,
    max_non_index_nominal: float = 500_000_000.0,
) -> List[Dict[str, Any]]:
    """
    偵測異常期權活動 (Unusual Options Activity)。
    經過高並發 I/O 與 Pandas 向量化優化，並加入異常數據風控機制。
    """
    try:
        expiries = await market_data_service.get_all_option_expiries(symbol)
        if not expiries:
            return []

        # 1. 取得即時現價並進行風控驗證
        spot_price = 0.0
        try:
            quote = await market_data_service.get_quote(symbol)
            spot_price = quote.get("c", 0.0) if quote else 0.0
        except Exception as e:
            logger.warning(f"[{symbol}] detect_uoa 取得現價失敗: {e}")

        if spot_price <= 0:
            logger.error(
                f"[{symbol}] 現價異常或為零 ({spot_price})，熔斷 UOA 偵測以防 Greeks 誤判。"
            )
            return []

        uoa_list = []
        target_expiries = expiries[:max_expiries]

        # 2. 性能優化：使用 asyncio.gather 併發獲取所有期權鏈資料 (I/O 優化)
        tasks = [
            market_data_service.get_option_chain(symbol, exp) for exp in target_expiries
        ]
        chains = await asyncio.gather(*tasks, return_exceptions=True)

        today_dt = datetime.now().date()

        # 3. 處理每個到期日的期權鏈
        for exp, chain in zip(target_expiries, chains):
            if isinstance(chain, BaseException) or not chain:
                if isinstance(chain, BaseException):
                    logger.error(f"[{symbol}] 獲取到期日 {exp} 期權鏈失敗: {chain}")
                continue

            # 效能優化：預先打上標籤，消除內層迴圈中的 O(N) 重複查找
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

            # 4. 效能優化：利用 Pandas 向量化直接篩選符合 UOA 門檻的資料 (拋棄慢速 iterrows 預篩)
            # 排除 openInterest <= 0 的情況
            filter_mask = (
                (df_combined["openInterest"] > 0)
                & (df_combined["volume"] > vol_oi_ratio * df_combined["openInterest"])
                & (df_combined["volume"] > min_volume)
            )
            df_uoa_candidates = df_combined[filter_mask]

            if df_uoa_candidates.empty:
                continue

            # 計算到期天數常數
            try:
                exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = max((exp_dt - today_dt).days, 0.5)
                t_years = dte / 365.0
            except Exception as parse_err:
                logger.error(f"[{symbol}] 到期日格式解析失敗 {exp}: {parse_err}")
                continue

            # 5. 僅對篩選後的黃金樣本進行精細量化風控與 Greeks 計算
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
                    if "lastPrice" in row and pd.notna(row["lastPrice"])
                    else 0.0
                )

                # 風控檢驗 2：非指數虛擬名義價值過濾
                is_index = symbol in INDEX_SYMBOLS or symbol.startswith("^")
                nominal_val = vol * trade_price * 100.0
                if nominal_val > max_non_index_nominal and not is_index:
                    logger.warning(
                        f"[{symbol}] UOA 名義價值 ${nominal_val:,.2f} 超過限制。予以剔除。"
                    )
                    continue

                # 風控檢驗 3：異常 IV 熔斷與 Delta 深價內過濾
                iv_val = float(row.get("impliedVolatility", 0.0) or 0.0)

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
                if abs(d_val) > 0.70:
                    logger.warning(
                        f"[{symbol}] 深價內合約 Delta ({d_val:.2f}) 疑似除權息未調整資料。予以剔除。"
                    )
                    continue

                # 6. 分類與結果封裝
                trade_type = row.get("trade_type")
                if not trade_type:
                    trade_type = (
                        "BLOCK" if (vol > 1500 and int(vol) % 100 == 0) else "SWEEP"
                    )

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

                result = classify_uoa_trade(trade_input, current_price=spot_price)

                uoa_list.append(
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
                    }
                )

        # 依成交量降序排列，取前 5 大
        return sorted(uoa_list, key=lambda x: x["volume"], reverse=True)[:5]

    except Exception as e:
        logger.error(f"[{symbol}] UOA 偵測嚴重失敗: {e}", exc_info=True)
        return []
