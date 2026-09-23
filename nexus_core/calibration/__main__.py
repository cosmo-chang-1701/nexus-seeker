"""python -m calibration {fetch|run|forward-report|all|micro-snapshot|micro-report|skew-proxy}"""

import argparse
import asyncio
import dataclasses
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from calibration.config import CalibrationConfig
from calibration.data_store import DataStore


def _parse(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m calibration",
        description="回測校準工具 (只產出報告，不修改程式碼)",
    )
    parser.add_argument(
        "command",
        choices=[
            "fetch",
            "run",
            "forward-report",
            "all",
            "micro-snapshot",
            "micro-report",
            "skew-proxy",
        ],
    )
    parser.add_argument(
        "--universe", default="", help="逗號分隔標的清單；留空則自動組成"
    )
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--offline", action="store_true", help="只用快取，缺快取即失敗")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", default=None, help="報告輸出根目錄")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--force", action="store_true", help="略過記憶體安全檢查")
    parser.add_argument(
        "--any-time",
        action="store_true",
        help="micro-snapshot：略過「交易日收盤後、當天尚無快照」的排程保護",
    )
    return parser.parse_args(argv)


def _config(args: argparse.Namespace) -> CalibrationConfig:
    overrides: dict[str, Any] = {"offline": bool(args.offline)}
    if args.max_symbols is not None:
        overrides["max_symbols"] = args.max_symbols
    if args.seed is not None:
        overrides["seed"] = args.seed
    if args.out:
        overrides["out_dir"] = Path(args.out)
    if args.cache_dir:
        overrides["cache_dir"] = Path(args.cache_dir)
    return dataclasses.replace(CalibrationConfig(), **overrides)


async def _main(args: argparse.Namespace) -> int:
    from calibration import pipeline, report
    from calibration.universe import build_universe

    cfg = _config(args)
    for directory in (cfg.cache_dir, cfg.out_dir):
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
        except PermissionError:
            print(
                f"無法寫入 {directory}。容器預設以 uid 1001 執行，無法寫入 bind-mount 的 repo；"
                '請改用：docker compose run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp '
                "nexus-seeker python -m calibration ...",
                file=sys.stderr,
            )
            return 2
    if args.command in ("micro-snapshot", "micro-report", "skew-proxy"):
        return await _run_study(args, cfg)

    store = DataStore(cfg.cache_dir)
    symbols = (
        [s.strip().upper() for s in args.universe.split(",") if s.strip()]
        if args.universe
        else build_universe(cfg.max_symbols)
    )

    if args.command in ("fetch", "all") and not cfg.offline:
        from calibration.fetcher import YFinanceFetcher

        fetcher = YFinanceFetcher(cfg.fetch_sleep_seconds, cfg.fetch_retries)
        summary = await pipeline.fetch_all(cfg, store, fetcher, symbols)
        print(f"fetch 完成：{len(summary)} 份快取")
        if args.command == "fetch":
            return 0

    params: list[Any] = []
    tables: list[dict[str, Any]] = []
    forward = None
    if args.command in ("run", "all"):
        _labeled, params, tables = pipeline.run_event_study(cfg, store, symbols)
    if args.command in ("forward-report", "all"):
        from calibration.forward_log import build_forward_report, load_forward_rows

        try:
            forward = build_forward_report(load_forward_rows(), cfg)
        except Exception as e:
            forward = {
                "status": f"無法讀取前向蒐集資料: {e}",
                "total_labeled": 0,
                "sections": [],
            }

    results = report.build_results(
        cfg,
        params,
        tables,
        data_coverage=store.manifest(),
        forward=forward,
    )
    target = report.write_report(cfg, results, {"symbols": symbols})
    print(f"報告已輸出：{target}")
    return 0


async def _run_study(args: argparse.Namespace, cfg: CalibrationConfig) -> int:
    """微結構 (D-03/D-04) 與 Skew 代理研究。只寫快取與報告，不寫 DB。"""
    if args.command == "micro-snapshot":
        from calibration.microstructure import run_snapshot, snapshot_skip_reason
        from calibration.universe import build_universe

        if not args.any_time:
            reason = snapshot_skip_reason(Path(cfg.cache_dir))
            if reason:
                print(f"略過快照：{reason}")
                return 0

        symbols = (
            [s.strip().upper() for s in args.universe.split(",") if s.strip()]
            if args.universe
            else build_universe(cfg.max_symbols)
        )
        target = await run_snapshot(symbols, Path(cfg.cache_dir))
        print(f"快照已寫入：{target}")
        return 0

    if args.command == "micro-report":
        from calibration.microstructure import build_micro_report

        result = build_micro_report(Path(cfg.cache_dir))
    else:
        from calibration.skew_proxy import run_skew_proxy

        result = run_skew_proxy(Path(cfg.cache_dir))

    from calibration.report import write_study_report

    target = write_study_report(Path(cfg.out_dir), args.command, result)
    print(f"報告已輸出：{target}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse(argv)
    if not args.force:
        from services.llm_service import is_memory_safe

        if not is_memory_safe():
            print(
                "記憶體水位過高，中止 (請在開發機執行，或加 --force)", file=sys.stderr
            )
            return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
