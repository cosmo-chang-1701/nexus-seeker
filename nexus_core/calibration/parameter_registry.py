"""可校準參數登錄表：名稱 → 程式碼位置 → 現行值 (唯讀匯入)。

`tests/unit/test_calibration_registry.py` 逐項確認每個登錄參數都解析得到現存的
常數，常數改名或搬家時測試會失敗——避免報告引用一個已不存在的程式碼位置。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class CalibratableParameter:
    name: str
    code_path: str
    getter: Callable[[], Any]
    description: str


@dataclass
class ParameterResult:
    name: str
    code_path: str
    current: Any
    estimate: Optional[float]
    ci95: Optional[tuple[float, float]]
    n: int
    n_dates: int
    oos_agrees: bool
    proposed: Any
    sufficient: bool
    method: str
    guardrail_applied: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def _vix_tier_getter(index: int) -> Callable[[], float]:
    def _get() -> float:
        from config import VIX_LADDER_CONFIG

        return float(VIX_LADDER_CONFIG[index]["short_sizing_multiplier"])

    return _get


def _kelly_getter(side: str, index: int) -> Callable[[], float]:
    def _get() -> float:
        from market_analysis.kelly_priors import KELLY_WIN_RATE_PRIORS

        return float(KELLY_WIN_RATE_PRIORS[side][index].win_rate)  # type: ignore[index]

    return _get


def _regime_v_rsi_max() -> float:
    from market_analysis.dynamic_rollover.constants import _REGIME_V_RSI_MAX

    return float(_REGIME_V_RSI_MAX)


def _room_atr_1d_multiplier() -> float:
    from market_analysis.room_threshold import _ROOM_ATR_1D_MULTIPLIER

    return float(_ROOM_ATR_1D_MULTIPLIER)


def _breakdown_next_strike_multiplier() -> float:
    from market_analysis.room_threshold import (
        _BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER,
    )

    return float(_BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER)


def _room_absolute_floor_pct() -> float:
    from market_analysis.room_threshold import _ROOM_ABSOLUTE_FLOOR_PCT

    return float(_ROOM_ABSOLUTE_FLOOR_PCT)


def _gex_wall_min_depth_ratio() -> float:
    from market_analysis.gex_wall_depth import GEX_WALL_MIN_DEPTH_RATIO

    return float(GEX_WALL_MIN_DEPTH_RATIO)


def _skew_threshold_getter(attr: str) -> Callable[[], float]:
    def _get() -> float:
        from market_analysis.sentiment import skew_taxonomy

        return float(getattr(skew_taxonomy, attr))

    return _get


VIX_TIER_KEYS: tuple[str, ...] = (
    "dormant",
    "caution",
    "ready",
    "aggressive",
    "heavy",
    "extreme",
)


def _build_registry() -> tuple[CalibratableParameter, ...]:
    params: list[CalibratableParameter] = []
    for i, key in enumerate(VIX_TIER_KEYS):
        params.append(
            CalibratableParameter(
                name=f"short_vix_multiplier.{key}",
                code_path=f"config.py::VIX_LADDER_CONFIG[{i}]['short_sizing_multiplier']",
                getter=_vix_tier_getter(i),
                description="方向性做空的 VIX 倉位乘數 (倒 U 形)",
            )
        )
    for side in ("LONG", "SHORT"):
        for i, label in enumerate(("rsi_lt_50", "rsi_ge_50")):
            params.append(
                CalibratableParameter(
                    name=f"kelly_prior.{side}.{label}",
                    code_path=f"market_analysis/kelly_priors.py::KELLY_WIN_RATE_PRIORS['{side}'][{i}]",
                    getter=_kelly_getter(side, i),
                    description="凱利勝率先驗 (1.8:1 非對稱屏障)",
                )
            )
    params.extend(
        [
            CalibratableParameter(
                name="_REGIME_V_RSI_MAX",
                code_path="market_analysis/dynamic_rollover/constants.py::_REGIME_V_RSI_MAX",
                getter=_regime_v_rsi_max,
                description="Regime V 破位追空態的 15m RSI 上限",
            ),
            CalibratableParameter(
                name="_ROOM_ATR_1D_MULTIPLIER",
                code_path="market_analysis/room_threshold.py::_ROOM_ATR_1D_MULTIPLIER",
                getter=_room_atr_1d_multiplier,
                description="公式 A 單日波幅項倍數",
            ),
            CalibratableParameter(
                name="_BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER",
                code_path="market_analysis/room_threshold.py::_BREAKDOWN_NEXT_STRIKE_ATR_1D_MULTIPLIER",
                getter=_breakdown_next_strike_multiplier,
                description="公式 C 破位追空次級節點空間倍數",
            ),
            CalibratableParameter(
                name="_ROOM_ABSOLUTE_FLOOR_PCT",
                code_path="market_analysis/room_threshold.py::_ROOM_ABSOLUTE_FLOOR_PCT",
                getter=_room_absolute_floor_pct,
                description="空間門檻絕對底線 (風險政策，只報告不提案)",
            ),
            CalibratableParameter(
                name="GEX_WALL_MIN_DEPTH_RATIO",
                code_path="market_analysis/gex_wall_depth.py::GEX_WALL_MIN_DEPTH_RATIO",
                getter=_gex_wall_min_depth_ratio,
                description="薄牆門檻：每 1% 避險名目 ÷ 20 日平均成交額 (micro-report 守住率驗證)",
            ),
        ]
    )
    for attr, desc in (
        ("SKEW_TRIPLE_CONFLUENCE_PERCENTILE", "三重結構性風險合流的 Skew 分位"),
        ("SKEW_HIGH_DEFENSE_PERCENTILE", "防洗盤處置 / Skew Divergence Gate 分位"),
        ("SKEW_DIVERGENCE_HIGH_PERCENTILE", "SQZ 微觀背離偽突破 / 結構性背離上緣"),
        ("SKEW_DIVERGENCE_LOW_PERCENTILE", "結構性背離下緣"),
    ):
        params.append(
            CalibratableParameter(
                name=attr,
                code_path=f"market_analysis/sentiment/skew_taxonomy.py::{attr}",
                getter=_skew_threshold_getter(attr),
                description=f"{desc} (skew-proxy 先驗 + 日級母體前向蒐集)",
            )
        )
    return tuple(params)


REGISTRY: tuple[CalibratableParameter, ...] = _build_registry()


def get_parameter(name: str) -> CalibratableParameter:
    for p in REGISTRY:
        if p.name == name:
            return p
    raise KeyError(name)


def new_result(name: str, method: str) -> ParameterResult:
    p = get_parameter(name)
    current = p.getter()
    return ParameterResult(
        name=name,
        code_path=p.code_path,
        current=current,
        estimate=None,
        ci95=None,
        n=0,
        n_dates=0,
        oos_agrees=False,
        proposed=current,
        sufficient=False,
        method=method,
    )
