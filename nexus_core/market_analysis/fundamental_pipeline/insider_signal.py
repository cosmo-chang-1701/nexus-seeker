"""高管內部人交易訊號聚合評估器。

依據 SEC Form 4 滾動窗口（預設 30 天）明細進行定量清洗與行為聚合：
- 自費增持 (P=Open Market Purchase) vs 自行拋售 (S=Open Market Sale)
- 10b5-1 預先排程計畫過濾（識別真正具備資訊優勢之非計畫性交易）
- 聚類增持 (CLUSTER_BUY)、巨額非計畫性拋售 (HEAVY_INSIDER_SALE) 與中性 (NEUTRAL) 狀態機判定。
- 內部人身分鍵：主申報人 CIK（`InsiderTxRecord.owner_key`），避免共同申報的名稱串接字串
  被當成另一位內部人。
- 修正申報：Form 4/A 取代同一申報人、同交易日的原始明細（`dedupe_amended_transactions`）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from market_analysis.fundamental_pipeline.models import (
    InsiderSignalSummary,
    InsiderSignalVerdict,
    InsiderTxRecord,
)
from market_analysis.fundamental_pipeline.sec_item_router import (
    parse_sec_acceptance_datetime,
)

# 評判門檻常數
_MIN_CLUSTER_BUY_INSIDERS = 2  # 形成聚類買入之最少獨立內部人數
_C_SUITE_MIN_BUY_USD = 100_000.0  # 單一 C-Suite 高管買入達此金額即可構成聚類信號
_MIN_CLUSTER_SALE_INSIDERS = 3  # 形成非計畫拋售之最少獨立內部人數
_HEAVY_SALE_USD_THRESHOLD = 5_000_000.0  # 非 10b5-1 拋售達此金額構成重度拋售警示


def _filing_order_key(tx: InsiderTxRecord) -> tuple[datetime, str]:
    """修正申報先後順序：受理時間優先，無法解析時退回 accession 字串。"""
    try:
        accepted = parse_sec_acceptance_datetime(tx.filing_accepted_at)
    except (ValueError, TypeError):
        accepted = datetime.min.replace(tzinfo=timezone.utc)
    return accepted, tx.accession


def dedupe_amended_transactions(
    txs: Sequence[InsiderTxRecord],
) -> list[InsiderTxRecord]:
    """以 Form 4/A 修正申報取代同一申報人、同交易日的原始 Form 4 明細。

    分組鍵為 (身分鍵, 交易日)。組內只要存在修正申報，就只保留「最新一份修正申報」
    的明細（4/A 依 SEC 規定須完整重述被修正的交易列），原始 Form 4 與較舊的 4/A
    一律捨棄；組內沒有修正申報時原樣保留。輸出維持輸入順序。
    """
    latest_amendment: dict[tuple[str, str], InsiderTxRecord] = {}
    for t in txs:
        if not t.is_amendment:
            continue
        key = (t.owner_key, t.tx_date)
        current = latest_amendment.get(key)
        if current is None or _filing_order_key(t) > _filing_order_key(current):
            latest_amendment[key] = t

    if not latest_amendment:
        return list(txs)

    result: list[InsiderTxRecord] = []
    for t in txs:
        winner = latest_amendment.get((t.owner_key, t.tx_date))
        if winner is None or t.accession == winner.accession:
            result.append(t)
    return result


def evaluate_insider_signal(
    symbol: str,
    txs: Sequence[InsiderTxRecord],
    as_of_date: str | None = None,
    window_days: int = 30,
) -> InsiderSignalSummary:
    """評估指定標的在滾動窗口內的內部人交易訊號。"""
    sym_upper = symbol.strip().upper()
    if as_of_date is None:
        ref_dt = datetime.now(timezone.utc).date()
        as_of_str = ref_dt.isoformat()
    else:
        as_of_str = as_of_date.strip()[:10]
        try:
            ref_dt = date.fromisoformat(as_of_str)
        except ValueError:
            ref_dt = datetime.now(timezone.utc).date()

    cutoff_date = ref_dt - timedelta(days=window_days)
    cutoff_str = cutoff_date.isoformat()

    # 1. 篩選在窗口內的非衍生品交易（先以 4/A 取代原始明細，避免重複聚合）
    window_txs = [
        t
        for t in dedupe_amended_transactions(txs)
        if t.symbol.upper() == sym_upper
        and t.tx_date
        and t.tx_date >= cutoff_str
        and t.tx_date <= as_of_str
    ]

    buyers: set[str] = set()
    c_suite_buyers: set[str] = set()
    sellers_discretionary: set[str] = set()

    total_bought_shares = 0.0
    total_bought_usd = 0.0
    total_sold_usd = 0.0
    total_sold_usd_discretionary = 0.0

    for t in window_txs:
        # 代碼 P: 公開市場自費買入
        if t.tx_code == "P" and t.acquired_disposed == "A":
            cost_usd = t.shares * t.price if t.price > 0 else 0.0
            buyers.add(t.owner_key)
            if t.is_c_suite:
                c_suite_buyers.add(t.owner_key)
            total_bought_shares += t.shares
            total_bought_usd += cost_usd

        # 代碼 S: 公開市場賣出
        elif t.tx_code == "S" and t.acquired_disposed == "D":
            proceeds_usd = t.shares * t.price if t.price > 0 else 0.0
            total_sold_usd += proceeds_usd
            if not t.is_10b5_1:
                # 排除 10b5-1 預先排程賣單，聚焦自主決策賣出
                sellers_discretionary.add(t.owner_key)
                total_sold_usd_discretionary += proceeds_usd

    cluster_buy_count = len(buyers)
    c_suite_buy_count = len(c_suite_buyers)
    cluster_sale_count = len(sellers_discretionary)

    # 2. 狀態機仲裁判定
    verdict: InsiderSignalVerdict = "NEUTRAL"
    summary_parts: list[str] = []

    is_cluster_buy_criteria = cluster_buy_count >= _MIN_CLUSTER_BUY_INSIDERS or (
        c_suite_buy_count >= 1 and total_bought_usd >= _C_SUITE_MIN_BUY_USD
    )

    is_heavy_sale_criteria = (
        cluster_sale_count >= _MIN_CLUSTER_SALE_INSIDERS
        or total_sold_usd_discretionary >= _HEAVY_SALE_USD_THRESHOLD
    )

    # 仲裁：若非排程拋售顯著大於買入且符合拋售警戒門檻，絕不可誤報為 CLUSTER_BUY
    if total_bought_usd < total_sold_usd_discretionary and is_heavy_sale_criteria:
        verdict = "HEAVY_INSIDER_SALE"
        summary_parts.append(
            f"{window_days}D 內 {cluster_sale_count} 位內部人非計畫性拋售達 ${total_sold_usd_discretionary:,.0f}"
        )
    elif total_bought_usd >= total_sold_usd_discretionary and is_cluster_buy_criteria:
        verdict = "CLUSTER_BUY"
        summary_parts.append(
            f"{window_days}D 內 {cluster_buy_count} 位內部人自費增持達 ${total_bought_usd:,.0f}"
        )
        if c_suite_buy_count > 0:
            summary_parts.append(f"含 {c_suite_buy_count} 位 C-Suite 決策層")
    else:
        verdict = "NEUTRAL"
        if cluster_buy_count > 0:
            summary_parts.append(f"{window_days}D 內部人買入 ${total_bought_usd:,.0f}")
        elif total_sold_usd > 0:
            summary_parts.append(
                f"{window_days}D 內部人總拋售 ${total_sold_usd:,.0f}（含排程）"
            )
        else:
            summary_parts.append(f"最近 {window_days} 天無重大公開市場增減持")

    summary_text = "，".join(summary_parts)

    return InsiderSignalSummary(
        symbol=sym_upper,
        as_of_date=as_of_str,
        window_days=window_days,
        cluster_buy_count=cluster_buy_count,
        c_suite_buy_count=c_suite_buy_count,
        total_net_bought_shares=total_bought_shares,
        total_net_bought_usd=total_bought_usd,
        cluster_sale_count=cluster_sale_count,
        total_net_sold_usd=total_sold_usd,
        verdict=verdict,
        summary_text=summary_text,
    )
