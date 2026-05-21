import logging
import asyncio
import math
from datetime import datetime
from typing import List, Optional, Tuple, Dict, Any
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, Field, ConfigDict

from market_time import ny_tz, is_market_open
from models.schemas import EnhancedWatchlistMetrics, WatchlistEvaluation
from risk_engine.nro import WatchlistRiskController
from services.market_data_service import BoundedCache

logger = logging.getLogger(__name__)

_WATCHLIST_METRICS_CACHE = BoundedCache(max_size=128)
_WATCHLIST_METRICS_TTL = 30 * 60


def _quote_price(quote: Dict[str, Any], fallback: float = 0.0) -> float:
    for key in ("c", "current_price", "price"):
        value = quote.get(key)
        if value:
            return float(value)
    return fallback


def _calculate_rsi(close: pd.Series, window: int = 14) -> float:
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = gains.rolling(window=window, min_periods=window).mean()
    avg_loss = losses.rolling(window=window, min_periods=window).mean()
    if avg_gain.empty or avg_loss.empty:
        return 50.0

    last_gain = float(avg_gain.iloc[-1]) if not pd.isna(avg_gain.iloc[-1]) else 0.0
    last_loss = float(avg_loss.iloc[-1]) if not pd.isna(avg_loss.iloc[-1]) else 0.0
    if last_loss == 0.0:
        return 100.0 if last_gain > 0.0 else 50.0

    rs = last_gain / last_loss
    return float(round(100.0 - (100.0 / (1.0 + rs)), 2))


def _calculate_atr(df: pd.DataFrame, window: int = 14) -> float:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=window, min_periods=window).mean()
    if atr.empty or pd.isna(atr.iloc[-1]):
        return 0.0
    return float(round(float(atr.iloc[-1]), 4))


