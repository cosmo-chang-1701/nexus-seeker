"""基本面分析管線離線前向統計分析工具 (Fundamental Forward Report)。

遵循 Nexus Seeker 校準框架不變量：
- 僅於開發機離線執行，絕不寫入 Production 資料庫或修改線上參數。
- 支援 --snapshot-db 唯讀快照載入，未指定時以唯讀連線讀取本地快取庫。
- 產出估值安全邊際分佈、分析師修正動能分佈、PEAD 共振頻率與候選排名統計。

用法：
    python -m calibration fundamental-forward-report [--snapshot-db /path/to/snapshot.db] [--out /path/to/out]
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from market_analysis.fundamental_pipeline.fair_value import (
    DEEP_VALUE_MOS_THRESHOLD,
    FAIR_VALUE_MOS_BAND,
    MODERATE_DISCOUNT_MOS,
)

logger = logging.getLogger(__name__)

# load_fundamental_logs 回傳 dict 中記錄讀取問題的鍵（缺表 / 查詢失敗）
LOAD_ERRORS_KEY = "load_errors"

_FAIR_VALUE_SQL = """
SELECT symbol, trading_date, dcf_value, comps_value, fair_value,
       margin_of_safety, discount_rate, equity_risk_premium, flags_json,
       method, spot_price
FROM fair_value_log
ORDER BY trading_date ASC, symbol ASC
"""
# v094 定稿前的快照沒有 method / spot_price 欄位
_FAIR_VALUE_LEGACY_SQL = """
SELECT symbol, trading_date, dcf_value, comps_value, fair_value,
       margin_of_safety, discount_rate, equity_risk_premium, flags_json,
       NULL AS method, NULL AS spot_price
