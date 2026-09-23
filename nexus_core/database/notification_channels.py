"""通知頻道註冊表：`/notif_settings` 每一個頻道的單一真實來源。

過去頻道定義分散三處：`database/notifications.py`（key 清單、預設值、預設情境）、
`cogs/settings_ui.py::TRADING_MODULES`（標籤與分組）、以及各推播呼叫點的字串字面值。
新增一個頻道要改三個地方，漏改任何一處都只會在執行期靜默失效。現在：

- 頻道的 key / 模組 / 標籤 / 預設值 / 各預設情境的開關狀態只在本檔 `CHANNELS` 定義；
- `ALL_NOTIFICATION_KEYS`、`DEFAULT_NOTIFICATION_SETTINGS`、`PRESET_PROFILES`、
  `TRADING_MODULES` 全部由此衍生（原名稱由 `database/notifications.py` 與
  `cogs/settings_ui.py` 重新匯出，既有呼叫者不需改動）；
- 推播一律經 `services/notification_dispatcher.notify()`，其 `channel` 參數型別為
  `NotificationKey`，mypy 會攔下拼錯的頻道名稱。

本模組刻意只依賴 stdlib（比照 `sentiment/skew_taxonomy.py`），可被 database / cogs /
services 任一層匯入而不產生循環相依。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

NotificationKey = Literal[
    "briefing_pre_market",
    "briefing_post_market",
    "briefing_weekly_vtr",
    "system_lifecycle",
    "heartbeat_watchlist",
    "heartbeat_symbol_deep",
    "telemetry_orders",
    "advisory_entry_signal",
    "defense_portfolio_risk",
    "defense_option_rollover",
    "defense_margin_call",
    "defense_fundamental_thesis",
    "defense_macro_tail_risk",
    "advisory_core_levels",
    "alpha_market_signals",
    "alpha_option_scan",
    "alpha_polymarket",
    "alpha_wti_oil",
    "alpha_price_volume_watch",
    "risk_portfolio_downside",
]

ModuleKey = Literal["briefings", "telemetry", "defense", "alpha"]

PresetName = Literal["all_on", "all_off", "focus", "mute_intraday"]


@dataclass(frozen=True)
class NotificationChannel:
    key: NotificationKey
    module: ModuleKey
    label: str
    # 各手寫預設情境下此頻道的開關（all_on / all_off 由 comprehension 自動涵蓋）
    focus: bool
    mute_intraday: bool
    default: bool = True


@dataclass(frozen=True)
class NotificationModule:
    key: ModuleKey
    title: str
    description: str


MODULES: tuple[NotificationModule, ...] = (
    NotificationModule(
        "briefings",
        "📋 定時戰報與覆盤",
        "每日盤前宏觀自選、盤後 AI 深度覆盤與週五 VTR 績效週報。",
    ),
    NotificationModule(
        "telemetry",
        "📡 盤中自選與掛單遙測",
        "盤中主動推送自選股量化雷達、個股深度心跳與掛單對齊。",
    ),
    NotificationModule(
        "defense",
        "🛡️ 持倉風控與極端防禦",
        "即時監控持倉負 Gamma、DITM 獲利鎖定、動態轉倉與黑天鵝。",
    ),
    NotificationModule(
        "alpha",
        "🎯 Alpha 策略與情報",
        "即時捕捉 Nexus 量化 Alpha 機會、Polymarket 巨鯨與原油異動。",
    ),
)

CHANNELS: tuple[NotificationChannel, ...] = (
    # 1. 📋 定時戰報與覆盤
    NotificationChannel(
        "briefing_pre_market", "briefings", "🌅 盤前綜合戰報 (09:00 ET)", True, True
    ),
    NotificationChannel(
        "briefing_post_market",
        "briefings",
        "📋 盤後 AI 深度覆盤 (16:15 ET)",
        True,
        True,
    ),
    NotificationChannel(
        "briefing_weekly_vtr",
        "briefings",
        "📈 虛擬交易室 (VTR) 績效週報 (週五 17:05 ET)",
        True,
        True,
    ),
    # 機器人啟動 / 關閉廣播：過去完全不受任何開關控制，每次部署都會私訊所有使用者
    NotificationChannel(
        "system_lifecycle",
        "briefings",
        "🤖 機器人啟動 / 關閉通知",
        True,
        True,
    ),
    # 2. 📡 盤中自選與掛單遙測
    # 兩則心跳是獨立的推播路徑、頻率也不同，因此各自一個開關：
    # heartbeat_watchlist 走 cogs/trading/heartbeat.py 的 :00/:15/:30/:45
    # 批次雷達；heartbeat_symbol_deep 走 IntradayScanPipeline 的 30 分鐘
    # 單標的深度快照。過去共用一個 key，標籤還誤寫成 30 分鐘。
    NotificationChannel(
        "heartbeat_watchlist",
        "telemetry",
        "📡 自選股 15 分鐘批次量化雷達 (整批標的掃描總覽)",
        False,
        False,
    ),
    NotificationChannel(
        "heartbeat_symbol_deep",
        "telemetry",
        "🧱 個股 30 分鐘深度戰場心跳 (含微觀結構、Skew 與 UOA 巨鯨)",
        False,
        False,
    ),
    NotificationChannel(
        "telemetry_orders",
        "telemetry",
        "🌌 待成交掛單實時對齊與撤退線",
        True,
        False,
    ),
    # 進場顧問只在六重鐵律通過時才推播，正是「精準交易」要的高信號訊號；
    # 但屬盤中節奏的推播，盤中靜音模式下關閉
    NotificationChannel(
        "advisory_entry_signal",
        "telemetry",
        "🎯 自選標的進場顧問 (六重鐵律通過時推播進場價 / 停損 / 目標)",
        True,
        False,
    ),
    # 3. 🛡️ 持倉風控與極端防禦
    NotificationChannel(
        "defense_portfolio_risk",
        "defense",
        "🆘 持倉負 Gamma 斷層、DITM 獲利鎖定與保證金警戒",
        True,
        True,
    ),
    NotificationChannel(
        "defense_option_rollover",
        "defense",
        "🔄 動態轉倉、套牢股票備兌解套與衛星再平衡",
        True,
        False,
    ),
    # 保證金強制平倉警報屬帳戶生存等級警訊，與例行轉倉建議獨立分流，
    # 任何預設情境下皆不可靜音
    NotificationChannel(
        "defense_margin_call",
        "defense",
        "🚨 槓桿與保證金強制平倉警報 (帳戶生存等級)",
        True,
        True,
    ),
    # 每日僅 08:00 ET 盤前觸發一次的高信號護城河警報，不屬於盤中雜訊
    NotificationChannel(
        "defense_fundamental_thesis",
        "defense",
        "📜 SEC 財報自動掃描與護城河破滅警報 (B&H 持倉的主要出場訊號，建議保持開啟)",
        True,
        True,
    ),
    NotificationChannel(
        "defense_macro_tail_risk",
        "defense",
        "🦇 VIX 期限結構倒掛 (VTS >= 1.0) 與重大事件防護",
        True,
        True,
    ),
    # 持倉位階顧問屬持倉防禦等級的資訊，不受盤中頻率影響
    NotificationChannel(
        "advisory_core_levels",
        "defense",
        "🧭 B&H 持倉位階顧問 (僅告知目標區與結構失效，不建議減碼)",
        True,
        True,
    ),
    # 4. 🎯 Alpha 策略與情報
    NotificationChannel(
        "alpha_market_signals",
        "alpha",
        "✨ Nexus 戴維斯雙擊 (DDP) 與波動率優勢 (廉價期權)",
        False,
        False,
    ),
    # 15 分鐘 NRO 期權掃描（執行決策、PSQ、期權掃描卡）：過去完全不受任何開關控制
    NotificationChannel(
        "alpha_option_scan",
        "alpha",
        "🧮 NRO 期權掃描與執行決策 (含 PowerSqueeze)",
        False,
        False,
    ),
    # WTI / Polymarket 為全天候情報，不受盤中頻率影響，不屬於「Alpha 雜訊」
    NotificationChannel(
        "alpha_polymarket",
        "alpha",
        "🐳 Polymarket 巨鯨異動與預測機率突變 (Delta 閃崩/暴拉)",
        True,
        True,
    ),
    NotificationChannel(
        "alpha_wti_oil",
        "alpha",
        "🛢️ WTI 原油價格警報 (閾值突破與劇烈波動)",
        True,
        True,
    ),
    NotificationChannel(
        "alpha_price_volume_watch",
        "alpha",
        "📊 個股 15 分鐘價量突破警報 (自訂目標價與放量倍數)",
        False,
        False,
    ),
    # 投組下行風險（回撤階梯 / CVaR 預算）：帳戶層級的左尾防護訊號，任何預設情境
    # 下皆維持開啟（services/downside_risk_service.py）
    NotificationChannel(
        "risk_portfolio_downside",
        "defense",
        "📉 投組下行風險 (距高點回撤階梯 / CVaR 尾部風險超出預算)",
        True,
        True,
    ),
)

CHANNELS_BY_KEY: dict[str, NotificationChannel] = {c.key: c for c in CHANNELS}

# ---------------------------------------------------------------------------
# 衍生值（既有名稱由 database/notifications.py 與 cogs/settings_ui.py 重新匯出）
# ---------------------------------------------------------------------------

ALL_NOTIFICATION_KEYS: list[str] = [c.key for c in CHANNELS]

DEFAULT_NOTIFICATION_SETTINGS: dict[str, bool] = {c.key: c.default for c in CHANNELS}

PRESET_PROFILES: dict[str, dict[str, bool]] = {
    "all_on": {c.key: True for c in CHANNELS},
    "all_off": {c.key: False for c in CHANNELS},
    "focus": {c.key: c.focus for c in CHANNELS},
    "mute_intraday": {c.key: c.mute_intraday for c in CHANNELS},
}

TRADING_MODULES: dict[str, dict[str, Any]] = {
    m.key: {
        "title": m.title,
        "description": m.description,
        "items": {c.key: c.label for c in CHANNELS if c.module == m.key},
    }
    for m in MODULES
}

NOTIFICATION_KEY_VALUES: frozenset[str] = frozenset(get_args(NotificationKey))
