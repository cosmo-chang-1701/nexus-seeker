"""參數登錄表防漂移與校準護欄。"""

import numpy as np
import pandas as pd

from calibration.calibrators import kelly_priors as kelly_cal
from calibration.calibrators import vix_short
from calibration.config import CalibrationConfig
from calibration.parameter_registry import REGISTRY


def test_every_registered_parameter_resolves_to_live_constant() -> None:
    for p in REGISTRY:
        value = p.getter()
        assert isinstance(value, (int, float)), p.name
        module_path = p.code_path.split("::")[0]
        assert module_path.endswith(".py")


def _labeled(
    side: str, event_type: str, n: int, vix: float, win: int, rsi: float = 40.0
) -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", periods=n).strftime("%Y-%m-%d")
    dates = list(dates[: n // 2]) + list(
        pd.bdate_range("2024-06-03", periods=n - n // 2).strftime("%Y-%m-%d")
    )
    return pd.DataFrame(
        {
            "side": side,
            "event_type": event_type,
            "vix_prev": vix,
            "date": dates,
            "win": win,
            "win_rr18": win,
            "r_multiple": 1.0 if win else -1.0,
            "rsi": rsi,
        }
    )


def test_vix_extreme_is_never_proposed_even_with_positive_edge() -> None:
    cfg = CalibrationConfig(n_boot=100, min_events=50, min_dates=20)
    df = pd.concat(
        [
            _labeled("SHORT", "SCANNER_BTO_PUT", 400, 40.0, 1),
            _labeled("SHORT", "SCANNER_BTO_PUT", 400, 20.0, 1),
        ],
        ignore_index=True,
    )
    results = {r.name: r for r in vix_short.calibrate(df, cfg)}
    extreme = results["short_vix_multiplier.extreme"]
    assert extreme.proposed == 0.0
    assert extreme.guardrail_applied == "EXTREME_LOCKED_AT_ZERO"
    assert any("人工政策覆核" in n for n in extreme.notes)
    ready = results["short_vix_multiplier.ready"]
    assert 0.0 <= float(ready.proposed) <= 1.0


def test_short_kelly_prior_never_exceeds_long() -> None:
    cfg = CalibrationConfig(n_boot=100, min_events=50, min_dates=20)
    df = pd.concat(
        [
            _labeled("SHORT", "SCANNER_BTO_PUT", 600, 20.0, 1, rsi=40.0),
            _labeled("LONG", "SCANNER_BTO_CALL", 600, 20.0, 0, rsi=40.0),
        ],
        ignore_index=True,
    )
    results = {r.name: r for r in kelly_cal.calibrate(df, cfg)}
    short = results["kelly_prior.SHORT.rsi_lt_50"]
    long_ = results["kelly_prior.LONG.rsi_lt_50"]
    assert float(short.proposed) <= float(long_.proposed)
    assert short.guardrail_applied == "SHORT_CLAMPED_TO_LONG"


def test_insufficient_samples_keep_current_values() -> None:
    cfg = CalibrationConfig(n_boot=50)
    df = _labeled("SHORT", "SCANNER_BTO_PUT", 10, 20.0, 1)
    for r in vix_short.calibrate(df, cfg) + kelly_cal.calibrate(df, cfg):
        assert r.proposed == r.current
        assert np.isfinite(float(r.current))
