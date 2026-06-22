import sys
import os

# Ensure we can import from nexus_core
sys.path.append(os.path.join(os.getcwd(), "nexus_core"))

from services.asset_manager import AssetManager
from models.asset import Asset, ContextType
from database.watchlist import add_watchlist_symbol


def test_disallow_duplicate_assets(db_conn):
    """測試同一使用者不能在同一個情境 (ContextType) 下加入重複的標的，但不同情境允許並存"""
    user_id = 999888
    symbol = "TSLA"

    manager = AssetManager()

    # 1. 新增標的至 WATCH (應該成功)
    asset1 = Asset(
        user_id=user_id,
        symbol=symbol,
        context_type=ContextType.WATCH,
        metadata={"use_llm": True},
    )
    assert manager.add_asset(asset1) is True

    # 2. 嘗試再次新增同標的至 WATCH (預期失敗)
    asset2 = Asset(
        user_id=user_id,
        symbol=symbol,
        context_type=ContextType.WATCH,
        metadata={"use_llm": False},
    )
    assert manager.add_asset(asset2) is False

    # 3. 嘗試新增相同標的至 TRADE (因為 ContextType 不同，預期成功)
    asset3 = Asset(
        user_id=user_id,
        symbol=symbol,
        context_type=ContextType.TRADE,
        metadata={
            "opt_type": "call",
            "strike": 200.0,
            "expiry": "2026-06-18",
            "entry_price": 5.0,
            "quantity": 1,
        },
    )
    assert manager.add_asset(asset3) is True

    # 4. 嘗試新增第二個相同標的至 TRADE (因為是 TRADE 情境，預期也成功以支援多個不同期權部位)
    asset4 = Asset(
        user_id=user_id,
        symbol=symbol,
        context_type=ContextType.TRADE,
        metadata={
            "opt_type": "put",
            "strike": 190.0,
            "expiry": "2026-06-18",
            "entry_price": 4.0,
            "quantity": -1,
        },
    )
    assert manager.add_asset(asset4) is True


def test_add_watchlist_symbol_disallows_duplicates(db_conn):
    """測試 database.watchlist 模組底下的輔助函式也能阻擋重複標的"""
    user_id = 999888
    symbol = "AAPL"

    # 1. 第一加入 WATCH (應該成功)
    assert add_watchlist_symbol(user_id, symbol, use_llm=True) is True

    # 2. 第二加入同標的 WATCH (應該失敗)
    assert add_watchlist_symbol(user_id, symbol, use_llm=False) is False
