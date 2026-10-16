"""/x 面板：遠端薄牆不當 CallWall、助跌區標頭、UOA fallback 不稱「機構」。

夾具為 edge 2026-10-16T13:30:00Z VOO 快照（原始 GEX 單位）。
"""

from typing import Any

from cogs.embed_builders.portfolio_embeds import (
    _format_uoa_field,
    create_tactical_symbol_embed,
)

GEX_PROFILE_VOO_20261016_1330Z = {"535.0": 0.0, "540.0": 0.0, "545.0": 0.0, "550.0": 0.0, "555.0": 0.0, "560.0": 0.01, "565.0": 0.02, "575.0": 2.89, "580.0": 10.3, "585.0": 1.91, "590.0": 5.98, "595.0": 52.78, "600.0": 487.46, "605.0": 509.32, "610.0": 35481.14, "615.0": 5855.79, "620.0": 180901.66, "625.0": 40535.66, "630.0": 4417836.02, "635.0": 14055717.39, "640.0": 1722844.31, "645.0": 1122703.2, "650.0": 3781634.03, "655.0": 3623107.88, "660.0": 40548568.41, "665.0": 16501815.42, "670.0": 12049956.57, "675.0": 39759270.83, "677.5": 34001364.62, "680.0": 122484822.59, "682.5": 2003807.06, "685.0": 69289565.87, "687.5": 588277.32, "690.0": 108025220.35, "692.5": -3819145.9, "695.0": -116787246.1, "697.5": 1441815.29, "700.0": -1451078757.04, "702.5": -506005131.94, "705.0": -3601901348.94, "707.5": -74837244.62, "710.0": 303526351.95, "712.5": -39801792.1, "715.0": -415684116.28, "717.5": -2763091.77, "730.0": -4040253.91, "790.0": 2041178.28}  # fmt: skip  # noqa: E501

GEX_FIELD = "🧲 Gamma 曝險分布"
VACUUM = "上方無正 Gamma 牆（負 Gamma 真空"


def _gex_text(embed: Any) -> str:
    return "\n".join(
        f"{f.name}\n{f.value}" for f in embed.fields if GEX_FIELD in (f.name or "")
    )


def _voo(
    price: float,
    adv: float | None,
    call_wall: float = 710.0,
    drop_790: bool = False,
) -> dict[str, Any]:
    prof = dict(GEX_PROFILE_VOO_20261016_1330Z)
    if drop_790:
        prof.pop("790.0")
    gex: dict[str, Any] = {
        "spot": price,
        "put_wall": 705.0,
        "call_wall": call_wall,
        "net_gex": -1_000_000_000.0,
        "gex_profile": prof,
    }
    if adv is not None:
        gex["adv_dollar_20d"] = adv
    return {
        "symbol": "VOO",
        "price": price,
        "atr_1d": 6.0,
        "atr_15m": 1.2,
        "gex_profile_data": gex,
    }


def test_a_far_thin_wall_rejected_with_adv() -> None:
    text = _gex_text(create_tactical_symbol_embed(_voo(710.06, 5.46e9)))
    assert "CallWall: $790.00" not in text
    assert VACUUM in text
    # 有 ADV 時 scanner 自身的薄牆門檻已擋下 790，故不會有「已略過」註記。
    assert "$790.00" not in text


def test_b_far_thin_wall_rejected_without_adv() -> None:
    text = _gex_text(create_tactical_symbol_embed(_voo(710.06, None)))
    assert "CallWall: $790.00" not in text
    assert VACUUM in text
    assert "深度不足已略過" in text  # 無 ADV：scanner 放行、由相對規則擋下
    assert "$790.00 (距現價" not in text
    assert "貼牆" not in text


def test_c_below_wall_unchanged() -> None:
    text = _gex_text(create_tactical_symbol_embed(_voo(709.77, None)))
    assert "CallWall: $710.00" in text
    assert VACUUM not in text


def test_d_unknown_depth_keeps_behaviour() -> None:
    text = _gex_text(
        create_tactical_symbol_embed(_voo(709.77, None, call_wall=790.0, drop_790=True))
    )
    assert "CallWall: $790.00" in text
    assert VACUUM not in text


def test_edge_thin_wall_above_spot_rejected() -> None:
    text = _gex_text(create_tactical_symbol_embed(_voo(709.77, None, call_wall=790.0)))
    # 790 被拒後重錨到深度足夠的 710（現價下方 0.03%），不再顯示 790。
    assert "CallWall: $790.00" not in text
    assert "CallWall: $710.00 (動態重錨)" in text


# ── 助跌區標頭 ──
def _putwall_case(pw_net: float) -> dict[str, Any]:
    return {
        "symbol": "SPCX",
        "price": 160.0,
        "atr_1d": 3.0,
        "atr_15m": 0.8,
        "gex_profile_data": {
            "spot": 160.0,
            "put_wall": 150.0,
            "call_wall": 175.0,
            "net_gex": 5_000_000.0,
            "gex_profile": {
                "150.0": pw_net,
                "155.0": 90_000_000.0,
                "160.0": 1_000_000.0,
                "175.0": 3_000_000.0,
            },
        },
    }


def test_putwall_negative_header_renamed() -> None:
    text = _gex_text(create_tactical_symbol_embed(_putwall_case(-30_000_000.0)))
    assert "⚠️ 下檔助跌" in text
    assert "🛡️ 下檔支撐" not in text
    assert "實為助跌區" in text


def test_putwall_solid_header_unchanged() -> None:
    text = _gex_text(create_tactical_symbol_embed(_putwall_case(80_000_000.0)))
    assert "🛡️ 下檔支撐" in text


def test_no_putwall_does_not_raise() -> None:
    d = _putwall_case(80_000_000.0)
    d["gex_profile_data"]["put_wall"] = 0.0
    assert GEX_FIELD in _gex_text(create_tactical_symbol_embed(d))


# base（標頭仍為「🛡️ 下檔支撐」）渲染 _putwall_case(-3e7) 的 GEX 欄位字數。
_BASE_NEG_PUTWALL_GEX_CHARS = 965


def test_header_rename_does_not_grow_field() -> None:
    text = _gex_text(create_tactical_symbol_embed(_putwall_case(-30_000_000.0)))
    assert len(text) <= _BASE_NEG_PUTWALL_GEX_CHARS


# ── UOA fallback ──
def test_uoa_fallback_buy_side_not_institution() -> None:
    items = [
        {"expiry": "2026-10-23", "strike": 280.0, "type": "CALL", "volume": 100,
         "oi": 10, "ratio": 10.0, "trade_type": "SWEEP"},
        {"expiry": "2026-10-23", "strike": 270.0, "type": "PUT", "volume": 100,
         "oi": 10, "ratio": 10.0, "trade_type": "SWEEP"},
    ]  # fmt: skip
    out = _format_uoa_field(items)
    text = str(out)
    assert "機構在" not in text
    assert "主動買入" in text and "急買" in text
