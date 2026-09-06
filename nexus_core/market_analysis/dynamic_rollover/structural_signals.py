import time
from typing import Any, Dict, List, Optional, Tuple

from . import logger
from .constants import (
    _HOLDING_DTE_FORCED_SETTLEMENT_THRESHOLD,
    _HOLDING_DTE_LOCKOUT_THRESHOLD,
    _MICROSTRUCTURE_SL_WHALE_PUT_MIN_NOTIONAL_USD,
    _MICROSTRUCTURE_SL_WHALE_PUT_MIN_RATIO,
    _MICROSTRUCTURE_SL_WHALE_PUT_NEAR_ATM_PCT,
    _STRUCTURAL_SIGNALS_CACHE_TTL,
)


def evaluate_option_dte_tier(dte: int, position_intent: str) -> str:
    """DTE 三態狀態機：回傳 "NORMAL_EXECUTION" | "LOCKOUT_SKIP" |
    "MAINTAIN_RISK_MONITORING" | "EXPIRATION_SETTLEMENT_ALERT"。

    position_intent 為 "NEW_OPPORTUNITY"（新增轉倉/開倉判斷，例如 Scenario 2
    機會成本轉倉、Scenario 3 Euphoria 分支開立全新 Bear Call Spread）或
    "MANAGE_EXISTING"（既有持倉風控/停利，例如 Scenario 3 雙軌停損監控、
    Covered Call 權利金衰減停利）。

    純函式、零 I/O。僅對 OPTIONS 部位有意義；呼叫端對 SPOT/HOLDING 一律不應
    呼叫此函式（SPOT 無到期日概念）。dte<=1 時無論 position_intent 為何皆回傳
    EXPIRATION_SETTLEMENT_ALERT——結算保護優先權高於「新增/既有」的區分。
    """
    if dte >= _HOLDING_DTE_LOCKOUT_THRESHOLD:
        return "NORMAL_EXECUTION"
    if dte <= _HOLDING_DTE_FORCED_SETTLEMENT_THRESHOLD:
        return "EXPIRATION_SETTLEMENT_ALERT"
    if position_intent == "NEW_OPPORTUNITY":
        return "LOCKOUT_SKIP"
    return "MAINTAIN_RISK_MONITORING"


def _resolve_canonical_anchor_base(
    support_wall: float,
    put_wall: float,
    call_wall: float,
    gamma_flip: float,
    hvn: float,
    spot: float,
) -> float:
    """
    單一權威防守錨點 (anchor_base) 解析：合併 _correct_wall_topology 與
    _compute_structural_breakdown_signals 曾各自維護的優先序 (support_wall →
    拓撲修正 min(put_wall,call_wall) → put_wall → gamma_flip → hvn → spot)，
    避免同一輪次「為何清倉」(結構性破位判定) 與「停損設在哪」(報告顯示) 使用
    不同數字 —— 兩者過去僅在 support_wall<=0 (GEX 數據缺失/畸形) 時才會分歧。
    """
    if support_wall > 0:
        return support_wall
    if put_wall > 0 and call_wall > 0 and put_wall > call_wall:
        # 拓撲逆轉修復：較低價為做市商支撐底牆，較高價為上方阻力天花板
        return min(put_wall, call_wall)
    if put_wall > 0:
        return put_wall
    if gamma_flip > 0:
        return gamma_flip
    if hvn > 0:
        return hvn
    return spot


