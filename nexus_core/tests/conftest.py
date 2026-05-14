import os
import pytest
import sqlite3
from unittest.mock import AsyncMock, patch

# Ensure OpenAI API key is set for tests to avoid collection errors
os.environ["OPENAI_API_KEY"] = "sk-dummy-key-for-tests"

# Use a shared in-memory database for testing
TEST_DB_NAME = "file:testdb?mode=memory&cache=shared"


@pytest.fixture(scope="session", autouse=True)
def mock_db_name():
    with patch("config.DB_NAME", TEST_DB_NAME):
        yield


@pytest.fixture(scope="session")
def db_conn():
    conn = sqlite3.connect(TEST_DB_NAME)
    # Run migrations
    from database.core import run_migrations

    run_migrations()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clean_db(db_conn):
    # Clear tables before each test if needed,
    # but since it's :memory: and we might want to test persistence between some calls,
    # we can just clear specific tables.
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


@pytest.fixture
def mock_interaction():
    interaction = AsyncMock()
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.user.id = 123456789
    interaction.user.name = "testuser"
    interaction.guild_id = 987654321
    return interaction


@pytest.fixture
def mock_market_data():
    with patch(
        "services.market_data_service.get_quote", autospec=True
    ) as mock_price, patch(
        "services.market_data_service.get_history_df", autospec=True
    ) as mock_hist:
        mock_price.return_value = {"c": 150.0}
        yield mock_price, mock_hist


@pytest.fixture
def mock_llm():
    with patch(
        "services.llm_service.generate_market_report", autospec=True
    ) as mock_report:
        mock_report.return_value = "Mocked LLM Report"
        yield mock_report
