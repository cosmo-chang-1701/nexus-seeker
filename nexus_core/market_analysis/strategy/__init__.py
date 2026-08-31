"""選擇權策略分析核心：技術指標、合約篩選、風險驗證、單一標的分析管線。

依領域拆分為：
- indicators.py：技術指標、策略訊號、EMA 趨勢判定
- mmm_term_structure.py：財報日 MMM 與波動率期限結構
- contract_selection.py：到期日/合約篩選、垂直偏態、最佳合約搜尋
- liquidity_risk.py：流動性評估、風險驗證、VIX 階梯、倉位大小
- analyze.py：單一標的核心分析管線（analyze_symbol）

注意：`analyze_symbol`（analyze.py）呼叫 `_calculate_technical_indicators`、
`_fetch_opt_chain_and_best_contract`、`evaluate_ema_trend`、`_calculate_mmm`、
`_calculate_term_structure` 時皆改為函式內延遲匯入（`from market_analysis.strategy
import ...`），以確保測試對本套件頂層屬性的 `patch(...)` 仍然有效。
"""

from market_analysis.strategy.indicators import (
    _calculate_technical_indicators,
    _determine_strategy_signal,
    evaluate_ema_trend,
    detect_ema_signals,
)
from market_analysis.strategy.mmm_term_structure import (
    _calculate_mmm,
    _calculate_term_structure,
)
from market_analysis.strategy.contract_selection import (
    _find_target_expiry,
    _get_best_contract_data,
    _calculate_vertical_skew,
    _fetch_opt_chain_and_best_contract,
    find_best_contract,
    find_lowest_strike_call_above_floor,
)
from market_analysis.strategy.liquidity_risk import (
    _evaluate_option_liquidity,
    _validate_risk_and_liquidity,
    apply_vix_ladder,
    _calculate_sizing,
)
from market_analysis.strategy.analyze import (
    _as_awaitable,
    analyze_symbol,
)

__all__ = [
    "_calculate_technical_indicators",
    "_determine_strategy_signal",
    "evaluate_ema_trend",
    "detect_ema_signals",
    "_calculate_mmm",
    "_calculate_term_structure",
    "_find_target_expiry",
    "_get_best_contract_data",
    "_calculate_vertical_skew",
    "_fetch_opt_chain_and_best_contract",
    "find_best_contract",
    "find_lowest_strike_call_above_floor",
    "_evaluate_option_liquidity",
    "_validate_risk_and_liquidity",
    "apply_vix_ladder",
    "_calculate_sizing",
    "_as_awaitable",
    "analyze_symbol",
]
