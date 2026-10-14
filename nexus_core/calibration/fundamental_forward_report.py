"""基本面分析管線離線前向統計分析工具 (Fundamental Forward Report)。

遵循 Nexus Seeker 校準框架不變量：
- 僅於開發機離線執行，絕不寫入 Production 資料庫或修改線上參數。
- 支援 --snapshot-db 唯讀快照載入，未指定時以唯讀連線讀取本地快取庫。
- 產出估值安全邊際分佈、分析師修正動能分佈、PEAD 共振頻率與候選排名統計。

用法：
    python -m calibration fundamental-forward-report [--snapshot-db /path/to/snapshot.db] [--out /path/to/out]
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from market_analysis.fundamental_pipeline.fair_value import (
    DEEP_VALUE_MOS_THRESHOLD,
    FAIR_VALUE_MOS_BAND,
    FLAG_VALUATION_DIVERGENCE,
    MODERATE_DISCOUNT_MOS,
    VALUATION_DIVERGENCE_RATIO,
)

logger = logging.getLogger(__name__)

# load_fundamental_logs 回傳 dict 中記錄讀取問題的鍵（缺表 / 查詢失敗）
LOAD_ERRORS_KEY = "load_errors"

_FAIR_VALUE_SQL = """
SELECT symbol, trading_date, dcf_value, comps_value, fair_value,
       margin_of_safety, discount_rate, equity_risk_premium, flags_json,
       method, spot_price, comps_detail_json
FROM fair_value_log
ORDER BY trading_date ASC, symbol ASC
"""
# v095 前的快照沒有 comps_detail_json 欄位
_FAIR_VALUE_PRE_V095_SQL = """
SELECT symbol, trading_date, dcf_value, comps_value, fair_value,
       margin_of_safety, discount_rate, equity_risk_premium, flags_json,
       method, spot_price, NULL AS comps_detail_json
