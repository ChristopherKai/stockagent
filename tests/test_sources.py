"""数据源解析（用录制的响应片段，不联网）。"""

from __future__ import annotations

import json

import pytest
from datetime import datetime, timezone

from stockagent.config import Listing
from stockagent.sources import cninfo, eastmoney, google_news, hkex, tencent


class FakeResponse:
    def __init__(self, payload=None, text=None, content=None):
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.content = content if content is not None else self.text.encode("utf-8")

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.text)

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.response

    post = get


def test_eastmoney_secids():
    assert eastmoney.secid_for(Listing(market="HK", code="2577")) == "116.02577"
    assert eastmoney.secid_for(Listing(market="A", code="688337")) == "1.688337"
    assert eastmoney.secid_for(Listing(market="A", code="300308")) == "0.300308"
    assert eastmoney.secid_for(Listing(market="US", code="COHR", exchange="NYSE")) == "106.COHR"


def test_eastmoney_klines_parse():
    payload = {"data": {"klines": ["2026-09-28,39.400,37.360,39.420,36.940,5486508,205548294.000,6.29,-5.23,-2.060,0.61"]}}
    bars = eastmoney.fetch_klines(FakeSession(FakeResponse(payload)), "116.02577")
    assert bars == [{"date": "2026-09-28", "open": 39.4, "close": 37.36, "high": 39.42, "low": 36.94,
                     "volume": 5486508.0, "amount": 205548294.0, "turnover": 0.61}]


def test_eastmoney_quote_parse():
    payload = {"data": {"f43": 815.0, "f57": "300308", "f58": "中际旭创", "f162": "-", "f164": 46.94,
                        "f170": -9.03, "f116": 959996357415.0, "f189": 20120410}}
    q = eastmoney.fetch_quote(FakeSession(FakeResponse(payload)), "0.300308")
    assert q["pe_dynamic"] is None and q["pe_ttm"] == 46.94
    assert q["change_pct"] == pytest.approx(-0.0903) and q["listing_date"] == "20120410"


def test_tencent_symbols_and_klines():
    assert tencent.symbol_for("116.02577") == "hk02577"
    assert tencent.symbol_for("0.399006") == "sz399006"
    assert tencent.symbol_for("106.COHR") == "usCOHR.N"
    assert tencent.symbol_for("100.SPX") == "us.INX"
    assert tencent.symbol_for("133.HKDCNH") is None
    payload = {"data": {"hk02577": {"day": [["2026-09-28", "39.400", "37.360", "39.420", "36.940", "5486508.00", {}, "0.61", "20554.83"]]}}}
    bars = tencent.fetch_klines(FakeSession(FakeResponse(payload)), "116.02577")
    assert bars[0]["close"] == 37.36 and bars[0]["turnover"] == pytest.approx(0.0061)


def test_hkex_search_parse():
    rows = [{"FILE_INFO": "167KB", "NEWS_ID": "12343533", "TITLE": "臨時股東會投票表決結果",
             "DATE_TIME": "23/09/2026 19:38", "LONG_TEXT": "公告及通告 - [股東特別大會的結果 &#x2f; 修訂憲章文件]",
             "STOCK_CODE": "02577", "FILE_TYPE": "PDF", "FILE_LINK": "/listedco/listconews/sehk/2026/0923/x_c.pdf"}]
    s = FakeSession(FakeResponse({"result": json.dumps(rows), "hasNextRow": False}))
    out = hkex.announcements(s, 1000243259, datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 28, tzinfo=timezone.utc))
    assert out[0]["published_at"] == datetime(2026, 9, 23, 11, 38, tzinfo=timezone.utc)
    assert out[0]["category"] == "公告及通告 - [股東特別大會的結果 / 修訂憲章文件]"
    assert out[0]["url"].startswith("https://www1.hkexnews.hk/listedco/")
    assert s.calls[0][1]["params"]["fromDate"] == "20260901"


def test_hkex_empty_result():
    s = FakeSession(FakeResponse({"result": "null"}))
    assert hkex.announcements(s, 1, datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 2, tzinfo=timezone.utc)) == []


def test_cninfo_reports_filter():
    anns = [
        {"announcementId": "1", "announcementTitle": "2026年半年度报告", "announcementTime": 1787673600000,
         "adjunctUrl": "finalpage/2026-08-26/1.PDF", "adjunctType": "PDF", "adjunctSize": 2192},
        {"announcementId": "2", "announcementTitle": "2026年半年度报告摘要", "announcementTime": 1787673600000,
         "adjunctUrl": "finalpage/2026-08-26/2.PDF", "adjunctType": "PDF", "adjunctSize": 139},
        {"announcementId": "3", "announcementTitle": "2025年年度报告", "announcementTime": 1774454400000,
         "adjunctUrl": "finalpage/2026-03-26/3.PDF", "adjunctType": "PDF", "adjunctSize": 2507},
    ]
    s = FakeSession(FakeResponse({"announcements": anns, "hasMore": False}))
    out = cninfo.reports(s, Listing(market="A", code="688337"), "9900046628",
                         datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2026, 9, 28, tzinfo=timezone.utc))
    assert [(x["doc_type"], x["source_id"]) for x in out] == [("interim_report", "1"), ("annual_report", "3")]
    assert out[0]["url"] == "http://static.cninfo.com.cn/finalpage/2026-08-26/1.PDF"


