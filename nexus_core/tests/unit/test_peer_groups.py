from collections import Counter

from market_analysis.fundamental_pipeline.peer_groups import (
    CYCLICAL_GROUPS,
    NO_COMPS_SYMBOLS,
    PEER_GROUPS,
    resolve_peer_group,
)


def test_each_symbol_belongs_to_at_most_one_group() -> None:
    counts = Counter(s for members in PEER_GROUPS.values() for s in members)
    assert [s for s, n in counts.items() if n > 1] == []
    assert not (set(counts) & NO_COMPS_SYMBOLS)
    assert CYCLICAL_GROUPS <= set(PEER_GROUPS)


def test_group_resolution_excludes_self_and_keeps_order() -> None:
    r = resolve_peer_group("MU")
    assert r.source == "GROUP" and r.group == "記憶體與儲存"
    assert r.peers == ("SNDK", "SKHY", "WDC", "STX")
    assert r.cyclical is True


def test_case_and_whitespace_insensitive() -> None:
    assert resolve_peer_group("  mu ") == resolve_peer_group("MU")
    assert resolve_peer_group("tsla").source == "NO_COMPS"


def test_no_comps_and_finnhub_fallback() -> None:
    r = resolve_peer_group("TSLA")
    assert (r.source, r.group, r.peers, r.cyclical) == ("NO_COMPS", None, (), False)
    f = resolve_peer_group("ZZZZ")
    assert (f.source, f.group, f.peers, f.cyclical) == ("FINNHUB", None, (), False)


def test_cyclical_flag_only_for_cyclical_groups() -> None:
    assert resolve_peer_group("FCX").cyclical is True
    assert resolve_peer_group("MP").cyclical is True
    assert resolve_peer_group("NVDA").cyclical is False
