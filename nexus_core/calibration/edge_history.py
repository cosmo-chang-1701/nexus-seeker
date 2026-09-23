"""讀取 edge 前向蒐集的歷史，轉成 `micro-report` 的每日快照格式。

資料來源（edge 服務，見 nexus_edge_scraper/database.py）：
- `gex_snapshot_history`：盤中每 15 分鐘一筆的 production GEX 剖面（bot 實際使用
  的資料，不是重算版本），現價 ±25% 內的履約價，保留 180 天。
- `em_snapshot_history`：每個交易日收盤後一次，DTE 1~14 各到期日的價平跨式。

兩種讀取方式：
- HTTP：經 `TUNNEL_URL` 呼叫 edge 的 `/api/v1/cache/...` 端點。
- 檔案：直接讀取從 edge 主機複製來的 `edge_cache.db`（`--edge-db`）。

每個標的、每個交易日只取一筆 GEX：當日盤中 `[open, close]` 內最後一個 15 分鐘
分桶，最接近收盤時的牆體結構。edge 不處理國定假日，假日與盤外的紀錄一律依 NYSE
行事曆濾除。成交額與 ATR 以「快照日當天（含）之前」的日線計算，無前視偏差。

只讀取，不寫 DB、不改程式碼。
"""

import json
import logging
import math
import sqlite3
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

_PAGE_LIMIT = 500
_TS_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# 讀取來源
# ---------------------------------------------------------------------------


class EdgeHistorySource:
    """edge 歷史的讀取介面：`base_url`（HTTP）與 `db_path`（檔案）擇一。"""

    def __init__(
        self, base_url: Optional[str] = None, db_path: Optional[Path] = None
    ) -> None:
        if not base_url and not db_path:
            raise ValueError("需要 edge 的 TUNNEL_URL 或 --edge-db 檔案路徑")
        self.base_url = base_url.rstrip("/") if base_url else None
        self.db_path = Path(db_path) if db_path else None

    # --- HTTP ---------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        import httpx

        resp = httpx.get(f"{self.base_url}{path}", params=params, timeout=30.0)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"edge 回應錯誤 ({path}): {body.get('message')}")
        return body

    def _paged(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        since: Optional[str] = None
        while True:
            params: dict[str, Any] = {"limit": _PAGE_LIMIT}
            if since:
                params["since"] = since
            body = self._get(path, params)
            rows.extend(body.get("data") or [])
            since = body.get("next_since")
            if not since:
                return rows

    # --- 檔案 ---------------------------------------------------------------

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        from database.connection import connect_external_readonly

        assert self.db_path is not None
        conn = connect_external_readonly(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute(sql, params).fetchall())
        except sqlite3.OperationalError as e:
            # 舊版 edge DB 尚未建立 em_snapshot_history
            logger.warning(f"[edge_history] 查詢失敗: {e}")
            return []
        finally:
            conn.close()

    # --- 公開介面 -----------------------------------------------------------

    def list_symbols(self) -> list[str]:
        if self.base_url:
            return list(self._get("/api/v1/cache/history/symbols").get("data") or [])
        rows = self._query(
            "SELECT symbol FROM gex_snapshot_history "
            "UNION SELECT symbol FROM em_snapshot_history ORDER BY symbol"
        )
        if not rows:
            rows = self._query(
                "SELECT DISTINCT symbol FROM gex_snapshot_history ORDER BY symbol"
            )
        return [str(r[0]) for r in rows]

    def gex_history(self, symbol: str) -> list[dict[str, Any]]:
        if self.base_url:
            return self._paged(f"/api/v1/cache/gex/history/{symbol.upper()}")
        out: list[dict[str, Any]] = []
        for row in self._query(
            "SELECT bucket_ts, spot, net_gex, call_wall, put_wall, gex_profile_z "
            "FROM gex_snapshot_history WHERE symbol = ? ORDER BY bucket_ts",
            (symbol.upper(),),
        ):
            data = dict(row)
            blob = data.pop("gex_profile_z", None)
            try:
                data["gex_profile"] = (
                    json.loads(zlib.decompress(blob).decode("utf-8")) if blob else {}
                )
            except (zlib.error, ValueError):
                data["gex_profile"] = {}
            out.append(data)
        return out

    def em_history(self, symbol: str) -> list[dict[str, Any]]:
        if self.base_url:
            return self._paged(f"/api/v1/cache/em/history/{symbol.upper()}")
        return [
            dict(r)
            for r in self._query(
                "SELECT trade_date, expiry, dte, spot, strike, call_mid, put_mid "
                "FROM em_snapshot_history WHERE symbol = ? ORDER BY trade_date, expiry",
                (symbol.upper(),),
            )
        ]


# ---------------------------------------------------------------------------
# 轉換（純函式，可單元測試）
# ---------------------------------------------------------------------------


def _bucket_to_utc_str(bucket_ts: str) -> Optional[str]:
    """edge 的 'YYYY-MM-DDTHH:MM:SSZ' → market_time 的 'YYYY-MM-DD HH:MM:SS'。"""
    try:
        return datetime.strptime(bucket_ts, "%Y-%m-%dT%H:%M:%SZ").strftime(_TS_FMT)
    except (TypeError, ValueError):
        return None


def support_wall_from_profile(
    profile: Mapping[str, Any], spot: float
) -> tuple[float, float]:
    """現價下方淨 GEX 最大正值的履約價（與 structural_signals 的支撐牆候選定義相同）。"""
    best_k, best_v = 0.0, 0.0
    for k, v in (profile or {}).items():
        try:
            strike, val = float(k), float(v)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(strike) and math.isfinite(val)):
            continue
        if (
            strike < spot
            and not math.isclose(strike, spot, abs_tol=1e-4)
            and val > best_v
        ):
            best_k, best_v = strike, val
    return best_k, best_v


