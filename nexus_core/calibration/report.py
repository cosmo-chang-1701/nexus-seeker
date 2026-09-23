"""報告輸出：results.json + report.md (繁中) + manifest.json。

路徑限制：只允許寫入 `{out_dir}/calibration/` 之下；任何解析後落在外面的路徑
一律拒絕 (測試強制)。本模組是整個 calibration 套件唯一會寫檔的地方 (快取除外)。
"""

import json
import math
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from calibration.config import CalibrationConfig
from calibration.parameter_registry import ParameterResult

SCHEMA_VERSION = 1


class UnsafeOutputPathError(ValueError):
    pass


def resolve_output_dir(out_dir: Path, stamp: str) -> Path:
    base = (Path(out_dir) / "calibration").resolve()
    target = (base / stamp).resolve()
    if base not in target.parents:
        raise UnsafeOutputPathError(f"輸出路徑不在 {base} 之下: {target}")
    return target


def _git_sha() -> Optional[str]:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
            or None
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):  # numpy scalar
        return _jsonable(value.item())
    return value


def build_results(
    cfg: CalibrationConfig,
    parameters: Sequence[ParameterResult],
    event_tables: Sequence[dict[str, Any]],
    data_coverage: dict[str, Any],
    forward: Optional[dict[str, Any]] = None,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    config_dict = asdict(cfg)
    config_dict.pop("cache_dir", None)
    config_dict.pop("out_dir", None)
    payload: dict[str, Any] = _jsonable(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
            "git_sha": _git_sha(),
            "config": config_dict,
            "data_coverage": data_coverage,
            "parameters": [asdict(p) for p in parameters],
            "event_tables": list(event_tables),
            "forward_collection": forward,
        }
    )
    return payload


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return f"[{_fmt(value[0])}, {_fmt(value[1])}]"
    return str(value)


