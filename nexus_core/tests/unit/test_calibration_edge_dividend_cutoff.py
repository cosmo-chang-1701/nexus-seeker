"""edge 股息率修正切分 (docs/microstructure/03 §5.w)。全部合成資料、離線。"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from calibration import __main__ as cli
from calibration import forward_log, microstructure, regime_stop_report, report
from calibration.config import EDGE_DIVIDEND_FIX_SINCE, CalibrationConfig
from calibration.forward_log import build_forward_report, build_threshold_studies
from calibration.microstructure import (
    DEPTH_DOWNWARD_NOT_JUDGED,
    build_micro_report,
    depth_quartile_hold_rates,
)
from calibration.report import render_markdown

CFG = CalibrationConfig(n_boot=100, seed=7)
SINCE = EDGE_DIVIDEND_FIX_SINCE
PRE = "2026-10-29"
POST = "2026-11-02"


def _snap(day: str) -> dict[str, Any]:
    return {
        "date": day,
        "symbol": "MU",
        "spot": 1100.0,
        "adv_dollar_20d": 1e9,
        "atr_1d": 10.0,
        "gex": {
            "put_wall": 1050.0,
            "support_strike": 1000.0,
            "support_gex": 9e6,
            "net_profile": {"1050.0": -3e6, "1070.0": 2e6, "1000.0": 9e6},
        },
        "em": [
            {"dte": 7, "em_weekly_calendar": 10.0},
            {"dte": 1, "em_weekly_calendar": 12.0},
        ],
    }


def _snaps() -> list[dict[str, Any]]:
    return [_snap("2026-10-01"), _snap(PRE), _snap(SINCE), _snap(POST)]


def _patch_labeler(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    def _fake(
        snapshots: list[dict[str, Any]], horizon_days: int = 5
    ) -> list[dict[str, Any]]:
        seen.append([s["date"] for s in snapshots])
        return []

    monkeypatch.setattr(microstructure, "label_wall_holds", _fake)
    return seen


def test_gex_since_filters_only_gex_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _patch_labeler(monkeypatch)
    res = build_micro_report(tmp_path, snapshots=_snaps(), gex_since=SINCE)

    assert seen == [[SINCE, POST]]  # 切分日當天照計
    assert res["edge_gex_since"] == "2026-10-30"
    assert res["n_snapshots_before_gex_since"] == 2
    assert res["n_with_support_wall"] == 2
    assert sum(v["n"] for v in res["依成交額分組"].values()) == 2
    assert res["PutWall處淨GEX"]["n_put_wall_with_profile"] == 2
    assert res["GammaFlip重要性"]["n_with_profile"] == 2
    # 週 EM 與快照清單沿用全部快照
    assert res["週EM相對7DTE直接量測之比值"]["DTE=1"]["n"] == 4
    assert res["n_snapshots"] == 4
    assert res["snapshot_dates"] == ["2026-10-01", PRE, SINCE, POST]

    # 切分後沒有任何快照時各段落照常輸出空結果
    empty = build_micro_report(tmp_path, snapshots=[_snap(PRE)], gex_since=SINCE)
    assert empty["n_snapshots_before_gex_since"] == 1
    assert empty["現行500k絕對門檻通過率"] is None
    assert empty["n_labeled"] == 0


def test_gex_since_none_keeps_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _patch_labeler(monkeypatch)
    default = build_micro_report(tmp_path, snapshots=_snaps())
    explicit = build_micro_report(tmp_path, snapshots=_snaps(), gex_since=None)

    assert default == explicit
    assert seen[0] == ["2026-10-01", PRE, SINCE, POST]
    assert "edge_gex_since" not in default
    assert "n_snapshots_before_gex_since" not in default
    assert default["n_with_support_wall"] == 4
    assert default["GammaFlip重要性"]["n_with_profile"] == 4


def _depth_labeled(n_dates: int, held_by_q: dict[int, bool]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n_dates):
        d = (date(2026, 11, 2) + timedelta(days=i)).isoformat()
        for q in range(4):
            for r in range(2):
                out.append(
                    {
                        "date": d,
                        "depth_ratio": q * 10 + r,
                        "tested": True,
                        "held": held_by_q[q],
                    }
                )
    return out


def test_depth_table_discloses_downward_not_judged() -> None:
    all_held = dict.fromkeys(range(4), True)
    short = depth_quartile_hold_rates(_depth_labeled(5, all_held))
    same = depth_quartile_hold_rates(_depth_labeled(24, all_held))
    lower = depth_quartile_hold_rates(
        _depth_labeled(24, {0: True, 1: False, 2: True, 3: True})
    )
    for res in (short, same, lower):
        assert res["下調判讀"] == DEPTH_DOWNWARD_NOT_JUDGED
    assert DEPTH_DOWNWARD_NOT_JUDGED.startswith(
        "下調方向已擱置（2026-11-26 使用者決定）"
    )
    assert short["判讀"].startswith("樣本不足（可標註日期 5/20")
    assert same["判讀"] == "未見 Q2 明顯低於 Q3／Q4，維持 GEX_WALL_MIN_DEPTH_RATIO"
    assert "提案" in lower["判讀"]
    assert depth_quartile_hold_rates([]) == {}


def test_depth_downward_text_reflects_user_decision() -> None:
    assert "尚未定義" not in DEPTH_DOWNWARD_NOT_JUDGED
    assert "未測試" not in DEPTH_DOWNWARD_NOT_JUDGED
    res = depth_quartile_hold_rates(_depth_labeled(24, dict.fromkeys(range(4), True)))
    assert res["下調判讀"] == DEPTH_DOWNWARD_NOT_JUDGED


def _forward_rows(day: str, n: int) -> list[dict[str, Any]]:
    return [
        {
            "evaluator": "REGIME_CLASSIFIER",
            "regime": "I",
            "source": "WATCHLIST_ADVISOR",
            "symbol": f"S{i}",
            "bar_ts": f"{day}T10:00:00-05:00",
            "decision": 1,
            "direction": "LONG",
            "first_touch_k15": 1 if i % 2 else -1,
            "fwd_ret_5d": 0.01,
            "spot": 100.0,
            "next_negative_node": 90.0 - i * 0.01,
            "atr_1d": 2.0,
            "resistance_wall": 110.0 + i * 0.01,
            "atr_15m": 0.5,
            "put_wall": 95.0 - i * 0.01,
            "conditions_mask": None,
            "conditions_evaluated_mask": None,
            "features_json": json.dumps(
                {
                    "support_gex": 1e6 + i * 1e3,
                    "adv_dollar_20d": 1e9,
                    "gamma_flip_raw": 99.0,
                    "gamma_flip_ratio": 0.01 if i % 2 else 0.5,
                    "skew_percentile": float(i % 100),
                    "skew_percentile_source": "CANONICAL",
                }
            ),
        }
        for i in range(n)
    ]


def _mixed_forward_rows() -> pd.DataFrame:
    return pd.DataFrame(_forward_rows(PRE, 120) + _forward_rows(SINCE, 110))


def test_forward_gex_studies_respect_since() -> None:
    rows = _mixed_forward_rows()
    base = build_forward_report(rows, CFG)
    cut = build_forward_report(rows, CFG, gamma_flip_since=SINCE)

    assert "gex_since" not in base and "n_excluded_before_gex_since" not in base
    assert cut["gex_since"] == SINCE
    assert cut["n_excluded_before_gex_since"] == 120

    flip = cut["threshold_studies"]["gamma_flip_materiality"]
    assert flip["since"] == SINCE
    assert flip["n_excluded_before_since"] == 120
    assert sum(g["n"] for g in flip["groups"].values()) == 110
    base_flip = base["threshold_studies"]["gamma_flip_materiality"]
    assert "since" not in base_flip and "n_excluded_before_since" not in base_flip
    assert sum(g["n"] for g in base_flip["groups"].values()) == 230

    def _total(table: list[dict[str, Any]]) -> int:
        return sum(r["n"] for r in table)

    assert _total(base["threshold_studies"]["wall_depth_ratio"]) == 230
    assert _total(cut["threshold_studies"]["wall_depth_ratio"]) == 110
    for name in (
        "next_node_space_atr",
        "resistance_buffer_atr15",
        "put_wall_dist_pct",
    ):
        assert _total(base["magnitudes"][name]) == 230
        assert _total(cut["magnitudes"][name]) == 110

    # Skew、sections、出場分層與體制分組不受影響
    for key in ("skew_percentile", "skew_percentile_by_evaluator"):
        assert cut["threshold_studies"][key] == base["threshold_studies"][key]
    for key in ("sections", "exit_tiers", "regime_breakdown", "total_labeled"):
        assert cut[key] == base[key]

    # 切分後沒有任何列：沿用「資料累積中」路徑
    none_left = build_forward_report(
        pd.DataFrame(_forward_rows(PRE, 5)), CFG, gamma_flip_since=SINCE
    )
    studies = none_left["threshold_studies"]
    emptied = (
        f"資料累積中 (0/100)；樣本起日 {SINCE}，"
        f"排除起日前 {len(_forward_rows(PRE, 5))} 列"
    )
    assert studies["gamma_flip_materiality"] == emptied
    assert f"- {emptied}" in _markdown(none_left)
    assert studies["wall_depth_ratio"] == "資料累積中 (0/100)"
    assert none_left["magnitudes"]["put_wall_dist_pct"] == "資料累積中 (0/100)"
    assert none_left["n_excluded_before_gex_since"] == 5

    # 缺 date 欄時不得把來源不明的列當成切分後樣本
    undated = pd.DataFrame(
        {
            "features_json": [
                json.dumps({"gamma_flip_raw": 99.0, "gamma_flip_ratio": 0.5})
            ],
            "win": [1],
            "outcome": [1],
        }
    )
    assert (
        build_threshold_studies(undated, gamma_flip_since=SINCE)[
            "gamma_flip_materiality"
        ]
        == "資料累積中 (0/100)；樣本起日 2026-10-30，排除起日前 1 列"
    )


def _markdown(forward: dict[str, Any]) -> str:
    return render_markdown(
        {
            "generated_at": "x",
            "config": {},
            "parameters": [],
            "event_tables": [],
            "forward_collection": forward,
        }
    )


def test_markdown_shows_sample_since() -> None:
    rows = _mixed_forward_rows()
    md = _markdown(build_forward_report(rows, CFG, gamma_flip_since=SINCE))
    note = "（edge 股息率修正，microstructure/03 §5.w）；排除起日前 120 列"
    assert f"- 牆體深度比與牆距分佈：樣本起日 2026-10-30{note}" in md
    assert f"- 樣本起日 2026-10-30{note}" in md
    assert "樣本起日" not in _markdown(build_forward_report(rows, CFG))


def test_cli_wiring_applies_edge_cutoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edge = MagicMock(return_value=(_snaps(), 0))
    micro = MagicMock(return_value={})
    regime = MagicMock(return_value={})
    written: dict[str, dict[str, Any]] = {}

    def _write_study(
        out_dir: Path, name: str, result: dict[str, Any], markdown: Any = None
    ) -> Path:
        written[name] = result
        return tmp_path / "results.json"

    monkeypatch.setattr(cli, "_load_edge_snapshots", edge)
    monkeypatch.setattr(microstructure, "build_micro_report", micro)
    monkeypatch.setattr(regime_stop_report, "run_regime_stop_report", regime)
    monkeypatch.setattr(report, "write_study_report", _write_study)
    dirs = ["--cache-dir", str(tmp_path), "--out", str(tmp_path), "--force"]

    assert cli.main(["micro-report", *dirs]) == 0
    assert micro.call_args.kwargs["gex_since"] == "2026-10-30"
    assert len(micro.call_args.kwargs["snapshots"]) == 4  # 週 EM 需要全部快照
    assert written["micro-report"]["source"] == "edge"

    assert cli.main(["micro-report", "--source", "snapshot", *dirs]) == 0
    assert micro.call_args.kwargs.get("gex_since") is None
    assert micro.call_args.kwargs.get("snapshots") is None
    assert edge.call_count == 1

    assert cli.main(["regime-stop-report", "--snapshot-db", "x.db", *dirs]) == 0
    assert [s["date"] for s in regime.call_args.args[0]] == [SINCE, POST]
    result = written["regime-stop-report"]
    assert result["edge_gex_since"] == "2026-10-30"
    assert result["n_snapshots_before_gex_since"] == 2

    captured: dict[str, Any] = {}

    def _fake_forward(rows: Any, cfg: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "OK", "total_labeled": 0, "sections": []}

    async def _fake_squeeze(cfg: Any, snapshot: Any) -> dict[str, Any]:
        return {"status": "略過"}

    monkeypatch.setattr(forward_log, "load_forward_rows", lambda snapshot=None: None)
    monkeypatch.setattr(forward_log, "build_forward_report", _fake_forward)
    monkeypatch.setattr(cli, "_squeeze_flip_check", _fake_squeeze)
    monkeypatch.setattr(report, "write_report", lambda *a, **k: tmp_path)
    assert cli.main(["forward-report", "--offline", "--universe", "AAA", *dirs]) == 0
    assert captured["gamma_flip_since"] == "2026-10-30"
