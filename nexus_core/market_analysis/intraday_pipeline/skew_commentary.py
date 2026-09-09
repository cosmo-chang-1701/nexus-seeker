"""Skew 規則化判讀（方向徽章、進階過濾器）。"""

from typing import Any, Optional

from market_analysis.sentiment.skew_taxonomy import (
    SKEW_BULLISH_PERCENTILE,
    SKEW_DEFENSIVE_PERCENTILE,
    SKEW_STATE_BULLISH,
    SKEW_STATE_DEFENSIVE,
)
from models.schemas import EnhancedWatchlistMetrics, ScanParams, WatchlistTacticalPlan


_SKEW_PCR_DIVERGENCE_WARNING = (
    "[⚠️ 警告：結構性情緒背離] Skew 分位極端且 PCR 指向相反極端，"
    "代表市場結構分裂（常見為機構對沖 vs 散戶追逐買權）。"
    "此情境不宜解讀為『同步』，建議降槓桿、避免追價單腿，優先採用定義風險的價差/保護性結構。"
)

# Direction badge taxonomy: (ansi_prefix, emoji, label, bias)
# bias is one of "bullish", "bearish", "neutral" — used for route/momentum cross-checks.
_SKEW_BADGE_NEUTRAL = ("[1;33m", "🟡", "中性觀望", "neutral")
_SKEW_BADGE_PREMIUM_HARVEST = ("[1;33m", "🟠", "賣方收租（不追價）", "bearish")
_SKEW_BADGE_DEFENSIVE = ("[1;31m", "🔴", "防禦／避險需求升溫", "bearish")
_SKEW_BADGE_DIVERGENCE = ("[1;31m", "⚠️", "方向背離／降槓桿", "neutral")
_SKEW_BADGE_BULLISH = ("[1;32m", "🟢", "偏多／賣方收租", "bullish")


