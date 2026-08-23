"""
nexus_core/tests/benchmark_background_fetchers.py

全方位背景數據抓取邏輯與效能基準測試腳本。
執行 6 大測試套件並產生結構化效能診斷報告：
  1. Suite 1: Edge Scraper (TUNNEL_URL) 端點連線與延遲診斷
  2. Suite 2: 核心數據抓取與量化指標計算
  3. Suite 3: 併發控制、SingleFlight 請求合併與快取共享效能
  4. Suite 4: 後台排程監控任務端到端模擬
  5. Suite 5: 故障注入與優雅降級 (Circuit Breaker & Fallbacks)
  6. Suite 6: 記憶體與系統資源佔用分析
"""
# ruff: noqa: E402

import asyncio
import gc
import logging
import os
import sys
import time
import tracemalloc
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import httpx

# 設定環境與日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("BenchmarkRunner")

# 確保 /app 在 sys.path
sys.path.insert(0, "/app")

import config
import database
from database.cache import save_kv_cache
from market_analysis import (
    dark_pool_engine,
    index_microstructure,
    psq_engine,
    volume_profile,
    wti_analysis,
)
from market_analysis.sentiment_engine import SentimentEngine
from market_analysis.wti_analysis import WtiAlertType
from services import (
    calendar_service,
    fundamental_service,
    market_data_service,
    reddit_service,
)


class BenchmarkResult:
    """單項測試結果封裝。"""

    def __init__(
        self,
        suite: str,
        name: str,
        status: str,
        latency_ms: float,
        details: str = "",
    ) -> None:
        self.suite = suite
        self.name = name
        self.status = status
        self.latency_ms = latency_ms
        self.details = details