def test_google_news_parse():
    xml = """<?xml version="1.0"?><rss><channel>
    <item><title>美兩黨推法案限制中國光通訊產品 中際旭創收跌逾12％ - 工商時報</title>
      <link>https://news.google.com/rss/articles/abc</link><guid>abc</guid>
      <pubDate>Mon, 28 Sep 2026 09:59:00 GMT</pubDate><source url="https://ctee.com.tw">工商時報</source></item>
    <item><title>no date</title></item>
    </channel></rss>"""
    out = google_news.search(FakeSession(FakeResponse(text=xml)), '"中際旭創"', "zh-HK")
    assert len(out) == 1
    assert out[0]["title"] == "美兩黨推法案限制中國光通訊產品 中際旭創收跌逾12％"
    assert out[0]["media"] == "工商時報"
    assert out[0]["published_at"] == datetime(2026, 9, 28, 9, 59, tzinfo=timezone.utc)


def test_southbound_rows_are_summed_per_day():
    from stockagent.sources import emdata

    rows = [
        {"HOLD_DATE": "2026-09-24 00:00:00", "HOLD_NUM": 72430100, "HOLD_SHARES_RATIO": 8.05, "HOLD_MARKET_CAP": 1.0},
        {"HOLD_DATE": "2026-09-24 00:00:00", "HOLD_NUM": 37913391, "HOLD_SHARES_RATIO": 4.21, "HOLD_MARKET_CAP": 2.0},
        {"HOLD_DATE": "2026-09-23 00:00:00", "HOLD_NUM": 71450600, "HOLD_SHARES_RATIO": 7.94, "HOLD_MARKET_CAP": 1.0},
        {"HOLD_DATE": "2026-09-23 00:00:00", "HOLD_NUM": 37614891, "HOLD_SHARES_RATIO": 4.18, "HOLD_MARKET_CAP": 2.0},
    ]
    s = FakeSession(FakeResponse({"result": {"data": rows}}))
    series = emdata.southbound_holdings(s, "02577")
    assert [x["shares"] for x in series] == [109065491.0, 110343491.0]
    assert series[-1]["ratio"] == pytest.approx(12.26)


def test_search_relevance_filter():
    from stockagent.sources import web

    terms = web.query_terms('"东岳集团" R32 配额 2027 site:hkexnews.hk')
    assert terms == ["东岳集团", "r32", "配额", "hkexnews.hk"]
    ok = {"title": "東岳集團：R32 配額龍頭", "snippet": "", "url": "https://x.com/a"}
    junk = {"title": "Microsoft 365 for Individuals", "snippet": "Sign in to your account", "url": "https://www.microsoft.com/"}
    assert web.relevant(terms, ok) and not web.relevant(terms, junk)
    # 长中文词段：命中一半以上的相邻两字组合即可
    long_terms = web.query_terms("英飞凌侵权英诺赛科")
    assert web.relevant(long_terms, {"title": "英诺赛科起诉英飞凌专利侵权", "snippet": "", "url": ""})
    assert not web.relevant(long_terms, {"title": "玉渊潭樱花盛开", "snippet": "北京", "url": ""})


def test_bing_redirect_url_decoded():
    from stockagent.sources import web

    href = "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly93d3cuaW5ub3NjaWVuY2UuY29tLw&ntb=1"
    assert web._bing_url(href) == "https://www.innoscience.com/"
    assert web._bing_url("https://example.com/x") == "https://example.com/x"


def test_search_falls_through_blocked_and_irrelevant(monkeypatch):
    from stockagent.sources import web

    def blocked(q, n):
        raise web.Blocked("验证页")

    def junk(q, n):
        return [{"title": "网易163免费邮箱", "snippet": "登录", "url": "https://mail.163.com"}]

    def good(q, n):
        return [{"title": "东岳集团 R32 配额", "snippet": "2027 年配额", "url": "https://a.cn/1"}, *junk(q, n)]

    monkeypatch.setattr(web, "PROVIDERS", {"a": blocked, "b": junk, "c": good})
    monkeypatch.setattr(web, "_blocked", {})
    monkeypatch.setattr(web, "_cache", {})
    monkeypatch.setattr(web, "MIN_INTERVAL", 0)
    res, src = web.search(None, "东岳集团 R32 配额", 5, ["a", "b", "c"])
    assert src == "c" and len(res) == 1 and "a" in web._blocked
    res, src = web.search(None, "东岳集团 R32 配额", 5, ["a", "b"])
    assert res == [] and "暂停" in src and "无关" in src


