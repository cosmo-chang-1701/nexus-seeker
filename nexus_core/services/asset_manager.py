import sqlite3
import json
import logging
from typing import List, Optional, Dict, Any
import config
from models.asset import Asset, ContextType, TradeMetadata, HoldingMetadata

logger = logging.getLogger(__name__)


class AssetManager:
    def __init__(self, db_name: str = None):
        self.db_name = db_name or config.DB_NAME

    def _get_conn(self):
        conn = sqlite3.connect(self.db_name)
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
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                data = dict(row)
                data["metadata"] = (
                    json.loads(data["metadata"]) if data["metadata"] else {}
                )
                assets.append(Asset(**data))
        return assets

    def get_asset_by_symbol(
        self, user_id: int, symbol: str, context_type: ContextType
    ) -> Optional[Asset]:
        """根據代號與類型獲取單一資產"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM assets WHERE user_id = ? AND symbol = ? AND context_type = ?",
                (user_id, symbol.upper(), context_type.value),
            )
            row = cursor.fetchone()
            if row:
                data = dict(row)
                data["metadata"] = (
                    json.loads(data["metadata"]) if data["metadata"] else {}
                )
                return Asset(**data)
        return None

    def get_asset_by_id(self, user_id: int, asset_id: int) -> Optional[Asset]:
        """根據 ID 獲取單一資產"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM assets WHERE user_id = ? AND id = ?", (user_id, asset_id)
            )
            row = cursor.fetchone()
            if row:
                data = dict(row)
                data["metadata"] = (
                    json.loads(data["metadata"]) if data["metadata"] else {}
                )
                return Asset(**data)
        return None

    def update_asset(self, asset: Asset) -> bool:
        """更新完整的資產紀錄"""
        metadata_json = json.dumps(asset.metadata)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
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
                conn.commit()
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

        with self._get_conn() as conn:
            cursor = conn.cursor()
            try:
                # 1. 將原有 WATCH 改為 TRADE (或保留 WATCH 新增 TRADE，依 lifecycle 定義)
                # 這裡採取「轉換」策略：更新原有紀錄
                cursor.execute(
                    """
                    UPDATE assets
                    SET context_type = 'TRADE', metadata = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (metadata_json, watch_asset.id),
                )
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Promote to trade error: {e}")
                conn.rollback()
                return False

    def settle_to_holding(
        self, user_id: int, asset_id: int, execution_price: float
    ) -> bool:
        """將 TRADE 狀態結算為 HOLDING (例如選擇權履約或到期轉現貨)"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM assets WHERE id = ? AND user_id = ?", (asset_id, user_id)
            )
            row = cursor.fetchone()
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
            holding_meta = HoldingMetadata(
                quantity=holding_qty, avg_cost=execution_price
            )

            try:
                cursor.execute(
                    """
                    UPDATE assets
                    SET context_type = 'HOLDING', metadata = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (holding_meta.model_dump_json(), asset_id),
                )
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Settle to holding error: {e}")
                conn.rollback()
                return False

    def add_asset(self, asset: Asset) -> bool:
        """新增資產紀錄"""
        metadata_json = json.dumps(asset.metadata)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
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
                        metadata_json,
                    ),
                )
                conn.commit()
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
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM assets WHERE user_id = ? AND symbol = ? AND context_type = ?",
                (user_id, symbol.upper(), context_type.value),
            )
            changes = cursor.rowcount
            conn.commit()
            return changes > 0

    def delete_asset_by_id(self, user_id: int, asset_id: int) -> bool:
        """根據 ID 刪除資產"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM assets WHERE id = ? AND user_id = ?", (asset_id, user_id)
            )
            changes = cursor.rowcount
            conn.commit()
            return changes > 0
