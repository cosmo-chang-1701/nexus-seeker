"""回測引擎多資產改造的回歸不變式（最高優先）。

`calibration/backtest_engine_2025.py` 由寫死 SPY／NVDA／GLD 三標的，改為可設定的
「核心 + N 檔衛星 + 大盤訊號代理」結構。改造必須對既有預設配置（SPY/NVDA/GLD、
原權重、2025 期間）**逐位元不變**：逐日 NAV 與全部交易紀錄的每一個欄位都不得改變。

作法：以合成資料（固定種子）在兩種模式 × 兩組功能開關下執行，將逐日紀錄與交易
紀錄以 `repr()` 序列化後取 SHA-256，與改造**前**的引擎產出的雜湊比對。雜湊只涵蓋
改造前就存在的欄位，因此之後新增欄位不影響比對；任何既有欄位的值改變都會失敗。

⚠️ 不要為了讓測試通過而更新下方的 `_GOLDEN`。雜湊不符代表預設配置的行為被改變了，
應修正引擎，而不是修改基準。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from calibration.backtest_engine_2025 import RolloverBacktestEngine2025
from calibration.data_store import DataStore
from tests.unit.calibration_fixtures import synthetic_daily, synthetic_hourly

# 每組約 2 秒、共 12 組：屬 slow，由 CI 的完整測試把關（pre-push 快速子集不跑）。
pytestmark = pytest.mark.slow

# 改造前就存在的欄位；新增欄位不列入比對
_NAV_FIELDS = (
    "date",
    "nav",
    "cash",
    "positions_value",
    "benchmark_nav",
    "spy_weight",
    "nvda_weight",
    "gld_weight",
    "cash_weight",
    "vix",
    "market_regime",
    "daily_return",
    "benchmark_daily_return",
)
_TRADE_FIELDS = (
    "timestamp",
    "date",
    "symbol",
    "action",
    "shares",
    "price",
    "notional",
    "fee",
    "scenario",
    "reason",
    "realized_pnl",
)

_ALL_FLAGS: dict[str, bool] = {
    "enable_trend_continuation": True,
    "enable_tp1_trend_exempt": True,
    "enable_pyramid_add": True,
    "enable_escape_tiers": True,
}

# 以改造前的引擎（commit ad90b3f 的 backtest_engine_2025.py）產生。
_GOLDEN: dict[tuple[str, str, bool], str] = {
    (
        "base",
        "aggressive",
        False,
    ): "0ea813acc82169d9133ea2504f14d75cfb6620933e17c7b2827cf3e0f87e4ba5",
    (
        "base",
        "aggressive",
        True,
    ): "7e59c20459b25944f87dd32825e9f0980ffd806a7a9f5bd6381c5f183a323009",
    (
        "base",
        "defensive",
        False,
    ): "2668eaefacb133494c918b94c681b5a865d6de4c01d8b4c907a35eb80cbadac4",
    (
        "base",
        "defensive",
        True,
    ): "ffc9c2b96c869065c7727bd81dcd673a5ccd4e5979838b6025abd8570111563d",
    (
        "down",
        "aggressive",
        False,
    ): "b8e4b276636e95c9b7c1ff3687c4b4d4390ed08d8d87fe2a1851dfb26af3e45c",
    (
        "down",
        "aggressive",
        True,
    ): "be8589332c7125b5a02f21cf8ca5db6290b403b0b7582c3245d49c0a81dec44d",
    (
        "down",
        "defensive",
        False,
    ): "a09ba71281148b9622a5d1bc59622251c77e3ab7c879ec4b7ed54ecfa16d4326",
    (
        "down",
        "defensive",
        True,
    ): "9692dcc09a9f74d49a1402736d722afba5c3bad303d3239896750c15672fc713",
    (
        "up",
        "aggressive",
        False,
    ): "8e8628b1661919026682ced51fe5416df8288ad115296145f77cbdbe99be967e",
    (
        "up",
        "aggressive",
        True,
    ): "ec15cf107d0ab6276b989926a034d3bf6b41370e048d57bb054954d0a2044e38",
    (
        "up",
        "defensive",
        False,
    ): "3090d27a3ad6170ef7666ef8cebc4cd26cad1d5c3d5d3e0fa73ac00fe8833eda",
    (
        "up",
        "defensive",
        True,
    ): "3f3be2f45f2ba76de123ff63783a4d6c54b3ef09ccd1eb3480ccb42c2adc9684",
}


# (NVDA 漂移, 種子位移)：base 為一般行情；down 觸發左側接刀與轉換引擎；up 觸發
# 順勢加碼。三者合計涵蓋除 SHORT_ENTRY 外的全部情境（合成與 2025 真實資料都不會
# 觸發做空進場，該段程式碼以原樣搬移、只把標的改為參數）。
_VARIANTS: dict[str, tuple[float, int]] = {
    "base": (0.0006, 60),
    "down": (-0.003, 200),
    "up": (0.003, 200),
}


def _synthetic_store(tmp_path: Path, variant: str) -> tuple[str, str]:
    nvda_drift, seed_off = _VARIANTS[variant]
    store = DataStore(tmp_path)
    for i, (sym, drift) in enumerate(
        (
            ("SPY", 0.0006 if variant == "base" else 0.0004),
            ("NVDA", nvda_drift),
            ("GLD", 0.0006 if variant == "base" else 0.0008),
        )
    ):
        d = synthetic_daily(
            n_days=520, seed=i + seed_off, start="2024-01-02", drift=drift
        )
        store.save("1d", sym, d)
        store.save("1h", sym, synthetic_hourly(d, n_days=300, seed=i + seed_off + 20))
    vix = synthetic_daily(n_days=520, seed=97, start="2024-01-02", drift=0.0)
    # 12–42 區間：涵蓋 MARGIN_DEFENSE (≥25) 與逃頂 (≥28) 觸發
    store.save("1d", "^VIX", vix.assign(Close=lambda d: 12.0 + (d["Close"] % 30.0)))
    store.save("1d", "^VIX3M", vix.assign(Close=lambda d: 17.0 + (d["Close"] % 9.0)))
    dates = synthetic_daily(n_days=520, start="2024-01-02").index
    return str(dates[-280].date()), str(dates[-5].date())


def _digest(eng: RolloverBacktestEngine2025) -> str:
    h = hashlib.sha256()
    for rec in eng.portfolio.daily_history:
        h.update(repr(tuple(getattr(rec, f) for f in _NAV_FIELDS)).encode())
    for tr in eng.portfolio.trades:
        h.update(repr(tuple(getattr(tr, f) for f in _TRADE_FIELDS)).encode())
    return h.hexdigest()


def _run(
    tmp_path: Path, mode: str, all_flags: bool, variant: str = "base"
) -> RolloverBacktestEngine2025:
    start, end = _synthetic_store(tmp_path, variant)
    kwargs: dict[str, Any] = dict(_ALL_FLAGS) if all_flags else {}
    eng = RolloverBacktestEngine2025(
        cache_dir=tmp_path, start_date=start, end_date=end, mode=mode, **kwargs
    )
    eng.run_simulation()
    return eng


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
@pytest.mark.parametrize("mode", ["aggressive", "defensive"])
@pytest.mark.parametrize("all_flags", [False, True])
def test_default_config_is_bit_identical(
    tmp_path: Path, variant: str, mode: str, all_flags: bool
) -> None:
    eng = _run(tmp_path, mode, all_flags, variant)
    # 守衛：合成資料必須真的觸發交易，否則雜湊比對形同虛設
    assert len(eng.portfolio.trades) > 20
    assert _digest(eng) == _GOLDEN[(variant, mode, all_flags)]
