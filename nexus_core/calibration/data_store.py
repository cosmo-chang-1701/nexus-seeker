"""磁碟快取：{cache_dir}/{interval}/{SYMBOL}.csv.gz + manifest.json。

用 csv.gz 而非 parquet：映像檔沒有 pyarrow，不為離線工具增加 production 依賴。
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def to_utc_index(index: pd.Index) -> pd.DatetimeIndex:
    """`get_history_df` 回傳去掉時區的**美東時間** index；naive 一律視為美東再轉
    UTC 儲存。若誤當 UTC，日線日期會錯一天、日內時點偏 4–5 小時。"""
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is None:
        idx = idx.tz_localize(
            "America/New_York", ambiguous="NaT", nonexistent="shift_forward"
        )
    return idx.tz_convert("UTC")


class CacheMissError(RuntimeError):
    """--offline 模式下快取不存在。"""


class DataStore:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)

    def _path(self, interval: str, symbol: str) -> Path:
        safe = _SAFE_RE.sub("_", symbol.upper())
        return self.cache_dir / interval / f"{safe}.csv.gz"

    def has(self, interval: str, symbol: str) -> bool:
        return self._path(interval, symbol).exists()

    def load(self, interval: str, symbol: str) -> Optional[pd.DataFrame]:
        path = self._path(interval, symbol)
        if not path.exists():
            return None
        df = pd.read_csv(path, index_col=0, compression="gzip")
        df.index = pd.to_datetime(df.index, utc=True)
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("float32")
        return df.sort_index()

    def require(self, interval: str, symbol: str) -> pd.DataFrame:
        df = self.load(interval, symbol)
        if df is None:
            raise CacheMissError(f"快取不存在: {interval}/{symbol} (請先執行 fetch)")
        return df

    def save(self, interval: str, symbol: str, df: pd.DataFrame) -> int:
        """合併既有快取 (增量)，依 index 去重後寫回。回傳總列數。"""
        if df is None or df.empty:
            return 0
        frame = df[
            [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
        ].copy()
        frame.index = to_utc_index(frame.index)
        frame = frame[frame.index.notna()]
        existing = self.load(interval, symbol)
        if existing is not None:
            frame = pd.concat([existing, frame])
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        path = self._path(interval, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, compression="gzip")
        self._update_manifest(interval, symbol, frame)
        return len(frame)

    def manifest(self) -> dict[str, Any]:
        path = self.cache_dir / "manifest.json"
        if not path.exists():
            return {}
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return data
        except (OSError, ValueError):
            return {}

    def _update_manifest(self, interval: str, symbol: str, frame: pd.DataFrame) -> None:
        data = self.manifest()
        data.setdefault(interval, {})[symbol.upper()] = {
            "rows": int(len(frame)),
            "start": frame.index[0].isoformat(),
            "end": frame.index[-1].isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "manifest.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# 研究型快取 (micro-snapshot / skew-proxy)：逐日累積的原始觀測
# ---------------------------------------------------------------------------


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    """覆寫一個 JSONL 快取檔，回傳寫入筆數。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return len(rows)


def read_jsonl_dir(directory: Path, pattern: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(Path(directory).glob(pattern)):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def save_frame(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
