"""Skew 規則化判讀（方向徽章、進階過濾器）。"""

from typing import Any, Optional

from models.schemas import EnhancedWatchlistMetrics, ScanParams, WatchlistTacticalPlan


_SKEW_PCR_DIVERGENCE_WARNING = (
    "[⚠️ 警告：結構性情緒背離] Skew 分位極端且 PCR 指向相反極端，"
    "代表市場結構分裂（常見為機構對沖 vs 散戶追逐買權）。"
    "此情境不宜解讀為『同步』，建議降槓桿、避免追價單腿，優先採用定義風險的價差/保護性結構。"
)

# Direction badge taxonomy: (ansi_prefix, emoji, label, bias)
# bias is one of "bullish", "bearish", "neutral" — used for route/momentum cross-checks.
_SKEW_BADGE_NEUTRAL = ("[1;33m", "🟡", "中性觀望", "neutral")
_SKEW_BADGE_PREMIUM_HARVEST = ("[1;33m", "🟠", "傾向：賣方收租（不追價）", "bearish")
_SKEW_BADGE_DEFENSIVE = ("[1;31m", "🔴", "傾向：防禦／避險需求升溫", "bearish")
_SKEW_BADGE_DIVERGENCE = ("[1;31m", "⚠️", "方向背離／降槓桿", "neutral")
_SKEW_BADGE_BULLISH = ("[1;32m", "🟢", "偏多／賣方收租", "bullish")


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

    if bias == "neutral" or scenario_bias == bias:
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


def _format_skew_commentary(
    badge: tuple[str, str, str, str],
    detail: str,
    metrics: EnhancedWatchlistMetrics,
    tactical: "WatchlistTacticalPlan | None",
) -> str:
    ansi_prefix, emoji, label, bias = badge

    skew_state = getattr(metrics, "option_skew_state", None)
    if skew_state and skew_state not in detail:
        detail = f"{detail}（Skew 型態：{skew_state}）"

    lines = [f"{ansi_prefix}{emoji} 操作方向：{label}[0m"]

    sync_note = _skew_route_sync_note(bias, tactical)
    momentum_note = _skew_momentum_note(bias, metrics)
    tail_notes = [note for note in (sync_note, momentum_note) if note]
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

    skew_val = float(getattr(metrics, "option_skew", 0.0) or 0.0)
    skew_percentile = float(getattr(metrics, "skew_percentile", 50.0) or 50.0)
    pcr = float(getattr(metrics, "pcr", 0.0) or 0.0)
    iv_rank = float(getattr(metrics, "iv_rank", 0.0) or 0.0)

    # High-pass filter: suppress normal-range noise
    if 30.0 <= skew_percentile <= 70.0:
        return _format_skew_commentary(
            _SKEW_BADGE_NEUTRAL,
            "Skew 分位屬常態 (30-70%)，已抑制警報。",
            metrics,
            tactical,
        )

    # Absolute tail-risk routes
    # Left-Tail Explosion (Put Panic)
    if skew_percentile > 90.0 and iv_rank > 70.0:
        return _format_skew_commentary(
            _SKEW_BADGE_PREMIUM_HARVEST,
            "[IV 火山爆發 ── 收租主動路由] 市場呈現左尾極端避險，建議優先收租/定義風險的 Premium Extraction。",
            metrics,
            tactical,
        )

    # Right-Tail Mania (Call FOMO)
    if pcr < 0.35:
        return _format_skew_commentary(
            _SKEW_BADGE_DEFENSIVE,
            "[FOMO 情緒泡沫 ── 靜默防守路由] 檢測到極端追漲行為，強烈封鎖單腿長權利金追價。",
            metrics,
            tactical,
        )

    # Structural divergence check (Skew vs PCR extremes)
    if (skew_percentile > 85.0 and 0.0 < pcr < 0.4) or (
        skew_percentile < 15.0 and pcr > 1.5
    ):
        return _format_skew_commentary(
            _SKEW_BADGE_DIVERGENCE, _SKEW_PCR_DIVERGENCE_WARNING, metrics, tactical
        )

    # Rigid skew sign ↔ interpretation mapping
    if skew_val > 0 and skew_percentile >= 80.0:
        return _format_skew_commentary(
            _SKEW_BADGE_DEFENSIVE,
            "⚠️ 市場下行保護需求極高，隱含避險情緒升溫（機構大舉購入 Put 保險）",
            metrics,
            tactical,
        )
    if skew_val < 0 and skew_percentile <= 20.0:
        return _format_skew_commentary(
            _SKEW_BADGE_BULLISH,
            "🔥 市場上行看漲需求爆發，動能抄底/追高情緒極端亢奮（散戶搶購末日 Call）",
            metrics,
            tactical,
        )

    return _format_skew_commentary(
        _SKEW_BADGE_NEUTRAL,
        (
            f"Skew {skew_val:+.2f}%（百分位 {skew_percentile:.0f}%）屬常態區；"
            "建議以價位牆與事件風控為主，避免對單一指標過度解讀。"
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

    # 3. UOA & Dark Pool
    dp_skew = getattr(metrics, "dark_pool_skew", None)
    if dp_skew is not None and dp_skew < params.dark_pool_skew_floor:
        return False, []

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
