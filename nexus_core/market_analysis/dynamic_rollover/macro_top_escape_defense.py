import math
from typing import Any, Dict, List, Optional

from . import logger
from .constants import (
    _EUPHORIA_SKEW_PERCENTILE,
    _MACRO_TOP_ESCAPE_HEDGE_SYMBOL,
    _MACRO_TOP_ESCAPE_PUT_DTE_MAX,
    _MACRO_TOP_ESCAPE_PUT_DTE_MIN,
    _MACRO_TOP_ESCAPE_PUT_TARGET_DELTA,
    _MACRO_TOP_ESCAPE_PUT_TIERS,
    _PROFIT_UNLOCK_TOLERANCE,
    _MACRO_TOP_ESCAPE_HEDGE_RATIO,
)
from .models import RolloverInstruction, RolloverScenario

# 三級動作相同 (買保護性 Put、對沖比例相同)，只有判讀文案隨分級升高。
_TIER_ASSESSMENT_TEXT: Dict[str, str] = {
    "WATCH": "前哨階段訊號初現，尚不足以判定真正逃頂。",
    "ELEVATED": "多項逃頂因子同時亮起，警戒升高。",
    "CRITICAL": "逃頂評分達確認級，下檔風險顯著升高。",
}


def _compute_satellite_euphoria_ratio(
    portfolio_assets: List[Dict[str, Any]],
) -> Optional[float]:
    """聚合使用者衛星持倉中，個別已符合 Scenario 3 亢奮出場條件 (現貨觸及
    Call Wall 或 Skew 百分位 <= 20) 的比例，作為 evaluate_macro_top_escape_score()
    的第 5 個 (可選) 因子輸入。直接重用 anti_washout.py 完全相同的兩條判定式與
    constants.py 的既有具名常數，不新增/複製任何量化門檻。沒有任何 SATELLITE
    持倉時回傳 None (該因子不參與評分)。
    """
    satellite_assets = [
        a for a in portfolio_assets if a.get("asset_class") == "SATELLITE"
    ]
    if not satellite_assets:
        return None

    euphoria_count = 0
    for asset in satellite_assets:
        # 值為 None 時以 `or` 退回 0.0 (float(None) 會丟 TypeError)；Skew 或其
        # 分位未知時跳過亢奮 Skew 指標，不補 50 冒充中性。
        spot = float(asset.get("spot_price") or 0.0)
        call_wall = float(asset.get("call_wall") or 0.0)
        skew_raw = asset.get("skew")
        sp_raw = asset.get("skew_percentile")

        is_profit_unlocked = (call_wall > 0 and spot > 0) and (
            spot >= call_wall
            or abs(spot - call_wall) / call_wall < _PROFIT_UNLOCK_TOLERANCE
        )
        is_euphoria_skew = (
            skew_raw is not None
            and sp_raw is not None
            and float(skew_raw) < 0
            and float(sp_raw) <= _EUPHORIA_SKEW_PERCENTILE
        )
        if is_profit_unlocked or is_euphoria_skew:
            euphoria_count += 1

    return euphoria_count / len(satellite_assets)


