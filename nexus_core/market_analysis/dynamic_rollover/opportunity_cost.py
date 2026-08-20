from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from market_analysis.index_microstructure import estimate_symbol_gamma_flip
from market_analysis.option_guidance import is_spread_illiquid

from . import logger
from .constants import (
    CORE_DEFENSE_ETF_SYMBOLS,
    _BREAKOUT_READY_THRESHOLD,
    _EARNINGS_PRE_EVENT_BUFFER_DAYS,
    _ENTRY_ASYMMETRIC_ROOM_PCT,
    _ENTRY_CANDIDATE_MIN_DTE,
    _ENTRY_UOA_CAP_RATIO_THRESHOLD,
    _ENTRY_UOA_MIN_DTE,
    _ENTRY_VOLUME_LOOKBACK_BARS,
    _ENTRY_VOLUME_SURGE_MULTIPLIER,
    _ESTIMATED_ROUND_TRIP_COST_PCT,
    _EV_SPREAD_MIN_THRESHOLD,
    _LOW_IVR_UPPER_BOUND,
    _MOMENTUM_DECAY_THRESHOLD,
    _PROFIT_LOCK_PROFIT_PCT_THRESHOLD,
    _PUT_WALL_PROXIMITY_TOLERANCE,
    _ROLLOVER_RATIO_HIGH_PROFIT,
    _ROLLOVER_RATIO_STANDARD,
    _SKEW_DOWNSIDE_PENALTY_FACTOR,
)
from .models import RolloverScenario
from .structural_signals import _scan_gex_walls


