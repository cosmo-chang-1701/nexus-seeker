"""
test_gex_scraper.py

單元測試 scrape_symbol_gex_core() 的 CallWall/PutWall 方向約束判定、
淨值曝險 (net_gex/gex_profile) 正負號慣例不變性，以及深度價外雜訊合約
過濾 (_filter_noise_contracts)。

透過假 Playwright Browser/Context/Page 與假 Stealth 組出符合
parse_table() 解析欄位需求的最小合成 HTML，端對端呼叫
scrape_symbol_gex_core()，不 mock 內部計算邏輯。
"""

from typing import Any, List, cast

from playwright.async_api import Browser
import pytest

import gex_scraper


class _FakeStealth:
    async def apply_stealth_async(self, context: Any) -> None:
        return None


class _FakePage:
    def __init__(self, html: str) -> None:
        self._html = html

    async def goto(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def wait_for_selector(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def wait_for_timeout(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self, html: str) -> None:
        self._page = _FakePage(html)

    async def route(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def new_page(self) -> _FakePage:
        return self._page

    async def unroute_all(self, **kwargs: Any) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self, html: str) -> None:
        self._html = html

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        return _FakeContext(self._html)


def _make_browser(html: str) -> Browser:
    return cast(Browser, _FakeBrowser(html))


def _row(contract_name: str, strike: float, oi: int, iv_pct: float) -> str:
    cols = [contract_name, "-", str(strike)] + ["-"] * 6 + [str(oi), f"{iv_pct}"]
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cols) + "</tr>"


def _make_html(spot: float, call_rows: List[str], put_rows: List[str]) -> str:
    calls_table = "<table><tr><th>hdr</th></tr>" + "".join(call_rows) + "</table>"
    puts_table = "<table><tr><th>hdr</th></tr>" + "".join(put_rows) + "</table>"
    return f'<div data-testid="qsp-price">{spot}</div>{calls_table}{puts_table}'


@pytest.fixture(autouse=True)
def _patch_stealth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gex_scraper, "Stealth", _FakeStealth)