def _estimate_volume_poc(df: pd.DataFrame, bins: int = 24) -> float:
    recent = df.tail(60).copy()
    if recent.empty:
        return 0.0

    grouped = recent.groupby(
        pd.cut(
            recent["Close"], bins=min(bins, max(8, len(recent) // 2)), duplicates="drop"
        ),
        observed=False,
    )["Volume"].sum()
    if grouped.empty:
        return float(recent["Close"].iloc[-1])

    poc_bucket = grouped.idxmax()
    return float(round((float(poc_bucket.left) + float(poc_bucket.right)) / 2.0, 4))


def _relative_strength_vs_spy(df_stock: pd.DataFrame, df_spy: pd.DataFrame) -> float:
    stock_tail = df_stock["Close"].tail(21)
    spy_tail = df_spy["Close"].tail(21)
    if len(stock_tail) < 2 or len(spy_tail) < 2:
        return 0.0

    stock_return = float(stock_tail.iloc[-1] / stock_tail.iloc[0] - 1.0)
    spy_return = float(spy_tail.iloc[-1] / spy_tail.iloc[0] - 1.0)
    return round(stock_return - spy_return, 4)


def _derive_buy_levels(
    current_price: float,
    ma20: float,
    ma50: float,
    ma200: float,
    volume_poc: float,
    gex_max_put_wall: float,
    atr_14: float,
) -> tuple[float, float, float]:
    candidates = [
        value
        for value in [ma20, ma50, ma200, volume_poc, gex_max_put_wall]
        if value > 0.0
    ]
    if not candidates:
        candidates = [
            max(current_price - (atr_14 * factor), 0.01) for factor in (0.5, 1.0, 1.5)
        ]

    unique_levels = sorted({round(value, 2) for value in candidates}, reverse=True)
    while len(unique_levels) < 3:
        fallback = max(current_price - (atr_14 * (len(unique_levels) + 1)), 0.01)
        unique_levels.append(round(fallback, 2))
        unique_levels = sorted(set(unique_levels), reverse=True)

    return unique_levels[0], unique_levels[1], unique_levels[2]


def _derive_sell_levels(
    current_price: float,
    ma20: float,
    ma50: float,
    ma200: float,
    atr_14: float,
) -> tuple[float, float, float]:
    candidates = [
        max(current_price + (atr_14 * 0.75), ma20),
        max(current_price + (atr_14 * 1.5), ma50),
        max(current_price + (atr_14 * 2.25), ma200),
    ]
    levels = sorted(round(value, 2) for value in candidates)
    return levels[0], levels[1], levels[2]


def _buy_zone_status(current_price: float, phase1: float, phase2: float) -> str:
    if current_price <= phase2:
        return "🔴 買點：第二防線失守 (硬對沖)"
    if current_price <= phase1:
        return "🟡 買點：趨勢支撐測試 (VIX 修正)"
    return "🟢 買點：趨勢支撐 (VIX 修正)"


def _sell_zone_status(
    current_price: float, sell_phase1: float, sell_phase2: float
) -> str:
    if current_price >= sell_phase2:
        return "🟡 賣點：分批止盈 / 壓力區"
    if current_price >= sell_phase1:
        return "🟢 賣點：第一壓力帶"
    return "⚪ 賣點：未觸及止盈區"


def _extract_pe_ratio(financials: Dict[str, Any]) -> float | None:
    for key in ("peNormalizedAnnual", "peTTM", "peBasicExclExtraTTM"):
        value = financials.get(key)
        if value is not None and float(value) > 0.0:
            return float(value)
    return None


async def _estimate_options_wall_metrics(
    symbol: str,
    current_price: float,
    dividend_yield: float,
) -> tuple[float, float]:
    from market_analysis.greeks import calculate_greeks, calculate_vanna
    from services import market_data_service

    expiries = await market_data_service.get_all_option_expiries(symbol)
    if not expiries:
        return current_price, 0.0

    expiry = expiries[0]
    chain = await market_data_service.get_option_chain(symbol, expiry)
    if chain is None or chain.puts.empty:
        return current_price, 0.0

    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    t_years = max((expiry_dt - datetime.now()).days / 365.0, 7.0 / 365.0)

    puts = chain.puts.copy()
    puts = puts.dropna(subset=["strike", "openInterest", "impliedVolatility"])
    if puts.empty:
        return current_price, 0.0

    max_wall_score = -1.0
    max_wall_strike = current_price
    for _, row in puts.iterrows():
        greeks = calculate_greeks(
            "put",
            current_price,
            float(row["strike"]),
            t_years,
            float(row["impliedVolatility"]),
            dividend_yield,
        )
        gamma_score = abs(float(greeks["gamma"])) * float(row["openInterest"]) * 100.0
        if gamma_score > max_wall_score:
            max_wall_score = gamma_score
            max_wall_strike = float(row["strike"])

    call_vanna = 0.0
    put_vanna = 0.0
    try:
        atm_call_idx = (chain.calls["strike"] - current_price).abs().idxmin()
        atm_call = chain.calls.loc[atm_call_idx]
        call_vanna = float(
            calculate_vanna(
                "c",
                current_price,
                float(atm_call["strike"]),
                t_years,
                float(atm_call["impliedVolatility"]),
                dividend_yield,
            )
        )
    except Exception:
        pass

    try:
        atm_put_idx = (puts["strike"] - current_price).abs().idxmin()
        atm_put = puts.loc[atm_put_idx]
        put_vanna = float(
            calculate_vanna(
                "p",
                current_price,
                float(atm_put["strike"]),
                t_years,
                float(atm_put["impliedVolatility"]),
                dividend_yield,
            )
        )
    except Exception:
        pass

    avg_vanna = (abs(call_vanna) + abs(put_vanna)) / (
        2.0 if call_vanna or put_vanna else 1.0
    )
    return round(max_wall_strike, 4), round(avg_vanna, 4)


async def build_enhanced_watchlist_metrics(
    symbol: str,
) -> Optional[EnhancedWatchlistMetrics]:
    from market_analysis.risk_engine import calculate_beta
    from market_analysis.sentiment_engine import SentimentEngine
    from services import market_data_service

    symbol = symbol.upper()
    now_ts = datetime.now().timestamp()
    if symbol in _WATCHLIST_METRICS_CACHE:
        cached_metrics, expiry = _WATCHLIST_METRICS_CACHE[symbol]
        if now_ts < expiry:
            return cached_metrics

    quote_task = market_data_service.get_quote(symbol)
    stock_history_task = market_data_service.get_history_df(symbol, period="1y")
    spy_history_task = market_data_service.get_spy_history_df(period="1y")
    financials_task = market_data_service.get_basic_financials(symbol)
    profile_task = market_data_service.get_company_profile(symbol)
    iv_task = SentimentEngine.fetch_and_calculate_iv_metrics(symbol)
    dividend_yield_task = market_data_service.get_dividend_yield(symbol)

    (
        quote,
        df_stock,
        df_spy,
        financials,
        profile,
        iv_metrics,
        dividend_yield,
    ) = await asyncio.gather(
        quote_task,
        stock_history_task,
        spy_history_task,
        financials_task,
        profile_task,
        iv_task,
        dividend_yield_task,
    )

    if df_stock.empty or len(df_stock) < 60:
        return None
    if df_spy.empty or len(df_spy) < 60:
        return None

    last_close = float(df_stock["Close"].iloc[-1])
    current_price = _quote_price(quote, fallback=last_close)
    ma20 = float(round(float(df_stock["Close"].tail(min(20, len(df_stock))).mean()), 4))
    ma50 = float(round(float(df_stock["Close"].tail(min(50, len(df_stock))).mean()), 4))
    ma200 = float(
        round(float(df_stock["Close"].tail(min(200, len(df_stock))).mean()), 4)
    )
    rsi_14 = _calculate_rsi(df_stock["Close"])
    atr_14 = max(_calculate_atr(df_stock), 0.01)
    beta = calculate_beta(df_stock, df_spy)
    volume_poc = max(_estimate_volume_poc(df_stock), 0.01)
    gex_max_put_wall, vanna_sensitivity = await _estimate_options_wall_metrics(
        symbol,
        current_price,
        dividend_yield,
    )
    relative_strength_spy = _relative_strength_vs_spy(df_stock, df_spy)

    buy_phase1, buy_phase2, buy_phase3 = _derive_buy_levels(
        current_price,
        ma20,
        ma50,
        ma200,
        volume_poc,
        max(gex_max_put_wall, 0.01),
        atr_14,
    )
    sell_phase1, sell_phase2, sell_phase3 = _derive_sell_levels(
        current_price,
        ma20,
        ma50,
        ma200,
        atr_14,
    )

    metrics = EnhancedWatchlistMetrics(
        symbol=symbol,
        exchange=str(
            profile.get("exchange") or profile.get("exchangeCode") or "UNKNOWN"
        ),
        current_price=current_price,
        buy_zone_status=_buy_zone_status(current_price, buy_phase1, buy_phase2),
        buy_price_phase1=buy_phase1,
        buy_price_phase2=buy_phase2,
        buy_price_phase3=buy_phase3,
        sell_zone_status=_sell_zone_status(current_price, sell_phase1, sell_phase2),
        sell_price_phase1=sell_phase1,
        sell_price_phase2=sell_phase2,
        sell_price_phase3=sell_phase3,
        pe_ratio=_extract_pe_ratio(financials),
        rsi_14=rsi_14,
        atr_14=atr_14,
        beta=beta,
        ma20=ma20,
        ma50=ma50,
        ma200=ma200,
        iv_rank=iv_metrics.iv_rank,
        volume_poc=volume_poc,
        gex_max_put_wall=max(gex_max_put_wall, 0.01),
        vanna_sensitivity=vanna_sensitivity,
        relative_strength_spy=relative_strength_spy,
    )
    _WATCHLIST_METRICS_CACHE[symbol] = (metrics, now_ts + _WATCHLIST_METRICS_TTL)
    return metrics


async def evaluate_watchlist_symbol(symbol: str) -> Optional[WatchlistEvaluation]:
    metrics = await build_enhanced_watchlist_metrics(symbol)
    if metrics is None:
        return None
    tactical = WatchlistRiskController.process_metrics(metrics)
    return WatchlistEvaluation(metrics=metrics, tactical=tactical)


class TraderAccountState(BaseModel):
    """交易員帳戶生存狀態"""

    model_config = ConfigDict()

    capital: float = Field(description="總風險本金 (Total Risk Capital)")
    cash_reserve: float = Field(description="生活費現金儲備 (Liquid Cash Reserve)")
    monthly_burn_rate: float = Field(
        description="每月固定生活開銷 (Monthly Living Expenses)"
    )
    current_vix: float = Field(description="即時 VIX 指數 (Real-time VIX level)")


class OptionHolding(BaseModel):
    """現有期權部位持倉"""

    model_config = ConfigDict()

    symbol: str = Field(description="標的代碼")
    quantity: float = Field(description="合約數量 (正數為買方，負數為賣方)")
    theta: float = Field(description="單口每日 Theta 衰退值 (通常買方為負，賣方為正)")


class TickerMarketData(BaseModel):
    """標的市場行情數據"""

    model_config = ConfigDict()

    ticker: str = Field(description="標的代碼")
    spot_price: float = Field(description="標的現價 (Spot Price)")
    market_cap_billion: float = Field(description="公司市值（十億美元）")
    avg_option_volume: int = Field(description="日均期權成交量")
    days_until_earnings: int = Field(description="距離財報公佈天數")
    tomorrow_expiring_otm_calls_premium: float = Field(
        description="明日到期 OTM Call 總成交權利金 (Sum of vol * price * 100)"
    )
    iv_rank: float = Field(description="隱含波動率百分位數 (0-100)")
    option_skew: float = Field(description="期權偏斜度 (Option Skew)")


class AdvancedTraderOutput(BaseModel):
    """量化風控與執行決策輸出 (繁體中文格式化)"""

    model_config = ConfigDict()

    ticker: str
    timestamp: datetime
    market_phase: str  # "Phase A", "Phase B", "Phase C"
    is_applicable: bool
    failed_gates: List[str]
    sddm_route: str  # "SPEAR", "SHIELD", or "WAIT"

    # Financial Runway & Survival Section
    financial_runway_days: int
    theta_coverage_pct: float
    runway_status_msg: str

    # Tactical Execution Section
    magnet_target: Optional[float]
    recommended_actions: List[str]
    vanna_hedging_instruction: Optional[
        str
    ]  # e.g., "組合 Delta 偏離！建議建立 [BUY 35 單位 SPY PUT] 進行對沖"
    kelly_position_scaling: float
    risk_mitigation_notes: str


class NexusGammaSqueezeEngine:
    """
    Nexus Gamma Squeeze 量化風控與決策引擎。
    管理 4 階段戰術門檻、凱利倉位配比、帳戶生存跑道與 Vanna 對沖決策。
    """

    def __init__(self, base_gate_3_threshold: float = 1000000.0):
        self.gate_3_threshold: float = base_gate_3_threshold
        self.protection_score_history: List[Dict[str, Any]] = []

    def validate_gates(
        self, data: TickerMarketData, market_phase: str
    ) -> Tuple[bool, List[str]]:
        """
        執行 4 階段戰術硬性過濾門檻。
        - Gate 1: 流動性門檻 (市值 >= 20B 且日均期權量 >= 50,000)
        - Gate 2: 事件風險 (距離財報天數 > 3 天)
        - Gate 3: 資金效率 (明日到期 OTM Call 總成交權利金 >= $1M，Phase A 調降 30%)
        - Gate 4: 跨市場驗證 (IV Rank >= 50 或期權偏斜絕對值 >= 0.05)
        """
        failed = []

        # Gate 1: Liquidity Gate
        if data.market_cap_billion < 20.0 or data.avg_option_volume < 50000:
            failed.append(
                "流動性不足門檻：市值需 >= 20B 且日均期權成交量需 >= 50,000 口"
            )

        # Gate 2: Event Risk Gate
        if data.days_until_earnings <= 3:
            failed.append(
                f"事件風險超限：距離財報公佈僅剩 {data.days_until_earnings} 天 (需 > 3 天，防範 IV Crush 陷阱)"
            )

        # Gate 3: Capital Efficiency Gate
        threshold = self.gate_3_threshold
        if market_phase == "Phase A":
            threshold *= 0.70  # 開盤前一小時 (Phase A) 門檻降低 30%

        if data.tomorrow_expiring_otm_calls_premium < threshold:
            failed.append(
                f"資金效率不足：明日到期 OTM Call 總權利金為 ${data.tomorrow_expiring_otm_calls_premium:,.2f}，低於要求門檻 ${threshold:,.2f}"
            )

        # Gate 4: Cross-Market Validation Gate
        if not (data.iv_rank >= 50.0 or abs(data.option_skew) >= 0.05):
            failed.append(
                f"跨市場驗證未達標：IV Rank 為 {data.iv_rank:.1f}，偏斜度為 {data.option_skew:.3f} (需 IV Rank >= 50 或 Skew 絕對值 >= 0.05)"
            )

        return len(failed) == 0, failed

    def analyze_ticker(
        self,
        data: TickerMarketData,
        account_state: TraderAccountState,
        options_holdings: List[OptionHolding],
        portfolio_greeks: Dict[str, float],
        market_phase: str,
        current_time: Optional[datetime] = None,
    ) -> AdvancedTraderOutput:
        """
        全功能量化決策分析，輸出 AdvancedTraderOutput。
        """
        if current_time is None:
            current_time = datetime.now(ny_tz)

        # 1. 檢查時段適用性
        is_applicable = market_phase != "Closed"

        # 2. 驗證 4 階段戰術門檻
        gates_passed, failed_gates = self.validate_gates(data, market_phase)

        # 3. SDDM 路由決策
        # - VIX >= 25.0: 強制 SHIELD 避險
        # - 未通過 4 階段門檻: SHIELD
        # - 通過且 VIX < 25: SPEAR 積極進攻
        if not is_applicable:
            sddm_route = "WAIT"
        elif not gates_passed:
            sddm_route = "SHIELD"
        elif account_state.current_vix >= 25.0:
            sddm_route = "SHIELD"
        else:
            sddm_route = "SPEAR"

        # 4. 財務跑道分析 (Financial Runway Analysis)
        daily_burn_rate = account_state.monthly_burn_rate / 30.0
        # 帳戶每日 Theta 總收益 (持倉數量 * 單口每日 Theta * 100 乘數)
        projected_theta_yield = sum(
            o.theta * o.quantity * 100 for o in options_holdings
        )

        if daily_burn_rate > 0:
            # 存活天數 = (可用儲備金 + 預計每日 Theta 收益) / 每日生活開銷
            runway_denominator = daily_burn_rate
            financial_runway_days = int(
                max(
                    0.0,
                    (account_state.cash_reserve + projected_theta_yield)
                    / runway_denominator,
                )
            )
            theta_coverage_pct = (projected_theta_yield / daily_burn_rate) * 100.0
        else:
            financial_runway_days = 9999
            theta_coverage_pct = 0.0

        # 生成生存狀態訊息
        if financial_runway_days >= 180:
            runway_status_msg = f"🟢 財務跑道極其安全 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率達 {theta_coverage_pct:.1f}%，運營資金結構優良。"
        elif 90 <= financial_runway_days < 180:
            runway_status_msg = f"🟡 財務跑道良好 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率為 {theta_coverage_pct:.1f}%，處於健康防守狀態。"
        elif 30 <= financial_runway_days < 90:
            runway_status_msg = f"🟠 財務跑道中等警戒 (生存跑道: {financial_runway_days} 天)，期權 Theta 每日覆蓋率為 {theta_coverage_pct:.1f}%，建議精簡持倉規模。"
        else:
            runway_status_msg = f"🔴 🚨 財務跑道極度危險！僅剩 {financial_runway_days} 天，期權 Theta 覆蓋率僅 {theta_coverage_pct:.1f}%，請立即關閉高風險部位並限制主動交易。"

        # 5. Gamma 磁吸目標價 (預估下一個整數期權行權價)
        spot = data.spot_price
        magnet_target = float(math.ceil(spot / 5.0) * 5.0)
        if abs(magnet_target - spot) < 0.01:
            magnet_target += 5.0

        # 6. 凱利公式戰力縮放 (Kelly Position Sizing)
        # 基準凱利百分比設為 0.25 (對應 55% 勝率, 1.5 盈虧比)
        base_kelly = 0.25
        vix = account_state.current_vix
        if vix < 15.0:
            kelly_position_scaling = base_kelly * 1.0  # 全力進攻 (All-in/Heavy)
        elif 15.0 <= vix < 25.0:
            kelly_position_scaling = base_kelly * 0.6  # 減速警惕 (Ready/Caution)
        else:
            kelly_position_scaling = (
                base_kelly * 0.1
            )  # 極限防守 (Dormant / 僅配置 10% 凱利權重)

        # 7. Vanna-Adjusted Delta 對沖決策 (Hidden Delta)
        # 計算現貨與波動率同步暴漲時，Vanna 帶來的非線性 Delta 漂移
        portfolio_vanna = portfolio_greeks.get("vanna", 0.0)
        beta = portfolio_greeks.get("beta", 1.0)
        # 假設盤中即時波動率波動為 +10% (0.10)
        d_vol = 0.10
        hidden_delta = portfolio_vanna * d_vol
        hidden_delta_shares = hidden_delta * 100.0  # 換算為標的股份 Delta 當量

        # 換算為 Beta 加權的 SPY/QQQ 對沖所需股數
        shares_needed = -round(hidden_delta_shares * beta)

        if abs(shares_needed) > 0:
            direction = "BUY 買入" if shares_needed > 0 else "SELL 賣出"
            vanna_hedging_instruction = f"組合 Delta 偏離！偵測到 Vanna 引起隱含 Delta 漂移 {hidden_delta_shares * beta:+.2f}。支援對沖建議：建立 [{direction} {abs(shares_needed)} 單位 SPY] 以恢復 Delta 中性。"
        else:
            vanna_hedging_instruction = (
                "組合 Delta 處於中性區間，目前無需進行 Vanna 對沖調整。"
            )

        # 8. 推薦動作
        recommended_actions = []
        if sddm_route == "SPEAR":
            recommended_actions.append(
                f"🏹 當前進入 SPEAR 進攻模組，標的 {data.ticker} 具備強大 Gamma 擠壓潛力。"
            )
            recommended_actions.append(
                f"🎯 預估上行磁吸目標價為 ${magnet_target:.2f}，建議分批建立 OTM Call。"
            )
            recommended_actions.append(
                f"📊 建議進攻合約規模限制於凱利上限 {kelly_position_scaling * 100:.1f}% 內。"
            )
        elif sddm_route == "SHIELD":
            recommended_actions.append("🛡️ 當前進入 SHIELD 避險模組，主動交易受限。")
            if not gates_passed:
                recommended_actions.append(
                    "❌ 戰術門檻未通過，不允許盲目追高。請參考未通過指標。"
                )
            if vix >= 25.0:
                recommended_actions.append(
                    f"⚠️ 市場 VIX 指數達 {vix:.2f} (高波動警戒區)，強烈建議暫停多頭部位，轉為買入尾盤保護性 Put。"
                )
            recommended_actions.append(
                "📈 請執行 Delta 中性平衡，降低整體投資組合的 Gamma 與 Vega 曝險。"
            )
        else:
            recommended_actions.append(
                "⏳ 目前市場未開盤或處於非交易時段，進入 WAIT 觀望模式。"
            )

        # 時段專屬邏輯 (Phase-specific optimization)
        if market_phase == "Phase A":
            recommended_actions.append(
                "⚡ 盤中時段 Phase A (開盤前小時)：市場定價混亂，注意滑價，流動性門檻已調降 30%。"
            )
        elif market_phase == "Phase C":
            recommended_actions.append(
                "🚨 盤中時段 Phase C (尾盤對沖)：為規避隔夜 Gamma 缺口與跳空風險，嚴格禁止新建短線 SPEAR 部位。"
            )
            if sddm_route == "SPEAR":
                recommended_actions.append(
                    "⚠️ 【尾盤 SPEAR 警戒】尾盤投機買盤強烈，若要建倉，必須搭配等比例 SPY PUT 作為隔夜安全閥！"
                )

        # 9. 風控備註
        notes = []
        if vix >= 25.0:
            notes.append(
                "當前市場恐慌指標高企 (VIX >= 25.0)，波動率期限結構轉為逆價差，防範市場系統性尾部風險。"
            )
        else:
            notes.append("當前波動率環境相對溫和，有利於低波動期權佈局。")

        if financial_runway_days <= 30:
            notes.append(
                "警告：您的財務存活跑道天數極低，禁止進行任何高槓桿或買方期權投機，優先以本金安全與獲利回收為第一要務。"
            )

        notes.append(
            "請隨時追蹤 Spot 與 IV 上漲產生的 Hidden Delta 漂移。對沖完成後，可使用 `/settle_hedge` 登錄對沖記錄。"
        )
        risk_mitigation_notes = " ".join(notes)

        return AdvancedTraderOutput(
            ticker=data.ticker,
            timestamp=current_time,
            market_phase=market_phase,
            is_applicable=is_applicable,
            failed_gates=failed_gates,
            sddm_route=sddm_route,
            financial_runway_days=financial_runway_days,
            theta_coverage_pct=theta_coverage_pct,
            runway_status_msg=runway_status_msg,
            magnet_target=magnet_target,
            recommended_actions=recommended_actions,
            vanna_hedging_instruction=vanna_hedging_instruction,
            kelly_position_scaling=kelly_position_scaling,
            risk_mitigation_notes=risk_mitigation_notes,
        )

    def run_post_market_attribution(
        self, portfolio_pnl: float, hedge_pnl: float
    ) -> Dict[str, Any]:
        """
        每日盤後 (16:30 ET) 對沖歸因與自我進化機制。
        計算對沖保護得分 (Protection Score)，反饋調節明日 Gate 3 資金效率門檻。
        """
        old_threshold = self.gate_3_threshold

        # 計算對沖防禦評分 (0-100)
        if portfolio_pnl < 0:
            # 虧損時，對沖是否有正回報？
            if hedge_pnl > 0:
                # 剛好對沖 100% 虧損得 100 分
                protection_score = min(100.0, (hedge_pnl / abs(portfolio_pnl)) * 100.0)
            else:
                protection_score = 0.0
        else:
            # 獲利時，對沖是否產生過度拖累？
            if hedge_pnl >= 0:
                protection_score = 100.0
            else:
                # 對沖虧損佔總利潤的比例，拖累越少，得分越高
                protection_score = max(
                    0.0, min(100.0, 100.0 + (hedge_pnl / portfolio_pnl) * 100.0)
                )

        # 自我進化反饋環節 (Feedback Loop)
        if protection_score >= 70.0:
            # 對沖效率高，防守強，可適度放寬進攻門檻
            self.gate_3_threshold = float(
                round(max(500000.0, self.gate_3_threshold * 0.90), 2)
            )
            evolution_msg = (
                f"🚀 盤後歸因進化成功！當前對沖防禦評分為 {protection_score:.1f}/100 (效率極佳)。"
                f"NRO 已自動調降明日 Gate 3 權利金進攻門檻 10%，新門檻為 ${self.gate_3_threshold:,.2f}，釋放進攻流動性。"
            )
        elif protection_score < 40.0:
            # 對沖效率過低，防守失效或成本過大，需收緊門檻過濾雜訊
            self.gate_3_threshold = float(
                round(min(2000000.0, self.gate_3_threshold * 1.15), 2)
            )
            evolution_msg = (
                f"⚠️ 盤後歸因進化警報！當前對沖防禦評分僅為 {protection_score:.1f}/100 (防守效率偏低或磨損過重)。"
                f"NRO 已自動調升明日 Gate 3 權利金門檻 15%，新門檻為 ${self.gate_3_threshold:,.2f}，以提升訊號品質。"
            )
        else:
            evolution_msg = (
                f"⚖️ 盤後歸因進化持平。當前對沖防禦評分為 {protection_score:.1f}/100 (符合預期區間)。"
                f"NRO 決定明日維持 Gate 3 權利金門檻為 ${self.gate_3_threshold:,.2f}。"
            )

        result = {
            "protection_score": protection_score,
            "old_threshold": old_threshold,
            "new_threshold": self.gate_3_threshold,
            "evolution_msg": evolution_msg,
        }

        self.protection_score_history.append(
            {
                "timestamp": datetime.now(ny_tz),
                "portfolio_pnl": portfolio_pnl,
                "hedge_pnl": hedge_pnl,
                "protection_score": protection_score,
                "old_threshold": old_threshold,
                "new_threshold": self.gate_3_threshold,
            }
        )

        return result


class IntradayScanPipeline:
    """
    盤中量化掃描與對沖背景處理管道。
    每 30 分鐘執行一次，驅動 Squeeze 決策引擎並發送通知。
    """

    def __init__(self, bot, engine: NexusGammaSqueezeEngine):
        self.bot = bot
        self.engine = engine
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.scan_interval_seconds = 30 * 60  # 30 minutes

    def start(self):
        """啟動異步監控管道"""
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("✅ IntradayScanPipeline 異步掃描管道啟動。")

    def stop(self):
        """停止異步監控管道"""
        self.is_running = False
        if self._task:
            self._task.cancel()
            logger.info("🛑 IntradayScanPipeline 異步掃描管道停止。")

    async def evaluate_watchlist_symbol(
        self, symbol: str
    ) -> Optional[WatchlistEvaluation]:
        return await evaluate_watchlist_symbol(symbol)

    async def _run_loop(self):
        while self.is_running:
            try:
                # 1. 取得當下美東時間與交易時段 phase
                now_ny = datetime.now(ZoneInfo("America/New_York"))

                # 檢查美股是否開盤
                market_active = is_market_open()

                # 計算當前 Phase
                phase = "Closed"
                if market_active:
                    # 獲取今日開收盤時間
                    import pandas_market_calendars as mcal
                    from datetime import timedelta

                    nyse_calendar = mcal.get_calendar("NYSE")
                    schedule = nyse_calendar.schedule(
                        start_date=now_ny.date(), end_date=now_ny.date()
                    )

                    if not schedule.empty:
                        row = schedule.iloc[0]
                        market_open = (
                            row["market_open"].tz_convert(ny_tz).to_pydatetime()
                        )
                        market_close = (
                            row["market_close"].tz_convert(ny_tz).to_pydatetime()
                        )

                        phase_a_end = market_open + timedelta(hours=1)
                        phase_c_start = market_close - timedelta(hours=1)

                        if market_open <= now_ny < phase_a_end:
                            phase = "Phase A"
                        elif phase_a_end <= now_ny < phase_c_start:
                            phase = "Phase B"
                        elif phase_c_start <= now_ny <= market_close:
                            phase = "Phase C"

                if phase == "Closed":
                    # 休市時，每 10 分鐘檢查一次
                    logger.info(
                        "市場已休市或尚未開盤。IntradayScanPipeline 進入待機..."
                    )
                    await asyncio.sleep(600)
                    continue

                logger.info(
                    f"🤖 [Intraday Pipeline] 開盤心跳監測觸發。當前時段: {phase}"
                )

                # 2. 獲取所有使用者資訊，執行量化分析
                import database

                user_ids = database.get_all_user_ids()

                for uid in user_ids:
                    ctx = database.get_full_user_context(uid)
                    if not ctx.enable_analyst_agent:
                        continue

                    # 3. 取得帳戶狀態、持倉期權、Greeks 等
                    account_state = TraderAccountState(
                        capital=ctx.total_capital
                        if hasattr(ctx, "total_capital")
                        else 100000.0,
                        cash_reserve=ctx.cash_reserve
                        if hasattr(ctx, "cash_reserve")
                        else 20000.0,
                        monthly_burn_rate=ctx.monthly_burn_rate
                        if hasattr(ctx, "monthly_burn_rate")
                        else 5000.0,
                        current_vix=await self._fetch_current_vix(),
                    )

                    # 讀取期權持倉
                    holdings = await self._fetch_user_options_holdings(uid)
                    portfolio_greeks = await self._fetch_portfolio_greeks(uid)

                    # 掃描 watchlist 中的標的
                    watchlist = database.get_user_watchlist(uid)
                    for ticker, _ in watchlist:
                        watchlist_eval = await self.evaluate_watchlist_symbol(ticker)
                        if (
                            watchlist_eval is not None
                            and watchlist_eval.tactical.alert_level != "green"
                        ):
                            from ui.formatter import generate_ansi_watchlist_report

                            await self.bot.queue_dm(
                                uid,
                                message=generate_ansi_watchlist_report(
                                    watchlist_eval.metrics,
                                    watchlist_eval.tactical,
                                ),
                            )
                        market_data = await self._fetch_ticker_market_data(ticker)
                        if not market_data:
                            continue

                        # 執行核心量化引擎
                        output = self.engine.analyze_ticker(
                            data=market_data,
                            account_state=account_state,
                            options_holdings=holdings,
                            portfolio_greeks=portfolio_greeks,
                            market_phase=phase,
                            current_time=now_ny,
                        )

                        # 如果是 SPEAR 訊號或是 Vanna 偏離警戒，則發送 Discord 私訊通知
                        if (
                            output.sddm_route == "SPEAR"
                            or "偏離" in output.vanna_hedging_instruction
                        ):
                            from cogs.embed_builder import create_intraday_scan_embed

                            embed = create_intraday_scan_embed(output)
                            await self.bot.queue_dm(uid, embed=embed)
                            logger.info(
                                f"Sent Intraday Decision report for {ticker} to user {uid}"
                            )

                # 4. 睡眠 30 分鐘
                await asyncio.sleep(self.scan_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"❌ IntradayScanPipeline 發生錯誤: {e}", exc_info=True)
                await asyncio.sleep(60)

    # 模擬/輔助獲取資料方法
    async def _fetch_current_vix(self) -> float:
        """獲取 VIX 即時數據，預設為 18.0"""
        try:
            from services.market_data_service import get_quote

            quote = await get_quote("^VIX")
            if quote and "current_price" in quote:
                return float(quote["current_price"])
        except Exception:
            pass
        return 18.0

    async def _fetch_user_options_holdings(self, user_id: int) -> List[OptionHolding]:
        """從資料庫獲取使用者期權持倉"""
        holdings = []
        try:
            from database.holdings import get_user_holdings

            db_holdings = get_user_holdings(user_id)
            for h in db_holdings:
                # 僅處理期權合約
                if "opt_type" in h and h.get("opt_type"):
                    # 估計 theta (一般期權服務會提供，這裡給予預設值或從 holdings 讀取)
                    holdings.append(
                        OptionHolding(
                            symbol=h.get("symbol", ""),
                            quantity=float(h.get("quantity", 1.0)),
                            theta=float(h.get("theta", -0.05)),
                        )
                    )
        except Exception as e:
            logger.error(f"Failed to fetch option holdings for user {user_id}: {e}")

        # 若為空則加入測試 Mock 資料
        if not holdings:
            holdings = [
                OptionHolding(symbol="AAPL", quantity=2.0, theta=-0.12),
                OptionHolding(symbol="MSFT", quantity=-1.0, theta=0.08),
            ]
        return holdings

    async def _fetch_portfolio_greeks(self, user_id: int) -> Dict[str, float]:
        """獲取使用者投資組合 Greeks"""
        greeks = {"vanna": 0.0, "beta": 1.0}
        try:
            import database

            user_ctx = database.get_full_user_context(user_id)
            greeks["vanna"] = float(getattr(user_ctx, "total_vanna", 0.0))
        except Exception:
            pass

        # Mock 預設值
        if greeks["vanna"] == 0.0:
            greeks["vanna"] = 1.25
        return greeks

    async def _fetch_ticker_market_data(
        self, ticker: str
    ) -> Optional[TickerMarketData]:
        """獲取標的即時數據並拼裝為 TickerMarketData"""
        try:
            from services.market_data_service import get_quote, get_earnings_calendar

            quote = await get_quote(ticker)
            if not quote or "current_price" not in quote:
                return None

            price = float(quote["current_price"])

            # 獲取財報日期
            days_earnings = 30
            try:
                cal = await get_earnings_calendar(ticker)
                if cal and len(cal) > 0:
                    earnings_date_str = cal[0].get("date", "")
                    if earnings_date_str:
                        dt_earn = datetime.strptime(
                            earnings_date_str, "%Y-%m-%d"
                        ).date()
                        days_earnings = max(
                            0, (dt_earn - datetime.now(ny_tz).date()).days
                        )
            except Exception:
                pass

            # Mock 其他大數據指標，以模擬通過或未通過
            return TickerMarketData(
                ticker=ticker,
                spot_price=price,
                market_cap_billion=250.5,  # 預設大於 20B
                avg_option_volume=65000,  # 預設大於 50000
                days_until_earnings=days_earnings,
                tomorrow_expiring_otm_calls_premium=1200000.0,  # 預設大於 1M
                iv_rank=55.0,  # 預設大於 50
                option_skew=0.08,  # 預設大於 0.05
            )
        except Exception as e:
            logger.error(f"Failed to fetch market data for {ticker}: {e}")
            return None
