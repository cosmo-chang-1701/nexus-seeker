"""進場四重嚴格過濾鐵律 (Entry Ironclad Rules)。

與 `market_analysis/dynamic_rollover/opportunity_cost.py::_confirm_entry_signal`
（進場訊號六重鐵律）刻意完全獨立、互不取代、互不合併：後者是機會成本轉倉候選
標的確認的既有生產路徑（含總經/財報安全閥、候選標的自身 DTE 檢查等 I/O），
本模組是全新、更嚴格的獨立閘門，僅供：
1. `market_analysis/dynamic_rollover/core_deployment.py` 核心資金部署機會分支
   的「50% 資金配置」觸發判斷（State A）。
2. `cogs/embed_builders/rollover_embeds.py` 報告輸出層的「防洗盤實戰策略」
   四規則 Pass/Fail 檢核清單渲染。

刻意設計為純函式、零 I/O：所有 15 分鐘量價快照與 GEX/UOA 資料一律由呼叫端
預先抓取傳入（例如 `market_analysis/price_volume_alert.py::get_confirmed_15m_bar`），
方便單元測試以邊界條件表格驅動，也讓呼叫端能自行決定資料新鮮度策略。

與六重鐵律的交叉對照表（完整版另見
`market_analysis/dynamic_rollover/constants.py` 中 `_ENTRY_*` 系列常數上方的
對稱註解——兩處務必同步維護，任一側常數調整時應一併檢查另一側註解是否
仍準確）：
    六重條件一 (放量倍數 1.2x)              <-> 本模組規則一 (_IRONCLAD_VOLUME_SURGE_MULTIPLIER = 1.5x，更嚴)
    六重條件二 (_scan_gex_walls 完整掃描)    <-> 本模組規則二 (僅檢查 put_wall > 0 且現價站上，簡化版)
    六重條件三 (無上緣界限、嚴格 >)          <-> 本模組規則三 (限定 (spot, spot*1.05] 視窗、改用 >=)
    六重條件四 (僅檢查 DTE>=7，不檢查 ratio)  <-> 本模組規則四 (額外要求 ratio>=_IRONCLAD_UOA_ENTRY_MIN_RATIO)
    六重條件五 (總經/財報安全閥)             <-> 本模組無對應（純函式不含此 I/O）
    六重條件六 (candidate 自身 DTE)          <-> 本模組無對應（純函式不含此 I/O）
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from market_analysis.index_microstructure import estimate_symbol_gamma_flip

# 條件一：15m 放量倍數門檻。對照六重鐵律 dynamic_rollover/constants.py::
# _ENTRY_VOLUME_SURGE_MULTIPLIER = 1.2，本鐵律要求更高的量能確認強度 (1.5x)。
_IRONCLAD_VOLUME_SURGE_MULTIPLIER: float = 1.5

# 條件三：STO Call 物理封頂的 ratio (成交量/未平倉量) 門檻，採 >=。對照六重
# 鐵律 dynamic_rollover/constants.py::_ENTRY_UOA_CAP_RATIO_THRESHOLD (同為
# 1.0，但六重鐵律沿用的 index_microstructure.detect_uoa_sto_call_physical_cap
# 是嚴格 >、且無上緣界限，語意不同，故本模組不可直接沿用該 helper，見下方
# _check_rule3 docstring）。
_IRONCLAD_UOA_CAP_RATIO_THRESHOLD: float = 1.0
# 條件三：上方須保留的非對稱獲利空間視窗上緣 (spot, spot*1.05]。對照六重鐵律
# dynamic_rollover/constants.py::_ENTRY_ASYMMETRIC_ROOM_PCT（同為 0.05，但
# 六重鐵律該常數用於「Call Wall 緊貼現價」的獨立判斷，非物理封頂視窗邊界，
# 兩者語意不同，僅數值恰好相同，不可互換引用）。
_IRONCLAD_UOA_CAP_UPSIDE_ROOM_PCT: float = 0.05
# 條件四：驅動進場的主力 UOA BTO Call 買盤最低 DTE 要求。對照六重鐵律
# dynamic_rollover/constants.py::_ENTRY_UOA_MIN_DTE（數值同為 7）。
_IRONCLAD_UOA_ENTRY_MIN_DTE: int = 7
# 條件四：主力買盤最低 ratio 要求。六重鐵律無對應常數——condition4 只檢查
# DTE，不檢查 ratio，本鐵律額外要求買盤規模達顯著門檻，是四重鐵律獨有的
# 更嚴格條件。
_IRONCLAD_UOA_ENTRY_MIN_RATIO: float = 0.8


@dataclass
class RuleCheck:
    """單一規則的判定結果，供 Task 5 embed 直接渲染。"""

    name: str
    label: str
    passed: bool
    detail: str


@dataclass
class RuleCheckResult:
    """進場四重鐵律的彙總判定結果。"""

    all_passed: bool
    checks: list[RuleCheck] = field(default_factory=list)

    def as_dict_list(self) -> list[dict[str, Any]]:
        """序列化為 list[dict]，供 RolloverInstruction (TypedDict) 邊界使用，
        Task 5 embed 消費端讀回渲染。"""
        return [
            {
                "name": c.name,
                "label": c.label,
                "passed": c.passed,
                "detail": c.detail,
            }
            for c in self.checks
        ]


def _check_rule1_breakout(
    gex_profile: Optional[dict],
    target_spot: float,
    price_15m_close: float,
    volume_15m: float,
    volume_15m_sma20: float,
) -> RuleCheck:
    """規則一：結構性右側放量突破確認 (15m 實體收盤站穩 Gamma Flip 估算門檻 +
    量能達 20 根均量的 1.5 倍)。"""
    gamma_flip_est = estimate_symbol_gamma_flip(
        gex_profile if isinstance(gex_profile, dict) else {}, target_spot
    )
    is_closed_above = gamma_flip_est > 0 and price_15m_close > gamma_flip_est
    is_volume_surge = (
        volume_15m_sma20 > 0
        and volume_15m >= volume_15m_sma20 * _IRONCLAD_VOLUME_SURGE_MULTIPLIER
    )
    passed = is_closed_above and is_volume_surge
    if gamma_flip_est <= 0:
        detail = "無法估算 Gamma Flip 門檻 (GEX Profile 無交叉點)"
    else:
        detail = (
            f"15m收盤 ${price_15m_close:.2f} "
            f"{'>' if is_closed_above else '<='} Gamma Flip估算 ${gamma_flip_est:.2f}，"
            f"量能 {volume_15m:.0f} vs 均量×{_IRONCLAD_VOLUME_SURGE_MULTIPLIER} "
            f"= {volume_15m_sma20 * _IRONCLAD_VOLUME_SURGE_MULTIPLIER:.0f}"
        )
    return RuleCheck(
        name="rule_1_breakout",
        label="結構性右側放量突破",
        passed=passed,
        detail=detail,
    )


def _check_rule2_put_wall_floor(put_wall: float, current_price: float) -> RuleCheck:
    """規則二：做市商正 Gamma 底牆完好 (PutWall > 0 且現價站上底牆)。"""
    passed = put_wall > 0 and current_price > put_wall
    detail = (
        f"現價 ${current_price:.2f} 站上 PutWall ${put_wall:.2f}"
        if passed
        else f"PutWall {'缺失' if put_wall <= 0 else f'${put_wall:.2f} 未站穩'}"
    )
    return RuleCheck(
        name="rule_2_put_wall_floor",
        label="正 Gamma 底牆完好",
        passed=passed,
        detail=detail,
    )


def _check_rule3_no_physical_cap(
    uoa_list: list, current_price: float, call_wall: float
) -> RuleCheck:
    """規則三：UOA 無實質物理封頂。

    刻意不直接沿用 index_microstructure.detect_uoa_sto_call_physical_cap：
    該 helper 判定條件為 `strike > spot and ratio > threshold`（嚴格大於、
    無上緣界限），語意與本規則的 `spot < strike <= spot*1.05 且 ratio >= 1.0`
    （限定於非對稱獲利空間視窗內、含等於）不同，故在此重新掃描，切勿為求
    「簡化重用」而改回呼叫該 helper。
    """
    upside_ceiling = current_price * (1.0 + _IRONCLAD_UOA_CAP_UPSIDE_ROOM_PCT)
    capping_strike = 0.0
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "")).upper() != "CALL":
            continue
        if "STO" not in str(entry.get("action", "")):
            continue
        strike = float(entry.get("strike", 0.0) or 0.0)
        ratio = float(entry.get("ratio", 0.0) or 0.0)
        if (
            current_price < strike <= upside_ceiling
            and ratio >= _IRONCLAD_UOA_CAP_RATIO_THRESHOLD
        ):
            capping_strike = strike
            break

    is_below_call_wall = call_wall <= 0 or current_price < call_wall
    passed = capping_strike <= 0 and is_below_call_wall
    if capping_strike > 0:
        detail = (
            f"偵測到單筆 ratio>={_IRONCLAD_UOA_CAP_RATIO_THRESHOLD}x OI 的 "
            f"STO Call 物理封頂 @ ${capping_strike:.2f} "
            f"(現價上方 {_IRONCLAD_UOA_CAP_UPSIDE_ROOM_PCT:.0%} 視窗內)"
        )
    elif not is_below_call_wall:
        detail = f"現價 ${current_price:.2f} 已貫穿 Call Wall ${call_wall:.2f}"
    else:
        detail = "上方無實質物理封頂，非對稱空間充足"
    return RuleCheck(
        name="rule_3_no_physical_cap",
        label="無 UOA 物理封頂",
        passed=passed,
        detail=detail,
    )


def _check_rule4_bto_call_conviction(uoa_list: list) -> RuleCheck:
    """規則四：主力進攻 BTO Call 買盤確認 (DTE >= 7 且 ratio >= 0.8)，剔除
    末日對倒雜訊。刻意額外要求 ratio >= 0.8（既有六重鐵律 condition4 只檢查
    DTE，不檢查 ratio），確保買盤規模達顯著門檻。"""
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "")).upper() != "CALL":
            continue
        if "BTO" not in str(entry.get("action", "")):
            continue
        ratio = float(entry.get("ratio", 0.0) or 0.0)
        if ratio < _IRONCLAD_UOA_ENTRY_MIN_RATIO:
            continue
        try:
            expiry_str = str(entry.get("expiry", ""))
            exp_dt = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            dte = (exp_dt - datetime.now().date()).days
        except (ValueError, TypeError):
            continue
        if dte >= _IRONCLAD_UOA_ENTRY_MIN_DTE:
            return RuleCheck(
                name="rule_4_bto_call_conviction",
                label="主力 BTO Call 買盤確認",
                passed=True,
                detail=(
                    f"主力買盤 DTE={dte}、ratio={ratio:.2f}x OI "
                    f"(符合門檻 DTE>={_IRONCLAD_UOA_ENTRY_MIN_DTE}、"
                    f"ratio>={_IRONCLAD_UOA_ENTRY_MIN_RATIO})"
                ),
            )
    return RuleCheck(
        name="rule_4_bto_call_conviction",
        label="主力 BTO Call 買盤確認",
        passed=False,
        detail=(
            f"未偵測到符合門檻 (DTE>={_IRONCLAD_UOA_ENTRY_MIN_DTE}、"
            f"ratio>={_IRONCLAD_UOA_ENTRY_MIN_RATIO}) 的主力 CALL BTO 買盤"
        ),
    )


def check_entry_ironclad_rules(
    candidate_symbol: str,
    target_spot: float,
    gex_profile_data: Optional[dict],
    uoa_list: list,
    price_15m_close: float,
    volume_15m: float,
    volume_15m_sma20: float,
) -> RuleCheckResult:
    """進場四重嚴格過濾鐵律：純函式、零 I/O，四項條件必須同時成立
    (all_passed=True) 才判定符合轉倉資格。

    :param candidate_symbol: 候選標的代號 (僅用於錯誤訊息/未來擴充，目前判斷
        邏輯不依賴此參數)。
    :param target_spot: 候選標的現價。
    :param gex_profile_data: GEX 快照 dict，形狀比照
        `fetch_symbol_gex_metrics()`：{put_wall, call_wall, net_gex,
        gex_profile}。
    :param uoa_list: UOA 快照 list[dict]，形狀比照 `SentimentEngine.detect_uoa()`
        輸出：每筆含 type/action/strike/ratio/expiry 等欄位。
    :param price_15m_close: 最近一根已收盤 15 分鐘 K 棒收盤價（例如
        `price_volume_alert.get_confirmed_15m_bar().close`）。
    :param volume_15m: 該根已收盤 15 分鐘 K 棒成交量。
    :param volume_15m_sma20: 前 20 根 15 分鐘 K 棒均量。
    """
    del candidate_symbol  # 目前判斷邏輯不依賴，保留供未來錯誤訊息擴充

    gex_profile = (
        gex_profile_data.get("gex_profile")
        if isinstance(gex_profile_data, dict)
        else None
    )
    put_wall = (
        float(gex_profile_data.get("put_wall", 0.0) or 0.0)
        if isinstance(gex_profile_data, dict)
        else 0.0
    )
    call_wall = (
        float(gex_profile_data.get("call_wall", 0.0) or 0.0)
        if isinstance(gex_profile_data, dict)
        else 0.0
    )
    safe_uoa_list = uoa_list or []

    checks = [
        _check_rule1_breakout(
            gex_profile, target_spot, price_15m_close, volume_15m, volume_15m_sma20
        ),
        _check_rule2_put_wall_floor(put_wall, target_spot),
        _check_rule3_no_physical_cap(safe_uoa_list, target_spot, call_wall),
        _check_rule4_bto_call_conviction(safe_uoa_list),
    ]
    all_passed = all(c.passed for c in checks)
    return RuleCheckResult(all_passed=all_passed, checks=checks)


__all__: list[str] = [
    "RuleCheck",
    "RuleCheckResult",
    "check_entry_ironclad_rules",
]
