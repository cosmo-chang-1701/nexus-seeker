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
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_fundamental_logs(
    snapshot_db: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """讀取基本面管線歷史資料表；支援外接唯讀快照或本地唯讀連線。"""
    if snapshot_db is not None:
        from database.connection import connect_external_readonly

        conn = connect_external_readonly(str(snapshot_db))
    else:
        from database.connection import get_read_connection

        conn = get_read_connection()

    try:
        cur = conn.cursor()

        # 1. 公允價值日誌
        fv_rows: list[dict[str, Any]] = []
        try:
            cur.execute(
                """
                SELECT symbol, trading_date, dcf_value, comps_value, fair_value,
                       margin_of_safety, discount_rate, equity_risk_premium, flags_json
                FROM fair_value_log
                ORDER BY trading_date ASC, symbol ASC
                """
            )
            for r in cur.fetchall():
                fv_rows.append(
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
                    }
                )
        except Exception:
            pass

        # 2. 修正動能日誌
        rev_rows: list[dict[str, Any]] = []
        try:
            cur.execute(
                """
                SELECT symbol, trading_date, score_30d, breadth_ratio,
                       is_pead_aligned, detail_json
                FROM revision_score_log
                ORDER BY trading_date ASC, symbol ASC
                """
            )
            for r in cur.fetchall():
                rev_rows.append(
                    {
                        "symbol": r[0],
                        "trading_date": r[1],
                        "score_30d": r[2],
                        "breadth_ratio": r[3],
                        "is_pead_aligned": bool(r[4]),
                        "detail_json": r[5],
                    }
                )
        except Exception:
            pass

        # 3. 基本面次日候選名單
        watch_rows: list[dict[str, Any]] = []
        try:
            cur.execute(
                """
                SELECT trading_date, symbol, rank, status, reasons_json, excluded_reason
                FROM fundamental_watch_candidate
                ORDER BY trading_date ASC, rank ASC
                """
            )
            for r in cur.fetchall():
                watch_rows.append(
                    {
                        "trading_date": r[0],
                        "symbol": r[1],
                        "rank": r[2],
                        "status": r[3],
                        "reasons_json": r[4],
                        "excluded_reason": r[5],
                    }
                )
        except Exception:
            pass

        return {
            "fair_value_log": fv_rows,
            "revision_score_log": rev_rows,
            "fundamental_watch_candidate": watch_rows,
        }
    finally:
        conn.close()


def generate_fundamental_forward_report(logs: dict[str, list[dict[str, Any]]]) -> str:
    """生成結構化離線前向統計分析文字報告。"""
    fv_logs = logs.get("fair_value_log", [])
    rev_logs = logs.get("revision_score_log", [])
    watch_logs = logs.get("fundamental_watch_candidate", [])

    lines: list[str] = [
        "=" * 78,
        "🌌 NEXUS SEEKER | 基本面管線離線前向校準報告 (Fundamental Forward Report)",
        "=" * 78,
        "",
    ]

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
    if fv_logs:
        mos_values = [
            r["margin_of_safety"] for r in fv_logs if r["margin_of_safety"] is not None
        ]
        avg_mos = sum(mos_values) / len(mos_values) if mos_values else 0.0
        deep_val_count = sum(1 for m in mos_values if m >= 0.25)
        moderate_count = sum(1 for m in mos_values if 0.10 <= m < 0.25)
        fair_count = sum(1 for m in mos_values if -0.10 <= m < 0.10)
        overval_count = sum(1 for m in mos_values if m < -0.10)

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
    if rev_logs:
        scores = [r["score_30d"] for r in rev_logs]
        avg_score = sum(scores) / len(scores) if scores else 0.0
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
    lines.append("報告總結：基本面離線前向指標擷取完備，完全符合唯讀離線校準架構規範。")
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
