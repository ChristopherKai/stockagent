from __future__ import annotations

from datetime import datetime, timezone

from stockagent import docs
from stockagent.config import Company, Listing
from stockagent.sources import sec

HTML = """<html><head><title>x</title></head><body>
<div style="display:none"><ix:header>HIDDEN-XBRL-HEADER</ix:header></div>
<p>BLOOM ENERGY 10-K</p>
<hr style="page-break-after:always">
<p>Item 1. Business</p>
<table><tr><td>Total revenue</td><td>$</td><td>2,023,994</td><td></td><td>(</td><td>5.1</td><td>)</td><td>%</td></tr></table>
<div style="page-break-before: always"><p>Item 7. MD&amp;A</p></div>
</body></html>"""


class _Resp:
    def __init__(self, payload=None, body=b""):
        self.payload, self.body = payload, body

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload

    def iter_content(self, n):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Session:
    def __init__(self, submissions, body):
        self.submissions, self.body, self.calls = submissions, body, []

    def get(self, url, **kw):
        self.calls.append((url, kw.get("headers")))
        return _Resp(self.submissions, self.body) if "submissions" in url else _Resp(body=self.body + url.encode())  # 每个文件内容不同（按哈希去重）


SUBMISSIONS = {"filings": {"recent": {
    "form": ["10-Q", "8-K", "10-K", "10-Q"],
    "acceptanceDateTime": ["2026-07-28T20:00:00.000Z", "2026-07-28T19:00:00.000Z", "2026-02-09T21:00:00.000Z", "2025-10-28T20:00:00.000Z"],
    "accessionNumber": ["0001-26-3", "0001-26-2", "0001-26-1", "0001-25-9"],
    "primaryDocument": ["be-q2.htm", "be-8k.htm", "be-10k.htm", "be-q3.htm"],
    "reportDate": ["2026-06-30", "2026-07-28", "2025-12-31", "2025-09-30"],
}}}


def test_html_text_pages_tables_and_hidden_header():
    text, pages = docs.html_text(HTML)
    assert pages == 3 and "HIDDEN-XBRL-HEADER" not in text
    assert text.startswith("[第1页]\nBLOOM ENERGY 10-K")
    assert "Total revenue | 2,023,994 | (5.1)%" in text
    assert "[第3页]\nItem 7. MD&A" in text
    assert docs.html_text("<p>没有分页样式</p>") == ("没有分页样式", None)


