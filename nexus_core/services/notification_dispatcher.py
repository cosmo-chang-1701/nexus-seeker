"""主動推播的唯一入口：頻道開關 → 去重 → 持久化 DM 佇列 → 寫入去重旗標。

過去約 40 個推播點各自 `if database.is_notification_enabled(...): await bot.queue_dm(...)`，
導致三類缺陷：有推播完全沒查開關（機器人啟停廣播、NRO 期權掃描）、有推播在 LLM 呼叫與
DB 寫入之後才查開關、有推播先寫去重旗標才查開關（靜音期間的事件因此永久遺失）。本模組
把順序固定下來，`tests/unit/test_notification_dispatch_centralization.py` 以 AST 掃描強制
除白名單外不得直接呼叫 `queue_dm`。

順序的理由：
1. **開關優先**：關閉的頻道不做任何 I/O，也**不寫去重旗標**——使用者之後重新開啟時，
   當天尚未送達的事件仍能送出。
2. **去重在入列前**：已送過的事件不重複入列。
3. **旗標在入列後**：入列失敗（例如非 leader 實例）不會燒掉當天的旗標。

開關與去重一律經 `database` 套件屬性（`database.is_notification_enabled`、
`database.get_kv_cache`、`database.save_kv_cache`）呼叫，既有測試對這些名稱的 patch 仍然
有效；開關讀的是每使用者快取，熱路徑不會每次都開 DB 連線。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Optional

import discord

import database
from database.notification_channels import NotificationKey
from services.notification_dispatch_recorder import DispatchRecord, record_dispatch

logger = logging.getLogger(__name__)


async def is_channel_enabled(user_id: int, channel: NotificationKey) -> bool:
    """頻道是否開啟。昂貴工作（LLM 摘要、額外抓取、DB 寫入）之前先以此判斷。"""
    return bool(database.is_notification_enabled(user_id, channel))


async def notify(
    bot: Any,
    user_id: int,
    channel: NotificationKey,
    *,
    embed: Optional[discord.Embed] = None,
    message: Optional[str] = None,
    dedup_key: Optional[str] = None,
    record: Optional[DispatchRecord] = None,
) -> bool:
    """依頻道開關與去重旗標推播一則私訊，回傳是否實際入列。

    `dedup_key` 為每日一次性去重旗標的完整鍵；其前綴必須登記於
    `database/cache.py::_KV_CACHE_DEDUP_KEY_PREFIXES`（由
    `tests/unit/test_kv_cache_dedup_whitelist.py` 強制），否則旗標會永久堆積。

    `record` 為可行動通知的反事實描述；只在實際入列後交給
    `services/notification_dispatch_recorder.record_dispatch()`，供 03:30 ET 標註
    「照做 vs 持有」的 Sortino / MDD / CVaR 差異。
    """
    if not await is_channel_enabled(user_id, channel):
        return False
    if dedup_key is not None:
        already_sent = await asyncio.to_thread(database.get_kv_cache, dedup_key)
        if already_sent:
            return False
    if message is None:
        await bot.queue_dm(user_id, embed=embed)
    else:
        await bot.queue_dm(user_id, message=message, embed=embed)
    if dedup_key is not None:
        await database.save_kv_cache(dedup_key, True)
    if record is not None:
        record_dispatch(bot, user_id, channel, record)
    return True


async def notify_many(
    bot: Any,
    user_id: int,
    channel: NotificationKey,
    embeds: Sequence[discord.Embed],
    *,
    dedup_key: Optional[str] = None,
    record: Optional[DispatchRecord] = None,
) -> bool:
    """同一則通知由多個 embed 組成（例如分頁雷達）時使用：開關與去重只判斷一次。"""
    if not embeds:
        return False
    if not await is_channel_enabled(user_id, channel):
        return False
    if dedup_key is not None:
        already_sent = await asyncio.to_thread(database.get_kv_cache, dedup_key)
        if already_sent:
            return False
    for embed in embeds:
        await bot.queue_dm(user_id, embed=embed)
    if dedup_key is not None:
        await database.save_kv_cache(dedup_key, True)
    if record is not None:
        record_dispatch(bot, user_id, channel, record)
    return True
