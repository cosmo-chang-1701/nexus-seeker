from typing import Any
import sqlite3
import json
import logging
import unicodedata
from typing import List, Optional, Dict
import config
from database.connection import (
    connect_db,
    execute_write,
    execute_write_many,
    execute_write_rowcount,
)
from models.asset import Asset, ContextType, TradeMetadata, HoldingMetadata

logger = logging.getLogger(__name__)

# 單一使用者觀察清單 (WATCH) 可同時追蹤的標的數量上限，避免無限制增長拖慢
# 15 分鐘心跳掃描週期（VPS 記憶體與 API 呼叫量防護）。
#
# 心跳採全域去重（跨使用者共用同一標的的抓取結果），實際負載是去重後的唯一標的數，
# 不是使用者數 × 標的數。以 50 為上限估算：get_quote() 是 Finnhub 優先且 quote cache
# 只有 15 秒，故每輪心跳都會即時打 1 次 Finnhub /quote；15 分鐘節奏下每小時 4 輪 × 50 =
# 200 次/小時，相對於 Finnhub 背景額度 15 次/分 = 900 次/小時，使用率約 22%，仍有餘裕。
# 選擇權鏈/IV/Max Pain/PCR 等其餘呼叫則受 20 分鐘快取 TTL 保護，15 分鐘節奏下會形成
# 「隔一輪命中快取」的交替模式，重抓取頻率大致維持每小時 2 次，不會隨節奏壓縮而翻倍。
# 若未來上限需要調整，可參考 cogs/trading/heartbeat.py 新增的 Pass 2 耗時/標的數 log。
#
# 與 database/price_volume_watch.py 的 _MAX_WATCHES_PER_USER 命名/防護模式一致。
_MAX_WATCHLIST_SYMBOLS_PER_USER = 50


class WatchlistLimitExceededError(Exception):
    """使用者觀察清單標的數量超過 `_MAX_WATCHLIST_SYMBOLS_PER_USER` 上限時拋出。"""