def test_sec_reports_keep_only_annual_and_quarterly():
    s = _Session(SUBMISSIONS, b"")
    rows = sec.reports(s, 1, "ua", datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [(r["doc_type"], r["period"]) for r in rows] == [
        ("quarterly_report", "2026-06-30"), ("annual_report", "2025"), ("quarterly_report", "2025-09-30")]
    assert rows[0]["url"].endswith("/edgar/data/1/0001263/be-q2.htm")


def test_us_only_company_downloads_10k_and_latest_10q(app):
    co = Company(id="bloom", name="Bloom Energy", listings=[Listing(market="US", code="BE", exchange="NYSE")])
    s = _Session(SUBMISSIONS, HTML.encode())
    app.db.kv_set("sec_cik:BE", "1664703")
    msgs = docs.fetch_reports(app.db, s, app.data_dir, co, "me@example.com")
    got = {(d["doc_type"], d["period"]) for d in app.db.documents("bloom")}
    assert got == {("annual_report", "2025"), ("quarterly_report", "2026-06-30")}, msgs
    assert all(h == {"User-Agent": "me@example.com"} for u, h in s.calls if "/Archives/" in u)
    assert not any("中期" in m for m in msgs)
    # 再跑一次：已入库的期间不重复下载
    n = len(s.calls)
    docs.fetch_reports(app.db, s, app.data_dir, co, "me@example.com")
    assert len([c for c in s.calls[n:] if "/Archives/" in c[0]]) == 0


def test_us_only_without_user_agent_is_skipped(app):
    co = Company(id="bloom", name="Bloom Energy", listings=[Listing(market="US", code="BE", exchange="NYSE")])
    assert "sec_user_agent" in docs.fetch_reports(app.db, _Session({}, b""), app.data_dir, co)[0]


def _facts(end: str, val: float) -> dict:
    return {"facts": {"us-gaap": {"Assets": {"units": {"USD": [{"end": end, "val": val, "form": "10-Q", "filed": "2026-04-29"}]}}}}}


def test_statement_flags_stale_xbrl_and_refetches(app, monkeypatch):
    from stockagent import tools

    listing = Listing(market="US", code="BE", exchange="NYSE")
    app.db.kv_set("sec_cik:BE", "1664703")
    app.settings.sources.sec_user_agent = "me@example.com"
    monkeypatch.setattr(sec, "reports", lambda *a, **k: [{"doc_type": "quarterly_report", "period_end": "2026-06-30"}])

    calls = []
    def stale(session, cik, ua, fresh=False):
        calls.append(fresh)
        return _facts("2026-03-31", 1e9)
    monkeypatch.setattr(sec, "company_facts", stale)
    out = tools.statement_table(app, listing, "balance", 4, False)
    assert calls == [False, True] and "XBRL 数据最新只到 2026-03-31" in out and "2026-06-30" in out

    def caught_up(session, cik, ua, fresh=False):
        return _facts("2026-06-30" if fresh else "2026-03-31", 1e9)
    monkeypatch.setattr(sec, "company_facts", caught_up)
    out = tools.statement_table(app, listing, "balance", 4, False)
    assert "⚠" not in out and "| 总资产 | 10.00" in out and "2026-06-30" in out


# ---------------------------------------------------------------- 报表推导（Q4 / TTM）、IFRS、数据清洗

def _fact(start, end, val, form="10-Q", filed="2026-05-01"):
    d = {"end": end, "val": val, "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


def _flow_facts(taxonomy="us-gaap", rev="RevenueFromContractWithCustomerExcludingAssessedTax", extra=None):
    """2025 全年 400（Q1 70 / 前三季累计 280），2026 Q1 100（上年同期 70）→ Q4(2025) = 120，TTM(2026Q1) = 400 + 100 − 70 = 430。"""
    revenue = [
        _fact("2025-01-01", "2025-03-31", 70), _fact("2025-01-01", "2025-09-30", 280), _fact("2025-07-01", "2025-09-30", 110),
        _fact("2025-01-01", "2025-12-31", 400, "10-K", "2026-02-10"), _fact("2026-01-01", "2026-03-31", 100),
    ]
    assets = [_fact(None, "2025-12-31", 900, "10-K"), _fact(None, "2026-03-31", 950)]
    concepts = {rev: {"units": {"USD": revenue}}, "Assets": {"units": {"USD": assets}}}
    concepts.update(extra or {})
    return {"facts": {taxonomy: concepts}}


def test_q4_and_ttm_are_derived_from_annual_and_ytd():
    d = sec.statement_from_facts(_flow_facts(), "income", 6)
    cols = {p["label"]: v for p, v in zip(d["periods"], dict(d["rows"])["收入"])}
    assert cols["2026-03-31（近12个月·推导）"] == 430
    assert cols["2025-12-31（3个月·推导）"] == 120 and cols["2025-12-31（全年）"] == 400
    assert cols["2026-03-31（3个月）"] == 100 and cols["2025-09-30（3个月）"] == 110
    assert d["missing"] == ["净利润"]
    assert sec.statement_from_facts(_flow_facts(), "income", 6, annual_only=True)["periods"] == [
        {"date": "2025-12-31", "label": "2025-12-31（全年）", "days": 365}]


def test_ifrs_filer_uses_ifrs_concepts():
    f = {"facts": {"ifrs-full": {"Revenue": {"units": {"CNY": [_fact("2025-01-01", "2025-12-31", 50, "20-F")]}},
                                 "Assets": {"units": {"CNY": [_fact(None, "2025-12-31", 80, "20-F")]}}}}}
    d = sec.statement_from_facts(f, "income", 4)
    assert d["currency"] == "CNY" and dict(d["rows"])["收入"] == [50]
    assert sec.statement_from_facts({"facts": {}}, "income") is None


def test_cost_takes_largest_concept_and_bad_eps_is_dropped():
    q3 = ("2025-07-01", "2025-09-30")
    extra = {
        "CostOfGoodsAndServicesSold": {"units": {"USD": [_fact(*q3, 2)]}},  # 小项
        "CostOfRevenue": {"units": {"USD": [_fact(*q3, 80)]}},                # 总成本
        "NetIncomeLoss": {"units": {"USD": [_fact(*q3, -10)]}},
        "EarningsPerShareBasic": {"units": {"USD/shares": [_fact(*q3, -100)]}},  # 应是 -0.1
        "WeightedAverageNumberOfSharesOutstandingBasic": {"units": {"shares": [_fact(*q3, 100)]}},
    }
    d = sec.statement_from_facts(_flow_facts(extra=extra), "income", 6)
    rows = dict(d["rows"])
    i = [p["date"] for p in d["periods"]].index("2025-09-30")
    assert rows["营业成本"][i] == 80
    assert "基本每股收益" not in rows and any("基本每股收益申报值 -100" in n for n in d["notes"])


def test_exhibit_text_and_8k_item_labels(app):
    class S:
        def get(self, url, **kw):
            if url.endswith("index.json"):
                return _Resp({"directory": {"item": [{"name": "d1k.htm"}, {"name": "d1dex992.htm"}, {"name": "d1dex991.htm"}]}})
            assert url.endswith("d1dex991.htm")
            r = _Resp()
            r.text = "<p>XPeng Announces Q2 Results</p><p>Revenue was RMB 20 billion.</p>"
            return r
    head, body = sec.exhibit_text(S(), 7, "0001-26-3", "ua")
    assert head == "XPeng Announces Q2 Results" and "RMB 20 billion" in body

    from stockagent import app as appmod
    co = Company(id="x", name="X", listings=[Listing(market="US", code="XPEV", exchange="NYSE")])
    app.settings.sources.sec_user_agent = "ua"
    rows = [{"source_id": "0001-26-3", "title": "6-K FORM 6-K", "category": "6-K", "published_at": datetime.now(timezone.utc)},
            {"source_id": "0001-26-4", "title": "10-Q", "category": "10-Q", "published_at": datetime.now(timezone.utc)}]
    app._session = S()
    appmod._add_exhibits(app, co, co.listings[0], 7, rows)
    assert rows[0]["title"] == "6-K XPeng Announces Q2 Results" and rows[0]["snippet"].startswith("[新闻稿正文]")
    assert "snippet" not in rows[1]  # 10-Q 不取附件
    s = _Session({"filings": {"recent": {"form": ["8-K"], "acceptanceDateTime": ["2026-07-28T19:00:00.000Z"], "accessionNumber": ["a"],
                                         "primaryDocument": ["x.htm"], "items": ["2.02,9.01"]}}}, b"")
    out = sec.filings(s, 1, "ua", datetime(2026, 1, 1, tzinfo=timezone.utc), {"8-K"})
    assert "items: 2.02,9.01 · 业绩公告" in out[0]["title"]


def test_profile_excerpt_keeps_10k_mdna_heading():
    from stockagent import profile

    filler = "x" * 40
    text = "\n".join(
        ["ITEM 1—BUSINESS"] + [filler] * 400 + ["ITEM 1A—RISK FACTORS"] + [filler] * 3000
        + ["ITEM 7—MANAGEMENT’S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION", "收入同比增长 37%"] + [filler] * 3000
        + ["Liquidity and Capital Resources", "现金 5 亿美元"] + [filler] * 500)
    # 正文里的交叉引用（不在行首）不能抢走预算
    text = text.replace("ITEM 1—BUSINESS", "ITEM 1—BUSINESS see Management’s Discussion and Analysis", 1)
    out, cut = profile.select_text(text, 20000)
    assert cut and "ITEM 7—MANAGEMENT" in out and "收入同比增长 37%" in out
    assert "\nLiquidity and Capital Resources\n现金 5 亿美元" in out  # 离 Item 7 开头较远，也要单独保留


# ---------------------------------------------------------------- 美股 EV/FCF、估值序列、股东、做空、新闻稿

def _full_facts():
    f = _flow_facts(extra={
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            _fact("2025-01-01", "2025-12-31", 120, "10-K"), _fact("2025-01-01", "2025-03-31", 20), _fact("2026-01-01", "2026-03-31", 50)]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
            _fact("2025-01-01", "2025-12-31", 30, "10-K"), _fact("2025-01-01", "2025-03-31", 5), _fact("2026-01-01", "2026-03-31", 10)]}},
        "PaymentsForSoftware": {"units": {"USD": [_fact("2025-01-01", "2025-12-31", 10, "10-K"), _fact("2026-01-01", "2026-03-31", 2)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_fact(None, "2026-03-31", 500)]}},
        "ShortTermInvestments": {"units": {"USD": [_fact(None, "2026-03-31", 100)]}},
        "LongTermDebtNoncurrent": {"units": {"USD": [_fact(None, "2026-03-31", 300)]}},
        "DebtCurrent": {"units": {"USD": [_fact(None, "2026-03-31", 40)]}},
        "Liabilities": {"units": {"USD": [_fact(None, "2026-03-31", 600)]}},
    })
    return f


