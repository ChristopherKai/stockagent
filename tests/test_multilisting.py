"""多地上市：定价上市地（港股优先）、ADS 换股比例与 ADS 溢价、按两地现价之比折算等值价、
跟踪卡记录定价上市地、EV 按定价上市地股价计算、观察清单表单写入。全部离线。"""

from __future__ import annotations

import textwrap

import pytest
from test_tracker import _bars
from test_valuation import DOSSIERS, save_dossier

from stockagent import tools
from stockagent import valuation as V
from stockagent.app import App
from stockagent.config import Company, Listing
from stockagent.web import yamledit

WATCHLIST = """
companies:
  - id: innolight
    name: 中际旭创
    listings: [{market: A, code: "300308"}, {market: HK, code: "03308"}]
  - id: xpeng
    name: 小鹏汽车
    listings: [{market: HK, code: "09868"}, {market: US, code: XPEV, exchange: NYSE, ads_ratio: 2}]
  - id: pinned
    name: 指定定价
    pricing: A
    listings: [{market: A, code: "601869"}, {market: HK, code: "06869"}]
"""


@pytest.fixture
def mapp(project) -> App:
    (project / "watchlist.yaml").write_text(textwrap.dedent(WATCHLIST), encoding="utf-8")
    a = App.load(project / "config.yaml")
    _bars(a, "133.HKDCNH", [0.9] * 10)
    _bars(a, "133.USDCNH", [7.02] * 10)  # 1 美元 = 7.8 港元
    yield a
    a.db.close()


def test_pricing_listing_defaults_to_hk():
    c = Company(id="x", name="x", listings=[Listing(market="A", code="300308"), Listing(market="HK", code="03308")])
    assert c.pricing_listing.key == "HK:03308"
    assert Company(id="x", name="x", pricing="A", listings=c.listings).pricing_listing.key == "A:300308"
    assert Company(id="y", name="y", listings=[Listing(market="US", code="NVDA", exchange="NASDAQ")]).pricing_listing.market == "US"
    with pytest.raises(ValueError, match="不在 listings"):
        Company(id="x", name="x", pricing="US", listings=c.listings)
    with pytest.raises(ValueError, match="只适用于美股"):
        Listing(market="HK", code="00700", ads_ratio=2)
    with pytest.raises(ValueError, match="必须为正"):
        Listing(market="US", code="XPEV", exchange="NYSE", ads_ratio=0)


def test_adr_premium_and_market_line(mapp):
    _bars(mapp, "116.09868", [39.0] * 30)
    _bars(mapp, "106.XPEV", [11.0] * 30)
    p = mapp.ads_premium(mapp.watchlist.get("xpeng"))
    assert p["current"] == pytest.approx(11.0 / (39.0 * 2 / 7.8) - 1)  # 港股折合每 ADS 10 美元，美股 11 美元 → 溢价 10%
    line = mapp.market_line(mapp.watchlist.get("xpeng"))
    assert "09868.HK（定价上市地）" in line and "ADS溢价（美股相对港股）：+10.0%" in line
    assert mapp.ads_premium(mapp.watchlist.get("innolight")) is None


def test_equivalents_keep_the_same_upside(mapp):
    _bars(mapp, "0.300308", [808.0] * 30)
    _bars(mapp, "116.03308", [1062.0] * 30)
    m = V.ValuationModel(driver_label="价", multiple_label="—", shares=1, price_currency="CNY", report_currency="CNY",
                         listing="A:300308", scenarios=[V.ScenarioPoint(name="base", driver=900, multiple=1, probability=1)])
    out = V.equivalents(mapp, m, mapp.watchlist.get("innolight"), 900.0)
    assert len(out) == 1 and f"03308.HK 等值 {900 * 1062 / 808:,.2f} HKD" in out[0]
    assert f"汇率平价 {900 / 0.9:,.2f} HKD（不计溢价）" in out[0]
    _bars(mapp, "116.09868", [39.0] * 30)
    _bars(mapp, "106.XPEV", [11.0] * 30)
    xm = m.model_copy(update={"listing": "HK:09868", "price_currency": "HKD"})
    out = V.equivalents(mapp, xm, mapp.watchlist.get("xpeng"), 40.0)
    assert f"XPEV 等值 {40 * 11 / 39:,.2f} USD" in out[0] and f"汇率平价 {40 / 7.8 * 2:,.2f} USD" in out[0]


def test_quote_shares_for_ads():
    us = Listing(market="US", code="XPEV", exchange="NYSE", ads_ratio=2)
    assert V.quote_shares({"price": 9.47, "market_cap": 9084945581.115}, us) == pytest.approx(19.19, abs=0.01)
    assert V.quote_shares({"price": 9.47, "market_cap": 9084945581.115}) == pytest.approx(9.59, abs=0.01)


def test_baseline_uses_card_price_listing(mapp):
    spec = DOSSIERS["innolight"]
    d = save_dossier(mapp, "innolight", spec)
    legacy, _ = V.build_baseline(mapp, mapp.watchlist.get("innolight"), d)
    assert legacy.listing == "A:300308" and legacy.price_currency == "CNY"   # 旧卡：当时按第一个上市地定价
    d["card"]["price_listing"] = "HK:03308"
    model, flags = V.build_baseline(mapp, mapp.watchlist.get("innolight"), d)
    assert model.listing == "HK:03308" and model.price_currency == "HKD"
    assert any("03308.HK 的行情快照" in f for f in flags)


def test_fcf_snapshot_prices_ev_on_pricing_listing(mapp, monkeypatch):
    data = {"flows": [{"date": "2025-12-31", "annual": True, "ocf": 50e8, "capex": 20e8}],
            "balance": {"date": "2025-12-31", "cash": 80e8, "invest": 20e8, "debt": 10e8, "debt_items": {"短期借款": 10e8},
                        "minority": 0, "liabilities": 50e8}}
    monkeypatch.setattr(tools.emdata, "fcf_data", lambda s, l: data)
    mapp.db.save_quote("0.300308", {"price": 10.0, "market_cap": 300e8})
    mapp.db.save_quote("116.03308", {"price": 11.0, "market_cap": 330e8})
    out = tools.fcf_snapshot(mapp, mapp.watchlist.get("innolight"))
    # 定价上市地是港股：市值 330 亿港元 × 0.9 = 297 亿人民币；EV = 297 + 10 − 100 = 207
    assert "市值按定价上市地 03308.HK" in out and "EV = 市值 + 有息负债 + 少数股东权益 − 现金及短期投资 = 207.00" in out
    assert "按 300308.SZ 股价：市值 300.00" in out and "EV 210.00" in out


def test_watchlist_entry_writes_pricing_and_ads_ratio():
    c = {"id": "xpeng", "name": "小鹏汽车", "pricing": "HK", "aliases": [], "watch_items": [], "news_queries": {},
         "listings": [{"market": "HK", "code": "09868"}, {"market": "US", "code": "XPEV", "exchange": "NYSE", "ads_ratio": 2.0}]}
    text = yamledit.dump({"companies": [yamledit.company_entry(c)]})
    assert "pricing: HK" in text and "ads_ratio: 2}" in text
    assert "ads_ratio" not in yamledit.dump({"companies": [yamledit.company_entry(
        {**c, "listings": [{"market": "HK", "code": "09868", "ads_ratio": 2}]})]})  # 只写美股的换股比例


def test_pinned_pricing_listing(mapp):
    c = mapp.watchlist.get("pinned")
    assert c.pricing_listing.key == "A:601869"
    assert "定价上市地：601869.SH" in mapp.company_context(c)