FROM fair_value_log
ORDER BY trading_date ASC, symbol ASC
"""
# v094 定稿前的快照沒有 method / spot_price 欄位
_FAIR_VALUE_LEGACY_SQL = """
SELECT symbol, trading_date, dcf_value, comps_value, fair_value,
       margin_of_safety, discount_rate, equity_risk_premium, flags_json,
       NULL AS method, NULL AS spot_price, NULL AS comps_detail_json
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
    legacy_sqls: Sequence[str] = (),
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
        if legacy_sqls and "no such column" in msg:
            logger.warning(
                f"[FundamentalForwardReport] {table} 為舊版欄位結構，以相容查詢讀取: {msg}"
            )
            errors.append({"table": table, "kind": "LEGACY_SCHEMA", "error": msg})
            return _query_table(cur, table, legacy_sqls[0], errors, legacy_sqls[1:])
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
                "comps_detail_json": r[11],
            }
            for r in _query_table(
                cur,
                "fair_value_log",
                _FAIR_VALUE_SQL,
                errors,
                (_FAIR_VALUE_PRE_V095_SQL, _FAIR_VALUE_LEGACY_SQL),
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


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """線性內插百分位數（輸入須已排序且非空，q 介於 0~1）。"""
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def _distribution(values: Sequence[float]) -> dict[str, float] | None:
    """回傳 median / p25 / p75 / mean；無資料時回傳 None。"""
    if not values:
        return None
    ordered = sorted(values)
    return {
        "median": _percentile(ordered, 0.5),
        "p25": _percentile(ordered, 0.25),
        "p75": _percentile(ordered, 0.75),
        "mean": sum(ordered) / len(ordered),
    }


def _row_spot(row: dict[str, Any]) -> float | None:
    """現價：優先 spot_price；舊快照無此欄時由 fair_value 與 MOS 反推 (spot = FV * (1 - MOS))。"""
    spot = row.get("spot_price")
    if spot is not None:
        try:
            return float(spot) if float(spot) > 0 else None
        except (TypeError, ValueError):
            return None
    fv, mos = row.get("fair_value"), row.get("margin_of_safety")
    if fv is None or mos is None:
        return None
    try:
        derived = float(fv) * (1.0 - float(mos))
    except (TypeError, ValueError):
        return None
    return derived if derived > 0 else None


def _positive(val: Any) -> float | None:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _is_diverged(row: dict[str, Any]) -> bool:
    """DCF 與 Comps 皆 > 0 且比值 >= 門檻；舊資料沒有旗標，改由比值現算。"""
    dcf, comps = _positive(row.get("dcf_value")), _positive(row.get("comps_value"))
    if dcf is None or comps is None:
        return False
    return max(dcf, comps) / min(dcf, comps) >= VALUATION_DIVERGENCE_RATIO


def _parse_detail(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_valuation_robustness(
    fv_logs: Sequence[dict[str, Any]],
    watch_logs: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """估值穩健度統計（純函式，只讀日誌，不改任何判定）。

    - mos: 有效估值之 MOS 分佈（中位數與四分位為主，平均受極端值主導僅供參考）。
    - per_method: 各估值法單獨計算的 MOS，(value - spot) / value；只計 value > 0 且現價可得者。
    - candidate_robustness: CANDIDATE 在 DCF_ONLY 假設下是否仍 >= 25%，以及估值分歧筆數。
    - candidate_comps_detail: 每檔 CANDIDATE 最新一筆的 comps 輸入（舊資料為 None）。
    """
    valid = [r for r in fv_logs if is_valid_fair_value_row(r)]
    result: dict[str, Any] = {
        "mos": _distribution([float(r["margin_of_safety"]) for r in valid]),
    }

    per_method: dict[str, Any] = {}
    for label, key in (("dcf", "dcf_value"), ("comps", "comps_value")):
        mos_list: list[float] = []
        for r in fv_logs:
            val, spot = _positive(r.get(key)), _row_spot(r)
            if val is not None and spot is not None:
                mos_list.append((val - spot) / val)
        per_method[label] = {
            "n": len(mos_list),
            "distribution": _distribution(mos_list),
            "n_ge_deep_value": sum(
                1 for m in mos_list if m >= DEEP_VALUE_MOS_THRESHOLD
            ),
        }
    result["per_method"] = per_method

    fv_index: dict[tuple[str, str], dict[str, Any]] = {
        (r["trading_date"], r["symbol"]): r for r in fv_logs
    }
    cand_rows = [r for r in watch_logs if r.get("status") == "CANDIDATE"]
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for c in cand_rows:
        fv_row = fv_index.get((c["trading_date"], c["symbol"]))
        if fv_row is not None:
            matched.append((c, fv_row))

    dcf_only_pass = 0
    diverged = 0
    for _, fv_row in matched:
        dcf, spot = _positive(fv_row.get("dcf_value")), _row_spot(fv_row)
        if dcf is not None and spot is not None:
            if (dcf - spot) / dcf >= DEEP_VALUE_MOS_THRESHOLD:
                dcf_only_pass += 1
        flags = _safe_flags(fv_row.get("flags_json"))
        if FLAG_VALUATION_DIVERGENCE in flags or _is_diverged(fv_row):
            diverged += 1
    result["candidate_robustness"] = {
        "n_candidates": len(cand_rows),
        "n_matched": len(matched),
        "dcf_only_still_deep_value": dcf_only_pass,
        "valuation_divergence": diverged,
    }

    latest: dict[str, dict[str, Any]] = {}
    for _, fv_row in matched:  # fv_logs 依日期遞增，後者覆蓋前者即最新
        latest[fv_row["symbol"]] = fv_row
    detail_rows: list[dict[str, Any]] = []
    for sym in sorted(latest):
        detail = _parse_detail(latest[sym].get("comps_detail_json"))
        peers = (
            [
                str(p.get("symbol"))
                for p in detail.get("peers", [])
                if isinstance(p, dict)
            ]
            if detail
            else []
        )
        detail_rows.append(
            {
                "symbol": sym,
                "trading_date": latest[sym]["trading_date"],
                "median_pe": detail.get("median_pe") if detail else None,
                "forward_eps": detail.get("forward_eps") if detail else None,
                "peers": peers,
            }
        )
    result["candidate_comps_detail"] = detail_rows
    return result


def _safe_flags(raw: Any) -> list[str]:
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _fmt_dist(d: dict[str, float] | None) -> str:
    if d is None:
        return "無資料"
    return f"{d['median']:+.2%} (p25 {d['p25']:+.2%} / p75 {d['p75']:+.2%})"


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

        dist = _distribution(mos_values)
        lines.append(f"• 中位數安全邊際 (Median MOS) : {_fmt_dist(dist)}")
        lines.append(
            f"• 平均安全邊際 (Mean MOS) : {avg_mos:+.2%}（MOS 下界無限，受極端值主導，僅供參考）"
        )
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

        robustness = build_valuation_robustness(fv_logs, watch_logs)
        lines.append("")
        lines.append(
            "  ▸ 各估值法單獨計算的 MOS（(估值 - 現價) / 估值，只計估值 > 0 者）"
        )
        for label, name in (("dcf", "DCF"), ("comps", "Comps")):
            pm = robustness["per_method"][label]
            lines.append(
                f"    - {name:<5}: n={pm['n']}，中位數 {_fmt_dist(pm['distribution'])}，"
                f"MOS >= 25% 共 {pm['n_ge_deep_value']} 筆"
            )
        cr = robustness["candidate_robustness"]
        lines.append("  ▸ 候選名單穩健度（CANDIDATE 與同日估值配對）")
        if cr["n_matched"] == 0:
            lines.append("    - 無可配對的 CANDIDATE 估值")
        else:
            lines.append(
                f"    - DCF_ONLY 假設下仍 >= 25% : {cr['dcf_only_still_deep_value']}/{cr['n_matched']} 筆"
            )
            lines.append(
                f"    - DCF 與 Comps 相差 {VALUATION_DIVERGENCE_RATIO:.0f} 倍以上 : "
                f"{cr['valuation_divergence']}/{cr['n_matched']} 筆（舊資料由比值現算）"
            )
        details = robustness["candidate_comps_detail"]
        if details:
            lines.append(
                "  ▸ CANDIDATE 同業乘數輸入（各標的最新一筆；「—」為尚無 comps_detail_json）"
            )
            for d in details:
                if (
                    d["median_pe"] is None
                    and d["forward_eps"] is None
                    and not d["peers"]
                ):
                    lines.append(f"    - {d['symbol']}: —")
                    continue
                med = f"{d['median_pe']:.2f}" if d["median_pe"] is not None else "—"
                eps = f"{d['forward_eps']:.2f}" if d["forward_eps"] is not None else "—"
                peers_str = ", ".join(d["peers"]) if d["peers"] else "—"
                lines.append(
                    f"    - {d['symbol']}: 中位數 PE {med}，forward EPS {eps}，同業 {peers_str}"
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