async def test_call_wall_only_selects_from_strikes_at_or_above_spot() -> None:
    """迴歸測試：現價下方一筆龐大 Put 曝險不應被誤判為 CallWall。"""
    html = _make_html(
        spot=100.0,
        call_rows=[_row("TESTC1", 105.0, 500, 20)],
        put_rows=[_row("TESTP1", 90.0, 5000, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core("TEST", _make_browser(html))
    assert result["call_wall"] == 105.0
    assert result["call_wall"] >= 100.0


async def test_put_wall_only_selects_from_strikes_at_or_below_spot() -> None:
    """迴歸測試：現價上方一筆龐大 Call 曝險不應被誤判為 PutWall。"""
    html = _make_html(
        spot=100.0,
        call_rows=[_row("TESTC1", 110.0, 5000, 20)],
        put_rows=[_row("TESTP1", 95.0, 500, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core("TEST", _make_browser(html))
    assert result["put_wall"] == 95.0
    assert result["put_wall"] <= 100.0


async def test_call_wall_returns_zero_when_no_strike_above_spot_has_calls() -> None:
    """空候選集回 0.0（缺失 sentinel），不回現價——否則下游無法與「牆恰在現價上」
    區分，會把 0.00% 牆距當成真實數據渲染。"""
    html = _make_html(
        spot=100.0,
        call_rows=[_row("TESTC1", 95.0, 1000, 20)],
        put_rows=[_row("TESTP1", 85.0, 1000, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core("TEST", _make_browser(html))
    assert result["call_wall"] == 0.0
    assert result["put_wall"] == 85.0


async def test_put_wall_returns_zero_when_no_strike_below_spot_has_puts() -> None:
    """同上：底牆側的空候選集 sentinel 亦為 0.0。"""
    html = _make_html(
        spot=100.0,
        call_rows=[_row("TESTC1", 115.0, 1000, 20)],
        put_rows=[_row("TESTP1", 110.0, 1000, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core("TEST", _make_browser(html))
    assert result["put_wall"] == 0.0
    assert result["call_wall"] == 115.0


async def test_net_gex_and_gex_profile_unchanged_sign_convention() -> None:
    """本次修正不應變動 net_gex/gex_profile 的正負號語意（Call 正、Put 反號）。"""
    html = _make_html(
        spot=100.0,
        call_rows=[_row("TESTC1", 105.0, 300, 20)],
        put_rows=[_row("TESTP1", 95.0, 400, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core("TEST", _make_browser(html))

    t = 7.0 / 365.0
    gamma_call = gex_scraper._calculate_gamma(100.0, 105.0, t, 0.04, 0.20)
    gamma_put = gex_scraper._calculate_gamma(100.0, 95.0, t, 0.04, 0.20)
    expected_call_gex = 300 * 100.0 * gamma_call * 100.0 * 100.0
    expected_put_gex = 400 * 100.0 * gamma_put * 100.0 * 100.0
    expected_net = expected_call_gex - expected_put_gex

    assert result["net_gex"] == pytest.approx(round(expected_net, 2), abs=0.05)
    assert result["gex_profile"][105.0] == pytest.approx(
        round(expected_call_gex, 2), abs=0.05
    )
    assert result["gex_profile"][95.0] == pytest.approx(
        round(-expected_put_gex, 2), abs=0.05
    )


async def test_deep_otm_low_delta_high_oi_contract_excluded_from_wall() -> None:
    """深度價外 (delta<0.02)、OI 巨大的長天期合約不應扭曲 CallWall 判定。"""
    html = _make_html(
        spot=100.0,
        call_rows=[
            _row("TESTC1", 105.0, 200, 20),  # 近月價平，delta 正常
            _row("AAPL301231C", 400.0, 1_000_000, 20),  # 深度價外、超巨量 OI
        ],
        put_rows=[_row("TESTP1", 95.0, 100, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core("TEST", _make_browser(html))
    assert result["call_wall"] == 105.0


async def test_noise_filter_failsafe_keeps_side_when_all_contracts_are_low_delta() -> (
    None
):
    """若某一邊全數合約皆為深度價外，過濾後不應清空該邊 (fail-safe 保留 oi>0)。"""
    html = _make_html(
        spot=100.0,
        call_rows=[_row("AAPL301231C", 400.0, 1_000_000, 20)],
        put_rows=[_row("TESTP1", 95.0, 100, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core("TEST", _make_browser(html))
    assert result["call_wall"] == 400.0


def test_bsm_merton_dividend_yield_correction() -> None:
    """ISSUE-4.3: 驗證連續股息率 q 對 Gamma 與 Delta 的校正作用。"""
    S, K, t, r, sigma = 100.0, 100.0, 0.5, 0.04, 0.25
    gamma_no_div = gex_scraper._calculate_gamma(S, K, t, r, sigma, q=0.0)
    gamma_with_div = gex_scraper._calculate_gamma(S, K, t, r, sigma, q=0.03)

    assert gamma_no_div > 0
    assert gamma_with_div > 0
    # continuous dividend discount e^(-q*t) reduces gamma for ATM options
    assert gamma_with_div < gamma_no_div

    delta_call_no_div = gex_scraper._calculate_delta(
        S, K, t, r, sigma, is_call=True, q=0.0
    )
    delta_call_with_div = gex_scraper._calculate_delta(
        S, K, t, r, sigma, is_call=True, q=0.03
    )
    assert delta_call_with_div < delta_call_no_div


async def test_scrape_symbol_gex_core_with_dividend_yield() -> None:
    """ISSUE-4.3: 驗證傳入 dividend_yield 時 GEX 計算正確代入 q。"""
    html = _make_html(
        spot=100.0,
        call_rows=[_row("TESTC1", 100.0, 500, 20)],
        put_rows=[_row("TESTP1", 100.0, 500, 20)],
    )
    result = await gex_scraper.scrape_symbol_gex_core(
        "TEST", _make_browser(html), risk_free_rate=0.04, dividend_yield=0.03
    )
    t = 7.0 / 365.0
    gamma = gex_scraper._calculate_gamma(100.0, 100.0, t, 0.04, 0.20, q=0.03)
    expected_gex = 500 * 100.0 * gamma * 100.0 * 100.0
    assert expected_gex > 0
    assert result["gex_profile"][100.0] == pytest.approx(0.0, abs=1.0)
    # 本 fixture 只有 ATM 履約價，兩側候選池皆空 → 缺失 sentinel 0.0
    assert result["call_wall"] == 0.0
    assert result["put_wall"] == 0.0


async def test_atm_strike_within_tolerance_excluded_from_walls() -> None:
    """驗證 math.isclose 輔助判定：微小浮點浮動（例如 100.00005 與 99.99995）不應被誤判為 CallWall 或 PutWall。"""
    # 案例 1：存在大 OI 的 ATM 浮點擾動合約與較小 OI 的真實 OTM 合約時，應選取真實 OTM 合約
    html_with_otm = _make_html(
        spot=100.0,
        call_rows=[
            _row("TESTC1", 100.00005, 5000, 20),
            _row("TESTC2", 105.0, 1000, 20),
        ],
        put_rows=[
            _row("TESTP1", 99.99995, 5000, 20),
            _row("TESTP2", 95.0, 1000, 20),
        ],
    )
    result_with_otm = await gex_scraper.scrape_symbol_gex_core(
        "TEST", _make_browser(html_with_otm)
    )
    assert result_with_otm["call_wall"] == 105.0
    assert result_with_otm["put_wall"] == 95.0

    # 案例 2：若僅有 ATM 浮點擾動合約而無其他 OTM 合約時，候選池為空並回 0.0
    html_atm_only = _make_html(
        spot=100.0,
        call_rows=[_row("TESTC1", 100.00005, 5000, 20)],
        put_rows=[_row("TESTP1", 99.99995, 5000, 20)],
    )
    result_atm_only = await gex_scraper.scrape_symbol_gex_core(
        "TEST", _make_browser(html_atm_only)
    )
    assert result_atm_only["call_wall"] == 0.0
    assert result_atm_only["put_wall"] == 0.0