def test_search_rotates_between_top_providers(monkeypatch):
    from stockagent.sources import web

    def make(name):
        return lambda q, n: [{"title": f"东岳集团 {name}", "snippet": "", "url": f"https://{name}.cn"}]

    monkeypatch.setattr(web, "PROVIDERS", {"x": make("x"), "y": make("y"), "z": make("z")})
    monkeypatch.setattr(web, "_blocked", {})
    monkeypatch.setattr(web, "_last", {})
    monkeypatch.setattr(web, "_cache", {})
    monkeypatch.setattr(web, "MIN_INTERVAL", 0)
    monkeypatch.setattr(web, "ROTATE", 2)
    used = [web.search(None, f"东岳集团 {i}", 3, ["x", "y", "z"])[1] for i in range(4)]
    assert used == ["x", "y", "x", "y"]  # 第三个来源只在前两个都失败时使用
    assert web.search(None, "东岳集团 0", 3, ["x", "y", "z"])[1] == "x"  # 相同检索式用缓存，不再请求


def test_paid_search_api_first_and_bad_key_paused(monkeypatch):
    from stockagent.sources import web

    calls = []

    def paid(q, n):
        calls.append("paid")
        raise web.Blocked("key 无效或额度不足（HTTP 401）", 3600)

    monkeypatch.setattr(web, "PROVIDERS", {"bocha": paid, "x": lambda q, n: [{"title": "东岳集团", "snippet": "", "url": "u"}]})
    monkeypatch.setattr(web, "_blocked", {})
    monkeypatch.setattr(web, "_cache", {})
    monkeypatch.setattr(web, "MIN_INTERVAL", 0)
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    assert web.search(None, "东岳集团 a", 3, ["x", "bocha"])[1] == "x" and calls == []  # 没有 key 就不用
    monkeypatch.setenv("BOCHA_API_KEY", "k")
    assert web.search(None, "东岳集团 b", 3, ["x", "bocha"])[1] == "x" and calls == ["paid"]  # 有 key 排在最前
    assert web.search(None, "东岳集团 c", 3, ["x", "bocha"])[1] == "x" and calls == ["paid"]  # key 无效后暂停


def test_paid_search_api_response_parsing(monkeypatch):
    from stockagent.sources import web

    replies = {
        "https://api.bochaai.com/v1/web-search": {"code": 200, "data": {"webPages": {"value": [
            {"name": "东岳集团获配额", "url": "https://a.cn", "snippet": "片段", "summary": "摘要", "siteName": "证券时报",
             "datePublished": "2026-09-20T08:00:00+08:00"}]}}},
        "https://api.tavily.com/search": {"results": [{"title": "Dongyue quota", "url": "https://b.com", "content": "text"}]},
        "https://google.serper.dev/search": {"organic": [{"title": "东岳 R32", "link": "https://c.cn", "snippet": "报价", "date": "3 天前"}]},
    }

    class R:
        def __init__(self, url):
            self.url, self.status_code = url, 200

        def raise_for_status(self):
            pass

        def json(self):
            return replies[self.url]

    monkeypatch.setattr(web.requests, "post", lambda url, **kw: R(url))
    for k in web.KEYS.values():
        monkeypatch.setenv(k, "k")
    assert web._bocha("东岳", 5) == [{"title": "东岳集团获配额", "url": "https://a.cn", "snippet": "2026-09-20 证券时报 摘要"}]
    assert web._tavily("Dongyue", 5)[0]["url"] == "https://b.com"
    assert web._serper("东岳", 5)[0] == {"title": "东岳 R32", "url": "https://c.cn", "snippet": "3 天前 报价"}


def test_toutiao_nested_jump_url():
    from stockagent.sources import web

    href = ("https://sou.toutiao.com/search/jump?url=https%3A%2F%2Fsou.toutiao.com%2Fsearch%2Fjump%3Furl%3Dhttp%253A%252F%252F"
            "www.toutiao.com%252Fa7566183164332081718%252F%253Fchannel%253D%2526source%253Dsearch_tab%26aid%3D4916&aid=4916")
    assert web._toutiao_url(href) == "http://www.toutiao.com/a7566183164332081718/?channel=&source=search_tab"


