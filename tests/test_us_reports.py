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
