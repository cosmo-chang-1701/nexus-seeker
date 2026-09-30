"""macro_refresh_service.py — 手動強制刷新大盤總經數據的共用流程。

供 `/force_macro_update`（`cogs/trading/admin_commands.py`）與 CLI
`admin force-macro-update`（`cli.py`）共用；兩邊只負責呈現 `MacroRefreshResult`。

刷新順序：
1. 大盤 GEX 與流動性指標（並行）。大盤端點完全無資料時改以 SPY 個股期權鏈
   即時估算 Gamma Flip，並以單一交易寫回大盤 GEX 快取。
2. （選用）VIX 期限結構與核心總經指標，僅 CLI 使用。
3. 總經日曆強制重抓（當月 + 下月）。
4. FedWatch 利率定價。日曆重寫已於資料層保留 `fedwatch_probability`
   （見 `database/calendar_cache.py::replace_macro_month_events`），順序不再影響
   正確性；仍維持日曆在前，讓 FedWatch 能寫入最新一批事件列。
5. CPI YoY 偏差值（讀取剛刷新的日曆快取，須在日曆之後）。

各步驟失敗不中斷後續步驟，結果以 `RefreshStep` 逐項記錄。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 步驟顯示名稱（使用者可見，須為繁中）
STEP_GEX = "GEX"
STEP_LIQUIDITY = "流動性指標"
STEP_VTS = "VIX 期限結構"
STEP_CORE = "核心總經指標"
STEP_CALENDAR = "總經日曆"
STEP_FEDWATCH = "FedWatch"
STEP_CPI = "CPI 偏差值"


@dataclass(frozen=True)
class RefreshStep:
    """單一刷新步驟的結果。`message` 為使用者可見的繁中摘要或失敗原因。"""

    name: str
    ok: bool
    message: str


@dataclass(frozen=True)
class GexSnapshot:
    spy_spot: float
    gamma_flip: float
    # True：大盤端點回傳帶 `_is_stale_cache` 的 last-known-good 快取
    is_stale_cache: bool
    # "macro"：大盤 GEX 端點；"spy_live"：SPY 個股期權鏈即時估算
    source: str


@dataclass
class MacroRefreshResult:
    steps: list[RefreshStep] = field(default_factory=list)
    gex: GexSnapshot | None = None
    ted_spread: float | None = None
    vts_ratio: float | None = None
    core_metrics: dict[str, Any] | None = None

    @property
    def all_ok(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def succeeded(self) -> list[RefreshStep]:
        return [step for step in self.steps if step.ok]

    @property
    def failed(self) -> list[RefreshStep]:
        return [step for step in self.steps if not step.ok]

    def step(self, name: str) -> RefreshStep | None:
        for item in self.steps:
            if item.name == name:
                return item
        return None


def _positive_float(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return num if num > 0 else 0.0


async def _fetch_spy_live_gex_fallback() -> dict[str, float] | None:
    """大盤 GEX 端點完全無資料（連 last-known-good 快取都沒有）時，改以 SPY
    個股期權鏈即時估算 Gamma Flip，並以單一交易寫回大盤 GEX 快取。

    回傳寫入成功的 GEX 數據；任何一步失敗回傳 None（不寫入任何鍵）。
    """
    from database.cache import save_kv_cache_many
    from market_analysis.index_microstructure import (
        estimate_symbol_gamma_flip,
        fetch_symbol_gex_metrics,
    )

    try:
        spy_gex = await fetch_symbol_gex_metrics("SPY", force_live=True)
    except Exception as e:
        logger.warning(f"SPY 個股端點即時計算 GEX 失敗: {e}")
        return None

    # force_live 即時抓取失敗時，fetch_symbol_gex_metrics 會優雅降級回傳帶
    # `_is_stale_cache` 標記的過期個股快取。過期資料不可當作即時數據寫回大盤
    # 快取（否則會以新時間戳覆寫 last-known-good，並把 macro_gex_is_fallback
    # 清為 0，讓下游誤判大盤 GEX 為新鮮數據）。
    if not isinstance(spy_gex, dict) or spy_gex.get("_is_stale_cache"):
        logger.warning("SPY 即時 GEX 抓取失敗（僅取得過期快取），不寫回大盤 GEX 快取")
        return None

    spot = _positive_float(spy_gex.get("spot"))
    if spot <= 0:
        return None
    gex_profile = spy_gex.get("gex_profile") or {}
    flip = estimate_symbol_gamma_flip(gex_profile, spot)
    if flip <= 0:
        return None

    gex_data = {
        "spy_spot": round(spot, 2),
        "gamma_flip": round(flip, 2),
        "put_wall": round(_positive_float(spy_gex.get("put_wall")), 2),
    }
    saved = await save_kv_cache_many(
        {
            "macro_spy_spot": gex_data["spy_spot"],
            "macro_spy_gamma_flip": gex_data["gamma_flip"],
            "macro_gamma_flip_line": gex_data["gamma_flip"] * 10.0,
            "macro_gex_is_fallback": 0,
            "macro_gex_metrics_cache": {"data": gex_data, "timestamp": time.time()},
        }
    )
    if not saved:
        logger.warning("SPY 即時估算 GEX 寫入大盤 GEX 快取失敗")
        return None
    return gex_data


async def _refresh_gex_and_liquidity(result: MacroRefreshResult) -> None:
    """強制刷新大盤 GEX 與流動性指標，結果寫入 result。"""
    try:
        from market_analysis.index_microstructure import (
            fetch_gex_metrics,
            fetch_liquidity_metrics,
            invalidate_market_regime_cache,
        )

        results: list[Any] = list(
            await asyncio.gather(
                fetch_gex_metrics(allow_empty=True),
                fetch_liquidity_metrics(),
                return_exceptions=True,
            )
        )
        gex_res, liq_res = results[0], results[1]

        # fetch_gex_metrics(allow_empty=True) 只會回傳三種形態：即時數據、
        # 帶 `_is_stale_cache` 標記的 last-known-good 快取，或無任何可用數據時
        # 的空 dict（不會回傳 510/515 靜態常數，該常數僅在 allow_empty=False
        # 時出現）。因此以「是否有有效 spy_spot」判斷是否需要 SPY 即時備援。
        gex_data: dict[str, Any] = {}
        source = "macro"
        if isinstance(gex_res, dict):
            gex_data = gex_res
        elif isinstance(gex_res, BaseException):
            logger.warning(f"強制刷新大盤 GEX 抓取失敗: {gex_res}")

        if _positive_float(gex_data.get("spy_spot")) <= 0:
            gex_data = await _fetch_spy_live_gex_fallback() or {}
            source = "spy_live"

        # 流動性：備援常數（`_is_fallback`）不可當作即時值呈現。
        ted_spread: float | None = None
        if isinstance(liq_res, dict) and not liq_res.get("_is_fallback"):
            ted_raw = liq_res.get("ted_spread")
            if ted_raw is not None:
                try:
                    ted_spread = float(ted_raw)
                except (TypeError, ValueError):
                    ted_spread = None

        # get_market_regime() 快取的組成輸入 (GEX/流動性) 剛被強制刷新，
        # 需一併清除其記憶體快取，避免手動刷新後市況判讀仍停留在舊資料
        # 直到 TTL 到期才更新。
        invalidate_market_regime_cache()

        spy_spot = _positive_float(gex_data.get("spy_spot"))
        if spy_spot > 0:
            snapshot = GexSnapshot(
                spy_spot=spy_spot,
                gamma_flip=_positive_float(gex_data.get("gamma_flip")),
                is_stale_cache=bool(gex_data.get("_is_stale_cache")),
                source=source,
            )
            result.gex = snapshot
            stale_tag = " ⚠️ [使用快取資料]" if snapshot.is_stale_cache else ""
            source_tag = " (SPY 即時估算)" if source == "spy_live" else ""
            result.steps.append(
                RefreshStep(
                    STEP_GEX,
                    True,
                    f"SPY: ${snapshot.spy_spot:.2f} / Gamma Flip: "
                    f"{snapshot.gamma_flip:.2f}{source_tag}{stale_tag}",
                )
            )
        else:
            result.steps.append(
                RefreshStep(STEP_GEX, False, "大盤端點與 SPY 即時估算皆無有效數據")
            )

        result.ted_spread = ted_spread
        if ted_spread is not None:
            result.steps.append(
                RefreshStep(STEP_LIQUIDITY, True, f"TED Spread: {ted_spread:.2f}")
            )
        else:
            result.steps.append(
                RefreshStep(STEP_LIQUIDITY, False, "無法取得 TED Spread 即時數據")
            )
    except Exception as e:
        logger.warning(f"強制刷新 GEX 與流動性失敗: {e}")
        result.steps.append(
            RefreshStep(STEP_GEX, False, f"GEX 與流動性抓取發生例外：{e}")
        )


async def _refresh_vts_and_core(result: MacroRefreshResult) -> None:
    """刷新 VIX 期限結構與核心總經指標（RRP 等）。判定標準同盤中總經快取排程
    (`cogs/trading/scheduler.py`)：VTS 必須 is_valid、比值落在 [0.5, 3.0] 且狀態
    非 UNKNOWN 才寫入 `macro_vts_ratio`。"""
    from database.cache import save_kv_cache
    from market_analysis.index_microstructure import (
        fetch_core_macro_metrics,
        invalidate_core_macro_metrics_cache,
    )
    from services.market_data_service import get_vix_term_structure

    # fetch_core_macro_metrics() 有記憶體 TTL 快取，強制刷新前先清除。
    invalidate_core_macro_metrics_cache()
    vts_res, core_res = await asyncio.gather(
        get_vix_term_structure(),
        fetch_core_macro_metrics(),
        return_exceptions=True,
    )

    vts_ratio = 0.0
    if isinstance(vts_res, dict):
        vts_ratio = _positive_float(vts_res.get("vts_ratio"))
    is_vts_valid = (
        isinstance(vts_res, dict)
        and bool(vts_res.get("is_valid", False))
        and 0.5 <= vts_ratio <= 3.0
        and vts_res.get("vts_state") != "UNKNOWN"
    )
    if is_vts_valid and await save_kv_cache("macro_vts_ratio", vts_ratio):
        result.vts_ratio = vts_ratio
        result.steps.append(RefreshStep(STEP_VTS, True, f"VTS 比值: {vts_ratio:.2f}"))
    else:
        if isinstance(vts_res, BaseException):
            logger.warning(f"強制刷新 VIX 期限結構失敗: {vts_res}")
        result.steps.append(
            RefreshStep(STEP_VTS, False, "無法取得有效的 VIX 期限結構數據")
        )

    if isinstance(core_res, dict) and not core_res.get("_is_fallback"):
        result.core_metrics = core_res
        result.steps.append(RefreshStep(STEP_CORE, True, f"RRP: {core_res.get('rrp')}"))
    else:
        if isinstance(core_res, BaseException):
            logger.warning(f"強制刷新核心總經指標失敗: {core_res}")
        result.steps.append(
            RefreshStep(STEP_CORE, False, "無法取得即時數據，沿用備援值")
        )


async def refresh_macro_data(
    *, include_vts_and_core: bool = False
) -> MacroRefreshResult:
    """強制刷新大盤總經數據並回傳逐項結果。

    Args:
        include_vts_and_core: 是否一併刷新 VIX 期限結構與核心總經指標
            （CLI 使用；Discord 指令維持 GEX / 日曆 / FedWatch / CPI）。
    """
    from services.calendar_service import calendar_service

    result = MacroRefreshResult()

    # 1. GEX 與流動性
    await _refresh_gex_and_liquidity(result)

    # 2. VTS 與核心總經指標（選用）
    if include_vts_and_core:
        try:
            await _refresh_vts_and_core(result)
        except Exception as e:
            logger.warning(f"強制刷新 VTS 與核心總經指標失敗: {e}")
            result.steps.append(RefreshStep(STEP_VTS, False, f"發生例外：{e}"))

    # 3. 總經日曆（TradingView）
    try:
        calendar_ok = await calendar_service.prefetch_monthly_macro_cache(
            months_ahead=1, force_fetch=True
        )
        result.steps.append(
            RefreshStep(STEP_CALENDAR, True, "已重新抓取並寫入快取")
            if calendar_ok
            else RefreshStep(STEP_CALENDAR, False, "邊緣爬蟲無有效回應，沿用既有快取")
        )
    except Exception as e:
        logger.warning(f"強制刷新總經日曆失敗: {e}")
        result.steps.append(RefreshStep(STEP_CALENDAR, False, f"發生例外：{e}"))

    # 4. FedWatch（失敗時不拋例外，以回傳值表示是否成功寫入）
    try:
        fedwatch_ok = await calendar_service.update_fedwatch_probability()
        result.steps.append(
            RefreshStep(STEP_FEDWATCH, True, "最新利率定價已寫入資料庫")
            if fedwatch_ok
            else RefreshStep(
                STEP_FEDWATCH,
                False,
                "無法取得有效利率定價（爬取失敗或數據未通過合理性檢查），沿用備援快取",
            )
        )
    except Exception as e:
        logger.warning(f"強制刷新 FedWatch 失敗: {e}")
        result.steps.append(RefreshStep(STEP_FEDWATCH, False, f"發生例外：{e}"))

    # 5. CPI 偏差值（讀取剛刷新的日曆快取）
    try:
        cpi_ok = await calendar_service.update_cpi_deviation()
        result.steps.append(
            RefreshStep(STEP_CPI, True, "最新 CPI YoY 實際值與預測值已寫入資料庫")
            if cpi_ok
            else RefreshStep(
                STEP_CPI,
                False,
                "日曆快取中無可用的已公布 CPI YoY 數據（或數據未通過合理性檢查），沿用備援值",
            )
        )
    except Exception as e:
        logger.warning(f"強制刷新 CPI 偏差值失敗: {e}")
        result.steps.append(RefreshStep(STEP_CPI, False, f"發生例外：{e}"))

    return result
