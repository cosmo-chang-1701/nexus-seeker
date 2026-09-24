"""通知頻道註冊表：`/notif_settings` 每一個頻道的單一真實來源。

過去頻道定義分散三處：`database/notifications.py`（key 清單、預設值、預設情境）、
`cogs/settings_ui.py::TRADING_MODULES`（標籤與分組）、以及各推播呼叫點的字串字面值。
新增一個頻道要改三個地方，漏改任何一處都只會在執行期靜默失效。現在：

- 頻道的 key / 模組 / 標籤 / 屬性只在本檔 `CHANNELS` 定義；
- `ALL_NOTIFICATION_KEYS`、`DEFAULT_NOTIFICATION_SETTINGS`、`PRESET_PROFILES`、
  `TRADING_MODULES` 全部由此衍生（原名稱由 `database/notifications.py` 與
  `cogs/settings_ui.py` 重新匯出，既有呼叫者不需改動）；
- 推播一律經 `services/notification_dispatcher.notify()`，其 `channel` 參數型別為
  `NotificationKey`，mypy 會攔下拼錯的頻道名稱。

分類準則（見 `docs/platform/03_notification_center.md`、
`docs/risk_portfolio/07_downside_risk_sortino_var_cvar.md`）：以「對 Buy & Hold 投組
報酬分佈的影響」分組，而非以功能來源分組。Sortino 只懲罰低於 MAR 的報酬、不懲罰上行
波動，因此：

- `LEFT_TAIL`（截斷左尾）：降低下行差、MDD、CVaR → 預設開啟，且任何預設情境都不會關閉
  （`preset_immune`），只能逐項手動關。
- `UPSIDE_CAPTURE`（捕捉上行）：進場、順勢加碼 → 提升 Sortino 分子。
- `UPSIDE_TRIM`（削減上行）：停利分批、獲利鎖定、比例修剪、換股、賣 Covered Call →
  對下行差貢獻小卻壓低報酬，`bh_defense` 預設情境關閉。
- `INTEL`（情報）：中性，但誘發過度交易。
- `BRIEFING`（定時戰報與系統通知）。

本模組刻意只依賴 stdlib（比照 `sentiment/skew_taxonomy.py`），可被 database / cogs /
services 任一層匯入而不產生循環相依。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

NotificationKey = Literal[
    # 🛡️ 左尾防護
    "defense_margin_call",
    "defense_fundamental_thesis",
    "defense_macro_tail_risk",
    "defense_event_calendar",
    "defense_hedge_advice",
    "defense_structure_break",
    "defense_gamma_fragility",
    "risk_portfolio_downside",
    # 🚀 上行捕捉
    "advisory_entry_signal",
    "entry_pyramid_add",
    "alpha_short_entry",
    # ✂️ 上行削減
    "defense_option_rollover",
    "trim_covered_call",
    "trim_profit_lock",
    "advisory_core_levels",
    # 📡 盤中情報
    "heartbeat_watchlist",
    "heartbeat_symbol_deep",
    "intel_market_scenario",
    "telemetry_orders",
    "alpha_market_signals",
    "alpha_option_scan",
    "alpha_price_volume_watch",
    # 🌐 全天候情報
    "alpha_polymarket",
    "alpha_wti_oil",
    # 📋 定時戰報與系統
    "briefing_pre_market",
    "briefing_post_market",
    "briefing_weekly_vtr",
    "vtr_virtual_trades",
    "system_lifecycle",
]

RiskRole = Literal["LEFT_TAIL", "UPSIDE_CAPTURE", "UPSIDE_TRIM", "INTEL", "BRIEFING"]

# 推播節奏：INTRADAY = 盤中週期性掃描；DAILY / WEEKLY = 每日 / 每週固定時點或每日去重後
# 至多一次；ALWAYS = 全天候（不受盤中時段影響）；EVENT = 非週期的系統 / 行事曆事件。
Cadence = Literal["INTRADAY", "DAILY", "WEEKLY", "ALWAYS", "EVENT"]

ModuleKey = Literal[
    "left_tail",
    "upside_capture",
    "upside_trim",
    "intel_intraday",
    "intel_always",
    "briefings",
]

PresetName = Literal["all_on", "all_off", "bh_defense", "focus", "mute_intraday"]

RISK_ROLE_TAGS: dict[str, str] = {
    "LEFT_TAIL": "截左尾",
    "UPSIDE_CAPTURE": "捕捉上行",
    "UPSIDE_TRIM": "削減上行",
    "INTEL": "情報",
    "BRIEFING": "戰報",
}

CADENCE_TAGS: dict[str, str] = {
    "INTRADAY": "盤中",
    "DAILY": "每日",
    "WEEKLY": "每週",
    "ALWAYS": "全天候",
    "EVENT": "事件",
}


@dataclass(frozen=True)
class NotificationChannel:
    key: NotificationKey
    module: ModuleKey
    label: str
    risk_role: RiskRole
    cadence: Cadence
    default: bool = True
    # 任何預設情境（含 all_off）都不會關閉；只能逐項手動關
    preset_immune: bool = False
    # 高頻、與使用者自身部位／掛單無直接關聯、或尚未校準的訊號
    noise: bool = False
    # 觸發條件由使用者自行設定門檻（價量目標價、WTI 價位）
    user_configured: bool = False
    # 由既有頻道拆分而來時的母頻道（遷移 v081 以母頻道的明確設定回填）
    parent_key: str | None = None


@dataclass(frozen=True)
class NotificationModule:
    key: ModuleKey
    title: str
    description: str


MODULES: tuple[NotificationModule, ...] = (
    NotificationModule(
        "left_tail",
        "🛡️ 左尾防護",
        "截斷下行：降低下行差、最大回撤與 CVaR。預設情境不會關閉這一區。",
    ),
    NotificationModule(
        "upside_capture",
        "🚀 上行捕捉",
        "進場與順勢加碼：提升 Sortino 分子，把握上漲波段。",
    ),
    NotificationModule(
        "upside_trim",
        "✂️ 上行削減",
        "停利分批、獲利鎖定、修剪與換股：壓低報酬但幾乎不降下行風險，B&H 建議關閉。",
    ),
    NotificationModule(
        "intel_intraday",
        "📡 盤中情報",
        "盤中自選雷達、個股深度心跳、掛單遙測與 Alpha 掃描。中性但容易誘發過度交易。",
    ),
    NotificationModule(
        "intel_always",
        "🌐 全天候情報",
        "Polymarket 巨鯨與 WTI 原油，不受盤中時段影響。",
    ),
    NotificationModule(
        "briefings",
        "📋 定時戰報與系統",
        "盤前盤後戰報、VTR 週報、虛擬交易通知與機器人啟停通知。",
    ),
)

CHANNELS: tuple[NotificationChannel, ...] = (
    # ---------------------------------------------------------------- 🛡️ 左尾防護
    NotificationChannel(
        "defense_margin_call",
        "left_tail",
        "🚨 保證金強制平倉與槓桿警戒 (帳戶生存等級)",
        "LEFT_TAIL",
        "INTRADAY",
        preset_immune=True,
    ),
    NotificationChannel(
        "defense_fundamental_thesis",
        "left_tail",
        "📜 SEC 財報護城河破滅警報 (B&H 持倉的主要出場訊號)",
        "LEFT_TAIL",
        "DAILY",
        preset_immune=True,
    ),
    NotificationChannel(
        "defense_macro_tail_risk",
        "left_tail",
        "🦇 VIX 恐慌 / 期限結構倒掛黑天鵝警報",
        "LEFT_TAIL",
        "DAILY",
        preset_immune=True,
    ),
    NotificationChannel(
        "defense_event_calendar",
        "left_tail",
        "🗓️ 48 小時內財報 / 經濟數據事件預警",
        "LEFT_TAIL",
        "EVENT",
        preset_immune=True,
        parent_key="defense_macro_tail_risk",
    ),
    NotificationChannel(
        "defense_hedge_advice",
        "left_tail",
        "🧯 VIX 急升 SPY 對沖股數與 Delta 再平衡建議",
        "LEFT_TAIL",
        "INTRADAY",
        preset_immune=True,
        parent_key="defense_macro_tail_risk",
    ),
    NotificationChannel(
        "defense_structure_break",
        "left_tail",
        "🧱 結構失效停損、狀態翻轉與逃頂保護性 Put",
        "LEFT_TAIL",
        "INTRADAY",
        preset_immune=True,
        parent_key="defense_option_rollover",
    ),
    NotificationChannel(
        "defense_gamma_fragility",
        "left_tail",
        "🆘 持倉負 Gamma 斷層與高 IV 崩跌風險",
        "LEFT_TAIL",
        "DAILY",
        preset_immune=True,
        parent_key="defense_portfolio_risk",
    ),
    # 投組下行風險（回撤階梯 / CVaR 預算）：帳戶層級的左尾防護訊號
    # （services/downside_risk_service.py）。全新頻道、無母頻道，預設開啟。
    NotificationChannel(
        "risk_portfolio_downside",
        "left_tail",
        "📉 投組下行風險 (距高點回撤階梯 / CVaR 尾部風險超出預算)",
        "LEFT_TAIL",
        "DAILY",
        preset_immune=True,
    ),
    # ------------------------------------------------------------- 🚀 上行捕捉
    NotificationChannel(
        "advisory_entry_signal",
        "upside_capture",
        "🎯 自選標的進場顧問 (六重鐵律通過時推播進場價 / 停損 / 目標)",
        "UPSIDE_CAPTURE",
        "INTRADAY",
    ),
    NotificationChannel(
        "entry_pyramid_add",
        "upside_capture",
        "📈 順勢加碼、停損上推與核心資金部署",
        "UPSIDE_CAPTURE",
        "INTRADAY",
        parent_key="defense_option_rollover",
    ),
    # 做空進場校準前較不宜打擾，歸為 noise：bh_defense / focus / mute_intraday 皆關
    NotificationChannel(
        "alpha_short_entry",
        "upside_capture",
        "🩳 做空進場訊號 (SHORT_SIDE 策略，校準中)",
        "UPSIDE_CAPTURE",
        "INTRADAY",
        noise=True,
        parent_key="alpha_market_signals",
    ),
    # ------------------------------------------------------------- ✂️ 上行削減
    NotificationChannel(
        "defense_option_rollover",
        "upside_trim",
        "🔄 停利分批、衛星再平衡、機會成本換股與動態保本",
        "UPSIDE_TRIM",
        "INTRADAY",
    ),
    NotificationChannel(
        "trim_covered_call",
        "upside_trim",
        "🪙 Covered Call 解套、Overlay 與權利金停利",
        "UPSIDE_TRIM",
        "INTRADAY",
        parent_key="defense_option_rollover",
    ),
    NotificationChannel(
        "trim_profit_lock",
        "upside_trim",
        "💰 DITM 深價內期權獲利鎖定",
        "UPSIDE_TRIM",
        "INTRADAY",
        parent_key="defense_portfolio_risk",
    ),
    # 每個 exit tier 每日至多一次、僅告知目標區位階，節奏視同每日
    NotificationChannel(
        "advisory_core_levels",
        "upside_trim",
        "🧭 B&H 持倉目標區位階告知 (不建議減碼)",
        "UPSIDE_TRIM",
        "DAILY",
    ),
    # ------------------------------------------------------------- 📡 盤中情報
    NotificationChannel(
        "heartbeat_watchlist",
        "intel_intraday",
        "📡 自選股 15 分鐘批次量化雷達",
        "INTEL",
        "INTRADAY",
        noise=True,
    ),
    NotificationChannel(
        "heartbeat_symbol_deep",
        "intel_intraday",
        "🧱 個股 30 分鐘深度戰場心跳 (微觀結構、Skew、UOA)",
        "INTEL",
        "INTRADAY",
        noise=True,
    ),
    NotificationChannel(
        "intel_market_scenario",
        "intel_intraday",
        "🎭 自選股市場情境事件 (巨鯨護航、結構破位等)",
        "INTEL",
        "INTRADAY",
        noise=True,
        parent_key="heartbeat_watchlist",
    ),
    NotificationChannel(
        "telemetry_orders",
        "intel_intraday",
        "🌌 待成交掛單實時對齊與撤退線",
        "INTEL",
        "INTRADAY",
    ),
    NotificationChannel(
        "alpha_market_signals",
        "intel_intraday",
        "✨ 戴維斯雙擊 (DDP)、廉價期權與 Gamma Squeeze",
        "INTEL",
        "INTRADAY",
        noise=True,
    ),
    NotificationChannel(
        "alpha_option_scan",
        "intel_intraday",
        "🧮 NRO 期權掃描與執行決策 (含 PowerSqueeze)",
        "INTEL",
        "INTRADAY",
        noise=True,
    ),
    NotificationChannel(
        "alpha_price_volume_watch",
        "intel_intraday",
        "📊 個股 15 分鐘價量突破警報 (自訂目標價與放量倍數)",
        "INTEL",
        "INTRADAY",
        noise=True,
        user_configured=True,
    ),
    # ------------------------------------------------------------- 🌐 全天候情報
    NotificationChannel(
        "alpha_polymarket",
        "intel_always",
        "🐳 Polymarket 巨鯨異動與預測機率突變",
        "INTEL",
        "ALWAYS",
    ),
    NotificationChannel(
        "alpha_wti_oil",
        "intel_always",
        "🛢️ WTI 原油價格警報 (閾值突破與劇烈波動)",
        "INTEL",
        "ALWAYS",
        user_configured=True,
    ),
    # ------------------------------------------------------------- 📋 定時戰報與系統
    NotificationChannel(
        "briefing_pre_market",
        "briefings",
        "🌅 盤前綜合戰報 (09:00 ET)",
        "BRIEFING",
        "DAILY",
    ),
    NotificationChannel(
        "briefing_post_market",
        "briefings",
        "📋 盤後 AI 深度覆盤 (16:15 ET)",
        "BRIEFING",
        "DAILY",
    ),
    NotificationChannel(
        "briefing_weekly_vtr",
        "briefings",
        "📈 虛擬交易室 (VTR) 績效週報 (週五 17:05 ET)",
        "BRIEFING",
        "WEEKLY",
    ),
    # 虛擬 (紙上) 交易的自動轉倉 / 平倉，不涉及真實部位
    NotificationChannel(
        "vtr_virtual_trades",
        "briefings",
        "👻 虛擬交易室自動轉倉 / 平倉通知",
        "BRIEFING",
        "INTRADAY",
        noise=True,
        parent_key="defense_option_rollover",
    ),
    NotificationChannel(
        "system_lifecycle",
        "briefings",
        "🤖 機器人啟動 / 關閉通知",
        "BRIEFING",
        "EVENT",
        default=False,
        noise=True,
    ),
)

CHANNELS_BY_KEY: dict[str, NotificationChannel] = {c.key: c for c in CHANNELS}


# ---------------------------------------------------------------------------
# 預設情境：由頻道屬性以規則衍生，新增頻道時不可能漏填
# ---------------------------------------------------------------------------


def _bh_defense(c: NotificationChannel) -> bool:
    """🧭 B&H 防守：上行捕捉開（不含做空）、上行削減全關、情報只留自訂門檻型。"""
    if c.risk_role == "UPSIDE_CAPTURE":
        return not c.noise
    if c.risk_role == "UPSIDE_TRIM":
        return False
    if c.risk_role == "INTEL":
        return c.user_configured
    return not c.noise


def _focus(c: NotificationChannel) -> bool:
    """🎯 精準交易：只關閉雜訊類頻道。"""
    return not c.noise


def _mute_intraday(c: NotificationChannel) -> bool:
    """🔕 盤中靜音：關閉雜訊與盤中節奏的推播，保留每日 / 每週 / 全天候頻道。"""
    return not c.noise and c.cadence != "INTRADAY"


_PRESET_RULES: dict[str, Callable[[NotificationChannel], bool]] = {
    "all_on": lambda c: True,
    "all_off": lambda c: False,
    "bh_defense": _bh_defense,
    "focus": _focus,
    "mute_intraday": _mute_intraday,
}

# preset_immune 的頻道在任何預設情境（含 all_off）一律為開
PRESET_PROFILES: dict[str, dict[str, bool]] = {
    name: {c.key: c.preset_immune or rule(c) for c in CHANNELS}
    for name, rule in _PRESET_RULES.items()
}

# ---------------------------------------------------------------------------
# 其他衍生值（既有名稱由 database/notifications.py 與 cogs/settings_ui.py 重新匯出）
# ---------------------------------------------------------------------------

ALL_NOTIFICATION_KEYS: list[str] = [c.key for c in CHANNELS]

DEFAULT_NOTIFICATION_SETTINGS: dict[str, bool] = {c.key: c.default for c in CHANNELS}

TRADING_MODULES: dict[str, dict[str, Any]] = {
    m.key: {
        "title": m.title,
        "description": m.description,
        "items": {c.key: c.label for c in CHANNELS if c.module == m.key},
    }
    for m in MODULES
}

NOTIFICATION_KEY_VALUES: frozenset[str] = frozenset(get_args(NotificationKey))


def channel_status_tags(key: str) -> str:
    """UI 用的「頻率 · 作用」標籤，例如 `盤中 · 截左尾`。"""
    c = CHANNELS_BY_KEY[key]
    return f"{CADENCE_TAGS[c.cadence]} · {RISK_ROLE_TAGS[c.risk_role]}"


# ---------------------------------------------------------------------------
# 動態轉倉指令 → 頻道（取代 portfolio_monitor 內的 if/elif 鏈）
# ---------------------------------------------------------------------------

# 情境層級的預設頻道。SATELLITE_REBALANCE 另依 exit tier 細分（見下方）。
ROLLOVER_SCENARIO_CHANNEL: dict[str, NotificationKey] = {
    "OPPORTUNITY_COST": "defense_option_rollover",
    "SATELLITE_REBALANCE": "defense_option_rollover",
    "MARGIN_DEFENSE": "defense_margin_call",
    "FUNDAMENTAL_BROKEN": "defense_fundamental_thesis",
    "CORE_DEPLOYMENT": "entry_pyramid_add",
    "MACRO_TOP_ESCAPE_DEFENSE": "defense_structure_break",
    "COVERED_CALL_PROFIT_LOCK": "trim_covered_call",
    "TRANSITION_ENGINE": "entry_pyramid_add",
    "SHORT_ENTRY": "alpha_short_entry",
    "PYRAMID_ADD": "entry_pyramid_add",
}

# 結構失效類出場分層：唯一「結構可能真的壞了」的訊號 → 左尾防護。
# SL_WHALE_CALL 是空頭部位的主力對沖鏡像；IVR_FAST_EXIT 是期權在 IV 崩跌時的快速平倉。
# SL_TRAILING_BREAKEVEN（動態保本）刻意不列入：它是獲利部位回吐到成本價時的出場，
# 性質是獲利部位管理而非結構失效，歸上行削減。
STRUCTURE_BREAK_EXIT_TIERS: frozenset[str] = frozenset(
    {
        "SL_STRUCTURAL",
        "SL_REGIME_FLIP",
        "SL_WHALE_PUT",
        "SL_WHALE_CALL",
        "EXTREME_TICK_BREACH",
        "IVR_FAST_EXIT",
    }
)


def resolve_rollover_channel(ins: Mapping[str, Any]) -> NotificationKey:
    """依轉倉指令的 scenario / action / exit_tier / overlay 旗標決定通知頻道。

    優先序：
    1. 保證金防禦 → defense_margin_call（帳戶生存等級，任何分層都不改道）
    2. 結構失效類 exit tier（含顧問模式的結構失效告知）→ defense_structure_break
    3. 顧問模式其餘告知（目標區位階）→ advisory_core_levels
    4. Covered Call Overlay（附掛於 CORE_DEPLOYMENT）→ trim_covered_call
    5. 情境對照表；未知情境 → defense_option_rollover
    """
    scenario = str(ins.get("scenario", ""))
    if scenario == "MARGIN_DEFENSE":
        return "defense_margin_call"
    if ins.get("exit_tier") in STRUCTURE_BREAK_EXIT_TIERS:
        return "defense_structure_break"
    if ins.get("action") == "ADVISORY":
        return "advisory_core_levels"
    if ins.get("is_covered_call_overlay"):
        return "trim_covered_call"
    return ROLLOVER_SCENARIO_CHANNEL.get(scenario, "defense_option_rollover")