def render_markdown(results: dict[str, Any]) -> str:
    lines = [
        "# 回測校準報告",
        "",
        f"- 產出時間：{results['generated_at']}",
        f"- git：`{results.get('git_sha') or 'unknown'}`",
        "- ⚠️ 本報告只提供建議值；**任何常數變更都必須人工審核後另開 PR**，工具不會修改程式碼。",
        "",
        "## 參數建議",
        "",
        "| 參數 | 現行值 | 估計值 | 95% CI | n | 交易日數 | 樣本外一致 | 充足 | 建議值 | 護欄 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for p in results["parameters"]:
        lines.append(
            f"| `{p['name']}` | {_fmt(p['current'])} | {_fmt(p['estimate'])} | {_fmt(p['ci95'])} "
            f"| {p['n']} | {p['n_dates']} | {'✅' if p['oos_agrees'] else '❌'} "
            f"| {'✅' if p['sufficient'] else '❌'} | {_fmt(p['proposed'])} | {p.get('guardrail_applied') or '—'} |"
        )
    lines += ["", "## 參數明細", ""]
    for p in results["parameters"]:
        lines.append(f"### `{p['name']}`")
        lines.append("")
        lines.append(f"- 程式碼位置：`{p['code_path']}`")
        lines.append(f"- 方法：{p['method']}")
        for note in p.get("notes") or []:
            lines.append(f"- {note}")
        lines.append("")
    lines += [
        "## 事件統計",
        "",
        "| 事件類型 | 方向 | n | 交易日數 | 勝率 | 期望值 (R) | 95% CI |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in results["event_tables"]:
        lines.append(
            f"| {t['event_type']} | {t['side']} | {t['n']} | {t['n_dates']} | {_fmt(t['win_rate'])} "
            f"| {_fmt(t['expectancy'])} | {_fmt(t['expectancy_ci'])} |"
        )
    forward = results.get("forward_collection")
    if forward:
        lines += [
            "",
            "## 前向蒐集 (production 評估紀錄)",
            "",
            f"- 狀態：{forward.get('status')}",
            f"- 已標註：{forward.get('total_labeled')}",
        ]
        for s in forward.get("sections", []):
            lines.append(
                f"- `{s['evaluator']}` decision={s['decision']}：n={s['n']}，{s.get('status')}"
                + (
                    f"，勝率 {_fmt(s.get('win_rate'))}，期望值 {_fmt(s.get('expectancy'))}R"
                    if s.get("status") == "OK"
                    else ""
                )
            )
        exit_tiers = forward.get("exit_tiers") or []
        if exit_tiers:
            lines += [
                "",
                "### 出場分層洗盤率 (EXIT_*)",
                "",
                "訊號正確＝平倉後價格先朝不利原部位的方向觸及 1.5×ATR₁D；"
                "洗盤＝反向先觸及 (被掃出後價格回到原方向)。",
                "",
                "| evaluator | 顧問持倉 | n | 訊號正確 | 洗盤 | 逾時 | 5 日報酬中位數 | 狀態 |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for e in exit_tiers:
                lines.append(
                    f"| `{e['evaluator']}` | {'是' if e['advisory'] else '否'} | {e['n']} "
                    f"| {_fmt(e['correct_rate'])} | {_fmt(e['washout_rate'])} "
                    f"| {_fmt(e['timeout_rate'])} | {_fmt(e['median_fwd_ret_5d'])} "
                    f"| {e['status']} |"
                )
    studies = (forward or {}).get("threshold_studies") or {}
    if studies:
        lines += ["", "### 門檻前向驗證 (docs §5.13)", ""]
        for name, title in (
            ("wall_depth_ratio", "牆體深度比四分位 (D-04)"),
            ("skew_percentile", "Skew 分位區間"),
        ):
            data = studies.get(name)
            lines.append(f"**{title}**")
            lines.append("")
            if data is None or isinstance(data, str):
                lines += [f"- {data or '無資料'}", ""]
                continue
            groups: list[tuple[str, Any]] = (
                list(data.items()) if isinstance(data, dict) else [("", data)]
            )
            for group, rows in groups:
                if group:
                    lines.append(f"- 母體來源 `{group}`")
                lines += [
                    "",
                    "| 區間 | n | 勝率 | 逆向先觸及 | 狀態 |",
                    "|---|---|---|---|---|",
                ]
                for r in rows:
                    lines.append(
                        f"| {r['bucket']} | {r['n']} | {_fmt(r['win_rate'])} "
                        f"| {_fmt(r['adverse_rate'])} | {r['status']} |"
                    )
                lines.append("")
    lines += [
        "",
        "## 限制與警語",
        "",
        "- **倖存者偏差**：標的池取自現在的 watchlist／持倉與固定流動性清單，已下市或被移出的標的不在樣本內。",
        "- **GEX 代理薄弱**：歷史 GEX 不存在，Put Wall／Gamma Flip／次級負 GEX 節點全以純價格代理 (前 10/60 日高低點、SMA20)。GEX 相關常數的調整必須等待前向蒐集資料，本報告的相關建議僅供參考。",
        "- **RSI 週期代理**：分類器使用 15m RSI，離線研究以 1h RSI 代理 (yfinance 15m 僅 60 天)；ATR₁₅ₘ 以 ATR₁ₕ/2 近似。",
        "- **未模擬選擇權損益**：只標註標的價格路徑，不含權利金、Theta、IV Crush 與滑價。",
        "- **多重比較**：RSI 門檻掃描的訓練期最佳值有偏高傾向，須以樣本外一致性為準。",
        "",
        "## 重現",
        "",
        "```",
        "cd nexus_core",
        'docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker python -m calibration fetch --max-symbols 40',
        'docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp nexus-seeker '
        f"python -m calibration run --offline --seed {results['config'].get('seed')}",
        "```",
        "",
    ]
    return "\n".join(lines)


def write_report(
    cfg: CalibrationConfig,
    results: dict[str, Any],
    manifest: dict[str, Any],
    stamp: Optional[str] = None,
) -> Path:
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = resolve_output_dir(cfg.out_dir, stamp)
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (target / "report.md").write_text(render_markdown(results), encoding="utf-8")
    (target / "manifest.json").write_text(
        json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def write_study_report(out_dir: Path, name: str, result: dict[str, Any]) -> Path:
    """研究型子命令 (micro-report / skew-proxy) 的輸出：{out_dir}/calibration/{name}_{stamp}/results.json。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = resolve_output_dir(out_dir, f"{name}_{stamp}")
    target.mkdir(parents=True, exist_ok=True)
    path = target / "results.json"
    path.write_text(
        json.dumps(_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
