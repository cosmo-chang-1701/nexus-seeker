"""投組下行風險監控的 I/O 層：持倉曝險、模擬報酬序列、NAV 快照與推播。

判定邏輯與閾值在 `market_analysis/downside_monitor.py`；指標定義在
`market_analysis/downside_risk.py`（單一權威）。本模組只負責讀資料、快取、寫快照、推播。

報酬序列的建構（「現權重 × 1 年歷史報酬」模擬）：
- 系統沒有淨值歷史，因此以**目前的持倉**回推過去一年：若一直持有這組部位，每天的
  報酬會是多少。它回答的是「這組部位的下行風險多大」，而不是使用者實際經歷的淨值；
  後者由 16:15 ET 寫入的 `portfolio_nav_daily` 逐日累積（≥ 60 個交易日後並列顯示）。
- 曝險（帶號）：現貨 = 股數 × 價格；期權 = 原始 Delta × 口數 × 100 × 標的價格
  （Delta 等值曝險，忽略 Gamma / Vega——對深價外或臨到期合約會低估尾部）。
  `TradeMetadata.delta` 為舊資料的 None 時，以 ±0.5（Call 正、Put 負）近似。
- 權重分母為帳戶規模（比照 `calculate_auto_capital` 取量值）：Σ|現貨市值| +
  Σ|期權口數| × 100 × 進場權利金 + 現金儲備；現金報酬視為 0。
- 權重固定（每日再平衡），日報酬 = Σ 權重 × 標的日報酬，只取所有標的共同交易日。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd

import config
import database
import market_time
from database.connection import execute_write_many_async, get_read_connection
from database.user_settings import get_user_risk_limit
from market_analysis.downside_monitor import (
    LOOKBACK_DAYS,
    DownsideSnapshot,
    NavSnapshot,
    compute_snapshot,
    cvar_budget,
    evaluate_cvar_breaches,
    evaluate_drawdown_tier,
    realized_returns_from_snapshots,
)
from services.notification_dispatcher import notify

logger = logging.getLogger(__name__)

# 未 refresh 過 Greeks 的舊期權資料，以價平 Delta 近似
_FALLBACK_OPTION_DELTA = 0.5

# 盤中檢查所需的最少即時報價覆蓋率（以曝險權重計）；低於此值不計算盤中報酬，
# 避免少數標的報價缺失讓當下回撤被系統性低估
_MIN_LIVE_PRICE_COVERAGE = 0.8

# 與 is_memory_safe 一致：1GB VPS 上歷史 K 線並行抓取上限
_HISTORY_CONCURRENCY = 3

_MAX_CACHED_USERS = 500


@dataclass(frozen=True)
class PortfolioExposure:
    """單一使用者的持倉曝險（股數皆帶號；期權為 Delta 等值股數）。"""

    stock_shares: Mapping[str, float]
    option_delta_shares: Mapping[str, float]
    option_premium: float
    cash: float

    @property
    def symbols(self) -> list[str]:
        return sorted(
            s
            for s in set(self.stock_shares) | set(self.option_delta_shares)
            if self.total_shares(s) != 0.0
        )

    def total_shares(self, symbol: str) -> float:
        return float(self.stock_shares.get(symbol, 0.0)) + float(
            self.option_delta_shares.get(symbol, 0.0)
        )

    def gross_nav(self, prices: Mapping[str, float]) -> float:
        stock_value = sum(
            abs(q) * float(prices.get(s, 0.0)) for s, q in self.stock_shares.items()
        )
        return stock_value + self.option_premium + self.cash

    def signature(self) -> str:
        payload = json.dumps(
            {
                "s": {k: round(v, 4) for k, v in sorted(self.stock_shares.items())},
                "o": {
                    k: round(v, 4) for k, v in sorted(self.option_delta_shares.items())
                },
                "p": round(self.option_premium, 2),
                "c": round(self.cash, 2),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class PortfolioReturnSeries:
    """模擬日報酬序列與建構時的權重、最後收盤價。"""

    user_id: int
    dates: list[str]
    returns: np.ndarray
    weights: dict[str, float]
    last_closes: dict[str, float]
    nav: float
    exposure: PortfolioExposure
    built_on: str
    signature: str = field(default="")


_series_cache: dict[int, PortfolioReturnSeries] = {}


def clear_series_cache() -> None:
    _series_cache.clear()


def get_cached_series(user_id: int) -> Optional[PortfolioReturnSeries]:
    return _series_cache.get(user_id)


# ---------------------------------------------------------------------------
# 持倉讀取
# ---------------------------------------------------------------------------


def load_portfolio_exposure(user_id: int) -> PortfolioExposure:
    """讀取使用者現貨、期權與現金儲備（同步；呼叫端以 asyncio.to_thread 執行）。"""
    stock: dict[str, float] = {}
    options: dict[str, float] = {}
    premium = 0.0
    cash = 0.0
    conn = get_read_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT cash_reserve FROM user_settings WHERE user_id = ?", (user_id,)
        )
        row = cur.fetchone()
        cash = float(row[0]) if row and row[0] is not None else 0.0
        cur.execute(
            "SELECT context_type, symbol, metadata FROM assets "
            "WHERE user_id = ? AND context_type IN ('HOLDING', 'TRADE')",
            (user_id,),
        )
        for ctx, sym, meta_json in cur.fetchall():
            meta = json.loads(meta_json) if meta_json else {}
            symbol = str(sym).upper()
            qty = float(meta.get("quantity", 0.0) or 0.0)
            if qty == 0.0:
                continue
            if ctx == "HOLDING":
                stock[symbol] = stock.get(symbol, 0.0) + qty
            else:
                raw_delta = meta.get("delta")
                if raw_delta is None:
                    is_put = str(meta.get("opt_type", "call")).lower().startswith("p")
                    raw_delta = (
                        -_FALLBACK_OPTION_DELTA if is_put else _FALLBACK_OPTION_DELTA
                    )
                options[symbol] = (
                    options.get(symbol, 0.0) + float(raw_delta) * qty * 100.0
                )
                premium += abs(qty) * 100.0 * float(meta.get("entry_price", 0.0) or 0.0)
    finally:
        conn.close()
    return PortfolioExposure(stock, options, premium, cash)


def get_users_with_positions() -> list[int]:
    conn = get_read_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT user_id FROM assets "
            "WHERE context_type IN ('HOLDING', 'TRADE')"
        )
        return sorted(int(r[0]) for r in cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 報酬序列
# ---------------------------------------------------------------------------


def compute_return_series(
    user_id: int,
    exposure: PortfolioExposure,
    closes: Mapping[str, pd.Series],
    *,
    drop_on_or_after: Optional[date] = None,
    built_on: str = "",
) -> Optional[PortfolioReturnSeries]:
    """由各標的收盤價序列計算固定權重的模擬日報酬（純函式，無 I/O）。"""
    symbols = [s for s in exposure.symbols if s in closes and not closes[s].empty]
    if not symbols:
        return None
    frame = pd.concat({s: closes[s] for s in symbols}, axis=1).dropna()
    if drop_on_or_after is not None:
        idx_dates = pd.Index([pd.Timestamp(i).date() for i in frame.index])
        frame = frame[idx_dates < drop_on_or_after]
    frame = frame.tail(LOOKBACK_DAYS + 1)
    if len(frame) < 2:
        return None
    last = {s: float(frame[s].iloc[-1]) for s in symbols}
    nav = exposure.gross_nav(last)
    if nav <= 0.0:
        return None
    weights = {s: exposure.total_shares(s) * last[s] / nav for s in symbols}
    pct = frame.pct_change().dropna()
    w_vec = np.array([weights[s] for s in symbols])
    returns = pct[symbols].to_numpy() @ w_vec
    return PortfolioReturnSeries(
        user_id=user_id,
        dates=[pd.Timestamp(i).strftime("%Y-%m-%d") for i in pct.index],
        returns=np.asarray(returns, dtype=float),
        weights=weights,
        last_closes=last,
        nav=nav,
        exposure=exposure,
        built_on=built_on,
        signature=exposure.signature(),
    )


async def _fetch_closes(symbols: list[str]) -> dict[str, pd.Series]:
    from services import market_data_service

    sem = asyncio.Semaphore(_HISTORY_CONCURRENCY)

    async def _one(sym: str) -> tuple[str, Optional[pd.Series]]:
        async with sem:
            try:
                df = await market_data_service.get_history_df(sym, "1y")
            except Exception as e:
                logger.warning(f"[DownsideRisk] {sym} 歷史 K 線抓取失敗: {e}")
                return sym, None
        if df is None or df.empty or "Close" not in df:
            return sym, None
        return sym, df["Close"].astype(float)

    results = await asyncio.gather(*[_one(s) for s in symbols])
    return {s: c for s, c in results if c is not None}


async def build_portfolio_return_series(
    user_id: int, *, force: bool = False
) -> Optional[PortfolioReturnSeries]:
    """建構（或取用快取的）模擬報酬序列。持倉改變或跨日時自動重建。"""
    exposure = await asyncio.to_thread(load_portfolio_exposure, user_id)
    if not exposure.symbols:
        _series_cache.pop(user_id, None)
        return None
    today = datetime.now(market_time.ny_tz).date()
    cached = _series_cache.get(user_id)
    if (
        not force
        and cached is not None
        and cached.signature == exposure.signature()
        and cached.built_on == today.isoformat()
    ):
        return cached
    closes = await _fetch_closes(exposure.symbols)
    # 盤中建構時，yfinance 日線會包含今天未完成的 K 棒；丟掉它，讓「最後收盤價」
    # 恆為前一交易日收盤，盤中報酬才能以即時價 / 昨收計算。
    drop_today = today if market_time.is_market_open() else None
    series = compute_return_series(
        user_id,
        exposure,
        closes,
        drop_on_or_after=drop_today,
        built_on=today.isoformat(),
    )
    if series is None:
        return None
    if len(_series_cache) >= _MAX_CACHED_USERS and user_id not in _series_cache:
        _series_cache.pop(next(iter(_series_cache)))
    _series_cache[user_id] = series
    return series


def intraday_return(
    series: PortfolioReturnSeries, live_prices: Mapping[str, float]
) -> Optional[float]:
    """以即時價 / 最後收盤價計算今日盤中投組報酬；報價覆蓋率不足時回 None。"""
    total = sum(abs(w) for w in series.weights.values())
    if total <= 0.0:
        return None
    covered = 0.0
    r = 0.0
    for sym, w in series.weights.items():
        live = live_prices.get(sym)
        base = series.last_closes.get(sym)
        if live is None or base is None or live <= 0.0 or base <= 0.0:
            continue
        covered += abs(w)
        r += w * (live / base - 1.0)
    if covered / total < _MIN_LIVE_PRICE_COVERAGE:
        return None
    return r


# ---------------------------------------------------------------------------
# NAV 快照（portfolio_nav_daily）
# ---------------------------------------------------------------------------

_UPSERT_NAV_SQL = """
    INSERT OR REPLACE INTO portfolio_nav_daily (user_id, date, nav, positions_json)
    VALUES (?, ?, ?, ?)