def daily_close_gex(
    rows: Sequence[Mapping[str, Any]], sessions: Mapping[str, tuple[str, str]]
) -> dict[str, dict[str, Any]]:
    """每個交易日取盤中最後一個 15 分鐘分桶；回傳 {trade_date: 分桶資料}。"""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    best: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for row in rows:
        ts = _bucket_to_utc_str(str(row.get("bucket_ts", "")))
        if ts is None:
            continue
        trade_date = (
            datetime.strptime(ts, _TS_FMT)
            .replace(tzinfo=timezone.utc)
            .astimezone(ny)
            .strftime("%Y-%m-%d")
        )
        bounds = sessions.get(trade_date)
        if bounds is None or not (bounds[0] <= ts <= bounds[1]):
            continue
        prev = best.get(trade_date)
        if prev is None or ts >= prev[0]:
            best[trade_date] = (ts, row)
    return {d: dict(v[1]) for d, v in best.items()}


def adv_and_atr_asof(
    hist: Any, trade_date: str
) -> tuple[Optional[float], Optional[float]]:
    """以 trade_date 當天（含）之前的日線計算 20 日平均成交額與 14 日 ATR。"""
    if hist is None or getattr(hist, "empty", True):
        return None, None
    df = hist[hist.index.strftime("%Y-%m-%d") <= trade_date]
    df = df.dropna(subset=["Close", "High", "Low", "Volume"])
    if len(df) < 21:
        return None, None
    last20 = df.iloc[-20:]
    adv = float((last20["Close"] * last20["Volume"]).mean())
    tr = (
        (df["High"] - df["Low"])
        .combine((df["High"] - df["Close"].shift()).abs(), max)
        .combine((df["Low"] - df["Close"].shift()).abs(), max)
    )
    return adv, float(tr.iloc[-14:].mean())