def _scan_gex_walls(
    symbol: str, gex_profile_data: Optional[Dict[str, Any]]
) -> Tuple[float, float, float, float]:
    """
    掃描 gex_profile（履約價 -> GEX 曝險值）找出 support_wall/resistance_wall
    及其對應的 GEX 曝險值。抽自 _compute_structural_breakdown_signals，供該函式
    (Scenario 3/4 結構性破位判定) 與 _confirm_entry_signal (Scenario 2 進場確認
    條件二) 共用，確保「什麼算正 Gamma 支撐牆」在進場/出場兩端定義一致。

    回傳 (support_wall, resistance_wall, support_gex, resistance_gex)，
    找不到對應牆時該值維持 0.0。若最大正 GEX 履約價的曝險值低於
    GEX_THIN_WALL_THRESHOLD（薄弱紙牆，`/x` 終端機既有的 `(薄)` 標記邏輯），
    classify_gex_wall 會回傳 THIN_SUPPORT_WALL 而非 SUPPORT_GEX_WALL——本函式
    的 if/elif 判斷式未對 THIN_SUPPORT_WALL 另開分支，故薄弱牆會直接落空，
    support_wall/support_gex 維持 0.0，等同「未偵測到支撐牆」。此為刻意的
    保守設計：避免轉倉引擎的結構性破位判定/停損錨點信任一面隨時可能被打穿的
    薄紙牆。"""
    from market_analysis.index_microstructure import (
        GEX_THIN_WALL_THRESHOLD,
        classify_gex_wall,
    )

    support_wall: float = 0.0
    resistance_wall: float = 0.0
    support_gex: float = 0.0
    resistance_gex: float = 0.0
    if not (
        gex_profile_data
        and "gex_profile" in gex_profile_data
        and isinstance(gex_profile_data["gex_profile"], dict)
    ):
        return support_wall, resistance_wall, support_gex, resistance_gex

    gex_prof = gex_profile_data["gex_profile"]
    max_positive: float = 0.0
    for k, v in gex_prof.items():
        try:
            val = float(v)
            if val > max_positive:
                max_positive = val
        except (ValueError, TypeError) as e:
            logger.debug(f"[{symbol}] GEX strike {k}/{v} 解析失敗，略過: {e}")
    for k, v in gex_prof.items():
        try:
            val = float(v)
            strike = float(k)
            wall_type = classify_gex_wall(
                val,
                max_positive,
                is_heavy_otm_call=False,
                min_effective_gex=GEX_THIN_WALL_THRESHOLD,
            )
            if wall_type == "SUPPORT_GEX_WALL" and strike > support_wall:
                support_wall = strike
                support_gex = val
            elif wall_type == "RESISTANCE_CALL_WALL" and strike > resistance_wall:
                resistance_wall = strike
                resistance_gex = val
        except (ValueError, TypeError) as e:
            logger.debug(f"[{symbol}] GEX strike {k}/{v} 解析失敗，略過: {e}")

    return support_wall, resistance_wall, support_gex, resistance_gex


def _detect_whale_put_bto_block(
    uoa_list: Optional[List[Dict[str, Any]]], spot: float
) -> bool:
    """SL-主力對沖（微觀結構出場決策矩陣）：偵測近平值單筆 PUT BTO 大單壓制。

    取代舊版 `is_whale_sto_block` 的動能代理 (sqz_mom<0 且 skew<-0.3)，改用
    真實 UOA 名目金額/比率資料。比照既有 `opportunity_cost.py::
    _confirm_entry_condition4_uoa_dte` 的 UOA 掃描模式（型別+動作+ratio+
    名目金額+strike 位置門檻），僅方向反轉為 PUT BTO（主力买入 PUT 押注下跌，
    做市商需即時空頭對沖）。uoa_list 為 None 或空list 時 fail-safe 回傳 False。

    ratio 門檻優先採用 `paced_ratio`（uoa_detector.py 依當日交易時段進度
    正規化後的 Volume/OI 預估值），未提供時 fail-safe 退回原始 `ratio`
    （向後相容手動組裝的 uoa_list，例如既有測試 fixture）。OI 本身仍是前一
    交易日收盤的固定值——時段正規化解決的是「同一原始比值在盤中不同時刻
    代表的異常程度不同」，並非讓 OI 本身變成即時數據，這點無法透過任何
    正規化解決，屬選擇權市場資料的先天限制。
    """
    if not uoa_list or spot <= 0:
        return False
    for entry in uoa_list:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type", "")).upper() != "PUT":
            continue
        if "BTO" not in str(entry.get("action", "")):
            continue
        ratio = float(entry.get("paced_ratio", entry.get("ratio", 0.0)) or 0.0)
        if ratio < _MICROSTRUCTURE_SL_WHALE_PUT_MIN_RATIO:
            continue
        notional_value = float(entry.get("notional_value", 0.0) or 0.0)
        if notional_value < _MICROSTRUCTURE_SL_WHALE_PUT_MIN_NOTIONAL_USD:
            continue
        strike = float(entry.get("strike", 0.0) or 0.0)
        if strike <= 0:
            continue
        if abs(strike - spot) / spot > _MICROSTRUCTURE_SL_WHALE_PUT_NEAR_ATM_PCT:
            continue
        return True
    return False