"""


def nav_snapshot_row(
    series: PortfolioReturnSeries, trading_date: str
) -> tuple[int, str, float, str]:
    exposure = series.exposure
    payload = {
        "shares": {s: exposure.total_shares(s) for s in exposure.symbols},
        "closes": dict(series.last_closes),
        "cash": exposure.cash,
        "option_premium": exposure.option_premium,
    }
    return (series.user_id, trading_date, series.nav, json.dumps(payload))


def load_nav_snapshots(
    user_id: int, limit: int = LOOKBACK_DAYS + 1
) -> list[NavSnapshot]:
    conn = get_read_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT date, nav, positions_json FROM portfolio_nav_daily "
            "WHERE user_id = ? ORDER BY date DESC LIMIT ?",
            (user_id, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    out: list[NavSnapshot] = []
    for d, nav, pj in rows:
        payload = json.loads(pj) if pj else {}
        out.append(
            NavSnapshot(
                date=str(d),
                nav=float(nav),
                shares={k: float(v) for k, v in payload.get("shares", {}).items()},
                closes={k: float(v) for k, v in payload.get("closes", {}).items()},
            )
        )
    return sorted(out, key=lambda s: s.date)


# ---------------------------------------------------------------------------
# 判定與推播
# ---------------------------------------------------------------------------


def _dd_state_key(user_id: int) -> str:
    # 刻意不以 downside_dd_ 開頭：該前綴是每日去重旗標、會被 03:00 ET 清理；
    # 武裝狀態必須跨日保留，否則三天後同一階梯會在仍處回撤中時重複推播。
    return f"downside_state_dd_{user_id}"


def _cvar_state_key(user_id: int) -> str:
    return f"downside_state_cvar_{user_id}"


async def _evaluate_and_notify(
    bot: Any,
    user_id: int,
    snapshot: DownsideSnapshot,
    *,
    include_cvar: bool,
    today_str: str,
) -> None:
    from cogs.embed_builders.alert_embeds.downside_alerts import (
        create_portfolio_downside_alert_embed,
    )

    # 1. 回撤階梯（盤中與收盤皆檢查）
    raw_state = await asyncio.to_thread(database.get_kv_cache, _dd_state_key(user_id))
    armed = float(raw_state) if isinstance(raw_state, (int, float)) else 0.0
    tier, new_armed = evaluate_drawdown_tier(snapshot.current_drawdown, armed)
    if tier is not None:
        sent = await notify(
            bot,
            user_id,
            "risk_portfolio_downside",
            embed=create_portfolio_downside_alert_embed(
                "DRAWDOWN", snapshot, tier=tier
            ),
            dedup_key=f"downside_dd_{user_id}_{int(round(tier * 100))}_{today_str}",
        )
        # 只有實際送達才前進武裝狀態：頻道關閉期間不前進，使用者重新開啟後仍會收到
        if sent:
            await database.save_kv_cache(_dd_state_key(user_id), new_armed)
    elif new_armed != armed:
        await database.save_kv_cache(_dd_state_key(user_id), new_armed)

    # 2. CVaR（只在收盤後以完整日線判定；盤中 CVaR 不會變）
    if not include_cvar:
        return
    risk_limit = await asyncio.to_thread(get_user_risk_limit, user_id)
    reasons = evaluate_cvar_breaches(snapshot, risk_limit)
    raw_prev = await asyncio.to_thread(database.get_kv_cache, _cvar_state_key(user_id))
    prev = set(raw_prev) if isinstance(raw_prev, list) else set()
    new_reasons = [r for r in reasons if r not in prev]
    if new_reasons:
        sent = await notify(
            bot,
            user_id,
            "risk_portfolio_downside",
            embed=create_portfolio_downside_alert_embed(
                "CVAR",
                snapshot,
                reasons=tuple(reasons),
                budget=cvar_budget(risk_limit),
            ),
            dedup_key=f"downside_cvar_{user_id}_{'_'.join(sorted(reasons))}_{today_str}",
        )
        if sent:
            await database.save_kv_cache(_cvar_state_key(user_id), sorted(reasons))
    elif set(reasons) != prev:
        # 條件解除（或縮減）時更新狀態，讓下次重新進入時能再推播
        await database.save_kv_cache(_cvar_state_key(user_id), sorted(reasons))


def _job_allowed(bot: Any) -> bool:
    if not getattr(bot, "_is_leader_instance", True):
        return False
    from services.llm_service import is_memory_safe

    if not is_memory_safe():
        logger.warning("[DownsideRisk] 記憶體水位過高，跳過本輪下行風險檢查。")
        return False
    return True


async def warm_downside_series(bot: Any) -> int:
    """盤前預熱：為所有有部位的使用者建構報酬序列，供盤中檢查使用。回傳成功數。"""
    if not _job_allowed(bot):
        return 0
    uids = await asyncio.to_thread(get_users_with_positions)
    built = 0
    for uid in uids:
        try:
            if await build_portfolio_return_series(uid) is not None:
                built += 1
        except Exception as e:
            logger.error(f"[DownsideRisk] 預熱 uid={uid} 失敗: {e}")
    return built


async def run_intraday_downside_checks(
    bot: Any, live_prices: Mapping[str, float]
) -> None:
    """盤中回撤檢查：只使用已快取的報酬序列 + 即時報價，不抓任何歷史資料。"""
    if not _job_allowed(bot):
        return
    today_str = datetime.now(market_time.ny_tz).strftime("%Y%m%d")
    for uid, series in list(_series_cache.items()):
        try:
            r_today = intraday_return(series, live_prices)
            if r_today is None:
                continue
            snapshot = compute_snapshot(
                series.returns, config.RISK_FREE_RATE, intraday_return=r_today
            )
            if snapshot is None:
                continue
            await _evaluate_and_notify(
                bot, uid, snapshot, include_cvar=False, today_str=today_str
            )
        except Exception as e:
            logger.error(f"[DownsideRisk] 盤中檢查 uid={uid} 失敗: {e}")


async def run_daily_downside_job(bot: Any, trading_date: date) -> None:
    """16:15 ET：重建序列、寫入 NAV 快照、判定回撤與 CVaR。"""
    if not _job_allowed(bot):
        return
    uids = await asyncio.to_thread(get_users_with_positions)
    date_str = trading_date.isoformat()
    today_str = trading_date.strftime("%Y%m%d")
    nav_rows: list[tuple[int, str, float, str]] = []
    for uid in uids:
        try:
            series = await build_portfolio_return_series(uid, force=True)
            if series is None:
                continue
            nav_rows.append(nav_snapshot_row(series, date_str))
            snapshot = compute_snapshot(series.returns, config.RISK_FREE_RATE)
            if snapshot is None:
                continue
            await _evaluate_and_notify(
                bot, uid, snapshot, include_cvar=True, today_str=today_str
            )
        except Exception as e:
            logger.error(f"[DownsideRisk] 收盤檢查 uid={uid} 失敗: {e}")
    if nav_rows:
        try:
            await execute_write_many_async([(_UPSERT_NAV_SQL, nav_rows, True)])
        except Exception as e:
            logger.error(f"[DownsideRisk] NAV 快照寫入失敗: {e}")


async def get_downside_snapshots(
    user_id: int,
) -> tuple[Optional[DownsideSnapshot], Optional[DownsideSnapshot]]:
    """供戰報使用：(模擬快照, 已實現快照)。已實現快照在 NAV 歷史不足時為 None。"""
    series = await build_portfolio_return_series(user_id)
    simulated = (
        compute_snapshot(series.returns, config.RISK_FREE_RATE) if series else None
    )
    snaps = await asyncio.to_thread(load_nav_snapshots, user_id)
    realized_returns = realized_returns_from_snapshots(snaps)
    realized = compute_snapshot(realized_returns, config.RISK_FREE_RATE)
    return simulated, realized


def extract_live_prices(radar_cache_map: Mapping[str, Any]) -> dict[str, float]:
    """從盤中雷達快取取出即時價（`quote.c`）。"""
    out: dict[str, float] = {}
    for sym, data in radar_cache_map.items():
        if not isinstance(data, dict):
            continue
        quote = data.get("quote") or {}
        try:
            price = float(quote.get("c") or 0.0)
        except (TypeError, ValueError):
            continue
        if price > 0.0:
            out[str(sym).upper()] = price
    return out