def _build_protective_put_instruction(
    user_ctx: Any,
    score: int,
    tier: str,
    tier_title: str,
    factor_lines: str,
    flagged: set,
) -> List[RolloverInstruction]:
    """WATCH／ELEVATED／CRITICAL 共用：不減碼，改買保護性 Put，保留 100% 上檔曝險。

    Q_put = ceil((Δ_β-weighted × _MACRO_TOP_ESCAPE_HEDGE_RATIO) / (|Δ_put| × 100))

    Δ_β-weighted 直接複用 `user_ctx.total_weighted_delta`（database/user_settings.py，
    Beta 加權 Delta 的既有單一權威來源，`market_analysis/hedging.py` 對沖建議
    引擎已採同一欄位），避免另立第二套組合曝險計算。標的固定為
    _MACRO_TOP_ESCAPE_HEDGE_SYMBOL (SPY)，沿用 hedging.py 既有以 SPY 作為組合
    對沖代理的慣例——逃頂訊號是系統性的，指數 Put 的流動性與價差優於個股。

    total_weighted_delta 已含使用者登錄的期權部位 (含先前買進的 HEDGE Put)，
    因此同日由 WATCH 升級後重算的數量，是在既有對沖之上的增量。

    組合已淨平/淨空 (total_weighted_delta <= 0) 時無下檔方向性曝險可對沖，
    回傳空列表 (fail-safe，避免建議一筆語意上矛盾的「加碼防護」)。
    """
    total_weighted_delta = float(getattr(user_ctx, "total_weighted_delta", 0.0) or 0.0)
    if total_weighted_delta <= 0:
        return []
    if (_MACRO_TOP_ESCAPE_HEDGE_SYMBOL, "OPTIONS") in flagged:
        return []

    put_qty = math.ceil(
        (total_weighted_delta * _MACRO_TOP_ESCAPE_HEDGE_RATIO)
        / (abs(_MACRO_TOP_ESCAPE_PUT_TARGET_DELTA) * 100.0)
    )
    if put_qty < 1:
        return []

    reason_text = (
        "🧭 **宏觀逃頂前瞻防禦 (Macro Top-Escape Anticipatory Defense)**\n"
        f"綜合評分：{tier_title} ({score} 分)\n"
        f"{factor_lines}\n"
        f"{_TIER_ASSESSMENT_TEXT.get(tier, '')}"
        "系統以買入並持有為主，減碼會放棄上檔曝險；"
        f"改為買進 {_MACRO_TOP_ESCAPE_HEDGE_SYMBOL} 保護性 Put，"
        f"對沖組合 Beta 加權 Delta 的 {_MACRO_TOP_ESCAPE_HEDGE_RATIO:.0%}，"
        "保留 100% 上檔曝險的同時鎖住下檔。"
        "已登錄為 HEDGE 的部位已計入組合 Delta，本次數量是在既有對沖之上的增量。\n"
        f"⚠️ 請以 /add_trade 登錄本筆合約，並將 trade_category 設為 `HEDGE`"
        "——分類錯誤會讓對沖績效引擎誤判為方向性做空部位，之後在多頭共振訊號"
        "出現時建議您平掉自己的保護。"
    )
    suggested_strategy = (
        f"BUY {put_qty} × {_MACRO_TOP_ESCAPE_HEDGE_SYMBOL} PUT "
        f"(Delta ≈ {_MACRO_TOP_ESCAPE_PUT_TARGET_DELTA:.2f}，"
        f"DTE {_MACRO_TOP_ESCAPE_PUT_DTE_MIN}-{_MACRO_TOP_ESCAPE_PUT_DTE_MAX}，"
        "trade_category=HEDGE)"
    )

    return [
        {
            "symbol": _MACRO_TOP_ESCAPE_HEDGE_SYMBOL,
            "action": "BUY_PROTECTIVE_PUT",
            "sell_ratio": 0.0,
            "target_core": _MACRO_TOP_ESCAPE_HEDGE_SYMBOL,
            "reason": reason_text,
            "suggested_strategy": suggested_strategy,
            "scenario": RolloverScenario.MACRO_TOP_ESCAPE_DEFENSE.value,
            "is_manual_override_required": True,
            "instrument_type": "OPTIONS",
            "direction": "BTO",
            "opt_type": "PUT",
            "macro_tier": tier,
        }
    ]