class AssetManager:
    def __init__(self, db_name: str | None = None) -> Any:  # type: ignore
        self.db_name = db_name or config.DB_NAME

    def _get_conn(self) -> Any:
        """僅供讀取使用。所有寫入一律走 DatabaseWriteQueue（單一寫入者）。

        ⚠️ 呼叫端請用 `try/finally: conn.close()`，不要只用 `with conn:`：
        sqlite3 的 context manager 只會 commit/rollback，**不會關閉連線**。
        """
        conn = connect_db()
        conn.row_factory = sqlite3.Row
        return conn

    def get_assets(
        self, user_id: int, context_type: Optional[ContextType] = None
    ) -> List[Asset]:
        """獲取指定使用者的資產清單"""
        query = """
            SELECT a.*, GROUP_CONCAT(t.tag_name, ', ') as tags
            FROM assets a
            LEFT JOIN watchlist_tags t ON a.user_id = t.user_id AND a.symbol = t.symbol
            WHERE a.user_id = ?
        """
        params: list[Any] = [user_id]
        if context_type:
            query += " AND a.context_type = ?"
            params.append(context_type.value)

        query += " GROUP BY a.id"

        assets = []
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                data = dict(row)
                data["metadata"] = (
                    json.loads(data["metadata"]) if data["metadata"] else {}
                )
                assets.append(Asset(**data))
        finally:
            conn.close()
        return assets

    def get_asset_by_symbol(
        self, user_id: int, symbol: str, context_type: ContextType
    ) -> Optional[Asset]:
        """根據代號與類型獲取單一資產"""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM assets WHERE user_id = ? AND symbol = ? AND context_type = ?",
                (user_id, symbol.upper(), context_type.value),
            )
            row = cursor.fetchone()
        finally:
            conn.close()
        if row:
            data = dict(row)
            data["metadata"] = json.loads(data["metadata"]) if data["metadata"] else {}
            return Asset(**data)
        return None

    def get_asset_by_id(self, user_id: int, asset_id: int) -> Optional[Asset]:
        """根據 ID 獲取單一資產"""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM assets WHERE user_id = ? AND id = ?", (user_id, asset_id)
            )
            row = cursor.fetchone()
        finally:
            conn.close()
        if row:
            data = dict(row)
            data["metadata"] = json.loads(data["metadata"]) if data["metadata"] else {}
            return Asset(**data)
        return None

    def update_asset(self, asset: Asset) -> bool:
        """更新完整的資產紀錄"""
        metadata_json = json.dumps(asset.metadata)
        try:
            execute_write(
                """
                UPDATE assets
                SET symbol = ?, context_type = ?, risk_weight = ?, entry_price = ?, metadata = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (
                    asset.symbol.upper(),
                    asset.context_type.value,
                    asset.risk_weight,
                    asset.entry_price,
                    metadata_json,
                    asset.id,
                    asset.user_id,
                ),
            )
            return True
        except Exception as e:
            logger.error(f"Update asset error: {e}")
            return False

    def update_asset_metadata(
        self, user_id: int, asset_id: int, updates: Dict[str, Any]
    ) -> bool:
        """部分更新資產的 metadata"""
        asset = self.get_asset_by_id(user_id, asset_id)
        if not asset:
            return False

        asset.metadata.update(updates)
        return self.update_asset(asset)

    def update_asset_metadata_by_symbol(
        self,
        user_id: int,
        symbol: str,
        context_type: ContextType,
        updates: Dict[str, Any],
    ) -> bool:
        """根據 symbol 與類型部分更新資產的 metadata"""
        asset = self.get_asset_by_symbol(user_id, symbol, context_type)
        if not asset:
            return False

        asset.metadata.update(updates)
        return self.update_asset(asset)

    def promote_to_trade(
        self, user_id: int, symbol: str, trade_details: Dict[str, Any]
    ) -> bool:
        """將 WATCH 狀態提升為 TRADE"""
        symbol = symbol.upper()
        watch_asset = self.get_asset_by_symbol(user_id, symbol, ContextType.WATCH)

        if not watch_asset:
            logger.warning(
                f"Promote failed: {symbol} not found in WATCH for user {user_id}"
            )
            return False

        # 準備 TRADE 詮釋資料
        trade_meta = TradeMetadata(**trade_details)
        metadata_json = trade_meta.model_dump_json()

        try:
            # 將原有 WATCH 改為 TRADE：採取「轉換」策略，更新原有紀錄
            execute_write(
                """
                UPDATE assets
                SET context_type = 'TRADE', metadata = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (metadata_json, watch_asset.id),
            )
            return True
        except Exception as e:
            logger.error(f"Promote to trade error: {e}")
            return False

    def settle_to_holding(
        self, user_id: int, asset_id: int, execution_price: float
    ) -> bool:
        """將 TRADE 狀態結算為 HOLDING (例如選擇權履約或到期轉現貨)"""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM assets WHERE id = ? AND user_id = ?", (asset_id, user_id)
            )
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            return False

        asset = Asset(**{**dict(row), "metadata": json.loads(row["metadata"])})
        if asset.context_type != ContextType.TRADE:
            return False

        trade_meta = TradeMetadata(**asset.metadata)

        # 簡單結算邏輯：若是 Put 履約，則以 (Strike - Price) 或直接以 Strike 作為成本
        # 這裡假設 settle 指的是轉換為 100 股現貨
        holding_qty = trade_meta.quantity * 100

        # 更新為 HOLDING
        holding_meta = HoldingMetadata(quantity=holding_qty, avg_cost=execution_price)

        try:
            execute_write(
                """
                UPDATE assets
                SET context_type = 'HOLDING', metadata = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (holding_meta.model_dump_json(), asset_id),
            )
            return True
        except Exception as e:
            logger.error(f"Settle to holding error: {e}")
            return False

    def add_asset(self, asset: Asset) -> bool:
        """新增資產紀錄

        Raises:
            WatchlistLimitExceededError: 新增 WATCH 類型資產時，該使用者的觀察清單
                已達 `_MAX_WATCHLIST_SYMBOLS_PER_USER` 上限。
        """
        if asset.context_type == ContextType.WATCH:
            conn = self._get_conn()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM assets WHERE user_id = ? AND context_type = 'WATCH'",
                    (asset.user_id,),
                )
                existing_count = cursor.fetchone()[0]
            finally:
                conn.close()
            if existing_count >= _MAX_WATCHLIST_SYMBOLS_PER_USER:
                raise WatchlistLimitExceededError(
                    f"觀察清單標的數量已達上限 ({_MAX_WATCHLIST_SYMBOLS_PER_USER} 檔)，請先移除部分標的後再新增。"
                )

        try:
            execute_write(
                """
                INSERT INTO assets (user_id, symbol, context_type, risk_weight, entry_price, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    asset.user_id,
                    asset.symbol.upper(),
                    asset.context_type.value,
                    asset.risk_weight,
                    asset.entry_price,
                    json.dumps(asset.metadata),
                ),
            )
            return True
        except sqlite3.IntegrityError as e:
            logger.warning(
                f"Add asset unique constraint triggered (already exists): {e}"
            )
            return False
        except Exception as e:
            logger.error(f"Add asset error: {e}")
            return False

    def delete_asset_by_symbol(
        self, user_id: int, symbol: str, context_type: ContextType
    ) -> bool:
        """刪除特定類型的資產"""
        return (
            execute_write_rowcount(
                "DELETE FROM assets WHERE user_id = ? AND symbol = ? AND context_type = ?",
                (user_id, symbol.upper(), context_type.value),
            )
            > 0
        )

    def delete_asset_by_id(self, user_id: int, asset_id: int) -> bool:
        """根據 ID 刪除資產"""
        return (
            execute_write_rowcount(
                "DELETE FROM assets WHERE id = ? AND user_id = ?", (asset_id, user_id)
            )
            > 0
        )

    def set_watchlist(self, user_id: int, symbols: list[str]) -> tuple[int, list[str]]:
        """以原子操作覆蓋特定使用者的觀察清單 (WATCH)。

        先清除該使用者所有既有的 WATCH 標的，再寫入傳入的標的清單。
        若過程中發生任何例外，將自動回滾以確保交易原子性。

        Args:
            user_id: 使用者 ID。
            symbols: 欲設定的標的代號列表。

        Returns:
            tuple[int, list[str]]: (原先清除的舊標的總數, 成功寫入的新標的代號列表)

        Raises:
            WatchlistLimitExceededError: 若傳入的有效標的數量超過上限。
        """
        clean_symbols: list[str] = []
        seen: set[str] = set()
        for s in symbols:
            s_norm = unicodedata.normalize("NFKC", s)
            sym_upper = s_norm.strip().strip("$＄").upper()
            if sym_upper and sym_upper not in seen:
                seen.add(sym_upper)
                clean_symbols.append(sym_upper)

        if len(clean_symbols) > _MAX_WATCHLIST_SYMBOLS_PER_USER:
            raise WatchlistLimitExceededError(
                f"觀察清單標的數量超過上限 ({_MAX_WATCHLIST_SYMBOLS_PER_USER} 檔)，請縮減標的數量後再設定。"
            )

        # DELETE + 批次 INSERT 必須同屬一個交易（覆蓋語意），批次寫入入口會回傳
        # 逐語句的 rowcount，因此清除筆數也能在同一個交易內取得，不必另開查詢。
        statements: list[tuple] = [
            (
                "DELETE FROM assets WHERE user_id = ? AND context_type = 'WATCH'",
                (user_id,),
            )
        ]
        if clean_symbols:
            metadata_json = json.dumps({})
            statements.append(
                (
                    """
                    INSERT INTO assets (user_id, symbol, context_type, risk_weight, entry_price, metadata)
                    VALUES (?, ?, 'WATCH', 1.0, NULL, ?)
                    """,
                    [(user_id, sym, metadata_json) for sym in clean_symbols],
                    True,
                )
            )

        rowcounts = execute_write_many(statements)
        cleared_count: int = max(0, rowcounts[0] if rowcounts else 0)
        return cleared_count, clean_symbols