async def compute_structural_breakdown_signals_impl(
    engine: Any,
    is_gamma_cliff_confirmed: Any,
    symbol: str,
    spot: float,
    put_wall: float,
    gamma_flip: float,
    atr_14: float,
    sqz_mom: float,
    skew: float,
    price_15m_close: float,
    gex_profile_data: Optional[Dict[str, Any]],
    asset_class: str,
    call_wall: float = 0.0,
    hvn: float = 0.0,
    uoa_list: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bool, bool, float, float, float, float]:
    """
    共用結構性破位 / 主力空頭封殺訊號計算：GEX 牆掃描 + anchor_base/gamma_cliff_level
    判定，供 _evaluate_structural_no_edge (Scenario 4) 與 check_satellite_rebalancing
    (Scenario 3) 共同呼叫，避免同一段門檻邏輯需要在兩處分別維護。

    anchor_base 透過與 _correct_wall_topology 共用的 _resolve_canonical_anchor_base
    解析（call_wall/hvn 為選填，未傳入時等同舊版行為），確保「是否觸發清倉」與
    報告顯示的「停損設在哪」在 support_wall<=0 (GEX 數據缺失/畸形) 時不再分歧。

    gamma_cliff_level 注意事項（刻意的三方分歧，不應合併）：此處為
    anchor_base - 1.5*atr_14（持倉專用，含 ATR 緩衝 + SQZ 動能疊加 +
    現貨/期權雙軌出場邏輯，判定門檻更嚴謹）。另有兩處各自維護的粗粒度變體：
      - market_analysis/scenario_classifier.py 的
        gamma_cliff_confirmation.is_below_gamma_defense_line：
        price < put_wall and price < gamma_flip（無 ATR 緩衝）
      - cogs/trading/heartbeat.py：gamma_cliff_level = min(put_wall, gamma_flip)
        （自選股 watchlist 進出場信號，無 ATR 緩衝，涵蓋未持有標的）
    同一標的若同時在自選股與持倉中，watchlist 心跳與持倉轉倉可能對「是否確認
    破位」給出不同判定，此為刻意設計而非缺陷（見下方回歸測試）。

    is_whale_sto_block（微觀結構出場決策矩陣 SL-主力對沖）：改用
    `_detect_whale_put_bto_block` 的真實 UOA 名目金額/比率判定，取代舊版
    `(sqz_mom<0) and (skew<-0.3)` 純動能代理。uoa_list 未傳入時等同舊版
    fail-safe 行為（一律判定為 False，不視為觸發）。

    回傳 (is_structural_breakdown, is_whale_sto_block, support_wall, resistance_wall,
          support_gex, resistance_gex)。
    """
    # 記憶化：同一 30 分鐘週期內 Scenario 3/4 對同一標的重複呼叫時直接複用結果，
    # 避免重跑一次完整 GEX 逐履約價掃描。gex_profile_data 以 id() 而非內容雜湊
    # 加入 key（兩個呼叫端在同一輪次餵入的是同一個 dict 物件參照），搭配短 TTL
    # 將「id 恰好被回收重用」的極低機率風險限制在可忽略範圍內。uoa_list 內容
    # (而非 id()) 需另外指紋化——與 gex_profile_data 不同，呼叫端可能對同一
    # 標的在同一輪次內傳入內容不同的 uoa_list 片段，僅比對物件參照可能誤判快取命中。
    uoa_fingerprint = tuple(
        sorted(
            (
                str(e.get("type")),
                str(e.get("action")),
                round(float(e.get("notional_value", 0.0) or 0.0)),
                round(float(e.get("ratio", 0.0) or 0.0), 2),
                e.get("strike"),
            )
            for e in (uoa_list or [])
            if isinstance(e, dict)
        )
    )
    cache_key = (
        symbol,
        round(spot, 2),
        round(put_wall, 2),
        round(call_wall, 2),
        round(gamma_flip, 2),
        round(hvn, 2),
        round(atr_14, 4),
        round(sqz_mom, 3),
        round(skew, 3),
        round(price_15m_close, 2),
        asset_class,
        id(gex_profile_data) if gex_profile_data is not None else None,
        uoa_fingerprint,
    )
    now = time.time()
    if cache_key in engine._structural_signals_cache:
        cached_result, expiry = engine._structural_signals_cache[cache_key]
        if now < expiry:
            return cached_result  # type: ignore

    support_wall, resistance_wall, support_gex, resistance_gex = _scan_gex_walls(
        symbol, gex_profile_data
    )

    anchor_base: float = _resolve_canonical_anchor_base(
        support_wall, put_wall, call_wall, gamma_flip, hvn, spot
    )
    gamma_cliff_level: float = anchor_base - (1.5 * atr_14) if anchor_base > 0 else 0.0

    is_structural_breakdown_pending: bool = (
        anchor_base > 0
        and spot < anchor_base
        and (price_15m_close < gamma_cliff_level or sqz_mom <= 0)
    )

    is_structural_breakdown = False
    if asset_class == "OPTIONS":
        # 期權快速通道：現價貫穿 anchor_base 即時判定破位，拒絕等待 15m 實體收盤
        if anchor_base > 0 and spot < anchor_base:
            is_structural_breakdown = True
    else:
        if is_structural_breakdown_pending and gamma_cliff_level > 0:
            is_structural_breakdown = await is_gamma_cliff_confirmed(
                symbol, gamma_cliff_level
            )

    is_whale_sto_block = _detect_whale_put_bto_block(uoa_list, spot)

    result = (
        is_structural_breakdown,
        is_whale_sto_block,
        support_wall,
        resistance_wall,
        support_gex,
        resistance_gex,
    )
    engine._structural_signals_cache[cache_key] = (
        result,
        now + _STRUCTURAL_SIGNALS_CACHE_TTL,
    )
    return result