def em_rows_by_date(
    rows: Sequence[Mapping[str, Any]], sessions: Mapping[str, tuple[str, str]]
) -> dict[str, list[dict[str, Any]]]:
    """edge EM 快照 → {trade_date: [micro-report 的 em 列]}；非交易日濾除。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        trade_date = str(r.get("trade_date", ""))
        if trade_date not in sessions:
            continue
        try:
            dte = int(r["dte"])
            straddle = float(r["call_mid"]) + float(r["put_mid"])
        except (KeyError, TypeError, ValueError):
            continue
        if dte < 1 or straddle <= 0:
            continue
        one_sigma = straddle * math.sqrt(math.pi / 2.0)
        out.setdefault(trade_date, []).append(
            {
                "dte": float(dte),
                "expiry": str(r.get("expiry", "")),
                "straddle": straddle,
                "em_weekly_calendar": one_sigma * math.sqrt(7.0 / dte),
            }
        )
    return out


def build_edge_snapshots(
    source: EdgeHistorySource,
    symbols: Optional[Sequence[str]] = None,
    history_loader: Any = None,
) -> list[dict[str, Any]]:
    """把 edge 歷史轉成 micro-report 的快照清單（每標的每交易日一筆）。

    `history_loader(symbol, start_date)` 回傳日線 DataFrame；預設為 yfinance，
    測試時注入替身。
    """
    from market_time import get_session_bounds_utc

    if history_loader is None:
        history_loader = _yf_daily_history

    snaps: list[dict[str, Any]] = []
    for sym in symbols or source.list_symbols():
        try:
            gex_rows = source.gex_history(sym)
            em_rows = source.em_history(sym)
        except Exception as e:
            logger.warning(f"[edge_history] {sym} 讀取失敗: {e}")
            continue
        dates = [
            str(r.get("bucket_ts", ""))[:10] for r in gex_rows if r.get("bucket_ts")
        ] + [str(r.get("trade_date", "")) for r in em_rows if r.get("trade_date")]
        if not dates:
            continue
        start = date.fromisoformat(min(dates)) - timedelta(days=1)
        end = date.fromisoformat(max(dates)) + timedelta(days=1)
        sessions = get_session_bounds_utc(start, end)

        gex_by_date = daily_close_gex(gex_rows, sessions)
        em_by_date = em_rows_by_date(em_rows, sessions)
        if not gex_by_date and not em_by_date:
            continue
        hist = history_loader(sym, start - timedelta(days=60))

        for trade_date in sorted(set(gex_by_date) | set(em_by_date)):
            adv, atr = adv_and_atr_asof(hist, trade_date)
            g = gex_by_date.get(trade_date)
            gex: dict[str, Any] = {}
            spot = None
            if g is not None:
                spot = float(g.get("spot") or 0.0)
                strike, support_gex = support_wall_from_profile(
                    g.get("gex_profile") or {}, spot
                )
                gex = {
                    "support_strike": strike,
                    "support_gex": support_gex,
                    "put_wall": float(g.get("put_wall") or 0.0),
                    "call_wall": float(g.get("call_wall") or 0.0),
                    "net_gex": float(g.get("net_gex") or 0.0),
                }
            if gex and adv is None:
                # 沒有成交額就無法算深度比；EM 仍可使用
                gex = {}
            snaps.append(
                {
                    "date": trade_date,
                    "symbol": sym,
                    "spot": spot,
                    "adv_dollar_20d": adv or 0.0,
                    "atr_1d": atr or 0.0,
                    "gex": gex,
                    "em": em_by_date.get(trade_date, []),
                    "source": "edge",
                }
            )
    return snaps


def _yf_daily_history(symbol: str, start: date) -> Any:
    import yfinance as yf

    try:
        df = yf.Ticker(symbol).history(start=start.isoformat(), interval="1d")
    except Exception as e:
        logger.warning(f"[edge_history] {symbol} 日線抓取失敗: {e}")
        return None
    if df is None or df.empty:
        return None
    df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
    return df
