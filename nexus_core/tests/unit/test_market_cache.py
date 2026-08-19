"""Unit tests for database.market_cache — covers the fundamental_scan_state
CRUD helpers (v062) used by the automated daily SEC filing scanner as a
dedup cursor (distinct from fundamental_cache, which stores the LLM verdict
itself and has no accession_number column)."""

from database.market_cache import (
    get_fundamental_scan_state,
    save_fundamental_scan_state,
)


def test_get_fundamental_scan_state_missing_returns_none() -> None:
    assert get_fundamental_scan_state("NOPE") is None


def test_save_and_get_fundamental_scan_state_roundtrip() -> None:
    assert save_fundamental_scan_state("amd", "0001-22", "10-Q") is True

    state = get_fundamental_scan_state("AMD")
    assert state is not None
    assert state["last_accession_number"] == "0001-22"
    assert state["last_form_type"] == "10-Q"


def test_save_fundamental_scan_state_upserts_on_new_filing() -> None:
    save_fundamental_scan_state("TSLA", "0001-22", "10-Q")
    save_fundamental_scan_state("TSLA", "0002-33", "8-K")

    state = get_fundamental_scan_state("TSLA")
    assert state is not None
    assert state["last_accession_number"] == "0002-33"
    assert state["last_form_type"] == "8-K"
