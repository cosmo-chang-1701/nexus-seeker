from typing import Any
import os
import pytest
import sqlite3
from unittest.mock import AsyncMock, patch

# Ensure API keys are set for tests to avoid collection errors
os.environ["OPENAI_API_KEY"] = "sk-dummy-key-for-tests"
os.environ["FINNHUB_API_KEY"] = "dummy-finnhub-key-for-tests"

# Use a shared in-memory database for testing
TEST_DB_NAME = "file:testdb?mode=memory&cache=shared"
os.environ["NEXUS_DB_NAME"] = TEST_DB_NAME

# Monkey-patch sqlite3.connect to force uri=True for shared in-memory URI paths and set busy timeout
_original_connect = sqlite3.connect


def _patched_connect(database: Any, *args, **kwargs):  # type: ignore
    if isinstance(database, str) and database.startswith("file:"):
        kwargs["uri"] = True
    if "timeout" not in kwargs:
        kwargs["timeout"] = 30.0
    return _original_connect(database, *args, **kwargs)


sqlite3.connect = _patched_connect


@pytest.fixture(scope="session", autouse=True)
def mock_tasks_loop_start() -> Any:
    """Globally prevent background loop tasks from starting and leaking in tests."""
    with patch("discord.ext.tasks.Loop.start") as mock:
        yield mock


@pytest.fixture(scope="session", autouse=True)
def mock_finnhub_client() -> Any:
    """Globally mock finnhub.Client to avoid real API calls."""
    with patch("finnhub.Client") as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_fetch_symbol_gex_metrics() -> Any:
    """全域 mock `fetch_symbol_gex_metrics`，避免微觀結構出場決策矩陣的
    force_live GEX 刷新 (portfolio_monitor.py) 在單元測試中真的發動網路呼叫
    (Edge Scraper Tunnel/Playwright)。預設回傳空 dict（falsy），讓呼叫端的
    `if not fresh_data: continue` 略過合併，等同「本次刷新無新資料，沿用既有
    雷達快取」——與此函式引入前的既有測試行為完全一致。個別測試如需驗證
    force_live 刷新確實生效，可自行以更內層的 patch 覆寫此 fixture。

    刻意採用函式層級 (非 session) 作用域：`test_macro_risk_upgrade.py` 需要
    測試 `fetch_symbol_gex_metrics` 本身的真實邏輯，以同名 fixture 在模組層級
    覆寫此 fixture。若本 fixture 為 session 作用域，一旦被「其他」測試檔案先
    觸發過一次，`with patch(...)` 便會持續開著直到整個 session 結束——同名
    覆寫只能讓特定模組的測試「改用」不同的 fixture 函式，無法追溯撤銷另一個
    fixture 執行個體早已生效、仍開著的 mock.patch（因為 patch 直接改寫的是
    真實模組屬性，並非受 pytest fixture 解析機制管轄的狀態）。函式作用域確保
    每個測試前後都乾淨地進入/離開 patch，同名覆寫才能真正在該模組內生效。"""
    with patch(
        "market_analysis.index_microstructure.fetch_symbol_gex_metrics",
        new_callable=AsyncMock,
        return_value={},
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def mock_vwap_and_confirmed_bar() -> Any:
    """全域 mock TP3-終局平倉 (微觀結構出場決策矩陣) 所需的 VWAP 帶量失守判定
    (`_build_symbol_metrics`)，避免任何測試一旦提供非 None 的 radar 資料
    (`r_data`)，就意外觸發 `fetch_session_vwap`/`get_confirmed_15m_bar` 的真實
    網路呼叫。預設 VWAP 回傳 0.0（fail-safe：呼叫端視為抓取失敗，不判定失守）、
    確認 K 棒回傳 None（同樣視為資料不足，不判定觸發）。個別測試如需驗證
    VWAP 帶量失守判定本身，可自行以更內層的 patch 覆寫這兩個 fixture。

    函式（非 session）作用域的理由與上方 `mock_fetch_symbol_gex_metrics`
    相同：確保任何模組需要以同名 fixture 覆寫時真正生效。"""
    with patch(
        "market_analysis.vwap_utils.fetch_session_vwap",
        new_callable=AsyncMock,
        return_value=0.0,
    ) as mock_vwap, patch(
        "market_analysis.price_volume_alert.get_confirmed_15m_bar",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_bar:
        yield mock_vwap, mock_bar


@pytest.fixture(scope="session", autouse=True)
def mock_symbol_validation() -> Any:
    """Globally mock validate_symbol to return True for common test symbols."""
    from services.market_data_service import validate_symbol as _real_validate_symbol

    with patch(
        "services.market_data_service.validate_symbol", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        mock.real_fn = _real_validate_symbol
        yield mock


@pytest.fixture(scope="session", autouse=True)
def mock_db_name() -> Any:
    with patch("config.DB_NAME", TEST_DB_NAME):
        yield


@pytest.fixture(scope="session")
def db_conn() -> Any:
    conn = sqlite3.connect(TEST_DB_NAME)
    # Run migrations
    from database.core import run_migrations

    run_migrations()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clean_db(db_conn: Any):  # type: ignore
    # Clear memory caches to avoid cross-test contamination
    try:
        from market_analysis.sentiment_engine import _iv_cache

        _iv_cache.clear()
    except Exception:
        pass
    try:
        from services.market_data_service import (
            _option_chain_cache,
            _option_expiries_cache,
            _quote_cache,
        )

        _option_chain_cache.clear()
        _option_expiries_cache.clear()
        if hasattr(_quote_cache, "clear"):
            _quote_cache.clear()
    except Exception:
        pass

    # Clear tables before each test if needed
    cursor = db_conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()

    import re

    table_pattern = re.compile(r"^[a-zA-Z0-9_]+$")

    for (table,) in tables:
        if table != "schema_versions":
            if table_pattern.match(table):
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                cursor.execute(f"DELETE FROM {table}")
    db_conn.commit()
    yield
    try:
        db_conn.rollback()
    except Exception:
        pass


@pytest.fixture
def mock_interaction() -> Any:
    interaction = AsyncMock()
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.user.id = 123456789
    interaction.user.name = "testuser"
    interaction.guild_id = 987654321
    return interaction


@pytest.fixture
def mock_market_data() -> Any:
    with patch(
        "services.market_data_service.get_quote", autospec=True
    ) as mock_price, patch(
        "services.market_data_service.get_history_df", autospec=True
    ) as mock_hist:
        mock_price.return_value = {"c": 150.0}
        yield mock_price, mock_hist


@pytest.fixture
def mock_llm() -> Any:
    with patch(
        "services.llm_service.generate_market_report", autospec=True
    ) as mock_report:
        mock_report.return_value = "Mocked LLM Report"
        yield mock_report