async def evaluate_macro_top_escape_defense_impl(
    get_full_user_context: Any,
    user_id: int,
    portfolio_assets: List[Dict[str, Any]],
    already_flagged_symbols: Optional[set] = None,
) -> List[RolloverInstruction]:
    """
    邏輯 (6): 宏觀逃頂前瞻防禦 (Macro Top-Escape Anticipatory Defense)

    觸發條件 (三道 Gate 缺一不可):
      Gate 1 — user_settings.enable_macro_top_escape_defense == True (嚴格
               opt-in：會動用戶資金/持倉的功能一律需要使用者明確同意，不預設開啟)。
      Gate 2 — evaluate_macro_top_escape_score() (index_microstructure.py)
               評分達 WATCH 以上分級 (VTS 逆價差 + Fear & Greed 極度貪婪 +
               FedWatch 鷹派 + 負 Gamma + 可選的衛星持倉亢奮廣度)。
      Gate 3 — 排除 already_flagged_symbols (已被 Scenario 3/4 標記的標的
               本輪不重複下指令，避免同一標的收到互相矛盾的建議)。

    動作：不賣股，改買保護性 Put（組合層級的單一建議，不逐一針對個別持倉）。
    系統以買入並持有為主要策略，減碼會放棄上檔、與「把握每次上漲」直接衝突；
    買保護保留 100% 上檔曝險，只付出權利金成本。原本 ELEVATED／CRITICAL 級的
    「減碼轉入 BOXX」分支已移除，這兩級改為同樣建議保護性 Put。

    already_flagged_symbols: 已被 Scenario 3/4 標記過的標的集合，會被跳過。
    刻意排在 dispatcher 順序最後一位——本情境是信心度最低、最具推測性的
    觸發，絕不能搶在更確定的訊號之前對同一標的下指令。
    """
    user_ctx = get_full_user_context(user_id)
    if not user_ctx or not getattr(user_ctx, "enable_macro_top_escape_defense", False):
        return []

    from market_analysis.index_microstructure import (
        evaluate_macro_top_escape_score,
        fetch_core_macro_metrics,
        get_market_regime,
    )
    from services.market_data_service import get_vix_term_structure

    # 未知一律傳 None，不補 NORMAL / 0.88 / 48 備援值：未知因子不計分並在
    # 卡片上標示「資料不足」，分數僅為下限 (不會因假資料觸發防禦性減碼)。
    is_negative_gamma: Optional[bool] = None
    try:
        regime = await get_market_regime()
        if regime != "UNKNOWN":
            is_negative_gamma = regime in (
                "SHORT_GAMMA_CRITICAL",
                "SYSTEMIC_LIQUIDITY_CRISIS",
            )
    except Exception as e:
        logger.warning(f"宏觀逃頂前瞻防禦: 取得市場 Regime 失敗: {e}")

    vts_ratio: Optional[float] = None
    try:
        vts_data = await get_vix_term_structure()
        if vts_data.get("is_valid", False) and vts_data.get("vts_ratio") is not None:
            vts_ratio = float(vts_data["vts_ratio"])
    except Exception as e:
        logger.warning(f"宏觀逃頂前瞻防禦: 取得 VTS 期限結構失敗: {e}")

    fear_greed: Optional[float] = None
    try:
        core_metrics = await fetch_core_macro_metrics()
        fg_raw = core_metrics.get("fear_greed")
        if fg_raw is not None and not core_metrics.get("_is_fallback"):
            fear_greed = float(fg_raw)
    except Exception as e:
        logger.warning(f"宏觀逃頂前瞻防禦: 取得 Fear & Greed 指數失敗: {e}")

    from database.cache import get_kv_cache_fresh
    from services.calendar_service import FEDWATCH_PROB_MAX_AGE_SECONDS

    # 逾期（> 12h）回傳 None → 評分函式視為未知因子、不計分，不改任何門檻
    prob = get_kv_cache_fresh(
        "macro_fedwatch_probability", FEDWATCH_PROB_MAX_AGE_SECONDS
    )

    satellite_euphoria_ratio = _compute_satellite_euphoria_ratio(portfolio_assets)

    score, tier, tier_title, factors = evaluate_macro_top_escape_score(
        vts_ratio=vts_ratio,
        fear_greed=fear_greed,
        prob=prob,
        is_negative_gamma=is_negative_gamma,
        satellite_euphoria_ratio=satellite_euphoria_ratio,
    )
    if tier not in _MACRO_TOP_ESCAPE_PUT_TIERS:
        return []

    flagged = already_flagged_symbols or set()
    factor_lines = "\n".join(f" ├─ {name}: {val}" for name, val in factors)
    return _build_protective_put_instruction(
        user_ctx, score, tier, tier_title, factor_lines, flagged
    )