def test_us_fcf_data_feeds_the_shared_metrics():
    from stockagent import fcf, usdata

    d = usdata.fcf_data(_full_facts())
    assert d["balance"]["debt"] == 340 and d["balance"]["cash"] == 500 and d["balance"]["invest"] == 100
    assert [(x["date"], x["annual"], x["ocf"], x["capex"]) for x in d["flows"]] == [
        ("2026-03-31", False, 50, 12), ("2025-12-31", True, 120, 40), ("2025-03-31", False, 20, 5)]
    m = fcf.metrics(d, 1000)
    assert m["ev"] == 1000 + 340 - 600 and m["fcf_base_label"].startswith("TTM") and m["fcf_base"] == (50 - 12) + (120 - 40) - (20 - 5)


def test_us_eps_points_drop_mistagged_eps():
    from stockagent import usdata

    f = _flow_facts(extra={
        "EarningsPerShareDiluted": {"units": {"USD/shares": [
            _fact("2025-01-01", "2025-12-31", 4.0, "10-K"), _fact("2025-07-01", "2025-09-30", -100)]}},
        "NetIncomeLoss": {"units": {"USD": [_fact("2025-07-01", "2025-09-30", 10), _fact("2025-01-01", "2025-12-31", 40, "10-K")]}},
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
            _fact("2025-07-01", "2025-09-30", 10), _fact("2025-01-01", "2025-12-31", 10, "10-K")]}},
    })
    pts, cur = usdata.eps_bps_points(f)
    assert cur == "USD" and [round(p[1], 2) for p in pts] == [4.0]  # -100 被剔除，只剩全年 4.0
    assert pts[0][0] == "2026-03-16"  # 年报截止日 + 75 天


