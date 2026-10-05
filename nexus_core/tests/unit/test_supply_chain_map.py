"""產業鏈因果對照表 (supply_chain_map) 單元測試。"""

from __future__ import annotations

from market_analysis.fundamental_pipeline.supply_chain_map import (
    ALL_SUPPLY_CHAIN_LINKS,
    CORE_SUPPLY_CHAIN_LINKS,
    SUPPLY_CHAIN_LINKS,
    get_all_links,
    get_links_by_pillar,
    get_links_for_symbol,
    get_supply_chain_link,
)


def test_core_supply_chain_links_count() -> None:
    """驗證核心產業鏈精準包含 17 條 (5 總體 + 5 太空 + 7 先進科技)。"""
    assert len(CORE_SUPPLY_CHAIN_LINKS) == 17
    assert len(SUPPLY_CHAIN_LINKS) == 17

    macro_links = [link for link in SUPPLY_CHAIN_LINKS if link.pillar == "MACRO_CORE"]
    space_links = [
        link for link in SUPPLY_CHAIN_LINKS if link.pillar == "SPACE_DEFENSE"
    ]
    frontier_links = [
        link for link in SUPPLY_CHAIN_LINKS if link.pillar == "FRONTIER_TECH"
    ]

    assert len(macro_links) == 5
    assert len(space_links) == 5
    assert len(frontier_links) == 7


def test_supply_chain_link_fields_integrity() -> None:
    """驗證所有產業鏈之必要屬性均完整且合規。"""
    for link in ALL_SUPPLY_CHAIN_LINKS:
        assert bool(link.link_key)
        assert bool(link.title)
        assert link.link_type in ("CAUSAL", "NOWCAST")
        assert link.pillar in ("MACRO_CORE", "SPACE_DEFENSE", "FRONTIER_TECH")
        assert len(link.drivers) > 0
        assert len(link.followers) > 0
        assert bool(link.description)
        assert bool(link.lead_lag_quarters)


def test_experimental_flags() -> None:
    """驗證實驗性 (experimental) 標記依規格書設定。"""
    exp_keys = {link.link_key for link in ALL_SUPPLY_CHAIN_LINKS if link.experimental}
    assert "SPACE_EO_DATA" in exp_keys
    assert "ADV_ENERGY_NUCLEAR_SMR" in exp_keys
    assert "ADV_ROBOTICS_EMBODIED_NOWCAST" in exp_keys
    assert "ADV_QUANTUM_COMPUTING" in exp_keys

    # 非實驗性鏈條驗證
    non_exp_keys = {
        link.link_key for link in SUPPLY_CHAIN_LINKS if not link.experimental
    }
    assert "AI_CAPEX" in non_exp_keys
    assert "FABLESS_FOUNDRY" in non_exp_keys
    assert "ADV_SEMI_COWOS_CPO" in non_exp_keys
    assert len(non_exp_keys) == 14


def test_get_supply_chain_link_lookup() -> None:
    """測試根據 link_key 查詢產業鏈定義。"""
    link = get_supply_chain_link("AI_CAPEX")
    assert link is not None
    assert link.title == "AI / 超級智慧算力資本支出鏈"
    assert link.link_type == "CAUSAL"

    # 大小寫不敏感
    link_lower = get_supply_chain_link("ai_capex")
    assert link_lower == link

    # 擴充鏈條可查
    quantum = get_supply_chain_link("ADV_QUANTUM_COMPUTING")
    assert quantum is not None
    assert quantum.experimental is True

    # 不存在的代碼
    assert get_supply_chain_link("NON_EXISTENT_LINK") is None


def test_get_links_for_symbol_tsla() -> None:
    """測試查詢 TSLA 關聯產業鏈 (包含儲能、移動載具、汽車總體與人形機器人)。"""
    tsla_links = get_links_for_symbol("TSLA")
    keys = {link.link_key for link in tsla_links}
    assert "ADV_ENERGY_THERMAL_GRID" in keys
    assert "ADV_AUTO_MOBILITY_TW_NOWCAST" in keys
    assert "ADV_AUTO_FLEET_DEMAND" in keys
    assert "ADV_ROBOTICS_EMBODIED_NOWCAST" in keys


def test_get_links_for_symbol_spcx() -> None:
    """測試查詢 SPCX / SpaceX 關聯產業鏈 (涵蓋超級算力、星系發射、Starlink、Starshield 等)。"""
    spcx_links = get_links_for_symbol("SPCX")
    keys = {link.link_key for link in spcx_links}
    assert "AI_CAPEX" in keys
    assert "SPACE_CONSTELLATION_LAUNCH" in keys
    assert "SPACE_STARLINK_BROADBAND_CHAIN" in keys
    assert "SPACE_STARLINK_TW_NOWCAST" in keys
    assert "SPACE_DEFENSE_PRIME" in keys


def test_get_links_for_symbol_tsm() -> None:
    """測試查詢台積電 (TSM / 2330) 雙重標識與晶圓代工關聯鏈。"""
    tsm_links = get_links_for_symbol("TSM")
    keys = {link.link_key for link in tsm_links}
    assert "FABLESS_FOUNDRY" in keys
    assert "ADV_SEMI_COWOS_CPO" in keys
    assert "ADV_SEMI_WFE_EQUIPMENT" in keys

    # 以 2330 查詢應具備相同結果
    tw_links = get_links_for_symbol("2330")
    tw_keys = {link.link_key for link in tw_links}
    assert "FABLESS_FOUNDRY" in tw_keys
    assert "ADV_SEMI_COWOS_CPO" in tw_keys


def test_get_links_for_unknown_symbol() -> None:
    """測試不存在於任何產業鏈之標的。"""
    links = get_links_for_symbol("UNKNOWN_TICKER_XYZ")
    assert links == []


def test_get_all_links_filter_experimental() -> None:
    """測試 get_all_links 過濾實驗性鏈條。"""
    all_core = get_all_links(include_experimental=True)
    assert len(all_core) == 17

    non_exp = get_all_links(include_experimental=False)
    assert len(non_exp) == 14
    for link in non_exp:
        assert link.experimental is False


def test_get_links_by_pillar() -> None:
    """測試依支柱領域過濾。"""
    macro = get_links_by_pillar("MACRO_CORE")
    assert len(macro) == 5

    space = get_links_by_pillar("SPACE_DEFENSE")
    assert len(space) == 5

    frontier = get_links_by_pillar("FRONTIER_TECH")
    # 7 條核心 + 1 條量子運算擴充
    assert len(frontier) == 8