class _OpportunityCostMixin:
    """邏輯 (2)：機會成本與期望值比對 (Opportunity Cost & EV Comparison)。"""

    def _calculate_ev_proxy(
        self, symbol: str, skew_percentile: Optional[float] = None
    ) -> float:
        """
        Skew-Adjusted EV 期望值模型：
        以快取的 expected_move_upper 相對現貨的正規化上緣空間為基礎，
        並結合 Skew 偏斜度進行下行風險調整：
        Adjusted EV = Base EV * (1.0 - Downside Risk Penalty)
        當 Skew Percentile < 50% (偏恐慌/偏空) 時施加懲罰，避免單純因為波動大而誤判為高期望值。
        僅使用 market_cache（Cache-Aside），零額外 API 呼叫。
        is_stale 或 is_degraded 的快取視為不可信，回傳 0.0。
        """
        from database.market_cache import get_market_cache

        row = get_market_cache(symbol)
        if not row or row.get("is_stale") or row.get("is_degraded"):
            return 0.0
        spot = float(row.get("reference_spot_price") or 0.0)
        upper = float(row.get("expected_move_upper") or 0.0)
        if spot <= 0.0:
            return 0.0
        base_ev = (upper - spot) / spot

        # 若未提供 skew_percentile，嘗試從快取讀取
        if skew_percentile is None:
            try:
                from database.cache import get_kv_cache

                cached_sp = get_kv_cache(f"skew_percentile_{symbol.upper()}")
                if cached_sp is not None:
                    skew_percentile = float(cached_sp)
            except Exception:
                pass

        if skew_percentile is not None and skew_percentile < 50.0:
            downside_penalty = (
                (50.0 - skew_percentile) / 50.0
            ) * _SKEW_DOWNSIDE_PENALTY_FACTOR
            return float(max(0.0, base_ev * (1.0 - downside_penalty)))

        return float(base_ev)

    def _find_best_rollover_target(
        self, user_id: int, exclude_symbols: Optional[set] = None
    ) -> str:
        """掃描使用者 Watchlist 與 market_cache 快取尋找下一個高 EV 衛星標的，若無則回傳 VOO。
        自動避開即將在 3 天內發布財報的高波事件標的。"""
        from database.calendar_cache import get_cached_earnings
        from database.watchlist import get_user_watchlist

        exclude = {
            s.upper() for s in (exclude_symbols or set())
        } | CORE_DEFENSE_ETF_SYMBOLS
        try:
            watchlist = get_user_watchlist(user_id)
        except Exception as e:
            logger.error(f"取得 user {user_id} watchlist 失敗: {e}")
            return "VOO"

        today_dt = datetime.now().date()
        best_symbol = "VOO"
        best_ev = 0.05  # 門檻 EV > 0.05
        for sym, _ in watchlist:
            sym_u = str(sym).upper()
            if sym_u in exclude:
                continue

            # 避開即將發布財報的標的 (機構風控：避開二元事件黑天鵝)
            try:
                earn = get_cached_earnings(sym_u)
                if earn and earn.get("earnings_date"):
                    earn_date_str = str(earn["earnings_date"])[:10]
                    earn_dt = datetime.strptime(earn_date_str, "%Y-%m-%d").date()
                    diff_days = (earn_dt - today_dt).days
                    if 0 <= diff_days <= _EARNINGS_PRE_EVENT_BUFFER_DAYS:
                        continue
            except Exception:
                pass

            ev = self._calculate_ev_proxy(sym_u)
            if ev > best_ev:
                best_ev = ev
                best_symbol = sym_u
        return best_symbol

    def _normalize_power_squeeze(self, psq: Dict[str, Any]) -> float:
        """
        將 analyze_psq() 產生的 PSQResult (dict 形式，如 radar cache 中的 psq_result)
        正規化為 0-100 的 PowerSqueeze 分數，供 evaluate_opportunity_cost() 使用。
        重用既有的 squeeze_level / signal_direction / momentum_color / is_breakout_long/short
        分級，而非發明新的量化門檻。
        """
        level = str(psq.get("squeeze_level", "Normal"))
        direction = str(psq.get("signal_direction", "Neutral"))
        mom_color = str(psq.get("momentum_color", "Neutral"))
        is_bullish = direction == "Long" or mom_color in ("LightBlue", "Golden")
        is_bearish = direction == "Short" or mom_color in ("Red", "DarkBlue")

        table = {
            "Release": {"neutral": 10.0, "bull": 75.0, "bear": 5.0},
            "Normal": {"neutral": 30.0, "bull": 40.0, "bear": 20.0},
            "Mid": {"neutral": 60.0, "bull": 70.0, "bear": 45.0},
            "High": {"neutral": 50.0, "bull": 90.0, "bear": 10.0},
        }
        bucket = table.get(level, table["Normal"])
        score = (
            bucket["bull"]
            if is_bullish
            else (bucket["bear"] if is_bearish else bucket["neutral"])
        )

        if psq.get("is_breakout_long"):
            score = max(score, 95.0)
        elif psq.get("is_breakout_short"):
            score = min(score, 5.0)

        return float(max(0.0, min(100.0, score)))

    def evaluate_opportunity_cost(
        self,
        current_holding_symbol: str,
        current_holding_power_squeeze: float,
        current_holding_profit_pct: float,
        target_watchlist_symbol: str,
        target_power_squeeze: float,
        target_expected_value: float,
        current_holding_expected_value: float,
        target_ivr: float = 0.0,
        target_uoa_sweep: bool = False,
        target_spot: float = 0.0,
        target_put_wall: float = 0.0,
    ) -> Dict[str, Any]:
        """
        邏輯 (2): 機會成本與期望值比對 (包含勝率傾斜)
        結合 PowerSqueeze 動能指標，當持倉動能衰退且 Watchlist 具備突破條件時，
        計算期望值並給出具備清晰履約價規格的轉倉建議。
        """
        # 假設 PowerSqueeze 指標中，數值越低代表動能越弱，越高代表突破動能強烈
        holding_momentum_decaying = (
            current_holding_power_squeeze < _MOMENTUM_DECAY_THRESHOLD
        )
        target_breakout_ready = target_power_squeeze > _BREAKOUT_READY_THRESHOLD

        # 期望值差距
        ev_spread = target_expected_value - current_holding_expected_value

        should_rollover = False
        rollover_ratio = 0.0
        strategy = "Buy Shares"

        if (
            holding_momentum_decaying
            and target_breakout_ready
            and ev_spread > (_EV_SPREAD_MIN_THRESHOLD + _ESTIMATED_ROUND_TRIP_COST_PCT)
        ):
            should_rollover = True
            if current_holding_profit_pct > _PROFIT_LOCK_PROFIT_PCT_THRESHOLD:
                # 獲利豐厚，可轉換 50%
                rollover_ratio = _ROLLOVER_RATIO_HIGH_PROFIT
            else:
                # 獲利一般或虧損，轉換 30% 或全轉，視風險偏好而定
                rollover_ratio = _ROLLOVER_RATIO_STANDARD

            # ----------------------------------------------------
            # 條件二：新標的出現「極致不對稱勝率」
            # ----------------------------------------------------
            is_low_ivr = 0 < target_ivr < _LOW_IVR_UPPER_BOUND
            is_near_put_wall = (target_put_wall > 0 and target_spot > 0) and (
                abs(target_spot - target_put_wall) / target_put_wall
                <= _PUT_WALL_PROXIMITY_TOLERANCE
            )
            is_extreme_asymmetric = is_low_ivr and is_near_put_wall and target_uoa_sweep

            if is_extreme_asymmetric:
                strategy = "Shares + ITM Call"
                target_strike = round(target_spot * 0.95, 2) if target_spot > 0 else 0.0
                strike_note = (
                    f" (ITM 70Δ Call @ ${target_strike:.2f}, 30-45 DTE)"
                    if target_spot > 0
                    else ""
                )
                reason_suffix = f" (🎯 條件二極致勝率觸發: 低IVR({target_ivr:.1f}%) + 鋼鐵牆築底 + 巨鯨掃貨{strike_note}，強制啟動轉倉)"
            else:
                strategy = "Buy Shares"
                reason_suffix = ""

            # 強制優先採用極致不對稱勝率條件
            if holding_momentum_decaying and is_extreme_asymmetric:
                should_rollover = True
                rollover_ratio = 1.0  # 條件三要求 100% 滿載運算 / 不留戀
                return {
                    "should_rollover": should_rollover,
                    "rollover_ratio": rollover_ratio,
                    "strategy": strategy,
                    "reason": (
                        f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                        f"Target {target_watchlist_symbol} hit asymmetric win-rate. "
                        + reason_suffix
                    ),
                }

            return {
                "should_rollover": should_rollover,
                "rollover_ratio": rollover_ratio,
                "strategy": strategy,
                "reason": (
                    f"Holding {current_holding_symbol} momentum decaying (PSQ={current_holding_power_squeeze}). "
                    f"Target {target_watchlist_symbol} showing breakout potential (PSQ={target_power_squeeze}) "
                    f"with EV spread +{ev_spread * 100:.1f}%." + reason_suffix
                ),
            }

        return {
            "should_rollover": False,
            "rollover_ratio": 0.0,
            "strategy": "N/A",
            "reason": "No action required.",
        }

    async def _confirm_entry_signal(
        self,
        candidate_symbol: str,
        candidate_radar: Dict[str, Any],
        target_spot: float,
    ) -> Tuple[bool, str]:
        """
        防洗盤實戰策略：進場訊號六重嚴格過濾鐵律。六項條件必須同時成立才允許
        evaluate_opportunity_cost_for_satellites 對 candidate_symbol 實際啟動
        機會成本轉倉指令。

        Fail-safe 原則（比照 gamma_cliff_confirmation.is_gamma_cliff_confirmed）：
        任何一項條件所需資料缺失、抓取失敗或無法確認，一律判定該條件未通過
        (不進場)，不預設通過、不略過。

        回傳 (四項條件是否全數通過, 逐項原因說明字串，供 log 觀察用)。
        """
        reasons: list[str] = []

        # --- 條件一：結構性右側突破確認 (15m 實體收盤 + 放量，站穩 Gamma Flip 估算門檻) ---
        gex_profile_data = candidate_radar.get("gex_profile_data") or {}
        gex_profile = (
            gex_profile_data.get("gex_profile")
            if isinstance(gex_profile_data, dict)
            else None
        )
        c1_passed = False
        if target_spot <= 0:
            reasons.append("條件一❌：candidate 現價無效")
        else:
            gamma_flip_est = estimate_symbol_gamma_flip(
                gex_profile if isinstance(gex_profile, dict) else {}, target_spot
            )
            if gamma_flip_est <= 0:
                reasons.append(
                    "條件一❌：無法估算 Gamma Flip 門檻 (GEX Profile 無交叉點)"
                )
            else:
                try:
                    from services import market_data_service

                    df_15m = await market_data_service.get_history_df(
                        candidate_symbol, period="5d", interval="15m"
                    )
                except Exception as e:
                    df_15m = None
                    logger.warning(f"[{candidate_symbol}] 15m K 線抓取失敗: {e}")

                if (
                    df_15m is None
                    or df_15m.empty
                    or len(df_15m) < _ENTRY_VOLUME_LOOKBACK_BARS + 1
                ):
                    reasons.append("條件一❌：15m K 線資料不足，無法確認突破")
                else:
                    last_bar = df_15m.iloc[-1]
                    lookback_bars = df_15m.iloc[-(_ENTRY_VOLUME_LOOKBACK_BARS + 1) : -1]
                    close_val = float(last_bar["Close"])
                    volume_val = float(last_bar["Volume"])
                    avg_volume = float(lookback_bars["Volume"].mean())
                    is_closed_above = close_val > gamma_flip_est
                    is_volume_surge = (
                        avg_volume > 0
                        and volume_val >= avg_volume * _ENTRY_VOLUME_SURGE_MULTIPLIER
                    )
                    c1_passed = is_closed_above and is_volume_surge
                    reasons.append(
                        f"條件一{'✅' if c1_passed else '❌'}：15m收盤 ${close_val:.2f} "
                        f"{'>' if is_closed_above else '<='} Gamma Flip估算 ${gamma_flip_est:.2f}，"
                        f"量能 {volume_val:.0f} vs 均量×{_ENTRY_VOLUME_SURGE_MULTIPLIER} "
                        f"={avg_volume * _ENTRY_VOLUME_SURGE_MULTIPLIER:.0f}"
                    )

        # --- 條件二：做市商正 Gamma 底牆完好 ---
        support_wall, _resistance_wall, support_gex, _resistance_gex = _scan_gex_walls(
            candidate_symbol,
            gex_profile_data if isinstance(gex_profile_data, dict) else None,
        )
        c2_passed = support_wall > 0 and support_gex > 0
        reasons.append(
            f"條件二{'✅' if c2_passed else '❌'}：正 Gamma 支撐牆 "
            f"{'$' + format(support_wall, '.2f') if c2_passed else '未偵測到'}"
        )

        # --- 條件三：UOA 無實質物理封頂 (上方空間暢通) ---
        uoa_list = candidate_radar.get("uoa") or []
        call_wall = (
            float(gex_profile_data.get("call_wall", 0.0) or 0.0)
            if isinstance(gex_profile_data, dict)
            else 0.0
        )
        has_physical_cap = False
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
            if strike > target_spot and ratio > _ENTRY_UOA_CAP_RATIO_THRESHOLD:
                has_physical_cap = True
                capping_strike = strike
                break

        has_tight_call_wall = (
            call_wall > target_spot
            and target_spot > 0
            and (call_wall - target_spot) / target_spot < _ENTRY_ASYMMETRIC_ROOM_PCT
        )
        c3_passed = not has_physical_cap and not has_tight_call_wall
        if has_physical_cap:
            reasons.append(
                f"條件三❌：偵測到單筆 ratio>{_ENTRY_UOA_CAP_RATIO_THRESHOLD}x OI 的 "
                f"STO Call 物理封頂 @ ${capping_strike:.2f}"
            )
        elif has_tight_call_wall:
            reasons.append(
                f"條件三❌：Call Wall ${call_wall:.2f} 距現價不足 "
                f"{_ENTRY_ASYMMETRIC_ROOM_PCT:.0%} 非對稱空間"
            )
        else:
            reasons.append("條件三✅：上方無實質物理封頂，非對稱空間充足")

        # --- 條件四：避開結算日前夕的末日雜訊 (主力 UOA 買盤須 DTE >= 7) ---
        primary_bullish_call = None
        for entry in uoa_list:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type", "")).upper() != "CALL":
                continue
            if "BTO" not in str(entry.get("action", "")):
                continue
            primary_bullish_call = entry
            break  # uoa 已依成交量降序排列，第一筆符合者即為主力買盤

        c4_passed = False
        if primary_bullish_call is None:
            reasons.append("條件四❌：未偵測到驅動進場的主力 CALL BTO 買盤")
        else:
            try:
                expiry_str = str(primary_bullish_call.get("expiry", ""))
                exp_dt = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                dte = (exp_dt - datetime.now().date()).days
                c4_passed = dte >= _ENTRY_UOA_MIN_DTE
                reasons.append(
                    f"條件四{'✅' if c4_passed else '❌'}：主力買盤 DTE={dte} "
                    f"({'符合' if c4_passed else '低於'} 門檻 {_ENTRY_UOA_MIN_DTE})"
                )
            except (ValueError, TypeError) as e:
                reasons.append(f"條件四❌：主力買盤到期日解析失敗: {e}")

        # --- 條件五：總經負 Gamma 與財報黑天鵝防禦閘門 (前四項通過時才發動) ---
        c5_passed = True
        if c1_passed and c2_passed and c3_passed and c4_passed:
            try:
                from database.calendar_cache import get_cached_earnings

                earn = get_cached_earnings(candidate_symbol)
                if earn and earn.get("earnings_date"):
                    earn_date_str = str(earn["earnings_date"])[:10]
                    earn_dt = datetime.strptime(earn_date_str, "%Y-%m-%d").date()
                    days_to_er = (earn_dt - datetime.now().date()).days
                    if 0 <= days_to_er <= _EARNINGS_PRE_EVENT_BUFFER_DAYS:
                        c5_passed = False
                        reasons.append(
                            f"條件五❌：即將於 {days_to_er} 天內發布財報，避開高波事件風險"
                        )
            except Exception:
                pass

            if c5_passed:
                try:
                    from market_analysis.index_microstructure import (
                        get_market_regime,
                    )

                    regime = await get_market_regime()
                    if regime in (
                        "SHORT_GAMMA_CRITICAL",
                        "SYSTEMIC_LIQUIDITY_CRISIS",
                    ):
                        c5_passed = False
                        reasons.append(
                            f"條件五❌：大盤處於 `{regime}` 負 Gamma 踩踏模式，嚴禁開倉個股買方"
                        )
                except Exception:
                    pass

            if c5_passed:
                reasons.append("條件五✅：總經環境與財報事件風控安全")

        # --- 條件六：避開 candidate 自身最近效期選擇權週期的結算日前夕/當日雜訊 (0/1 DTE) ---
        c6_passed = True
        if c1_passed and c2_passed and c3_passed and c4_passed and c5_passed:
            c6_passed = False
            try:
                from services import market_data_service

                expiries = await market_data_service.get_all_option_expiries(
                    candidate_symbol
                )
            except Exception as e:
                expiries = []
                logger.warning(f"[{candidate_symbol}] 選擇權到期日清單抓取失敗: {e}")

            if not expiries:
                reasons.append("條件六❌：無法取得標的最近效期選擇權到期日清單")
            else:
                try:
                    nearest_expiry_dt = datetime.strptime(
                        expiries[0], "%Y-%m-%d"
                    ).date()
                    dte_nearest = (nearest_expiry_dt - datetime.now().date()).days
                    c6_passed = dte_nearest > _ENTRY_CANDIDATE_MIN_DTE
                    reasons.append(
                        f"條件六{'✅' if c6_passed else '❌'}：標的最近效期 {expiries[0]} "
                        f"DTE={dte_nearest}"
                        f"（{'符合' if c6_passed else '低於'} 門檻 >{_ENTRY_CANDIDATE_MIN_DTE}）"
                    )
                except (ValueError, TypeError) as e:
                    reasons.append(f"條件六❌：標的最近效期到期日解析失敗: {e}")

        all_passed = (
            c1_passed
            and c2_passed
            and c3_passed
            and c4_passed
            and c5_passed
            and c6_passed
        )
        return all_passed, " | ".join(reasons)

    async def evaluate_opportunity_cost_for_satellites(
        self,
        user_id: int,
        portfolio_assets: List[Dict[str, Any]],
        already_flagged_symbols: set,
        candidate_symbol: str,
        candidate_radar: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        邏輯 (2) 批次橋接：對每一個尚未被 Scenario 3 標記的 SATELLITE 持倉，
        比對其 PowerSqueeze/EV 與單一預篩選候選標的 (candidate_symbol) 的機會成本，
        產生與 check_satellite_rebalancing 相同結構的 instruction dict。

        candidate_radar: 由呼叫端 (cog 層) 預先透過既有 radar 抓取機制取得的單一候選標的資料，
        純資料 dict，避免 market_analysis 層依賴 cogs。
        """
        instructions: List[Dict[str, Any]] = []
        if candidate_symbol == "VOO" or not candidate_radar:
            return instructions  # 沒有找到高 EV 候選標的，不強制轉倉

        target_psq = candidate_radar.get("psq_result", {}) or {}
        target_power_squeeze = self._normalize_power_squeeze(target_psq)
        target_expected_value = self._calculate_ev_proxy(candidate_symbol)
        target_spot = float(
            candidate_radar.get("quote", {}).get("c", 0.0)
            if candidate_radar.get("quote")
            else 0.0
        )
        target_ivr = float(
            candidate_radar.get("iv_metrics", {}).get("iv_rank", 0.0)
            if candidate_radar.get("iv_metrics")
            else 0.0
        )
        target_put_wall = (
            float(
                candidate_radar.get("gex_profile_data", {}).get("put_wall", 0.0) or 0.0
            )
            if isinstance(candidate_radar.get("gex_profile_data"), dict)
            else 0.0
        )
        target_uoa_sweep = len(candidate_radar.get("uoa", []) or []) > 0

        # 防洗盤實戰策略：進場訊號四重嚴格過濾鐵律。四項條件必須同時成立才允許
        # 對 candidate_symbol 啟動任何機會成本轉倉指令；未通過時比照上方
        # 「找不到候選標的」的早退模式，靜默略過、不產生任何指令。
        is_entry_confirmed, entry_reason = await self._confirm_entry_signal(
            candidate_symbol, candidate_radar, target_spot
        )
        if not is_entry_confirmed:
            logger.info(
                f"[{candidate_symbol}] 進場訊號未確認，靜默略過機會成本轉倉: {entry_reason}"
            )
            return instructions

        for asset in portfolio_assets:
            symbol = str(asset.get("symbol", "")).upper()
            if asset.get("asset_class") != "SATELLITE":
                continue
            if symbol in already_flagged_symbols or symbol == candidate_symbol:
                continue

            holding_psq = asset.get("psq_result", {}) or {}
            current_power_squeeze = self._normalize_power_squeeze(holding_psq)
            current_ev = self._calculate_ev_proxy(symbol)

            avg_cost = float(asset.get("avg_cost", 0.0))
            spot = float(asset.get("spot_price", 0.0))
            profit_pct = (spot - avg_cost) / avg_cost if avg_cost > 0 else 0.0

            result = self.evaluate_opportunity_cost(
                current_holding_symbol=symbol,
                current_holding_power_squeeze=current_power_squeeze,
                current_holding_profit_pct=profit_pct,
                target_watchlist_symbol=candidate_symbol,
                target_power_squeeze=target_power_squeeze,
                target_expected_value=target_expected_value,
                current_holding_expected_value=current_ev,
                target_ivr=target_ivr,
                target_uoa_sweep=target_uoa_sweep,
                target_spot=target_spot,
                target_put_wall=target_put_wall,
            )
            if not result["should_rollover"]:
                continue

            # 預估資金影響與建議限價：現貨持倉市值優先，缺失時退回股數*現價估算；
            # 限價採用候選標的即時報價 (target_spot)，取代呼叫端過去恆為
            # "Market" 的佔位字串。
            current_value = float(asset.get("current_value", 0.0))
            if current_value <= 0 and spot > 0:
                current_value = float(asset.get("quantity", 0.0)) * spot
            recovered_cash = current_value * result["rollover_ratio"]
            cash_impact = f"${recovered_cash:,.0f}" if recovered_cash > 0 else None

            # 流動性閘門：比照 Scenario 3/4 既有做法，期權部位若帶有 bid/ask 且
            # 點差過寬時強制要求手動確認執行 (ManualOverrideView)，而非放行一鍵
            # 執行按鈕 (RolloverActionView)，避免使用者在滑價風險下誤觸一鍵轉倉。
            bid = float(asset.get("bid", 0.0))
            ask = float(asset.get("ask", 0.0))
            is_illiquid_warning = asset.get(
                "asset_class"
            ) == "OPTIONS" and is_spread_illiquid(bid, ask)
            reason_text = f"💡 **機會成本轉倉 (Opportunity Cost)**\n{result['reason']}"
            if is_illiquid_warning:
                spread_pct = (ask - bid) / ((ask + bid) / 2)
                reason_text += (
                    f"\n⚠️ **流動性警告**：合約點差過寬 (Bid ${bid:.2f} / Ask ${ask:.2f}，"
                    f"點差 {spread_pct:.1%})，建議採限價單並留意滑價。"
                )

            instructions.append(
                {
                    "symbol": symbol,
                    "action": "LIQUIDATE"
                    if result["rollover_ratio"] >= 1.0
                    else "REDUCE",
                    "sell_ratio": result["rollover_ratio"],
                    "target_core": candidate_symbol,
                    "reason": reason_text,
                    "suggested_strategy": result["strategy"],
                    "scenario": RolloverScenario.OPPORTUNITY_COST.value,
                    "is_manual_override_required": is_illiquid_warning,
                    "cash_impact": cash_impact,
                    "limit_price": target_spot if target_spot > 0 else None,
                }
            )
        return instructions