def _optional_float(value: Any) -> Optional[float]:
    """把可能為 None / 非數值的量化欄位安全轉成 `float | None`。

    刻意不提供預設值：呼叫端需要能區分「資料缺失」與「真實數值剛好是 0」，
    這正是這些閘門過去誤判的來源。
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # 過濾 NaN


def _skew_route_sync_note(
    bias: str, tactical: "WatchlistTacticalPlan | None"
) -> str | None:
    """Cross-check the skew-derived bias against the already-computed SDDM tactical route."""

    if tactical is None:
        return None

    scenario_bias = {
        "premium-harvest": "bearish",
        "hard-hedge": "bearish",
        "wait": "neutral",
    }.get(tactical.scenario)

    if scenario_bias is None:
        return None

    # 中性判讀不構成對操盤路由的「確認」。舊實作讓任何 neutral 徽章都回報
    # 「✅ 同向」，包括紅燈 WAIT 路由——那會讀成系統兩邊都同意可以動作。
    if bias == "neutral":
        return (
            f"➖ 本判讀無方向性，不構成對操盤路由的確認 (SDDM: {tactical.sddm_route})"
        )
    if scenario_bias == bias:
        return f"✅ 與操盤路由同向 (SDDM: {tactical.sddm_route})"
    return f"⚠️ 訊號不同步，建議以操盤路由為準 (SDDM: {tactical.sddm_route})"


def _skew_momentum_note(bias: str, metrics: EnhancedWatchlistMetrics) -> str | None:
    """Flag a squeeze-momentum divergence against the skew-derived bias."""

    squeeze_momentum = getattr(metrics, "squeeze_momentum", None)
    if squeeze_momentum is None:
        return None
    squeeze_momentum = float(squeeze_momentum)

    if bias == "bullish" and squeeze_momentum < 0:
        return "🔻 動能背離：SQZ MOM 轉負，建議降低倉位確認"
    if bias == "bearish" and squeeze_momentum > 0:
        return "🔺 動能背離：SQZ MOM 仍為正，防禦立場可能過早"
    return None


def _skew_capital_retreat_note(tactical: "WatchlistTacticalPlan | None") -> str | None:
    """資金退守閘門啟動時，明講本判讀不得當成加碼依據。

    `evaluation.py` 的 skew>90 / 負 Gamma 閘門會設 `capital_retreat_required`，
    而本模組的「收租主動路由」等分支卻可能同時建議開倉賣方——同一封 embed 裡
    兩個方向打架。直接讀既有旗標交叉檢查，零額外 I/O。
    """
    if tactical is None or not getattr(tactical, "capital_retreat_required", False):
        return None
    return "🚨 資金退守閘門已啟動，本判讀不構成加碼/開倉依據"


def _format_skew_commentary(
    badge: tuple[str, str, str, str],
    detail: str,
    metrics: EnhancedWatchlistMetrics,
    tactical: "WatchlistTacticalPlan | None",
) -> str:
    ansi_prefix, emoji, label, bias = badge

    # 型態字串（option_skew_state）刻意不在這裡重複附掛：呈現層的
    # 「Skew: ⋯ ｜ {型態}」表頭已經是它的唯一承載處。

    lines = [f"{ansi_prefix}{emoji} 操作方向：{label}[0m"]

    sync_note = _skew_route_sync_note(bias, tactical)
    momentum_note = _skew_momentum_note(bias, metrics)
    retreat_note = _skew_capital_retreat_note(tactical)
    tail_notes = [note for note in (retreat_note, sync_note, momentum_note) if note]
    body_lines = tail_notes + [detail]
    for note in body_lines[:-1]:
        lines.append(f" ├─ {note}")
    lines.append(f" └─ {body_lines[-1]}")

    return "\n".join(lines)


def build_watchlist_skew_rule_commentary(
    metrics: EnhancedWatchlistMetrics,
    tactical: "WatchlistTacticalPlan | None" = None,
) -> str:
    """Deterministic skew diagnostics with a direction badge (no LLM).

    SDD changes:
    - Suppress standard warnings when skew percentile within [30, 70]
    - Only route on absolute tail anomalies per spec
    - Lead with an explicit direction badge, cross-checked against the
      already-computed SDDM tactical route and squeeze momentum so users
      can read the operating direction in the first line.
    """

    # 缺資料與真值 0 必須分開處理。過去這裡一律 `or 0.0` / `or 50.0`：
    #   - `pcr=None`（期權鏈抓取失敗）會變成 0.0，直接滿足下方 `pcr < 0.35`，
    #     憑空輸出「FOMO 情緒泡沫」防守路由；
    #   - `skew_percentile=None` 會變成 50.0，把「沒資料」報成「常態，已抑制警報」。
    # 現在保留 None，並讓每條規則各自要求它實際需要的欄位存在。
    skew_val = _optional_float(getattr(metrics, "option_skew", None))
    skew_percentile = _optional_float(getattr(metrics, "skew_percentile", None))
    iv_rank = _optional_float(getattr(metrics, "iv_rank", None))
    # PCR 為 0.0 同樣代表資料缺失而非「極端看漲」：calculate_pcr() 在分母
    # (call volume/OI) 為 0 時就直接回傳 0.0，盤前與流動性枯竭都會落在這裡。
    pcr = _optional_float(getattr(metrics, "pcr", None))
    if pcr is not None and pcr <= 0.0:
        pcr = None

    # 分位數據本身缺失時不做任何方向判讀，避免把資料缺口誤報為常態。
    if skew_percentile is None:
        return _format_skew_commentary(
            _SKEW_BADGE_NEUTRAL,
            "Skew 分位數據缺失（期權鏈或歷史樣本不足），已抑制判讀。",
            metrics,
            tactical,
        )

    # High-pass filter: suppress normal-range noise
    if 30.0 <= skew_percentile <= 70.0:
        return _format_skew_commentary(
            _SKEW_BADGE_NEUTRAL,
            "Skew 分位屬常態 (30-70%)，已抑制警報。",
            metrics,
            tactical,
        )

    # Structural divergence check (Skew vs PCR extremes)
    # 必須排在 FOMO 之前：FOMO 的條件 `pcr < 0.35` 是這裡第一條件
    # (`0 < pcr < 0.4`) 的子集，順序反了會把整條背離分支吃成死碼，只剩
    # [0.35, 0.4) 這 0.05 寬的窗。這個順序也與 evaluation.py 對同一組
    # 條件的判定結果一致（同一封 embed 不該自相矛盾）。
    if pcr is not None and (
        (skew_percentile > 85.0 and 0.0 < pcr < 0.4)
        or (skew_percentile < 15.0 and pcr > 1.5)
    ):
        return _format_skew_commentary(
            _SKEW_BADGE_DIVERGENCE, _SKEW_PCR_DIVERGENCE_WARNING, metrics, tactical
        )

    # Absolute tail-risk routes
    # Left-Tail Explosion (Put Panic)
    if skew_percentile > 90.0:
        if iv_rank is None:
            # IVR 缺失時不再靜默落入下一支：收租與否本來就取決於 IV 是否膨脹，
            # 沒有 IVR 就沒有判斷依據，據實揭露並取防禦側。
            return _format_skew_commentary(
                _SKEW_BADGE_DEFENSIVE,
                "[左尾極端避險] Skew 分位 >90% 但 IV Rank 數據缺失，無法判定權利金是否膨脹；"
                "暫取防禦立場，不建議據此開立賣方收租結構。",
                metrics,
                tactical,
            )
        if iv_rank > 70.0:
            return _format_skew_commentary(
                _SKEW_BADGE_PREMIUM_HARVEST,
                "[IV 火山爆發 ── 收租主動路由] 市場呈現左尾極端避險，建議優先收租/定義風險的 Premium Extraction。",
                metrics,
                tactical,
            )
        return _format_skew_commentary(
            _SKEW_BADGE_DEFENSIVE,
            "[左尾極端避險 ── 防禦路由] Skew 分位 >90% 但 IV Rank 未達 70%，"
            "避險需求集中在尾部而整體權利金並未膨脹，收租缺乏溢價補償；建議防禦而非賣方。",
            metrics,
            tactical,
        )

    # Right-Tail Mania (Call FOMO)
    # 加上 `skew_percentile < 30` 方向守衛：低 PCR 只有搭配右偏（Call 相對昂貴、
    # 分位偏低）才是追漲。舊實作不看分位，於是 skew_percentile=95（極端 Put 恐慌）
    # 配 pcr=0.2 會輸出「檢測到極端追漲行為」——方向完全相反。
    if pcr is not None and pcr < 0.35 and skew_percentile < 30.0:
        return _format_skew_commentary(
            _SKEW_BADGE_DEFENSIVE,
            "[FOMO 情緒泡沫 ── 靜默防守路由] 檢測到極端追漲行為，強烈封鎖單腿長權利金追價。",
            metrics,
            tactical,
        )

    # Rigid skew sign ↔ interpretation mapping（門檻與文案共用 skew_taxonomy，
    # 與 calculate_skew() 產生的 state 字串同源，避免兩處各自漂移）
    if (
        skew_val is not None
        and skew_val > 0
        and skew_percentile >= SKEW_DEFENSIVE_PERCENTILE
    ):
        return _format_skew_commentary(
            _SKEW_BADGE_DEFENSIVE, SKEW_STATE_DEFENSIVE, metrics, tactical
        )
    if (
        skew_val is not None
        and skew_val < 0
        and skew_percentile <= SKEW_BULLISH_PERCENTILE
    ):
        return _format_skew_commentary(
            _SKEW_BADGE_BULLISH, SKEW_STATE_BULLISH, metrics, tactical
        )

    # 兜底：此處必然落在 30-70 抑制窗之外（否則早已回傳），所以不能寫「屬常態區」。
    skew_val_text = f"{skew_val:+.2f}%" if skew_val is not None else "--"
    return _format_skew_commentary(
        _SKEW_BADGE_NEUTRAL,
        (
            f"Skew {skew_val_text}（百分位 {skew_percentile:.0f}%）已偏離常態區，"
            "但未達任何極端閾值；建議以價位牆與事件風控為主，避免對單一指標過度解讀。"
        ),
        metrics,
        tactical,
    )


def evaluate_advanced_filters(
    metrics: Any,
    symbol_gex: Optional[dict],
    uoa_data: Optional[list],
    params: ScanParams,
) -> tuple[bool, list[str]]:
    """
    快速在記憶體中比對高階過濾條件 (<100ms)。
    回傳 (是否通過過濾, 觸發的高階標籤列表)。
    """
    tags = []

    # 1. Volatility Squeeze & Momentum
    squeeze_status = getattr(metrics, "squeeze_status", False)
    squeeze_momentum = getattr(metrics, "squeeze_momentum", 0.0) or 0.0
    is_firing = bool(squeeze_status) and squeeze_momentum > 0
    if params.require_squeeze_firing and not is_firing:
        return False, []
    if is_firing:
        tags.append("[🔥 SQZ Firing]")

    if params.momentum_decay_rejection:
        # 動能衰竭保護：若處於極端衰竭 (這裡以 momentum < 0 或其他技術指標模擬) 予以剔除
        if squeeze_momentum < -5.0:
            return False, []

    # 2. Gamma Exposure
    current_price = getattr(metrics, "current_price", 0.0) or 0.0
    zero_gamma_price = symbol_gex.get("zero_gamma") if symbol_gex else None
    if zero_gamma_price is not None and zero_gamma_price > 0:
        if params.positive_gamma_regime_only and current_price <= zero_gamma_price:
            return False, []

        if params.proximity_to_gex_flip is not None:
            dist = abs(current_price - zero_gamma_price) / zero_gamma_price
            if dist > params.proximity_to_gex_flip:
                return False, []
            else:
                tags.append("[☢️ GEX 臨界]")

    # 3. UOA
    if uoa_data:

        def safe_float(v: Any):  # type: ignore
            return float(v) if v is not None else 0.0

        # UOA 意圖映射重構：Whale_Hedge (深價內避險 Put) 嚴禁計入多頭動能分數；
        # DTE 雜訊過濾器剔除 DTE < 7 的做市商結算對倒單，僅跨週期訂單計入攻擊權重。
        # 若上游未提供 dte 欄位 (舊資料源尚未升級)，視為未知而不套用此過濾，
        # 避免破壞既有呼叫端行為；僅在明確偵測到 dte 時才強制執行門檻。
        net_uoa_delta = sum(
            safe_float(item.get("delta", 0))
            if item.get("trade_type", "").upper() == "SWEEP"
            else -safe_float(item.get("delta", 0))
            for item in uoa_data
            if "Whale_Hedge" not in str(item.get("intent", ""))
            and ("dte" not in item or int(item.get("dte", 0) or 0) >= 7)
        )
        if net_uoa_delta < params.min_net_uoa_delta:
            return False, []
    elif params.min_net_uoa_delta > 0:
        # 無 UOA 資料但要求了最低 delta
        return False, []

    # 4. TDP Signal
    ma20 = getattr(metrics, "ma20", None)
    max_pain = getattr(metrics, "max_pain", None)
    volume_poc = getattr(metrics, "volume_poc", None)
    dp_poc = getattr(metrics, "dp_poc", None)

    is_tdp = True
    if ma20 is not None and current_price >= ma20:
        is_tdp = False
    if max_pain is not None and current_price >= max_pain:
        is_tdp = False
    if volume_poc is not None and current_price >= volume_poc:
        is_tdp = False
    if dp_poc is not None and current_price >= dp_poc:
        is_tdp = False

    # 若關鍵指標全為 None，避免誤判
    if ma20 is None and max_pain is None and dp_poc is None:
        is_tdp = False

    if params.require_tdp_signal and not is_tdp:
        return False, []
    if is_tdp:
        tags.append("[🔵 TDP 三擊]")

    return True, tags