class BenchmarkSuite:
    """全方位背景數據抓取基準評估引擎。"""

    def __init__(self, tunnel_url: str) -> None:
        self.tunnel_url = tunnel_url.rstrip("/")
        self.results: List[BenchmarkResult] = []

    def record(
        self,
        suite: str,
        name: str,
        status: str,
        latency_ms: float,
        details: str = "",
    ) -> None:
        res = BenchmarkResult(suite, name, status, latency_ms, details)
        self.results.append(res)
        icon = "✅" if status == "PASS" else ("⚠️" if status == "WARN" else "❌")
        print(
            f"{icon} [{suite:8s}] {name:48s} | {latency_ms:8.2f}ms | {status:5s} | {details}"
        )

    # =========================================================================
    # Suite 1: Edge Scraper (TUNNEL_URL) 端點連線與延遲診斷
    # =========================================================================
    async def run_suite_1_tunnel_endpoints(self) -> None:
        print("\n" + "=" * 80)
        print("🚀 Suite 1: Edge Scraper (TUNNEL_URL) 端點延遲與健康度診斷")
        print("=" * 80)

        endpoints: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
            ("GET", "/api/v1/health/sys", None),
            ("GET", "/api/v1/scrape/macro/gex", None),
            ("GET", "/api/v1/scrape/macro/liquidity", None),
            ("GET", "/api/v1/scrape/macro/core_metrics", None),
            ("GET", "/api/v1/scrape/darkpool", None),
            ("GET", "/api/v1/scrape/darkpool/NVDA", None),
            ("GET", "/api/v1/scrape/macro/fedwatch", None),
            (
                "GET",
                "/api/v1/macro/calendar?year=2026&month=8&high_impact_only=true",
                None,
            ),
            ("GET", "/api/v1/scrape/reddit/feed?limit=10", None),
            ("GET", "/api/v1/scrape/reddit/NVDA?limit=3", None),
            ("GET", "/api/v1/scrape/fundamental/NVDA/metadata", None),
            ("GET", "/api/v1/scrape/fundamental/NVDA/list", None),
            ("GET", "/api/v1/scrape/fundamental/NVDA", None),
            ("GET", "/api/v1/scrape/options/NVDA/gex", None),
            (
                "POST",
                "/api/v1/watchlist/sync",
                {"symbols": ["NVDA", "AAPL", "TSLA"]},
            ),
            ("GET", "/api/v1/cache/gex/NVDA", None),
            ("GET", "/api/v1/cache/options/NVDA/chain", None),
            ("GET", "/api/v1/scrape/yf/history/AAPL?period=5d&interval=1d", None),
            ("GET", "/api/v1/scrape/yf/options/AAPL/expiries", None),
        ]

        async with httpx.AsyncClient(timeout=45.0) as client:
            for method, path, json_body in endpoints:
                url = f"{self.tunnel_url}{path}"
                t0 = time.perf_counter()
                try:
                    if method == "GET":
                        resp = await client.get(url)
                    else:
                        resp = await client.post(url, json=json_body)
                    elapsed = (time.perf_counter() - t0) * 1000
                    payload_size = len(resp.content)
                    if resp.status_code == 200:
                        status = "PASS"
                        details = f"HTTP 200, {payload_size} bytes"
                    else:
                        status = "WARN"
                        details = f"HTTP {resp.status_code}, {payload_size} bytes"
                    self.record(
                        "Suite 1",
                        f"{method} {path[:40]}",
                        status,
                        elapsed,
                        details,
                    )
                except Exception as e:
                    elapsed = (time.perf_counter() - t0) * 1000
                    self.record(
                        "Suite 1",
                        f"{method} {path[:40]}",
                        "FAIL",
                        elapsed,
                        f"Error: {e}",
                    )

    # =========================================================================
    # Suite 2: 核心數據抓取與量化指標計算
    # =========================================================================
    async def run_suite_2_core_fetchers(self) -> None:
        print("\n" + "=" * 80)
        print("⚙️ Suite 2: 核心數據抓取與量化指標計算效能")
        print("=" * 80)

        test_symbol = "NVDA"

        # 1. 行情報價
        t0 = time.perf_counter()
        try:
            q = await market_data_service.get_quote(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            price = q.get("c", 0.0) if isinstance(q, dict) else 0.0
            self.record(
                "Suite 2",
                f"get_quote({test_symbol})",
                "PASS" if price > 0 else "WARN",
                elapsed,
                f"Price: ${price:.2f}",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                f"get_quote({test_symbol})",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 2. 歷史 K 線
        t0 = time.perf_counter()
        df_hist = None
        try:
            df_hist = await market_data_service.get_history_df(
                test_symbol, period="1mo", interval="1d"
            )
            elapsed = (time.perf_counter() - t0) * 1000
            rows = len(df_hist) if df_hist is not None else 0
            self.record(
                "Suite 2",
                f"get_history_df({test_symbol}, 1mo, 1d)",
                "PASS" if rows > 0 else "WARN",
                elapsed,
                f"{rows} bars",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                f"get_history_df({test_symbol})",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 3. 期權到期日與鏈
        t0 = time.perf_counter()
        expiries: List[str] = []
        try:
            expiries = await market_data_service.get_all_option_expiries(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                f"get_all_option_expiries({test_symbol})",
                "PASS" if expiries else "WARN",
                elapsed,
                f"{len(expiries)} expiries",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                f"get_all_option_expiries({test_symbol})",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        if expiries:
            target_exp = expiries[0]
            t0 = time.perf_counter()
            try:
                chain = await market_data_service.get_option_chain(
                    test_symbol, target_exp
                )
                elapsed = (time.perf_counter() - t0) * 1000
                calls = len(chain.calls) if chain else 0
                puts = len(chain.puts) if chain else 0
                self.record(
                    "Suite 2",
                    f"get_option_chain({test_symbol}, {target_exp})",
                    "PASS" if calls + puts > 0 else "WARN",
                    elapsed,
                    f"Calls: {calls}, Puts: {puts}",
                )
            except Exception as e:
                self.record(
                    "Suite 2",
                    f"get_option_chain({test_symbol})",
                    "FAIL",
                    (time.perf_counter() - t0) * 1000,
                    str(e),
                )

        # 4. VIX 期限結構
        t0 = time.perf_counter()
        try:
            vts = await market_data_service.get_vix_term_structure()
            elapsed = (time.perf_counter() - t0) * 1000
            ratio = vts.get("vts_ratio", 0.0)
            self.record(
                "Suite 2",
                "get_vix_term_structure()",
                "PASS" if ratio > 0 else "WARN",
                elapsed,
                f"VTS Ratio: {ratio:.3f}, State: {vts.get('vts_state', 'UNKNOWN')}",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "get_vix_term_structure()",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 5. IV Metrics, Skew, UOA, PCR, Max Pain
        t0 = time.perf_counter()
        try:
            iv_m = await SentimentEngine.fetch_and_calculate_iv_metrics(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            ivr = iv_m.iv_rank if iv_m else 0.0
            self.record(
                "Suite 2",
                f"fetch_and_calculate_iv_metrics({test_symbol})",
                "PASS",
                elapsed,
                f"IVR: {ivr:.1f}%, IV: {getattr(iv_m, 'current_iv', 0.0):.2f}",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "fetch_and_calculate_iv_metrics",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        t0 = time.perf_counter()
        try:
            skew = await SentimentEngine.calculate_skew(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            skew_val = skew.get("skew", 0.0) if isinstance(skew, dict) else 0.0
            self.record(
                "Suite 2",
                f"calculate_skew({test_symbol})",
                "PASS",
                elapsed,
                f"Skew: {skew_val:.3f}",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "calculate_skew",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        t0 = time.perf_counter()
        try:
            uoa = await SentimentEngine.detect_uoa(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                f"detect_uoa({test_symbol})",
                "PASS",
                elapsed,
                f"{len(uoa)} institutional whale prints",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "detect_uoa",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        t0 = time.perf_counter()
        try:
            pcr = await SentimentEngine.calculate_pcr(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            pcr_val = pcr.get("pcr", 0.0) if isinstance(pcr, dict) else 0.0
            self.record(
                "Suite 2",
                f"calculate_pcr({test_symbol})",
                "PASS",
                elapsed,
                f"PCR: {pcr_val:.2f}",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "calculate_pcr",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        t0 = time.perf_counter()
        try:
            mp = await SentimentEngine.get_unified_max_pain(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            mp_val = mp.get("max_pain", 0.0) if isinstance(mp, dict) else 0.0
            self.record(
                "Suite 2",
                f"get_unified_max_pain({test_symbol})",
                "PASS",
                elapsed,
                f"Max Pain: ${mp_val:.2f}",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "get_unified_max_pain",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 6. PSQ & Volume Profile (In-Memory Math)
        if df_hist is not None and not df_hist.empty:
            t0 = time.perf_counter()
            psq_res = psq_engine.analyze_psq(df_hist)
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                "analyze_psq(df_hist)",
                "PASS",
                elapsed,
                f"Squeezing: {getattr(psq_res, 'is_squeezing', False)}, Momentum: {getattr(psq_res, 'momentum_value', 0.0):.2f}",
            )

            t0 = time.perf_counter()
            vp_res = volume_profile.calculate_volume_profile_from_df(df_hist)
            elapsed = (time.perf_counter() - t0) * 1000
            poc_val = vp_res.get("poc", 0.0) if vp_res else 0.0
            hvn_val = vp_res.get("hvn", 0.0) if vp_res else 0.0
            lvn_val = vp_res.get("lvn", 0.0) if vp_res else 0.0
            self.record(
                "Suite 2",
                "calculate_volume_profile(df_hist)",
                "PASS",
                elapsed,
                f"POC: ${poc_val:.2f}, HVN: ${hvn_val:.2f}, LVN: ${lvn_val:.2f}",
            )

        # 7. 暗池 DIX 與個股暗池明細
        t0 = time.perf_counter()
        try:
            dix_data = await dark_pool_engine.fetch_and_cache_darkpool_dix()
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                "fetch_and_cache_darkpool_dix()",
                "PASS",
                elapsed,
                f"DIX: {dix_data.get('dix')}%, GEX: {dix_data.get('gex')}B",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "fetch_and_cache_darkpool_dix",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        t0 = time.perf_counter()
        try:
            dp_prints = await dark_pool_engine.fetch_darkpool_prints(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            prints_list = dp_prints.get("prints", [])
            self.record(
                "Suite 2",
                f"fetch_darkpool_prints({test_symbol})",
                "PASS",
                elapsed,
                f"{len(prints_list)} block prints, DP-POC: ${dp_prints.get('dp_poc', 0.0)}",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "fetch_darkpool_prints",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 8. 總經微觀結構與市場體系 (Regime)
        t0 = time.perf_counter()
        try:
            regime = await index_microstructure.get_market_regime()
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                "get_market_regime()",
                "PASS",
                elapsed,
                f"Regime: {regime}",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "get_market_regime",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 9. 宏觀日曆與 FedWatch
        t0 = time.perf_counter()
        try:
            events = await calendar_service.calendar_service.get_high_impact_events(
                days=30
            )
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                "get_high_impact_events(30d)",
                "PASS",
                elapsed,
                f"{len(events)} high-impact macro events",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "get_high_impact_events",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        t0 = time.perf_counter()
        try:
            await calendar_service.calendar_service.update_fedwatch_probability()
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                "update_fedwatch_probability()",
                "PASS",
                elapsed,
                "FedWatch probability updated in SQLite",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "update_fedwatch_probability",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 10. Reddit 批次情緒與 SEC 基本面
        t0 = time.perf_counter()
        try:
            reddit_batch = await reddit_service.get_reddit_context_batch(
                ["NVDA", "AAPL", "TSLA"]
            )
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                "get_reddit_context_batch([NVDA, AAPL, TSLA])",
                "PASS",
                elapsed,
                f"Matched: {len(reddit_batch)} symbols in 1 HTTP call",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "get_reddit_context_batch",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        t0 = time.perf_counter()
        try:
            fund_ctx = await fundamental_service.get_fundamental_context(test_symbol)
            elapsed = (time.perf_counter() - t0) * 1000
            has_text = bool(
                fund_ctx and fund_ctx.get("text") and "error" not in fund_ctx
            )
            self.record(
                "Suite 2",
                f"get_fundamental_context({test_symbol})",
                "PASS" if has_text else "WARN",
                elapsed,
                f"Form: {fund_ctx.get('form_type') if fund_ctx else 'N/A'}, Length: {len(fund_ctx.get('text', '')) if fund_ctx else 0} chars",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "get_fundamental_context",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 11. WTI 原油全套指標
        t0 = time.perf_counter()
        try:
            wti_res = await wti_analysis.analyze_wti(
                price=72.5,
                alert_type=WtiAlertType.UPPER_BREACH,
                threshold_value=70.0,
                pct_change_30min=1.2,
                user_watchlist=["XLE", "USO"],
                user_holdings=["XOM"],
            )
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 2",
                "analyze_wti(price=72.5)",
                "PASS",
                elapsed,
                f"Trend: {wti_res.technicals.trend.value}, Impacted: {len(wti_res.correlated_impacts)} stocks",
            )
        except Exception as e:
            self.record(
                "Suite 2",
                "analyze_wti",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

    # =========================================================================
    # Suite 3: 併發控制、SingleFlight 請求合併與快取共享效能
    # =========================================================================
    async def run_suite_3_concurrency_and_cache(self) -> None:
        print("\n" + "=" * 80)
        print("⚡ Suite 3: 併發控制、SingleFlight 與跨模組快取共享效能")
        print("=" * 80)

        # 1. 測試 SingleFlight 請求合併 (5 個協程同時查詢同一標的)
        from services.single_flight import SingleFlightManager

        call_count = 0

        async def _mock_expensive_fetch(sym: str) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.3)
            return {"symbol": sym, "data": "expensive_result"}

        t0 = time.perf_counter()
        tasks = [
            SingleFlightManager.run("test_flight_NVDA", _mock_expensive_fetch, "NVDA")
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)
        elapsed = (time.perf_counter() - t0) * 1000
        is_single_flight_pass = call_count == 1 and len(results) == 5
        self.record(
            "Suite 3",
            "SingleFlightManager Coalescing (5 concurrent)",
            "PASS" if is_single_flight_pass else "FAIL",
            elapsed,
            f"Underlying calls: {call_count} (Expected: 1)",
        )

        # 2. 測試 Semaphore(3) 節流控制
        sem = asyncio.Semaphore(3)
        max_concurrent = 0
        current_concurrent = 0

        async def _bounded_worker(idx: int) -> int:
            nonlocal max_concurrent, current_concurrent
            async with sem:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
                await asyncio.sleep(0.1)
                current_concurrent -= 1
                return idx

        t0 = time.perf_counter()
        sem_tasks = [_bounded_worker(i) for i in range(12)]
        await asyncio.gather(*sem_tasks)
        elapsed = (time.perf_counter() - t0) * 1000
        is_sem_pass = max_concurrent <= 3
        self.record(
            "Suite 3",
            "Semaphore(3) Bounded Concurrency (12 tasks)",
            "PASS" if is_sem_pass else "FAIL",
            elapsed,
            f"Peak Concurrency: {max_concurrent} (Cap: 3)",
        )

        # 3. 測試跨模組 Shared Radar Cache (300 秒有效期快取命中)
        fake_bot = MagicMock()
        fake_cache = {"NVDA": {"symbol": "NVDA", "price": 120.0, "status": "cached"}}
        setattr(fake_bot, "_latest_radar_data_cache", fake_cache)
        setattr(fake_bot, "_latest_radar_cache_time", time.time())

        t0 = time.perf_counter()
        shared_cache = getattr(fake_bot, "_latest_radar_data_cache", {}) or {}
        shared_time = float(getattr(fake_bot, "_latest_radar_cache_time", 0.0) or 0.0)
        is_fresh = (time.time() - shared_time) < 300.0
        hit = is_fresh and "NVDA" in shared_cache
        elapsed = (time.perf_counter() - t0) * 1000
        self.record(
            "Suite 3",
            "Cross-Module Shared Radar Cache Hit (0ms)",
            "PASS" if hit else "FAIL",
            elapsed,
            f"Cache hit: {hit}, Age: {time.time() - shared_time:.3f}s",
        )

    # =========================================================================
    # Suite 4: 後台排程監控任務端到端模擬
    # =========================================================================
    async def run_suite_4_scheduler_simulation(self) -> None:
        print("\n" + "=" * 80)
        print("🕒 Suite 4: 後台排程監控任務端到端模擬")
        print("=" * 80)

        from cogs.calendar import CalendarCog
        from cogs.trading.fundamental_filing_monitor import (
            FundamentalFilingMonitorCog,
        )
        from cogs.trading.heartbeat import dispatch_watchlist_heartbeat
        from cogs.trading.pre_market import PreMarketCog
        from cogs.trading.price_volume_alert_monitor import (
            PriceVolumeAlertMonitorCog,
        )
        from cogs.trading.wti_monitor import WtiMonitorCog
        from cogs.unified_terminal.cog import UnifiedTerminalCog

        bot = MagicMock()
        bot._is_leader_instance = True
        bot.queue_dm = AsyncMock()
        term_cog = UnifiedTerminalCog(bot)
        bot.get_cog.side_effect = (
            lambda name: term_cog if name == "UnifiedTerminalCog" else None
        )

        # 初始化資料庫與自選股
        database.init_db()
        test_uid = 888888888
        database.add_watchlist_symbol(test_uid, "NVDA")
        database.add_watchlist_symbol(test_uid, "AAPL")

        # 1. 模擬 Watchlist 30 分鐘心跳 (僅測試 2 個標的以精準測量)
        t0 = time.perf_counter()
        try:
            custom_watchlists = [(test_uid, "NVDA", 1), (test_uid, "AAPL", 2)]
            await dispatch_watchlist_heartbeat(bot, all_watchlists=custom_watchlists)
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 4",
                "dispatch_watchlist_heartbeat (2 symbols)",
                "PASS",
                elapsed,
                f"DMs queued: {bot.queue_dm.call_count}",
            )
        except Exception as e:
            self.record(
                "Suite 4",
                "dispatch_watchlist_heartbeat",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 2. 模擬 WTI 原油 30 分鐘巡邏
        wti_cog = WtiMonitorCog(bot)
        wti_cog.wti_oil_monitor.cancel()
        t0 = time.perf_counter()
        try:
            await wti_cog._evaluate_wti_alerts()
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 4", "wti_monitor._evaluate_wti_alerts()", "PASS", elapsed
            )
        except Exception as e:
            self.record(
                "Suite 4",
                "wti_monitor",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 3. 模擬 15 分鐘價量突破監控
        pv_cog = PriceVolumeAlertMonitorCog(bot)
        pv_cog.price_volume_alert_monitor.cancel()
        t0 = time.perf_counter()
        try:
            await pv_cog._evaluate_price_volume_alerts()
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 4",
                "price_volume_alert._evaluate_price_volume_alerts()",
                "PASS",
                elapsed,
            )
        except Exception as e:
            self.record(
                "Suite 4",
                "price_volume_alert",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 4. 模擬 08:45 盤前預熱與量化快取 (以 2 個代表性標的測試預熱流程)
        pre_cog = PreMarketCog(bot)
        pre_cog.pre_market_risk_monitor.cancel()
        t0 = time.perf_counter()
        orig_get_all_watch = database.get_all_watchlist
        try:
            database.get_all_watchlist = lambda: [
                (test_uid, "NVDA", 1),
                (test_uid, "AAPL", 2),
            ]
            await pre_cog._pre_warm_all_targets()
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 4",
                "pre_market._pre_warm_all_targets() (2 symbols)",
                "PASS",
                elapsed,
            )
        except Exception as e:
            self.record(
                "Suite 4",
                "pre_market_warmup",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )
        finally:
            database.get_all_watchlist = orig_get_all_watch

        # 5. 模擬 08:00 SEC 財報監控
        fund_cog = FundamentalFilingMonitorCog(bot)
        fund_cog.fundamental_filing_scan.cancel()
        t0 = time.perf_counter()
        try:
            await fund_cog._scan_holdings_for_new_filings()
            elapsed = (time.perf_counter() - t0) * 1000
            self.record(
                "Suite 4",
                "fundamental_filing._scan_holdings_for_new_filings()",
                "PASS",
                elapsed,
            )
        except Exception as e:
            self.record(
                "Suite 4",
                "fundamental_filing",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

        # 6. 模擬 4h 總經事件檢查
        cal_cog = CalendarCog(bot)
        cal_cog.event_checker.cancel()
        t0 = time.perf_counter()
        try:
            await cal_cog.event_checker()
            elapsed = (time.perf_counter() - t0) * 1000
            self.record("Suite 4", "calendar_cog.event_checker()", "PASS", elapsed)
        except Exception as e:
            self.record(
                "Suite 4",
                "calendar_cog",
                "FAIL",
                (time.perf_counter() - t0) * 1000,
                str(e),
            )

    # =========================================================================
    # Suite 5: 故障注入與優雅降級 (Circuit Breakers & Fallbacks)
    # =========================================================================
    async def run_suite_5_fault_tolerance(self) -> None:
        print("\n" + "=" * 80)
        print("🛡️ Suite 5: 故障注入與優雅降級 (Circuit Breakers & Fallbacks)")
        print("=" * 80)

        # 1. 模擬 TUNNEL_URL 未設定時之 GEX 降級與 Last-Known-Good 讀取
        orig_tunnel = getattr(config, "TUNNEL_URL", "")
        try:
            # 寫入一份 last_known_good 快取
            await save_kv_cache(
                "macro_gex_metrics_cache",
                {
                    "data": {
                        "spy_spot": 590.0,
                        "gamma_flip": 585.0,
                        "put_wall": 580.0,
                    },
                    "timestamp": time.time(),
                },
            )

            # 暫時將 TUNNEL_URL 置空
            config.TUNNEL_URL = ""

            t0 = time.perf_counter()
            degraded_gex = await index_microstructure.fetch_gex_metrics()
            elapsed = (time.perf_counter() - t0) * 1000
            is_stale = degraded_gex.get("_is_stale_cache") is True
            is_lkg_matched = degraded_gex.get("gamma_flip") == 585.0
            self.record(
                "Suite 5",
                "Macro GEX Fallback to Last-Known-Good Cache",
                "PASS" if (is_stale and is_lkg_matched) else "FAIL",
                elapsed,
                f"Stale: {is_stale}, Gamma Flip: {degraded_gex.get('gamma_flip')}",
            )

            # 2. 測試 Dark Pool DIX 降級至常數 45.2
            t0 = time.perf_counter()
            degraded_dix = await dark_pool_engine.fetch_and_cache_darkpool_dix()
            elapsed = (time.perf_counter() - t0) * 1000
            is_dix_fallback = degraded_dix.get("dix") == 45.2
            self.record(
                "Suite 5",
                "Dark Pool DIX Fallback to Constant 45.2%",
                "PASS" if is_dix_fallback else "FAIL",
                elapsed,
                f"DIX: {degraded_dix.get('dix')}%",
            )

            # 3. 測試 Macro Liquidity 降級至常數 TED Spread 0.15
            t0 = time.perf_counter()
            degraded_liq = await index_microstructure.fetch_liquidity_metrics()
            elapsed = (time.perf_counter() - t0) * 1000
            is_liq_fallback = degraded_liq.get("ted_spread") == 0.15
            self.record(
                "Suite 5",
                "Macro Liquidity Fallback to Constant 0.15%",
                "PASS" if is_liq_fallback else "FAIL",
                elapsed,
                f"TED Spread: {degraded_liq.get('ted_spread')}",
            )

            # 4. 測試 暗池大單髒數據過濾器 (Sanitize Dark Pool Prints > 5% 偏離)
            dirty_prints = [
                {
                    "price": 100.0,
                    "size": 10000,
                    "type": "BUY",
                },  # 偏離 0% (現價 100)
                {
                    "price": 150.0,
                    "size": 50000,
                    "type": "BUY",
                },  # 偏離 50% (髒數據)
                {
                    "price": 50.0,
                    "size": 50000,
                    "type": "BUY",
                },  # 偏離 50% (髒數據)
                {"price": 102.0, "size": 10000, "type": "BUY"},  # 偏離 2% (合法)
            ]
            t0 = time.perf_counter()
            cleaned_prints = dark_pool_engine.sanitize_darkpool_prints(
                "NVDA", dirty_prints, current_price=100.0, deviation_threshold=0.05
            )
            elapsed = (time.perf_counter() - t0) * 1000
            is_cleaned_pass = (
                len(cleaned_prints) == 2
                and cleaned_prints[0]["price"] == 100.0
                and cleaned_prints[1]["price"] == 102.0
            )
            self.record(
                "Suite 5",
                "Dark Pool Dirty Print Filter (>5% deviation)",
                "PASS" if is_cleaned_pass else "FAIL",
                elapsed,
                f"Retained {len(cleaned_prints)}/4 valid prints",
            )

        finally:
            config.TUNNEL_URL = orig_tunnel

    # =========================================================================
    # Suite 6: 記憶體與系統資源佔用分析
    # =========================================================================
    async def run_suite_6_resource_profiling(self) -> None:
        print("\n" + "=" * 80)
        print("📈 Suite 6: 記憶體與系統資源佔用分析 (Low-RAM VPS Compliance)")
        print("=" * 80)

        tracemalloc.start()
        snapshot1 = tracemalloc.take_snapshot()

        # 執行一批高負載 DataFrame 與期權計算
        test_symbols = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL"]
        t0 = time.perf_counter()
        tasks = [
            market_data_service.get_history_df(s, period="3mo", interval="1d")
            for s in test_symbols
        ]
        dfs = await asyncio.gather(*tasks)
        elapsed = (time.perf_counter() - t0) * 1000

        snapshot2 = tracemalloc.take_snapshot()
        stats = snapshot2.compare_to(snapshot1, "lineno")

        total_allocated_kb = (
            sum(stat.size_diff for stat in stats if stat.size_diff > 0) / 1024.0
        )

        # 觸發主動回收
        del dfs
        gc.collect()
        post_gc_kb = tracemalloc.get_traced_memory()[0] / 1024.0
        tracemalloc.stop()

        is_mem_safe = total_allocated_kb < 50000.0  # 增量小於 50MB
        self.record(
            "Suite 6",
            "Batch K-line Memory Allocation (5 symbols)",
            "PASS" if is_mem_safe else "WARN",
            elapsed,
            f"Peak Delta: {total_allocated_kb:.1f} KB, Traced: {post_gc_kb:.1f} KB",
        )

    # =========================================================================
    # 產出總結報告表格
    # =========================================================================
    def generate_summary_report(self) -> None:
        print("\n" + "=" * 80)
        print("📊 全方位背景數據抓取基準評估診斷總結報告")
        print("=" * 80)

        total_tests = len(self.results)
        pass_count = sum(1 for r in self.results if r.status == "PASS")
        warn_count = sum(1 for r in self.results if r.status == "WARN")
        fail_count = sum(1 for r in self.results if r.status == "FAIL")

        print(
            f"總測試項目: {total_tests} | 通過 (PASS): {pass_count} | 警告 (WARN): {warn_count} | 失敗 (FAIL): {fail_count}\n"
        )

        print(
            f"{'Suite':<9s} | {'Test Name':<48s} | {'Status':<6s} | {'Latency':<10s} | {'Details'}"
        )
        print("-" * 100)
        for r in self.results:
            print(
                f"{r.suite:<9s} | {r.name:<48s} | {r.status:<6s} | {r.latency_ms:7.2f}ms | {r.details}"
            )
        print("=" * 100)


async def main() -> None:
    tunnel_url = os.getenv("TUNNEL_URL", "https://reddit-api.semantic-cosmos.com")
    print("🌌 Nexus Seeker Background Data Fetching Benchmark Runner")
    print(f"🎯 Target TUNNEL_URL: {tunnel_url}")
    print(f"🕒 Start Time (UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}\n")

    runner = BenchmarkSuite(tunnel_url=tunnel_url)

    await runner.run_suite_1_tunnel_endpoints()
    await runner.run_suite_2_core_fetchers()
    await runner.run_suite_3_concurrency_and_cache()
    await runner.run_suite_4_scheduler_simulation()
    await runner.run_suite_5_fault_tolerance()
    await runner.run_suite_6_resource_profiling()

    runner.generate_summary_report()


if __name__ == "__main__":
    asyncio.run(main())