def test_us_tools_use_xbrl(app, monkeypatch):
    from stockagent import tools, usdata

    co = Company(id="bloom", name="Bloom", listings=[Listing(market="US", code="BE", exchange="NYSE")])
    app.watchlist.companies.append(co)
    monkeypatch.setattr(usdata, "load_facts", lambda a, l: (_full_facts(), "XBRL 落后提示"))
    app.db.save_quote("106.BE", {"price": 10.0, "market_cap": 1000.0})
    out = tools.ToolBox(app, co).call("fcf_yield", {})
    assert "SEC XBRL" in out and "XBRL 落后提示" in out and "有息负债" in out
    assert "收入" in tools.ToolBox(app, co).call("financials", {}) or "主要财务指标" in tools.ToolBox(app, co).call("financials", {})


def test_nasdaq_holders_insiders_and_finra_parsers():
    from stockagent.sources import finra, nasdaq

    assert nasdaq._num("(366,261)") == -366261 and nasdaq._num("$270.06") == 270.06 and nasdaq._num("-26.41%") == -26.41
    d = finra._row('"settlementDate"|"symbolCode"|"currentShortPositionQuantity"|"previousShortPositionQuantity"|"averageDailyVolumeQuantity"|"daysToCoverQuantity"|"changePercent"\n'
                   '"2026-09-15"|"BE"|"100"|"90"|"50"|"2.0"|"11.1"\n', "be")
    assert d["short"] == 100 and d["days_to_cover"] == 2.0
    from datetime import date
    ds = finra.settlement_dates(date(2026, 10, 5), 4)
    assert ds[0] == date(2026, 9, 30) and ds[1] == date(2026, 9, 15) and all(x.weekday() < 5 for x in ds)


