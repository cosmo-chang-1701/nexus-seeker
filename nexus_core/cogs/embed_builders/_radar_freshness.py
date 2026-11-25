"""雷達掃描列的資料時效判定（純函式，不做 I/O、不建 embed）。

供雷達 embed 的 🕓／🌙 標示與更新按鈕共用同一套判定。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from cogs.embed_builders._core import format_cache_age_suffix
from services.market_data_service import _EDGE_SNAPSHOT_MAX_AGE_SECONDS

# UOA kv_cache 新鮮度門檻：15 分鐘心跳週期的 2 倍緩衝，數值沿用 edge 快照最大年齡。
_UOA_SNAPSHOT_MAX_AGE_SECONDS: float = float(_EDGE_SNAPSHOT_MAX_AGE_SECONDS)

# 固定收益標的不讀 Max Pain
_FIXED_INCOME_SYMBOLS: frozenset[str] = frozenset({"BOXX", "BIL", "SHV"})


@dataclass(frozen=True)
class StaleSource:
    """單一過期／降級的資料來源。"""

    label: str  # "GEX" | "UOA" | "MP"
    age_seconds: Optional[float]  # None = 年齡不明
    degraded: bool = False  # True → 顯示「降級」而非年齡


@dataclass(frozen=True)
class RowFreshness:
    """單一掃描列的資料時效判定結果。"""

    sources: tuple[StaleSource, ...]
    is_premarket: bool
    is_genuine_degradation: bool  # MP 過期／降級或斷路器觸發

    @property
    def is_stale(self) -> bool:
        return bool(self.sources)

    @property
    def is_benign_premarket(self) -> bool:
        """非交易時段的單純年齡過期（屬正常現象）。"""
        return self.is_stale and self.is_premarket and not self.is_genuine_degradation

    def compact_note(self) -> str:
        """逐來源的精簡註記，例如「GEX 52分鐘前/MP 降級」。"""
        return "/".join(_source_note(s) for s in self.sources)


def _source_note(source: StaleSource) -> str:
    if source.degraded:
        return f"{source.label} 降級"
    suffix = format_cache_age_suffix(source.age_seconds)
    # suffix 形如「 (更新於N分鐘前)」；年齡不明時為空字串
    _, marker, tail = suffix.partition("更新於")
    age_text = tail.rstrip(")") if marker else "時間不明"
    return f"{source.label} {age_text}"


def assess_radar_row(r: Mapping[str, Any]) -> RowFreshness:
    """判定掃描列各資料來源是否過期／降級；缺鍵一律視為無異常。"""
    iv_metrics: Any = r.get("iv_metrics")
    is_premarket = False
    if iv_metrics is not None:
        if hasattr(iv_metrics, "is_premarket"):
            is_premarket = bool(iv_metrics.is_premarket)
        elif isinstance(iv_metrics, dict):
            is_premarket = bool(iv_metrics.get("is_premarket", False))

    sources: list[StaleSource] = []

    gex_metrics = r.get("gex_metrics")
    if isinstance(gex_metrics, dict) and gex_metrics.get("_is_stale_cache"):
        gex_age = (r.get("_freshness") or {}).get("gex_age_seconds")
        sources.append(StaleSource("GEX", gex_age))

    uoa_age = r.get("uoa_age_seconds")
    if uoa_age is not None and uoa_age > _UOA_SNAPSHOT_MAX_AGE_SECONDS:
        sources.append(StaleSource("UOA", uoa_age))

    mp_flagged = False
    circuit_breaker = False
    is_fixed_income = str(r.get("symbol") or "").upper() in _FIXED_INCOME_SYMBOLS
    mp_data = r.get("max_pain")
    if not is_fixed_income and isinstance(mp_data, dict):
        circuit_breaker = bool(mp_data.get("circuit_breaker_triggered", False))
        mp_flagged = bool(
            mp_data.get("is_stale", False)
            or mp_data.get("is_degraded", False)
            or mp_data.get("calculation_mode", "OI") == "Volume"
        )
    if mp_flagged:
        # MP 重算失敗回退舊值時 updated_at 會被蓋為現在，年齡不可信
        sources.append(StaleSource("MP", None, degraded=True))

    return RowFreshness(
        sources=tuple(sources),
        is_premarket=is_premarket,
        is_genuine_degradation=mp_flagged or circuit_breaker,
    )
