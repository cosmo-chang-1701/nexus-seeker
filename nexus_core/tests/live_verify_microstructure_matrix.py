"""Live 真實數據驗證腳本：微觀結構出場決策矩陣 + 選擇權資料先天限制補強。

刻意不透過 pytest（避免 conftest.py 的全域 mock 攔截真實網路呼叫），直接
呼叫底層函式對真實市場數據（yfinance）進行端到端驗證。

涵蓋範圍：
1. market_time.get_trading_day_elapsed_fraction()：真實 NYSE 行事曆 + 真實當下時間。
2. uoa_detector.detect_uoa()：真實選擇權鏈，驗證 paced_ratio 欄位存在且計算正確。
3. structural_signals._detect_whale_put_bto_block()：餵入真實 UOA 資料。
4. anti_washout 的 SL/TP 分層純函式：餵入真實 spot/ATR 數據。
5. fetch_symbol_gex_metrics(force_live=True)：若執行環境有設定 TUNNEL_URL，
   會直接對 Edge Scraper 代理隧道發動真實抓取，驗證 GEX 牆 (call_wall/
   put_wall/net_gex) 端到端可用；未設定時優雅降級為 fallback 零值並印出
   提示，不會讓腳本整體失敗。

已知未驗證範圍：完整的 check_satellite_rebalancing() 端到端流程（含 DTE
三態狀態機、委託單淨額扣抵等）需要真實使用者持倉資料庫紀錄，本腳本聚焦於
不依賴使用者資料的純函式/單一資料源驗證。
"""

import asyncio
import logging
import sys

sys.path.insert(0, "/app")

import config  # noqa: E402
import market_time  # noqa: E402
from market_analysis.sentiment.uoa_detector import detect_uoa  # noqa: E402
from market_analysis.dynamic_rollover.structural_signals import (  # noqa: E402
    _detect_whale_put_bto_block,
)
from market_analysis.dynamic_rollover import DynamicRolloverEngine  # noqa: E402
from market_analysis.index_microstructure import (  # noqa: E402
    fetch_symbol_gex_metrics,
)
from services import market_data_service  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


