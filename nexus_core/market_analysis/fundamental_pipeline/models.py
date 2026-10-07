"""基本面分析管線 (Fundamental Event Pipeline) 核心資料模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

LiquidityRegime = Literal["EASY", "NEUTRAL", "TIGHT", "UNKNOWN"]


@dataclass(frozen=True)
class LiquidityReading:
    """央行淨流動性與體制狀態讀數。"""

    trading_date: date
    nfci: float | None
    anfci: float | None
    net_liquidity_bn: float | None
    net_liquidity_chg_13w_pct: float | None
    reserves_chg_13w_pct: float | None
    us10y: float | None
    regime: LiquidityRegime
    equity_risk_premium: float | None


@dataclass(frozen=True)
class MacroSurpriseReading:
    """宏觀數據發布預期差標準化讀數。"""

    event_key: str
    release_time_utc: str
    actual: float
    forecast: float
    raw_diff: float
    z_score: float | None
    growth_sign: int  # 1: 正向經濟增長指標, -1: 反向/通膨/緊縮指標


# ============================================================================
# PR2 SEC 申報與治理審查資料模型
# ============================================================================

FilingSession = Literal["BMO", "RTH", "AMC", "OVERNIGHT"]
GovernanceSeverity = Literal["INFO", "REVIEW", "HIGH", "CRITICAL"]
InsiderSignalVerdict = Literal["CLUSTER_BUY", "HEAVY_INSIDER_SALE", "NEUTRAL"]


@dataclass(frozen=True)
class FilingCursorRecord:
    """SEC 申報追蹤游標記錄。"""

    symbol: str
    cik: str
    last_accepted_at: str
    last_accession: str
    updated_at: str = ""


@dataclass(frozen=True)
class FilingEventRecord:
    """SEC 申報事件記錄。"""

    accession: str
    symbol: str
    form: str
    items: str | None
    accepted_at: str
    session: FilingSession
    primary_doc_url: str | None = None
    routes_json: str | None = None
    is_backfill: bool = False
    created_at: str = ""


@dataclass(frozen=True)
class InsiderTxRecord:
    """內部人交易明細記錄。"""

    accession: str
    line_no: int
    symbol: str
    owner_name: str
    owner_role: str = "OTHER"
    is_c_suite: bool = False
    tx_date: str = ""
    tx_code: str = "UNKNOWN"
    shares: float = 0.0
    price: float = 0.0
    acquired_disposed: str = "D"
    shares_after: float = 0.0
    is_10b5_1: bool = False
    is_backfill: bool = False
    created_at: str = ""
    # 主申報人 CIK（10 位補零）：聚合時的內部人身分鍵；無 CIK 時退回 owner_name。
    # 持久化時編碼於 insider_transaction.owner_name 欄（`{cik}|{names}`），不改 schema。
    owner_cik: str | None = None
    # 是否來自修正申報（Form 4/A）；讀取時由 sec_filing_event.form 還原。
    is_amendment: bool = False
    # 所屬申報之受理時間（美東 ISO 8601），用於多份 4/A 取最新者；讀取時由事件表還原。
    filing_accepted_at: str = ""

    @property
    def owner_key(self) -> str:
        """內部人身分鍵：優先 CIK，否則為正規化後的申報人名稱。"""
        if self.owner_cik:
            return f"CIK:{self.owner_cik}"
        return f"NAME:{self.owner_name.strip().upper()}"


InsiderTransactionDTO = InsiderTxRecord


@dataclass(frozen=True)
class GovernanceFlagRecord:
    """公司治理審查旗標記錄。"""

    symbol: str
    source_accession: str
    flag_kind: str
    severity: GovernanceSeverity
    detail_json: str | None = None
    expires_at: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class InsiderSignalSummary:
    """內部人交易訊號聚合評估。"""

    symbol: str
    as_of_date: str
    window_days: int
    cluster_buy_count: int
    c_suite_buy_count: int
    total_net_bought_shares: float
    total_net_bought_usd: float
    cluster_sale_count: int
    total_net_sold_usd: float
    verdict: InsiderSignalVerdict
    summary_text: str


@dataclass(frozen=True)
class ActivistSignal:
    """激進投資人 / 13D 訊號。"""

    symbol: str
    accession: str
    investor_name: str
    ownership_pct: float
    is_delayed_filing: bool
    key_intents: list[str]
    summary_text: str


@dataclass(frozen=True)
class GovernanceStatus:
    """標的治理審查狀態。"""

    symbol: str
    is_clean: bool
    max_severity: GovernanceSeverity | None
    active_flags: list[GovernanceFlagRecord]