def test_gnews_provider_parses_rss(monkeypatch):
    from stockagent.sources import web

    rss = """<?xml version="1.0"?><rss><channel>
      <item><title>高盛：维持英诺赛科买入评级 目标价114港元 - 智通财经</title><link>https://news.google.com/rss/articles/x</link>
      <pubDate>Wed, 09 Sep 2026 02:00:00 GMT</pubDate><source url="https://zhitong.com">智通财经</source></item>
    </channel></rss>""".encode()

    class R:
        content = rss

        def raise_for_status(self):
            pass

    monkeypatch.setattr(web._http, "get", lambda *a, **k: R())
    out = web._gnews("英诺赛科 目标价", 5)
    assert out == [{"title": "高盛：维持英诺赛科买入评级 目标价114港元", "url": "https://news.google.com/rss/articles/x",
                    "snippet": "2026-09-09 智通财经 （新闻标题；可用 fetch_url 打开原文核实）"}]


EXA_SAMPLE = """Title: 9月底制冷剂价格：R32、R134a硬扛，R22先松口
URL: http://zlsq.hvacr.cn/Pages/ArticleDetail.aspx?aid=35841
Published: 2026-09-24T00:00:00.000Z
Author: N/A
Highlights:
R32 主流大厂内贸报盘集中在65,500元/吨。

---

Title: [制冷剂日评]：氟制冷剂R32市场高位横盘
URL: https://nenghua.m.mysteel.com/a/26092217/D91FE2F431ECEDAE_abc.html
Published: N/A
Highlights:
2026年9月份，家用空调排产975万台"""


def test_parse_exa_results():
    from stockagent.sources import web

    out = web.parse_exa_results(EXA_SAMPLE, 5)
    assert [r["url"] for r in out] == ["http://zlsq.hvacr.cn/Pages/ArticleDetail.aspx?aid=35841",
                                       "https://nenghua.m.mysteel.com/a/26092217/D91FE2F431ECEDAE_abc.html"]
    assert out[0]["snippet"].startswith("2026-09-24") and "65,500" in out[0]["snippet"]
    assert out[1]["title"].startswith("[制冷剂日评]") and "975万台" in out[1]["snippet"]


def test_exa_is_first_when_mcporter_available(monkeypatch):
    from stockagent.sources import web

    calls = []
    monkeypatch.setattr(web, "MCPORTER", "/usr/bin/mcporter")
    monkeypatch.setattr(web, "PROVIDERS", {
        "exa": lambda q, n: calls.append("exa") or [{"title": "东岳集团 R32", "snippet": "", "url": "https://a.cn"}],
        "x": lambda q, n: calls.append("x") or []})
    monkeypatch.setattr(web, "_blocked", {})
    assert web.search(None, "东岳集团 R32", 5, ["x", "exa"])[1] == "exa" and calls == ["exa"]
    monkeypatch.setattr(web, "MCPORTER", None)  # 没装 mcporter：跳过 Exa
    assert web.search(None, "东岳集团 R32 配额", 5, ["x", "exa"])[0] == [] and calls == ["exa", "x"]


def test_exa_rest_api_and_rate_limit(monkeypatch):
    import subprocess

    from stockagent.sources import web

    class R:
        status_code = 200

        def __init__(self, url, body):
            self.url, self.body = url, body

        def raise_for_status(self):
            pass

        def json(self):
            if self.url.endswith("/search"):
                return {"results": [{"title": "R32 周评", "url": "https://a.cn/1", "publishedDate": "2026-09-24T00:00:00.000Z",
                                     "highlights": ["R32 报盘 65500 元/吨"]}]}
            return {"results": [{"title": "东岳集团预测", "text": "目标价预测平均价为18.12" + "。" * 300}]}

    seen = []
    monkeypatch.setattr(web.requests, "post", lambda url, json=None, headers=None, timeout=None: seen.append(headers) or R(url, json))
    monkeypatch.setenv("EXA_API_KEY", "k")
    out = web._exa("R32 价格", 5)
    assert out == [{"title": "R32 周评", "url": "https://a.cn/1", "snippet": "2026-09-24 R32 报盘 65500 元/吨"}]
    assert seen[0] == {"x-api-key": "k"} and "exa" in web.api_providers()
    assert web.exa_fetch("https://www.futunn.com/hk/stock/00189-HK/forecast").startswith("（经 Exa 读取）\n标题：东岳集团预测")

    monkeypatch.delenv("EXA_API_KEY")
    monkeypatch.setattr(web, "MCPORTER", "/usr/bin/mcporter")
    monkeypatch.setattr(web.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 1, '{"error": "You\'ve hit Exa\'s free MCP rate limit", "issue": {"statusCode": 429}}', ""))
    with pytest.raises(web.Blocked) as e:
        web._exa("R32 价格", 5)
    assert e.value.seconds == 3600 and "EXA_API_KEY" in str(e.value)
