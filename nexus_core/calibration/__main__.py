"""python -m calibration {fetch|run|forward-report|all|micro-snapshot|micro-report|skew-proxy|notif-report|fetch-alpaca-1h|alpaca-seam}"""

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
            "notif-report",
            "fetch-alpaca-1h",
            "alpaca-seam",
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
        "--source",
        choices=["edge", "snapshot"],
        default="edge",
        help="micro-report 的資料來源：edge 前向蒐集歷史 (預設) 或 micro-snapshot 快取",
    )
    parser.add_argument(
        "--edge-db",
        default=None,
        help="micro-report --source edge：直接讀取複製來的 edge_cache.db；未指定時經 TUNNEL_URL 讀取",
    )
    parser.add_argument(
        "--snapshot-db",
        default=None,
        help="notif-report：以唯讀模式讀取複製來的 production 快照；未指定時用 NEXUS_DB_NAME",
    )
    parser.add_argument(
        "--start",
        default="2021-11-01",
        help="fetch-alpaca-1h / alpaca-seam：起始日 (ISO)",
    )
    parser.add_argument(
        "--end",
        default="2025-12-31",
        help="fetch-alpaca-1h / alpaca-seam：結束日 (ISO)",
    )
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
    if args.command in ("fetch-alpaca-1h", "alpaca-seam"):
        return await _run_alpaca(args, cfg)
    if args.command == "notif-report":
        from calibration.notif_report import (
            DEFAULT_SEED,
            build_notif_report,
            load_rows,
            write_notif_report,
        )

        rows = load_rows(Path(args.snapshot_db) if args.snapshot_db else None)
        result = build_notif_report(
            rows, seed=args.seed if args.seed is not None else DEFAULT_SEED
        )
        target = write_notif_report(Path(cfg.out_dir), result)
        print(f"報告已輸出：{target.parent}")
        return 0

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


async def _run_alpaca(args: argparse.Namespace, cfg: CalibrationConfig) -> int:
    """Alpaca 歷史 1h 線：取代（fetch-alpaca-1h）或與既有快取比對（alpaca-seam）。

    fetch-alpaca-1h 以 Alpaca 整段取代快取中 [--start, --end] 的 1h K 棒（區間外保留）；
    alpaca-seam 只比對、不寫快取，結果輸出為 JSON 報告。
    """
    import json

    from calibration.alpaca_history import (
        compare_with_cache,
        iter_hourly,
        replace_range,
    )

    symbols = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    if not symbols:
        print("請以 --universe 指定標的", file=sys.stderr)
        return 2
    store = DataStore(cfg.cache_dir)
    rows: list[dict[str, Any]] = []
    async for sym, df in iter_hourly(symbols, args.start, args.end):
        if args.command == "fetch-alpaca-1h":
            total = replace_range(store, sym, df, args.start, args.end)
            print(
                f"{sym}: 以 {len(df)} 根 Alpaca K 棒取代 {args.start}～{args.end}，"
                f"快取共 {total} 根",
                flush=True,
            )
        else:
            cmp = compare_with_cache(store, sym, df)
            if cmp:
                rows.append(cmp)
    if args.command == "fetch-alpaca-1h":
        return 0
    result = {"start": args.start, "end": args.end, "symbols": rows}
    from calibration.report import write_study_report

    target = write_study_report(Path(cfg.out_dir), "alpaca-seam", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
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

        snapshots = None
        if args.source == "edge":
            from calibration.edge_history import EdgeHistorySource, build_edge_snapshots

            if args.edge_db:
                source = EdgeHistorySource(db_path=Path(args.edge_db))
            else:
                from config import TUNNEL_URL

                if not TUNNEL_URL:
                    print(
                        "未設定 TUNNEL_URL：請提供 --edge-db <edge_cache.db>，"
                        "或改用 --source snapshot",
                        file=sys.stderr,
                    )
                    return 2
                source = EdgeHistorySource(base_url=str(TUNNEL_URL))
            edge_symbols: Optional[list[str]] = (
                [s.strip().upper() for s in args.universe.split(",") if s.strip()]
                if args.universe
                else None
            )
            snapshots = build_edge_snapshots(source, edge_symbols)
        result = build_micro_report(Path(cfg.cache_dir), snapshots=snapshots)
        result["source"] = args.source
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
