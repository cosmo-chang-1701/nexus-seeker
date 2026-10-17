"""/x Max Pain 指引須標明到期日，且負 Gamma 下不宣稱向上磁吸。"""

from cogs.embed_builders.portfolio_embeds import get_scenario_guidance

_OLD_ABOVE = "價格高於最大痛點，結算日前需防範向痛點震盪拉回的壓制力。"
_OLD_BELOW = "價格遠低於最大痛點，具備磁吸效應回升動能。"


def test_below_branch_names_expiry_and_price() -> None:
    text = get_scenario_guidance(188.32, 195.0, expiry="2026-10-23")
    assert "10/23" in text
    assert "低於" in text
    assert "回升" in text


def test_below_branch_negative_gamma_denies_magnet() -> None:
    text = get_scenario_guidance(
        188.32, 195.0, expiry="2026-10-23", negative_gamma=True
    )
    assert "回升" not in text
    assert "磁吸不成立" in text
    assert "10/23" in text


def test_above_branch_names_expiry_and_ignores_negative_gamma() -> None:
    text = get_scenario_guidance(205.0, 195.0, expiry="2026-10-23", negative_gamma=True)
    assert "10/23" in text
    assert "拉回" in text


def test_no_expiry_keeps_legacy_wording() -> None:
    assert get_scenario_guidance(350.0, 375.0) == _OLD_BELOW
    assert get_scenario_guidance(394.39, 375.0) == _OLD_ABOVE


def test_invalid_expiry_falls_back_to_legacy_wording() -> None:
    assert get_scenario_guidance(350.0, 375.0, expiry="bad") == _OLD_BELOW


def test_new_message_lengths_vs_old() -> None:
    above = get_scenario_guidance(205.0, 195.0, expiry="2026-10-23")
    below = get_scenario_guidance(188.32, 195.0, expiry="2026-10-23")
    neg = get_scenario_guidance(188.32, 195.0, expiry="2026-10-23", negative_gamma=True)
    neg_no_exp = get_scenario_guidance(188.32, 195.0, negative_gamma=True)
    assert len(above) <= len(_OLD_ABOVE)
    assert len(below) <= len(_OLD_BELOW)
    assert len(neg) <= len(_OLD_BELOW)
    assert len(neg_no_exp) <= len(_OLD_BELOW)