def test_sina_and_tencent_us_quote_parsing():
    from stockagent.sources import sina, tencent

    quote = 'var hq_str_gb_be="BE,287.85,-0.45,2026-10-05 23:21:21,-1.3,289.99,291.96,282.34,351.28,75.7,2953954,14775878,84779696546,-0.03,--,0.00,6.06,0.00,0.00,294527346,0,0.0,0.0,0.0,,Oct 05,289.15,0,1";'

    class S:
        def get(self, url, **kw):
            r = _Resp(body=b"")
            r.content = quote.encode()
            r.text = 'var _x=([{"d":"2026-10-02","o":"1","h":"2","l":"0.5","c":"1.5","v":"10","a":"15"}])'
            return r

    q = sina.fetch_quote(S(), "106.BE")
    assert q["market_cap"] == 84779696546 and q["shares"] == 294527346 and q["prev_close"] == 289.15
    assert sina.fetch_klines(S(), "106.BE")[0]["close"] == 1.5 and sina.fetch_klines(S(), "116.00100") == []
    assert tencent.symbol_for("106.BE") == "usBE.N"  # K 线要带交易所后缀；快照接口会在 fetch_quote 里去掉


def test_results_release_is_stored_as_document(app):
    from stockagent import app as appmod

    co = Company(id="x", name="X", listings=[Listing(market="US", code="XPEV", exchange="NYSE")])
    x8 = {"category": "8-K", "title": "8-K（8-K items: 2.02,9.01 · 业绩公告）", "source_id": "0001-26-9", "published_at": datetime.now(timezone.utc)}
    assert appmod._is_results_release(x8, "anything")
    assert appmod._is_results_release({"category": "6-K", "title": "6-K", "published_at": None}, "XPENG Reports Second Quarter 2026 Results")
    assert not appmod._is_results_release({"category": "6-K", "title": "6-K", "published_at": None}, "XPENG Announces Vehicle Delivery")
    appmod._store_release(app, co, x8, "Q2 Results", "Revenue was 10 billion.\nGuidance raised.", "https://sec/x.htm")
    d = app.db.documents("x")[0]
    assert d["doc_type"] == "other" and "新闻稿：Q2 Results" in d["title"] and d["source"] == "sec"


def test_us_only_company_also_gets_prior_10ks_and_proxy(app):
    subs = {"filings": {"recent": {
        "form": ["DEF 14A", "10-K", "10-K", "10-K", "10-K"],
        "acceptanceDateTime": ["2026-04-20T20:00:00.000Z", "2026-02-09T21:00:00.000Z", "2025-02-10T21:00:00.000Z",
                               "2024-02-12T21:00:00.000Z", "2023-02-13T21:00:00.000Z"],
        "accessionNumber": ["a1", "a2", "a3", "a4", "a5"], "primaryDocument": ["p.htm", "k25.htm", "k24.htm", "k23.htm", "k22.htm"],
        "reportDate": ["2026-04-20", "2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"]}}}
    co = Company(id="bloom", name="Bloom Energy", listings=[Listing(market="US", code="BE", exchange="NYSE")])
    app.db.kv_set("sec_cik:BE", "1664703")
    docs.fetch_reports(app.db, _Session(subs, HTML.encode()), app.data_dir, co, "me@example.com")
    got = {(d["doc_type"], d["period"]) for d in app.db.documents("bloom")}
    assert got == {("annual_report", "2025"), ("annual_report", "2024"), ("annual_report", "2023"), ("other", "DEF14A-2026")}