FROM fair_value_log
ORDER BY trading_date ASC, symbol ASC
"""
_REVISION_SQL = """
SELECT symbol, trading_date, score_30d, breadth_ratio, is_pead_aligned, detail_json
FROM revision_score_log
ORDER BY trading_date ASC, symbol ASC
"""
_WATCH_SQL = """
SELECT trading_date, symbol, rank, status, reasons_json, excluded_reason
FROM fundamental_watch_candidate
ORDER BY trading_date ASC, rank ASC
"""


def _query_table(
    cur: sqlite3.Cursor,
    table: str,
    sql: str,
    errors: list[dict[str, Any]],
    legacy_sql: str | None = None,
) -> list[tuple[Any, ...]]:
    """執行查詢；缺表與其他錯誤都記錄到 errors 並記 log，不默默吞掉。"""
    try:
        return list(cur.execute(sql).fetchall())
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "no such table" in msg:
            logger.warning(
                f"[FundamentalForwardReport] 缺少資料表 {table}（v094 未套用？）"
            )
            errors.append({"table": table, "kind": "MISSING_TABLE", "error": msg})
            return []
        if legacy_sql is not None and "no such column" in msg:
            logger.warning(
                f"[FundamentalForwardReport] {table} 為舊版欄位結構，以相容查詢讀取: {msg}"
            )
            errors.append({"table": table, "kind": "LEGACY_SCHEMA", "error": msg})
            return _query_table(cur, table, legacy_sql, errors)
        logger.error(f"[FundamentalForwardReport] 讀取 {table} 失敗: {msg}")
        errors.append({"table": table, "kind": "QUERY_ERROR", "error": msg})
        return []
    except sqlite3.Error as e:
        logger.error(f"[FundamentalForwardReport] 讀取 {table} 失敗: {e}")
        errors.append({"table": table, "kind": "QUERY_ERROR", "error": str(e)})
        return []


def load_fundamental_logs(
    snapshot_db: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """讀取基本面管線歷史資料表；支援外接唯讀快照或本地唯讀連線。

    缺表或查詢失敗時該表回傳空清單，並記錄到 `load_errors`（報告中明示），不默默略過。
    """
    if snapshot_db is not None:
        from database.connection import connect_external_readonly

        conn = connect_external_readonly(str(snapshot_db))
    else:
        from database.connection import get_read_connection

        conn = get_read_connection()

    errors: list[dict[str, Any]] = []
    try:
        cur = conn.cursor()

        fv_rows = [
            {
                "symbol": r[0],
                "trading_date": r[1],
                "dcf_value": r[2],
                "comps_value": r[3],
                "fair_value": r[4],
                "margin_of_safety": r[5],
                "discount_rate": r[6],
                "equity_risk_premium": r[7],
                "flags_json": r[8],
                "method": r[9],
                "spot_price": r[10],
            }
            for r in _query_table(
                cur, "fair_value_log", _FAIR_VALUE_SQL, errors, _FAIR_VALUE_LEGACY_SQL
            )
        ]
        rev_rows = [
            {
                "symbol": r[0],
                "trading_date": r[1],
                "score_30d": r[2],
                "breadth_ratio": r[3],
                "is_pead_aligned": bool(r[4]),
                "detail_json": r[5],
            }
            for r in _query_table(cur, "revision_score_log", _REVISION_SQL, errors)
        ]
        watch_rows = [
            {
                "trading_date": r[0],
                "symbol": r[1],
                "rank": r[2],
                "status": r[3],
                "reasons_json": r[4],
                "excluded_reason": r[5],
            }
            for r in _query_table(
                cur, "fundamental_watch_candidate", _WATCH_SQL, errors
            )
        ]

        return {
            "fair_value_log": fv_rows,
            "revision_score_log": rev_rows,
            "fundamental_watch_candidate": watch_rows,
            LOAD_ERRORS_KEY: errors,
        }
    finally:
        conn.close()


def is_valid_fair_value_row(row: dict[str, Any]) -> bool:
    """有效估值：method 不為 NONE、fair_value 與 margin_of_safety 非 NULL 且 fair_value > 0。

    舊版快照（無 method 欄位）只依 NULL / 非正值判定，0.0 哨兵視為無效。
    """
    method = row.get("method")
    fv = row.get("fair_value")
    mos = row.get("margin_of_safety")
    if method == "NONE" or fv is None or mos is None:
        return False
    try:
        return float(fv) > 0
    except (TypeError, ValueError):
        return False


def generate_fundamental_forward_report(logs: dict[str, list[dict[str, Any]]]) -> str:
    """生成結構化離線前向統計分析文字報告。"""
    fv_logs = logs.get("fair_value_log", [])
    rev_logs = logs.get("revision_score_log", [])
    watch_logs = logs.get("fundamental_watch_candidate", [])
    load_errors = logs.get(LOAD_ERRORS_KEY, [])

    lines: list[str] = [
        "=" * 78,
        "🌌 NEXUS SEEKER | 基本面管線離線前向校準報告 (Fundamental Forward Report)",
        "=" * 78,
        "",
    ]

    if load_errors:
        lines.append("【⚠️ 資料讀取問題】")
        for err in load_errors:
            kind = err.get("kind")
            table = err.get("table")
            if kind == "MISSING_TABLE":
                lines.append(f"• 缺少資料表 {table}（v094 尚未套用？），該節統計為空")
            elif kind == "LEGACY_SCHEMA":
                lines.append(
                    f"• {table} 為舊版欄位結構，已以相容查詢讀取：{err.get('error')}"
                )
            else:
                lines.append(f"• 讀取 {table} 失敗：{err.get('error')}")
        lines.append("")

    # 1. 總覽指標
    dates = sorted(
        {r["trading_date"] for r in fv_logs}
        | {r["trading_date"] for r in rev_logs}
        | {r["trading_date"] for r in watch_logs}
    )
    symbols = sorted(
        {r["symbol"] for r in fv_logs}
        | {r["symbol"] for r in rev_logs}
        | {r["symbol"] for r in watch_logs}
    )

    lines.append("【1. 涵蓋維度總覽】")
    lines.append(
        f"• 歷史掃描交易日數 : {len(dates)} 天 ({dates[0] if dates else 'N/A'} ~ {dates[-1] if dates else 'N/A'})"
    )
    lines.append(
        f"• 涵蓋獨立標的總數 : {len(symbols)} 檔 ({', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''})"
    )
    lines.append(f"• 公允價值估值筆數 : {len(fv_logs)} 筆")
    lines.append(f"• 分析師修正記錄數 : {len(rev_logs)} 筆")
    lines.append(f"• 候選觀察名單筆數 : {len(watch_logs)} 筆")
    lines.append("")

    # 2. 估值與安全邊際分佈
    lines.append("【2. 內在價值與安全邊際 (MOS) 統計】")
    valid_fv = [r for r in fv_logs if is_valid_fair_value_row(r)]
    if fv_logs and not valid_fv:
        lines.append(
            f"• 共 {len(fv_logs)} 筆估值皆無效（method = NONE 或公允價值 / 安全邊際為 NULL）"
        )
    elif fv_logs:
        mos_values = [float(r["margin_of_safety"]) for r in valid_fv]
        avg_mos = sum(mos_values) / len(mos_values)
        lines.append(
            f"• 有效估值筆數 : {len(valid_fv)} 筆（排除 method = NONE 或 NULL 共 "
            f"{len(fv_logs) - len(valid_fv)} 筆）"
        )
        deep_val_count = sum(1 for m in mos_values if m >= DEEP_VALUE_MOS_THRESHOLD)
        moderate_count = sum(
            1
            for m in mos_values
            if MODERATE_DISCOUNT_MOS <= m < DEEP_VALUE_MOS_THRESHOLD
        )
        fair_count = sum(
            1 for m in mos_values if -FAIR_VALUE_MOS_BAND <= m < MODERATE_DISCOUNT_MOS
        )
        overval_count = sum(1 for m in mos_values if m < -FAIR_VALUE_MOS_BAND)

        lines.append(f"• 平均安全邊際 (Mean MOS) : {avg_mos:+.2%}")
        lines.append(
            f"• 深度折價標的 (MOS >= 25%) : {deep_val_count} 筆 ({deep_val_count / len(mos_values):.1%})"
        )
        lines.append(
            f"• 中度折價標的 (10% <= MOS < 25%) : {moderate_count} 筆 ({moderate_count / len(mos_values):.1%})"
        )
        lines.append(
            f"• 合理估值區間 (-10% <= MOS < 10%) : {fair_count} 筆 ({fair_count / len(mos_values):.1%})"
        )
        lines.append(
            f"• 溢價偏高標的 (MOS < -10%) : {overval_count} 筆 ({overval_count / len(mos_values):.1%})"
        )

        dcf_valid_cnt = sum(1 for r in fv_logs if r["dcf_value"] is not None)
        comps_valid_cnt = sum(1 for r in fv_logs if r["comps_value"] is not None)
        lines.append(
            f"• DCF 模型有效覆蓋率 : {dcf_valid_cnt / len(fv_logs):.1%} ({dcf_valid_cnt}/{len(fv_logs)})"
        )
        lines.append(
            f"• Comps 模型有效覆蓋率 : {comps_valid_cnt / len(fv_logs):.1%} ({comps_valid_cnt}/{len(fv_logs)})"
        )
    else:
        lines.append("• 暫無公允價值歷史日誌")
    lines.append("")

    # 3. 分析師修正動能與 PEAD
    lines.append("【3. 分析師修正動能與 PEAD 共振分析】")
    scores = [float(r["score_30d"]) for r in rev_logs if r["score_30d"] is not None]
    if rev_logs and not scores:
        lines.append(
            f"• 共 {len(rev_logs)} 筆修正動能皆無可配對財期（score_30d 為 NULL）"
        )
    elif rev_logs:
        avg_score = sum(scores) / len(scores)
        lines.append(
            f"• 有效動能筆數 : {len(scores)} 筆（排除無可配對財期 "
            f"{len(rev_logs) - len(scores)} 筆）"
        )
        pos_rev_count = sum(1 for s in scores if s > 0)
        neg_rev_count = sum(1 for s in scores if s < 0)
        pead_aligned_count = sum(1 for r in rev_logs if r["is_pead_aligned"])

        lines.append(f"• 平均修正動能分數 : {avg_score:+.2f}")
        lines.append(
            f"• 正向向上調升比例 : {pos_rev_count / len(scores):.1%} ({pos_rev_count}/{len(scores)})"
        )
        lines.append(
            f"• 負向向下調降比例 : {neg_rev_count / len(scores):.1%} ({neg_rev_count}/{len(scores)})"
        )
        lines.append(
            f"• PEAD 共振觸發頻率 : {pead_aligned_count / len(rev_logs):.1%} ({pead_aligned_count}/{len(rev_logs)})"
        )
    else:
        lines.append("• 暫無分析師修正歷史日誌")
    lines.append("")

    # 4. 次日候選名單分佈
    lines.append("【4. 次日基本面候選名單統計】")
    if watch_logs:
        status_counter = Counter(r["status"] for r in watch_logs)
        lines.append(f"• CANDIDATE (優先候選) : {status_counter['CANDIDATE']} 筆")
        lines.append(f"• WATCH (觀察名單)     : {status_counter['WATCH']} 筆")
        lines.append(f"• EXCLUDED (風控排除)  : {status_counter['EXCLUDED']} 筆")

        top_candidates = [
            r["symbol"]
            for r in watch_logs
            if r["status"] == "CANDIDATE" and r["rank"] <= 3
        ]
        top_counter = Counter(top_candidates).most_common(5)
        if top_counter:
            freq_str = ", ".join(f"{sym} ({cnt} 次)" for sym, cnt in top_counter)
            lines.append(f"• 歷史排名前三常客     : {freq_str}")
    else:
        lines.append("• 暫無候選觀察名單歷史日誌")
    lines.append("")

    lines.append("=" * 78)
    if load_errors:
        lines.append(
            f"報告總結：有 {len(load_errors)} 項資料讀取問題（見開頭），相關統計不完整；唯讀離線執行。"
        )
    else:
        lines.append("報告總結：基本面離線前向指標擷取完備，唯讀離線執行。")
    lines.append("=" * 78)

    return "\n".join(lines)


def write_report(
    out_dir: Path, logs: dict[str, Any], markdown: str | None = None
) -> Path:
    """輸出 {out_dir}/calibration/fundamental-forward-report_{stamp}/（寫檔集中在 report.py）。"""
    from calibration.report import write_study_report

    if markdown is None:
        markdown = generate_fundamental_forward_report(logs)
    return write_study_report(
        out_dir, "fundamental-forward-report", logs, markdown=markdown
    )


def run_fundamental_forward_report(
    snapshot_db: Path | None = None,
    out_dir: Path | None = None,
) -> str:
    """執行離線分析並輸出或儲存報告。"""
    logs = load_fundamental_logs(snapshot_db=snapshot_db)
    report_text = generate_fundamental_forward_report(logs)

    if out_dir is not None:
        target = write_report(out_dir, logs, markdown=report_text)
        logger.info(f"報告已成功寫入: {target}")

    return report_text