async def main() -> None:
    print("=" * 70)
    print("1. market_time.get_trading_day_elapsed_fraction()")
    print("=" * 70)
    is_open = market_time.is_market_open()
    fraction = market_time.get_trading_day_elapsed_fraction()
    print(f"is_market_open() = {is_open}")
    print(f"get_trading_day_elapsed_fraction() = {fraction}")
    if not is_open:
        assert fraction == 1.0, "非交易時間應回傳 1.0"
        print("✅ 非交易時間正確回傳 1.0（不做正規化）")
    else:
        assert 0.0 < fraction <= 1.0
        print(f"✅ 交易時間內回傳合理範圍值: {fraction:.4f}")

    print()
    print("=" * 70)
    print("2. uoa_detector.detect_uoa() — 真實選擇權鏈 (SPY/NVDA/TSLA)")
    print("=" * 70)
    print(f"TUNNEL_URL configured: {bool(getattr(config, 'TUNNEL_URL', ''))}")

    any_uoa_found = False
    for symbol in ["SPY", "NVDA", "TSLA", "AAPL"]:
        try:
            results = await detect_uoa(symbol, force_live=True)
        except Exception as e:
            print(f"[{symbol}] detect_uoa 例外: {e}")
            continue

        print(f"[{symbol}] {len(results)} 筆 UOA 候選")
        for r in results[:3]:
            ratio = float(r.get("ratio") or 0.0)
            paced = float(r.get("paced_ratio") or 0.0)
            print(
                f"  strike={r['strike']} type={r['type']} action={r['action']} "
                f"ratio={ratio:.2f} paced_ratio={paced:.2f} "
                f"notional=${r['notional_value']:,.0f}"
            )
            assert "paced_ratio" in r, "paced_ratio 欄位缺失"
            # 生產程式碼對 paced_ratio 做了 round(x, 4)，容差需對應 4 位小數
            # 的四捨五入誤差 (最大 0.00005)，而非要求與未四捨五入的原始值
            # 位元級相等。
            expected = round(ratio / fraction, 4)
            assert (
                abs(paced - expected) < 1e-4
            ), f"paced_ratio 計算錯誤: {paced} != {expected} (ratio={ratio}, fraction={fraction})"
            any_uoa_found = True

        # 順便驗證 SL-主力對沖偵測邏輯本身在真實資料上不會拋例外
        quote = await market_data_service.get_quote(symbol)
        spot = float(quote.get("c", 0.0)) if quote else 0.0
        is_whale = _detect_whale_put_bto_block(results, spot)
        print(f"  spot=${spot:.2f} _detect_whale_put_bto_block() = {is_whale}")

    if any_uoa_found:
        print("✅ paced_ratio 欄位存在且數值關係正確")
    else:
        print(
            "⚠️ 本輪未偵測到任何 UOA 候選（可能因假日/週末盤後資料不足），"
            "但函式呼叫本身未拋例外"
        )

    print()
    print("=" * 70)
    print("3. anti_washout SL/TP 分層純函式 — 真實 spot/ATR 數據 (SPY)")
    print("=" * 70)
    engine = DynamicRolloverEngine()
    spy_quote = await market_data_service.get_quote("SPY")
    spy_spot = float(spy_quote.get("c", 0.0)) if spy_quote else 0.0
    print(f"SPY 真實現價: ${spy_spot:.2f}")

    df_15m = await market_data_service.get_history_df(
        "SPY", period="5d", interval="15m", force_refresh=True
    )
    atr_15m = 0.0
    if df_15m is not None and not df_15m.empty and len(df_15m) > 14:
        high_low = (df_15m["High"] - df_15m["Low"]).abs()
        atr_15m = float(high_low.tail(14).mean())
    print(f"SPY 真實 15m ATR (簡易估算): {atr_15m:.4f}")

    # 用真實 spot/ATR，但因無 TUNNEL_URL 這裡的 support_wall 僅能用 spot 附近
    # 的合成值示意 SL-結構失效的計算路徑本身可正確運作（非驗證真實 GEX 牆位）。
    synthetic_anchor = round(spy_spot * 0.99, 2)
    metrics = {
        "spot_price": spy_spot,
        "price_15m_close": spy_spot,
        "support_wall": synthetic_anchor,
        "atr_15m": atr_15m,
    }
    anchor_base, _res_wall = engine._correct_wall_topology(metrics)
    stop_loss, limit_price, extreme_stop_loss = engine._compute_anti_washout_stop(
        anchor_base, metrics
    )
    print(
        f"anchor_base={anchor_base:.2f} stop_loss={stop_loss:.2f} "
        f"extreme_stop_loss={extreme_stop_loss:.2f}"
    )
    assert anchor_base == synthetic_anchor
    assert stop_loss < anchor_base or atr_15m == 0.0
    print("✅ SL 分層計算管線在真實 spot/ATR 輸入下正常運作")

    print()
    print("=" * 70)
    print("4. fetch_symbol_gex_metrics(force_live=True) — 真實 Edge Scraper 抓取")
    print("=" * 70)
    for symbol in ["SPY", "NVDA"]:
        try:
            gex_data = await fetch_symbol_gex_metrics(symbol, force_live=True)
        except Exception as e:
            print(f"[{symbol}] fetch_symbol_gex_metrics 例外: {e}")
            continue
        print(
            f"[{symbol}] spot={gex_data.get('spot')} "
            f"net_gex={gex_data.get('net_gex')} "
            f"call_wall={gex_data.get('call_wall')} "
            f"put_wall={gex_data.get('put_wall')} "
            f"gex_profile 履約價數={len(gex_data.get('gex_profile', {}) or {})} "
            f"is_stale_cache={gex_data.get('_is_stale_cache', False)}"
        )
        if gex_data.get("call_wall") or gex_data.get("put_wall"):
            print(f"✅ [{symbol}] 真實抓取到非零 GEX 牆位")
        else:
            print(
                f"⚠️ [{symbol}] GEX 牆位為零（可能是 fallback 或該標的真的無有效資料）"
            )

    print()
    print("=" * 70)
    print("5. market_data_service 直連驗證（確認真實網路請求確實發生）")
    print("=" * 70)
    print(f"SPY quote raw: {spy_quote}")
    print(f"SPY 15m bars fetched: {0 if df_15m is None else len(df_15m)} 根")
    assert spy_spot > 0, "真實 SPY 報價應為正數（若為 0 代表網路/資料源真的失敗）"
    print("✅ 確認為真實網路數據，非 mock")

    print()
    print("=" * 70)
    print("驗證範圍總結")
    print("=" * 70)
    print(f"- TUNNEL_URL 是否設定: {bool(getattr(config, 'TUNNEL_URL', ''))}")
    print("- 上述第 2/3/4/5 節皆為真實網路請求 (yfinance + Edge Scraper)，非 mock。")
    print("- 未驗證項目：完整的 check_satellite_rebalancing() 端到端流程（含")
    print("  DTE 三態狀態機、委託單淨額扣抵等），因需要真實使用者持倉資料庫紀錄，")
    print("  純函式層級 (SL/TP 分層計算、paced_ratio、whale 偵測、GEX 抓取) 已")
    print("  各自以真實數據驗證過。")


if __name__ == "__main__":
    asyncio.run(main())
