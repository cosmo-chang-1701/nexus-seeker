"""配置偏離提醒的 I/O 層（docs/strategies/09_static_allocation_rebalance.md §7）。

計算邏輯在 `market_analysis/allocation_drift.py`（單一權威）；本模組只負責讀資料、
組裝輸入、推播並維護狀態。16:15 ET 由 `cogs/trading/after_market.py` 於提領跑道快照
之後呼叫：對 `target_equity_pct ≥ 0` 的使用者比較實際股票占比與目標，偏離 ≥ 5 pp 時經
`risk_allocation_drift` 頻道推播；第一天超標一次、之後仍超標則每個新月份的第一次執行再推。
固定比例、不擇時：總經狀態只顯示於註記。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Optional

import database
from database.macro_signal_log import load_recent_regimes
from database.user_settings import get_full_user_context
from market_analysis.allocation_drift import (
    DriftState,
    compute_breakdown,
    is_outside,
    next_state,
    parse_state,
    reminder_due,
    reserve_amount,
    state_to_json,
)
from services.downside_risk_service import (
    get_users_with_positions,
    load_nav_snapshots,
    load_portfolio_exposure,
)
from services.llm_service import is_memory_safe
from services.notification_dispatcher import notify
from services.withdrawal_runway_service import get_runway_snapshot

__all__ = ["run_allocation_drift_job"]

logger = logging.getLogger(__name__)


def _state_key(user_id: int) -> str:
    # alloc_drift_state_ 前綴刻意不列入每日去重白名單：狀態必須跨日保留
    return f"alloc_drift_state_{user_id}"


def _job_allowed(bot: Any) -> bool:
    if not getattr(bot, "_is_leader_instance", True):
        return False
    if not is_memory_safe():
        logger.warning("[AllocationDrift] 記憶體水位過高，跳過本輪配置偏離檢查。")
        return False
    return True


async def _macro_state(trading_date: date) -> Optional[str]:
    """當日已確認的總經狀態；查無當日資料回傳 None（僅供註記）。"""
    try:
        rows = await asyncio.to_thread(
            load_recent_regimes, trading_date + timedelta(days=1), 1
        )
    except Exception as e:
        logger.warning(f"[AllocationDrift] 讀取總經狀態失敗: {e}")
        return None
    if rows and rows[-1][0] == trading_date.isoformat():
        return rows[-1][2]
    return None


async def _evaluate_user(
    bot: Any, uid: int, trading_date: date, macro: Optional[str]
) -> bool:
    """評估單一使用者；回傳是否完成評估（略過者為 False）。"""
    from cogs.embed_builders.alert_embeds.allocation_alerts import (
        create_allocation_drift_embed,
    )

    ctx = await asyncio.to_thread(get_full_user_context, uid)
    if ctx.load_failed:
        return False
    key = _state_key(uid)
    state = parse_state(await asyncio.to_thread(database.get_kv_cache, key))
    target = ctx.target_equity_pct
    if target < 0:
        # 停用：清掉「仍超標」旗標，日後重新啟用才算新的一次超標
        if state is not None and state.outside:
            await database.save_kv_cache(
                key, state_to_json(DriftState(False, state.last_sent, None))
            )
        return False

    navs = await asyncio.to_thread(load_nav_snapshots, uid, 1)
    nav = navs[-1] if navs else None
    if nav is None or nav.date != trading_date.isoformat():
        logger.info(f"[AllocationDrift] uid={uid} NAV 快照非當日，略過")
        return False
    exposure = await asyncio.to_thread(load_portfolio_exposure, uid)

    reserve = 0.0
    next_date_str: Optional[str] = None
    if ctx.withdrawal_amount > 0:
        snap = await get_runway_snapshot(uid)
        if snap is None or snap.as_of != trading_date.isoformat():
            # fail closed：缺少預留會把即將提領的錢誤算成偏離
            logger.warning(f"[AllocationDrift] uid={uid} 提領跑道快照非當日，略過")
            return False
        if snap.next_date:
            next_date_str = snap.next_date
            reserve = reserve_amount(
                trading_date, date.fromisoformat(snap.next_date), snap.next_withdrawal
            )

    b = compute_breakdown(
        exposure.stock_shares, nav.closes, exposure.cash, target, reserve
    )
    if b is None:
        return False
    if b.missing_prices:
        logger.warning(
            f"[AllocationDrift] uid={uid} 缺收盤價 {list(b.missing_prices)}，略過"
        )
        return False

    outside = is_outside(b)
    reason = reminder_due(trading_date, outside, state, target)
    sent = False
    if reason:
        sent = await notify(
            bot,
            uid,
            "risk_allocation_drift",
            embed=create_allocation_drift_embed(b, reason, macro, next_date_str),
            dedup_key=f"alloc_drift_remind_{uid}_{trading_date:%Y%m%d}",
        )
    new = next_state(trading_date, outside, state, sent, target)
    if new is not None and new != state:
        await database.save_kv_cache(key, state_to_json(new))
    return True


async def run_allocation_drift_job(bot: Any, trading_date: date) -> int:
    """16:15 ET：逐一評估使用者的配置偏離並推播；回傳完成評估的使用者數。"""
    if not _job_allowed(bot):
        return 0
    uids = await asyncio.to_thread(get_users_with_positions)
    macro = await _macro_state(trading_date)
    evaluated = 0
    for uid in uids:
        try:
            if await _evaluate_user(bot, uid, trading_date, macro):
                evaluated += 1
        except Exception as e:
            logger.error(f"[AllocationDrift] uid={uid} 評估失敗: {e}")
    return evaluated
