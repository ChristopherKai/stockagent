"""推特（twitter-cli）适配器与 agent 工具：固定 argv、凭证规则、熔断、输出格式、工具白名单。"""

from __future__ import annotations

import json
import subprocess

import pytest

from stockagent import tracker
from stockagent.sources import twitter
from stockagent.tools import ToolBox, twitter_enabled


def tweet(i, text, author="wallstengine", likes=50, views=1000, verified=True, rt=False, at="2026-10-01T05:11:00+00:00"):
    return {"id": str(i), "text": text, "createdAtISO": at, "urls": [], "lang": "en", "isRetweet": rt,
            "metrics": {"likes": likes, "retweets": 3, "views": views},
            "author": {"screenName": author, "name": author, "verified": verified}}


@pytest.fixture
def tw(monkeypatch):
    """启用推特：假的可执行文件路径与凭证；subprocess.run 记录命令并返回预设输出。"""
    monkeypatch.setattr(twitter, "BIN", "/fake/twitter")
    monkeypatch.setenv(twitter.ENV_AUTH, "secret-auth")
    monkeypatch.setenv(twitter.ENV_CT0, "secret-ct0")
    state = {"calls": [], "out": {"ok": True, "data": []}}

    def run(argv, **kw):
        state["calls"].append({"argv": argv, "env": kw["env"], "timeout": kw.get("timeout")})
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(state["out"]), stderr="warning: whatever")

    monkeypatch.setattr(twitter.subprocess, "run", run)
    return state


def test_search_argv_is_fixed_and_credentials_only_in_env(tw):
    tw["out"] = {"ok": True, "data": [tweet(1, "XPeng delivered 41,256 vehicles")]}
    posts = twitter.search("$XPEV; rm -rf / --from evil", n=5, days=3, min_likes=10, sort="top", from_user="@FirstSquawk")
    argv = tw["calls"][0]["argv"]
    assert argv[:2] == ["/fake/twitter", "search"]
    assert argv[-2:] == ["--", "$XPEV; rm -rf / --from evil"]  # 检索词在 -- 之后，只当数据；所有选项（含 --json）都在它之前
    assert argv.index("--json") < argv.index("--") and argv.count("--from") == 1
    assert argv[argv.index("--type") + 1] == "top" and argv[argv.index("--min-likes") + 1] == "10"
    assert argv[argv.index("--from") + 1] == "FirstSquawk" and "retweets" in argv
    assert "secret-auth" not in " ".join(argv) and "secret-ct0" not in " ".join(argv)
    env = tw["calls"][0]["env"]
    assert env[twitter.ENV_AUTH] == "secret-auth" and env[twitter.ENV_CT0] == "secret-ct0"
    assert posts[0]["url"] == "https://x.com/wallstengine/status/1" and posts[0]["likes"] == 50


def test_read_limit_and_timeout_scale_with_count(tw):
    twitter.user_posts("FirstSquawk", 500)  # twitter-cli 自动翻页：共享账号要读上百条；上限 200，超时随条数放大
    c = tw["calls"][0]
    assert c["argv"][c["argv"].index("-n") + 1] == "200" and c["timeout"] == twitter.TIMEOUT + 200 * 0.3
    twitter.search("XPeng", 5)
    c = tw["calls"][1]
    assert c["argv"][c["argv"].index("-n") + 1] == "5" and c["timeout"] == twitter.TIMEOUT + 5 * 0.3


def test_search_lang_option_is_validated_and_passed(tw, app):
    twitter.search("XPeng", 5, lang="en")
    argv = tw["calls"][0]["argv"]
    assert argv[argv.index("--lang") + 1] == "en" and argv.index("--lang") < argv.index("--")
    for bad in ("en;rm", "English", "e", "zh-CN"):
        with pytest.raises(twitter.TwitterError):
            twitter.search("XPeng", 5, lang=bad)
    assert len(tw["calls"]) == 1
    twitter.search("XPeng", 5)  # 不指定语种：不带 --lang
    assert "--lang" not in tw["calls"][1]["argv"]
    ToolBox(app, None).call("twitter_search", {"query": "VLCC rates TD3C", "lang": " EN "})  # 工具：agent 可指定语种
    argv = tw["calls"][2]["argv"]
    assert argv[argv.index("--lang") + 1] == "en"
    assert "英文" in twitter.HINT and "lang=en" in twitter.HINT and "lang=zh" in twitter.HINT


def test_invalid_input_never_runs(tw):
    for bad in ("", "a b", "x" * 16, "evil;rm", "-n"):
        with pytest.raises(twitter.TwitterError):
            twitter.user_posts(bad)
    with pytest.raises(twitter.TwitterError):
        twitter.search("   ")
    with pytest.raises(twitter.TwitterError):
        twitter.search("x" * 300)
    with pytest.raises(twitter.TwitterError):
        twitter.search("XPeng", from_user="a b")
    assert tw["calls"] == []


def test_no_credentials_means_no_subprocess(monkeypatch, tmp_path):
    """没有凭证时不运行：twitter-cli 会在这种情况下自动读取浏览器 Cookie。"""
    monkeypatch.setattr(twitter, "BIN", "/fake/twitter")
    called = []
    monkeypatch.setattr(twitter.subprocess, "run", lambda *a, **k: called.append(a))
    assert not twitter.available()
    with pytest.raises(twitter.TwitterError) as e:
        twitter.user_posts("XPENG_Global")
    assert e.value.code == "unavailable" and called == []


def test_credentials_fall_back_to_agent_reach_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("twitter_auth_token: aaa\ntwitter_ct0: bbb\n", encoding="utf-8")
    monkeypatch.setattr(twitter, "AGENT_REACH_CONFIG", cfg)
    monkeypatch.setattr(twitter, "BIN", "/fake/twitter")
    assert twitter.credentials() == ("aaa", "bbb") and twitter.available()
    monkeypatch.setenv(twitter.ENV_AUTH, "env-a")
    monkeypatch.setenv(twitter.ENV_CT0, "env-b")
    assert twitter.credentials() == ("env-a", "env-b")  # .env 优先


@pytest.mark.parametrize("code,msg,expect", [
    ("not_found", "User @x not found", "not_found"),
    ("api_error", "Cookie expired or invalid (HTTP 401)", "auth"),
    ("api_error", "HTTP 429 Too Many Requests", "rate_limit"),
])
def test_error_classification(tw, code, msg, expect):
    tw["out"] = {"ok": False, "error": {"code": code, "message": msg}}
    with pytest.raises(twitter.TwitterError) as e:
        twitter.user_posts("someone")
    assert e.value.code == expect and e.value.trips_breaker == (expect != "not_found")


def test_tools_offered_only_when_available(app, tw, monkeypatch):
    assert twitter_enabled(app) and {"twitter_search", "twitter_user"} <= set(ToolBox(app, None).tools)
    app.settings.sources.twitter.agent_tools = False
    assert not {"twitter_search", "twitter_user"} & set(ToolBox(app, None).tools)
    app.settings.sources.twitter.agent_tools = True
    monkeypatch.delenv(twitter.ENV_AUTH)
    assert not {"twitter_search", "twitter_user"} & set(ToolBox(app, None).tools)


def test_search_tool_output_defaults_and_untrusted_header(app, tw):
    tw["out"] = {"ok": True, "data": [tweet(1, "XPeng delivered 41,256 vehicles in September", likes=49, views=12345),
                                      tweet(2, "retweeted thing", author="x2", verified=False, rt=True)]}
    out = ToolBox(app, None).call("twitter_search", {"query": "XPeng deliveries"})
    argv = tw["calls"][0]["argv"]
    assert argv[argv.index("--min-likes") + 1] == "3" and "--since" in argv  # 配置里的默认值
    assert "只作线索" in out and "数字与事实须另行核实" in out
    assert "@wallstengine✓" in out and "赞49" in out and "浏览12,345" in out and "https://x.com/wallstengine/status/1" in out
    assert "（转推）" in out and "@x2｜" in out and "10-01 13:11" in out  # 时间按北京时间显示


def test_empty_result_does_not_claim_nonexistence(app, tw):
    out = ToolBox(app, None).call("twitter_search", {"query": "nothing", "min_likes": 0, "days": 1})
    assert not out.startswith("错误") and "不代表没有这件事" in out


def test_auth_failure_trips_breaker_shared_across_processes(app, tw):
    tw["out"] = {"ok": False, "error": {"code": "api_error", "message": "Cookie expired or invalid (HTTP 401)"}}
    box = ToolBox(app, None)
    out = box.call("twitter_user", {"handle": "XPENG_Global"})
    assert out.startswith("错误") and "重新导出" in out
    assert app.db.kv_get("down:twitter")
    n = len(tw["calls"])
    again = ToolBox(app, None).call("twitter_search", {"query": "XPeng"})
    assert again.startswith("错误") and "web_search" in again and len(tw["calls"]) == n  # 熔断期间不再调用


def test_not_found_does_not_trip_breaker(app, tw):
    tw["out"] = {"ok": False, "error": {"code": "not_found", "message": "User @nobody not found"}}
    assert ToolBox(app, None).call("twitter_user", {"handle": "nobody"}).startswith("错误")
    assert not app.db.kv_get("down:twitter")


def test_max_results_capped_by_config(app, tw):
    app.settings.sources.twitter.max_results = 2
    tw["out"] = {"ok": True, "data": [tweet(i, f"post {i}") for i in range(5)]}
    out = ToolBox(app, None).call("twitter_user", {"handle": "XPENG_Global", "max_results": 40})
    assert "-n" in tw["calls"][0]["argv"] and tw["calls"][0]["argv"][tw["calls"][0]["argv"].index("-n") + 1] == "2"
    assert out.count("https://x.com/") == 2


def test_tracker_drops_twitter_when_web_disabled(app, tw):
    allow, _ = tracker._allowed_tools(app)
    assert "twitter_search" in allow and "twitter_user" in allow
    app.settings.tracker.web_search = False
    allow, why = tracker._allowed_tools(app)
    assert not {"web_search", "twitter_search", "twitter_user"} & set(allow) and why


# ---------------------------------------------------------------- 新闻源

from datetime import timedelta  # noqa: E402

from stockagent import triage, twitterfeed  # noqa: E402
from stockagent.utils import utcnow  # noqa: E402

_REAL_TRANSLATE = twitterfeed.translate_terms  # feed 夹具会把它换成空操作；翻译自己的测试用这份


@pytest.fixture
def feed(app, tw, monkeypatch):
    """开启新闻源，twitter.search / user_posts 换成按参数返回预设帖子的假函数（记录调用）。"""
    app.settings.sources.twitter.news_feed = True
    state = {"search": {}, "user": {}, "calls": [], "langs": []}

    def search(query, n=10, days=7, min_likes=0, sort="latest", from_user="", lang=""):
        state["calls"].append(("search", query, min_likes))
        state["langs"].append((query, lang))
        return [twitter._post(x) for x in state["search"].get(query, [])]

    def user_posts(handle, n=10):
        state["calls"].append(("user", handle))
        return [twitter._post(x) for x in state["user"].get(handle, [])]

    monkeypatch.setattr(twitter, "search", search)
    monkeypatch.setattr(twitter, "user_posts", user_posts)
    monkeypatch.setattr(twitterfeed, "translate_terms", lambda app, companies: [])  # 翻译另有测试；这里不调用模型
    return state


def recent(hours=2):
    return (utcnow() - timedelta(hours=hours)).isoformat()


def test_feed_default_on_but_needs_credentials_and_switch(app, tw, monkeypatch):
    c = app.watchlist.get("acme")
    assert app.settings.sources.twitter.news_feed and twitterfeed.enabled(app)  # 默认开启
    app.settings.sources.twitter.news_feed = False
    assert not twitterfeed.enabled(app) and twitterfeed.fetch(app, [c]) == []
    app.settings.sources.twitter.news_feed = True
    monkeypatch.delenv(twitter.ENV_AUTH)
    assert not twitterfeed.enabled(app) and twitterfeed.fetch(app, [c]) == []  # 没有凭证：不运行


def test_name_queries_are_english_first_with_chinese_name_separate(app):
    from stockagent.config import Company

    xp = Company.model_validate({"id": "xp", "name": "小鹏汽车", "name_en": "XPeng", "aliases": ["XPENG Inc"],
                                 "listings": [{"market": "HK", "code": "9868"}, {"market": "US", "code": "XPEV", "exchange": "NYSE"}]})
    cfg = app.settings.sources.twitter
    assert twitterfeed.english_terms(xp, 5) == ["$XPEV", "XPeng", "XPENG Inc"] and twitterfeed.english_terms(xp, 2) == ["$XPEV", "XPeng"]
    dup = Company.model_validate({"id": "xp2", "name": "小鹏汽车", "name_en": "XPeng", "aliases": ["XPEV", "xpeng"],
                                  "listings": [{"market": "US", "code": "XPEV", "exchange": "NYSE"}]})
    assert twitterfeed.english_terms(dup, 5) == ["$XPEV", "XPeng"]  # 纯代号别名与 $代号重复、大小写不同的同名只留一个
    # 英文叫法并成一条 OR 检索式（括号让后面附加的过滤条件作用于整体，带空格的词加引号），只看英文；中文公司名单独检索，只看中文
    assert twitterfeed.name_queries(xp, cfg) == [('($XPEV OR XPeng OR "XPENG Inc")', "en"), ("小鹏汽车", "zh")]
    cfg.feed_chinese_name = False
    assert twitterfeed.name_queries(xp, cfg) == [('($XPEV OR XPeng OR "XPENG Inc")', "en")]
    cfg.feed_chinese_name = True
    assert twitterfeed.name_queries(app.watchlist.get("acme"), cfg) == [("Acme", "en"), ("测试公司", "zh")]  # 太短（<4）的英文别名不用
    latin = Company.model_validate({"id": "fh", "name": "FIT HON TENG", "listings": [{"market": "HK", "code": "6088"}]})
    assert twitterfeed.name_queries(latin, cfg) == [('"FIT HON TENG"', "en")]  # 公司名本身是英文：不再重复一条中文检索
    cn = Company.model_validate({"id": "cn", "name": "贝塔科技", "listings": [{"market": "A", "code": "300002"}]})
    assert twitterfeed.name_queries(cn, cfg) == [("贝塔科技", "zh")]  # 没有英文叫法：只有中文


def test_feed_registered_accounts_are_confirmed_and_not_keyword_matched_search_hits_are_not(app, feed):
    c = app.watchlist.get("acme")
    c.twitter_official, c.twitter_accounts = ["AcmeOfficial"], ["newswire"]
    feed["user"]["AcmeOfficial"] = [tweet(1, "New product launch event next week!", author="AcmeOfficial", likes=40, at=recent())]
    feed["user"]["newswire"] = [tweet(2, "Acme wins big order from customer", author="newswire", likes=30, at=recent()),
                                tweet(3, "Totally unrelated market chatter today", author="newswire", likes=80, at=recent())]  # 登记账号：不做关键词匹配
    feed["search"]["Acme"] = [tweet(4, "Acme guidance raised, per people familiar", author="rando", likes=25, at=recent()),
                              tweet(2, "Acme wins big order from customer", author="newswire", likes=30, at=recent()),  # 与账号来源重复
                              tweet(5, "Acme to the moon", author="shill", likes=3, at=recent()),                       # 低于点赞门槛
                              tweet(6, "Acme old news", author="old", likes=50, at=recent(24 * 30)),                    # 超出回溯期
                              tweet(7, "RT Acme something", author="rt", likes=50, rt=True, at=recent()),               # 转推
                              tweet(8, "Search noise that mentions nothing relevant", author="noise", likes=60, at=recent())]  # 检索结果没提到公司：丢弃
    msgs = twitterfeed.fetch(app, [c])
    assert "读取账号 2 个、检索 2 次、跟踪词 0 个，收录 4 条（新增 4）" in msgs[0]
    rows = {r["url"].rsplit("/", 1)[1]: r for r in app.db._all("SELECT * FROM items WHERE source='twitter'")}
    assert set(rows) == {"1", "2", "3", "4"}  # 低赞、旧帖、转推、检索里没提到公司的都没收
    assert rows["1"]["snippet"].startswith(twitterfeed.OFFICIAL_TAG + " New product launch") and twitterfeed.TAG not in rows["1"]["snippet"]  # 官方号：按已证实的消息
    for i in ("2", "3"):  # 登记的媒体 / 快讯 / 关键人物账号：按已证实的消息，所有帖子交给分级判断相关性（无关的那条也进来）
        assert rows[i]["snippet"].startswith(twitterfeed.ACCOUNT_TAG + " ") and twitterfeed.TAG not in rows[i]["snippet"] and rows[i]["source_name"] == "@newswire"
    assert "赞30" in rows["2"]["snippet"] and rows["3"]["snippet"].startswith("[登记账号] Totally unrelated market chatter")
    assert rows["4"]["snippet"].startswith(twitterfeed.TAG + " ") and rows["4"]["source_name"] == "@rando"  # 检索词找到的任意作者：未经证实
    assert rows["4"]["url"] == "https://x.com/rando/status/4" and rows["4"]["kind"] == "news"
    assert ("search", "Acme", app.settings.sources.twitter.feed_min_likes) in feed["calls"]
    again = twitterfeed.fetch(app, [c])  # 第二次：同样的帖子不重复新增
    assert "收录 4 条（新增 0）" in again[0]


@pytest.fixture
def router(app, monkeypatch):
    """分流用的假模型：按帖子正文里的关键词决定分到哪些公司（state["rules"] = [(关键词, [公司 id])]），记录每次调用。"""
    import re
    from types import SimpleNamespace
    from stockagent.config import Company
    from stockagent.llm import LLMError

    app.watchlist.companies.append(Company.model_validate({"id": "beta", "name": "贝塔科技", "name_en": "Betatech", "sector": "测试行业",
                                                           "listings": [{"market": "A", "code": "300002"}]}))
    app.settings.sources.twitter.shared_accounts = ["wallstengine"]
    state = {"calls": [], "rules": [("Acme", ["acme"]), ("Betatech", ["beta"])], "fail": False}

    def complete(task, system, user, *, schema=None, validate=None, max_tokens=16000, profile=None):
        state["calls"].append({"task": task, "system": system, "user": user, "schema": schema})
        if state["fail"]:
            raise LLMError("boom")
        items = []
        for sid, text in re.findall(r"id=([0-9a-f]{12}) \| @\S+ \| [^\n]*\n    (.*)", user):
            cs = [c for kw, cids in state["rules"] if kw in text for c in cids]
            items.append({"id": sid, "companies": cs})
        return SimpleNamespace(data=validate({"items": items}))

    monkeypatch.setattr(app.llm, "complete", complete)
    return state


def shared_posts(feed):
    feed["user"]["wallstengine"] = [tweet(1, "Acme wins big order from customer", author="wallstengine", likes=30, at=recent()),
                                    tweet(2, "Betatech raises guidance sharply for the year", author="wallstengine", likes=30, at=recent()),
                                    tweet(3, "Oil prices drift lower on Friday", author="wallstengine", likes=90, at=recent()),
                                    tweet(4, "Acme again", author="wallstengine", likes=2, at=recent())]  # 低于点赞门槛：不读入分流


def twitter_items(app):
    return sorted((r["company_id"], r["url"].rsplit("/", 1)[1]) for r in app.db._all("SELECT company_id, url FROM items WHERE source='twitter'"))


def test_shared_posts_are_routed_in_one_call_not_keyword_matched(app, feed, router):
    """共享账号：每个账号只读一次；新帖子连同全部自选股简介一次调用批量分流，只给分到的公司写入。"""
    acme = app.watchlist.get("acme")
    shared_posts(feed)
    msgs = twitterfeed.fetch(app, [acme])  # 本次名单只有 acme，但 beta 也在自选股里：分到它的帖子照样写入
    assert [c for c in feed["calls"] if c[0] == "user"] == [("user", "wallstengine")]
    assert len(router["calls"]) == 1 and router["calls"][0]["task"] == "triage"  # 3 条帖子、2 家公司：只一次模型调用
    call = router["calls"][0]
    assert "| @wallstengine |" in call["user"] and call["user"].count("id=") == 3
    digest = call["system"][1]
    assert "acme｜测试公司" in digest and "beta｜贝塔科技" in digest and "测试行业" in digest  # 全部自选股的简介
    assert call["schema"]["properties"]["items"]["items"]["properties"]["companies"]["items"]["enum"] == ["acme", "beta"]
    assert twitter_items(app) == [("acme", "1"), ("beta", "2")]  # 油价快讯与所有自选股无关：不写入；也没有关键词匹配
    rows = app.db._all("SELECT snippet FROM items WHERE source='twitter'")
    assert all(r["snippet"].startswith(twitterfeed.SHARED_TAG + " ") and twitterfeed.TAG not in r["snippet"] for r in rows)  # 共享账号：按已证实的消息，不带「未经证实」
    assert "读取 1 个，3 条符合条件，新帖子 3 条，分流到公司 2 条（新增 2）" in msgs[0]
    assert len(app.db._all("SELECT 1 FROM twitter_routed")) == 3  # 无关的那条也记为已分流
    twitterfeed.fetch(app, [acme])  # 再运行：已分流的不再送模型，也不重复写入
    assert len(router["calls"]) == 1 and twitter_items(app) == [("acme", "1"), ("beta", "2")]
    row = app.db._one("SELECT * FROM items WHERE source='twitter' AND company_id='acme'")
    assert twitterfeed.SHARED_TAG in triage._describe(row, 1)  # 分级看得到这条标注


def test_shared_routing_batches_by_triage_batch_size(app, feed, router):
    app.settings.report.triage_batch_size = 2
    shared_posts(feed)
    twitterfeed.fetch(app, [app.watchlist.get("acme")])
    assert len(router["calls"]) == 2  # 3 条帖子，每批 2 条


def test_shared_routing_failure_retries_next_time_and_unavailable_model_skips(app, feed, router, monkeypatch):
    acme = app.watchlist.get("acme")
    shared_posts(feed)
    router["fail"] = True
    msgs = twitterfeed.fetch(app, [acme])
    assert any("共享账号分流失败" in m for m in msgs) and not twitter_items(app)
    assert not app.db._all("SELECT 1 FROM twitter_routed")  # 失败不记为已分流：下次重试
    router["fail"] = False
    monkeypatch.setattr(app.llm, "unavailable_reason", lambda task: "没有可用的模型")
    msgs = twitterfeed.fetch(app, [acme])
    assert any("跳过共享账号分流：没有可用的模型" in m for m in msgs) and not twitter_items(app) and len(router["calls"]) == 1
    monkeypatch.setattr(app.llm, "unavailable_reason", lambda task: None)
    twitterfeed.fetch(app, [acme])
    assert twitter_items(app) == [("acme", "1"), ("beta", "2")]


def test_shared_routing_caps_companies_per_post_and_drops_unknown_ids(app, feed, router):
    app.settings.sources.twitter.shared_route_max = 1
    router["rules"] = [("Acme", ["nonexistent", "beta", "acme"])]  # 未知 id 丢弃，其余按顺序取前 1 家
    feed["user"]["wallstengine"] = [tweet(1, "Acme wins big order from customer", author="wallstengine", likes=30, at=recent())]
    twitterfeed.fetch(app, [app.watchlist.get("acme")])
    assert twitter_items(app) == [("beta", "1")]


def test_route_validator_filters_ids():
    out = twitterfeed._validate_route({"items": [{"id": "x", "companies": ["a", "zzz", "a", "b"]}, {"id": "y"}]}, {"a", "b"})
    assert out == {"items": [{"id": "x", "companies": ["a", "b"]}, {"id": "y", "companies": []}]}


def test_triage_rules_for_each_trust_level():
    rules = triage.TRIAGE_SYSTEM
    r12 = rules.split("12. ")[1].split("13. ")[0]
    r13 = rules.split("13. ")[1].split("14. ")[0]
    r14 = rules.split("14. ")[1].split("15. ")[0]
    r15 = rules.split("15. ")[1].split("字段说明")[0]
    assert twitterfeed.TAG in r12 and "只当线索" in r12 and "未经证实" in r12
    assert twitterfeed.SHARED_TAG in r13 and "已证实的新闻" in r13 and "画像" in r13 and "relevant 为 false" in r13
    assert twitterfeed.OFFICIAL_TAG in r14 and "一手披露" in r14 and "营销图文" in r14
    assert twitterfeed.ACCOUNT_TAG in r15 and "已证实的新闻" in r15 and "画像" in r15 and "relevant 为 false" in r15 and "关键人物" in r15
    assert "媒体 / 关键人物" not in r12  # 登记账号不再是「未经证实」


# ---------------------------------------------------------------- 跟踪词与调用预算

def set_plan(app, cid, keywords):
    import json
    app.db._exec("INSERT OR REPLACE INTO plans(company_id, dossier_id, plan_json, updated_at) VALUES(?, NULL, ?, ?)",
                 (cid, json.dumps({"news_keywords": keywords, "related_tickers": []}, ensure_ascii=False), "2026-10-01T00:00:00+00:00"))


def test_plan_queries_english_first_then_chinese_original(app):
    import json

    acme = app.watchlist.get("acme")
    set_plan(app, "acme", ["A词", " A词 ", "B词", "C词", "D词"])
    assert twitterfeed.plan_keywords(app, acme, 3) == ["A词", "B词", "C词"]  # 去重、取前 3 个
    app.db.kv_set(twitterfeed.TERMS_KEY, json.dumps({"A词": "alpha topic", "B词": "", "C词": "gamma topic", "D词": "delta topic"}, ensure_ascii=False))
    # 英文名额：按顺序取有译文的（B 不宜翻译，跳过）；再补中文原词名额
    assert twitterfeed.plan_queries(app, acme, 2, 1) == [("alpha topic", "A词", "en"), ("gamma topic", "C词", "en"), ("A词", "A词", "zh")]
    assert twitterfeed.plan_queries(app, acme, 0, 2) == [("A词", "A词", "zh"), ("B词", "B词", "zh")]
    assert twitterfeed.plan_queries(app, acme, 3, 0) == [("alpha topic", "A词", "en"), ("gamma topic", "C词", "en"), ("delta topic", "D词", "en")]
    app.db.kv_delete(twitterfeed.TERMS_KEY)  # 还没有任何译文（比如模型不可用）：2 个英文名额先用中文原词顶上，中文名额再取下一个不重复的词
    assert twitterfeed.plan_queries(app, acme, 2, 1) == [("A词", "A词", "zh"), ("B词", "B词", "zh"), ("C词", "C词", "zh")]


def test_feed_queries_are_english_first_tag_the_original_keyword_and_filter_by_language(app, feed):
    import json

    c = app.watchlist.get("acme")
    set_plan(app, "acme", ["R32长协价", "制冷剂配额", "含氟高分子"])
    app.db.kv_set(twitterfeed.TERMS_KEY, json.dumps({"R32长协价": "R32 contract price", "制冷剂配额": "refrigerant quota", "含氟高分子": "fluoropolymers"}))
    cfg = app.settings.sources.twitter
    cfg.feed_plan_keywords, cfg.feed_plan_keywords_zh = 2, 1
    zh = tweet(3, "R32长协价本月上调", author="zh_user", likes=15, at=recent())
    zh["lang"] = "zh"
    feed["search"]["R32 contract price"] = [tweet(1, "R32 refrigerant contract price up 500 yuan per ton", author="chem_analyst", likes=30, at=recent())]
    feed["search"]["refrigerant quota"] = [tweet(2, "Draft refrigerant quota plan released", author="chem_analyst", likes=20, at=recent())]
    feed["search"]["R32长协价"] = [zh]
    msgs = twitterfeed.fetch(app, [c])
    # 英文优先：英文叫法、中文公司名、2 个跟踪词译文（英文）、1 个中文原词（中文）；每次检索都按语种过滤
    assert feed["langs"] == [("Acme", "en"), ("测试公司", "zh"), ("R32 contract price", "en"), ("refrigerant quota", "en"), ("R32长协价", "zh")]
    assert "检索 2 次、跟踪词 3 个，收录 3 条" in msgs[0]
    rows = app.db._all("SELECT snippet FROM items WHERE source='twitter' ORDER BY url")
    assert rows[0]["snippet"].startswith("[推特·未经证实][跟踪词：R32长协价] R32 refrigerant contract price")  # 标注的是原中文跟踪词
    assert "[跟踪词：制冷剂配额]" in rows[1]["snippet"] and "[跟踪词：R32长协价] R32长协价本月上调" in rows[2]["snippet"]
    # 账号发现也用这些检索结果：同一作者出现 2 次 → 候选
    assert [x["handle"] for x in twitterfeed.candidates(app)] == ["chem_analyst"]
    cfg.feed_languages = ["en"]  # 不收录中文：不再跑中文检索
    feed["langs"].clear()
    twitterfeed.fetch(app, [c])
    assert feed["langs"] == [("Acme", "en"), ("R32 contract price", "en"), ("refrigerant quota", "en")]
    cfg.feed_languages = ["en", "zh"]
    cfg.feed_plan_keywords = cfg.feed_plan_keywords_zh = 0
    feed["langs"].clear()
    twitterfeed.fetch(app, [c])
    assert feed["langs"] == [("Acme", "en"), ("测试公司", "zh")]  # 0 = 不用跟踪词


@pytest.fixture
def translator(app, monkeypatch):
    """翻译用的假模型：按预设词典返回英文，记录每次调用的提示词。"""
    import re
    from types import SimpleNamespace
    from stockagent.llm import LLMError

    state = {"calls": [], "dict": {}, "fail": False}

    def complete(task, system, user, *, schema=None, validate=None, **kw):
        state["calls"].append({"task": task, "system": system, "user": user})
        if state["fail"]:
            raise LLMError("boom")
        items = [{"id": sid, "en": state["dict"].get(zh.strip(), "")} for sid, zh in re.findall(r"id=([0-9a-f]{12}) \| 公司：[^|]*\| 中文词：(.*)", user)]
        return SimpleNamespace(data={"items": items})

    monkeypatch.setattr(app.llm, "complete", complete)
    return state


def test_translate_terms_batches_caches_and_validates(app, translator):
    acme = app.watchlist.get("acme")
    set_plan(app, "acme", ["VLCC 运价 TD3C", "霍尔木兹海峡通航", "地方政策文号", "南向资金"])
    app.settings.sources.twitter.feed_plan_keywords = 2  # 每家公司译前 2 + 3 个（留余量）；这里共 4 个
    translator["dict"] = {"VLCC 运价 TD3C": "VLCC rates TD3C", "霍尔木兹海峡通航": '"Strait of Hormuz" shipping', "地方政策文号": "",
                          "南向资金": "southbound 资金"}  # 空串 = 不宜翻译；含汉字的译文不合格；引号去掉
    msgs = _REAL_TRANSLATE(app, [acme])
    assert len(translator["calls"]) == 1 and translator["calls"][0]["task"] == "triage"
    assert "公司：测试公司（—）| 中文词：VLCC 运价 TD3C" in translator["calls"][0]["user"]  # 公司与行业作语境
    assert "新译 4 个（可用 2 个" in msgs[0]
    assert twitterfeed.cached_terms(app) == {"VLCC 运价 TD3C": "VLCC rates TD3C", "霍尔木兹海峡通航": "Strait of Hormuz shipping", "地方政策文号": "", "南向资金": ""}
    assert _REAL_TRANSLATE(app, [acme]) == [] and len(translator["calls"]) == 1  # 已缓存（含空串）：不再调用模型
    set_plan(app, "acme", ["VLCC 运价 TD3C", "新词"])  # 计划更新出了新词：只译新词
    translator["dict"]["新词"] = "new topic"
    _REAL_TRANSLATE(app, [acme])
    assert len(translator["calls"]) == 2 and "中文词：新词" in translator["calls"][1]["user"] and "VLCC" not in translator["calls"][1]["user"]
    assert twitterfeed.cached_terms(app)["新词"] == "new topic"


def test_translate_terms_splits_into_batches_of_40(app, translator):
    set_plan(app, "acme", [f"词{i}" for i in range(45)])
    app.settings.sources.twitter.feed_plan_keywords = 42  # 42 + 3 = 45 个
    translator["dict"] = {f"词{i}": f"term {i}" for i in range(45)}
    _REAL_TRANSLATE(app, [app.watchlist.get("acme")])
    assert len(translator["calls"]) == 2 and len(twitterfeed.cached_terms(app)) == 45


def test_translate_terms_failure_and_unavailable_fall_back_to_chinese(app, translator, monkeypatch):
    acme = app.watchlist.get("acme")
    set_plan(app, "acme", ["R32长协价", "制冷剂配额"])
    translator["fail"] = True
    msgs = _REAL_TRANSLATE(app, [acme])
    assert "跟踪词翻译失败" in msgs[0] and twitterfeed.cached_terms(app) == {}  # 失败不缓存：下次重试
    assert twitterfeed.plan_queries(app, acme, 2, 1) == [("R32长协价", "R32长协价", "zh"), ("制冷剂配额", "制冷剂配额", "zh")]  # 先用中文原词顶上
    monkeypatch.setattr(app.llm, "unavailable_reason", lambda task: "没有可用的模型")
    assert "跳过跟踪词翻译：没有可用的模型" in _REAL_TRANSLATE(app, [acme])[0] and len(translator["calls"]) == 1
    app.settings.sources.twitter.feed_plan_keywords = 0  # 不用英文跟踪词：不翻译
    assert _REAL_TRANSLATE(app, [acme]) == []


def test_call_budget_rotates_through_companies(app, feed):
    from stockagent.config import Company

    for cid, name, en in (("beta", "贝塔科技", "Betatech"), ("gamma", "伽马科技", "Gammatech")):
        app.watchlist.companies.append(Company.model_validate({"id": cid, "name": name, "name_en": en, "listings": [{"market": "A", "code": "300003" if cid == "gamma" else "300002"}]}))
    cos = app.watchlist.companies  # acme, beta, gamma；每家 2 个任务（英文名检索 + 1 个跟踪词）
    app.settings.sources.twitter.feed_queries_per_company = 1
    app.settings.sources.twitter.feed_chinese_name = False
    app.settings.sources.twitter.feed_plan_keywords = 1
    app.settings.sources.twitter.feed_plan_keywords_zh = 0
    for c in cos:
        set_plan(app, c.id, [f"kw-{c.id}"])
    app.settings.sources.twitter.feed_max_calls = 3  # 一次只够 1 家（2 次），再加 1 家就超了

    def run():
        feed["calls"].clear()
        msgs = twitterfeed.fetch(app, cos)
        return [x[1] for x in feed["calls"]], msgs

    q, msgs = run()
    assert q == ["Acme", "kw-acme"] and any("还有 2 家公司留到下次运行，从「贝塔科技」继续" in m for m in msgs)
    assert app.db.kv_get("twitter_cursor") == "beta"
    q, _ = run()
    assert q == ["Betatech", "kw-beta"] and app.db.kv_get("twitter_cursor") == "gamma"  # 从上次停下的公司继续
    q, _ = run()
    assert q == ["Gammatech", "kw-gamma"] and app.db.kv_get("twitter_cursor") == "acme"  # 回绕
    app.settings.sources.twitter.feed_max_calls = 0  # 不限：一次跑完，游标清除
    q, msgs = run()
    assert q == ["Acme", "kw-acme", "Betatech", "kw-beta", "Gammatech", "kw-gamma"] and app.db.kv_get("twitter_cursor") is None
    assert not any("留到下次运行" in m for m in msgs)
    app.db.kv_set("twitter_cursor", "beta")
    feed["calls"].clear()
    twitterfeed.fetch(app, [cos[2]])  # 只跑部分公司（如 stock fetch 某一家）：不读也不改轮转游标
    assert app.db.kv_get("twitter_cursor") == "beta" and [x[1] for x in feed["calls"]] == ["Gammatech", "kw-gamma"]
    app.db.kv_delete("twitter_cursor")
    app.settings.sources.twitter.feed_max_calls = 1  # 预算小于一家公司的任务数：这家仍然完整处理，不会死循环
    q, _ = run()
    assert q == ["Acme", "kw-acme"]


def test_shared_account_auth_failure_stops_everything(app, feed, monkeypatch):
    app.settings.sources.twitter.shared_accounts = ["wallstengine"]

    def boom(*a, **k):
        raise twitter.TwitterError("rate_limit", "429")

    monkeypatch.setattr(twitter, "user_posts", boom)
    msgs = twitterfeed.fetch(app, [app.watchlist.get("acme")])
    assert len(msgs) == 1 and msgs[0].startswith("✗") and app.db.kv_get("down:twitter")
    assert not [c for c in feed["calls"] if c[0] == "search"]  # 不再继续逐公司检索


def test_feed_language_filter_and_html_entities(app, feed):
    c = app.watchlist.get("acme")
    c.twitter_official = ["AcmeOfficial"]
    es = tweet(1, "Acme lanza un nuevo modelo hoy mismo", author="AcmeOfficial", likes=40, at=recent())
    es["lang"] = "es"
    zh = tweet(2, "Acme 宣布新订单 &amp; 扩产", author="AcmeOfficial", likes=40, at=recent())
    zh["lang"] = "zh"
    feed["user"]["AcmeOfficial"] = [es, zh]
    twitterfeed.fetch(app, [c])
    rows = app.db._all("SELECT title FROM items WHERE source='twitter'")
    assert [r["title"] for r in rows] == ["Acme 宣布新订单 & 扩产"]  # 西语帖子被过滤；&amp; 还原
    app.settings.sources.twitter.feed_languages = []  # 留空 = 不限
    twitterfeed.fetch(app, [c])
    assert len(app.db._all("SELECT 1 FROM items WHERE source='twitter'")) == 2


def test_feed_auth_failure_trips_breaker_and_stops(app, feed, monkeypatch):
    c = app.watchlist.get("acme")
    c.twitter_official = ["AcmeOfficial"]

    def boom(*a, **k):
        raise twitter.TwitterError("auth", "Cookie expired")

    monkeypatch.setattr(twitter, "user_posts", boom)
    msgs = twitterfeed.fetch(app, [c])
    assert any("Cookie expired" in m and m.startswith("✗") for m in msgs) and app.db.kv_get("down:twitter")
    assert twitterfeed.fetch(app, [c]) == ["✗ 推特暂不可用（凭证失效或被限流，稍后自动恢复）"]


def test_feed_not_found_account_only_skips_that_account(app, feed, monkeypatch):
    c = app.watchlist.get("acme")
    c.twitter_official = ["ghost"]
    real = twitter.user_posts

    def nf(handle, n=10):
        raise twitter.TwitterError("not_found", "no such user")

    monkeypatch.setattr(twitter, "user_posts", nf)
    feed["search"]["Acme"] = [tweet(9, "Acme announces results date", author="n", likes=50, at=recent())]
    msgs = twitterfeed.fetch(app, [c])
    assert "收录 1 条" in msgs[0] and not app.db.kv_get("down:twitter")


def test_triage_caps_only_unconfirmed_twitter_materiality(app, feed):
    """未经证实的推特帖子（公司名检索 / 跟踪词命中的任意作者）重要性由代码封顶；共享账号、官方号、登记账号按已证实的消息，不封顶。"""
    from stockagent.app import _item
    from stockagent.utils import sha1

    c = app.watchlist.get("acme")
    app.settings.sources.twitter.feed_max_materiality = 1
    tags = (("unconfirmed", twitterfeed.TAG), ("shared", twitterfeed.SHARED_TAG), ("official", twitterfeed.OFFICIAL_TAG), ("account", twitterfeed.ACCOUNT_TAG))
    for kind, tag in tags:
        for i in range(14):  # 模拟模型的重要性按 id 散列取值：14 条里总有 >1 的
            h = sha1(kind, i)
            x = {"source_id": f"{kind}{i}", "title": f"{h[:6]}公司{h[6:12]}发布{h[12:18]}合同{h[18:24]}", "published_at": utcnow() - timedelta(minutes=i),
                 "url": f"https://x.com/a/status/{kind}{i}", "snippet": f"{tag} {h}"}
            app.db.add_item(_item(c, None, "news", "twitter", x, "@a"))
    triage.run(app, [c])
    mat = {k: [r["materiality"] for r in app.db._all("SELECT materiality FROM items WHERE source='twitter' AND snippet LIKE ?", (t + "%",))] for k, t in tags}
    assert all(len(v) == 14 for v in mat.values())
    assert set(mat["unconfirmed"]) == {1}  # 封顶
    assert all(max(mat[k]) > 1 for k in ("shared", "official", "account"))  # 不封顶
    row = app.db._one("SELECT * FROM items WHERE source='twitter' AND snippet LIKE ?", (twitterfeed.TAG + "%",))
    assert twitterfeed.TAG in triage._describe(row, 1)


def test_alert_fetch_skips_twitter(app, feed, monkeypatch):
    """盘中 stock alert 不抓推特（fetch(twitter=False)）。"""
    from stockagent import app as appmod

    called = []
    monkeypatch.setattr(twitterfeed, "fetch", lambda a, cs: called.append(1) or [])
    appmod.fetch(app, [app.watchlist.get("acme")], prices=False, filings=False, market=False, ima=False, twitter=False)
    assert not called
    appmod.fetch(app, [app.watchlist.get("acme")], prices=False, filings=False, market=False, ima=False)
    assert called == [1]


# ---------------------------------------------------------------- 账号发现

def test_discovery_records_search_authors_and_ranks_candidates(app, feed):
    c = app.watchlist.get("acme")
    c.twitter_accounts = ["known"]
    app.settings.sources.twitter.shared_accounts = ["sharedone"]
    feed["user"]["known"] = [tweet(20, "Acme news from known account", author="known", likes=30, at=recent())]
    feed["search"]["Acme"] = [
        tweet(1, "Acme wins order", author="analystA", likes=50, at=recent()),
        tweet(2, "Acme margin outlook", author="analystA", likes=70, at=recent(5)),
        tweet(3, "Acme again, one more take", author="oneoff", likes=40, at=recent()),         # 只出现一次：低于最少次数
        tweet(4, "Acme from known", author="known", likes=60, at=recent()),                    # 已登记：不列
        tweet(5, "Acme from shared", author="SharedOne", likes=60, at=recent()),               # 已是共享账号（大小写不敏感）：不列
        tweet(6, "Unrelated chatter", author="noise", likes=90, at=recent()),                  # 没提到公司：不收录也不记
    ]
    twitterfeed.fetch(app, [c])
    rows = app.db._all("SELECT handle FROM twitter_authors")
    assert {r["handle"] for r in rows} == {"analystA", "oneoff", "known", "SharedOne"}  # 账号来源的帖子（id 20）不记；无关帖子不记
    cands = twitterfeed.candidates(app)
    assert [x["handle"] for x in cands] == ["analystA"]
    a = cands[0]
    assert a["posts"] == 2 and a["likes"] == 120 and a["companies"] == {"acme": "测试公司"} and a["verified"]
    assert a["url"] == "https://x.com/analystA/status/2" and a["sample"] == "Acme margin outlook"  # 示例取赞数最高的一条
    assert a["suggest"] == "accounts:acme"  # 只涉及一家公司：预选该公司的媒体 / 关键人物账号
    twitterfeed.fetch(app, [c])  # 重复运行：同一帖子不重复计数
    assert twitterfeed.candidates(app)[0]["posts"] == 2
    app.settings.sources.twitter.discover_min_posts = 1
    assert [x["handle"] for x in twitterfeed.candidates(app)] == ["analystA", "oneoff"]
    app.db.twitter_dismiss("ANALYSTa")
    assert [x["handle"] for x in twitterfeed.candidates(app)] == ["oneoff"]


def test_discovery_can_be_switched_off(app, feed):
    app.settings.sources.twitter.discover = False
    feed["search"]["Acme"] = [tweet(1, "Acme wins order", author="analystA", likes=50, at=recent())]
    twitterfeed.fetch(app, [app.watchlist.get("acme")])
    assert not app.db._all("SELECT 1 FROM twitter_authors")


def test_discovery_suggests_official_for_company_named_handles_and_shared_for_multi_company(app, feed):
    from datetime import timedelta
    from stockagent.config import Company

    app.watchlist.companies.append(Company.model_validate({"id": "beta", "name": "贝塔科技", "name_en": "Betatech", "listings": [{"market": "A", "code": "300002"}]}))

    def add(cid, handle, name, i):
        app.db.twitter_author_add(cid, {"id": f"{handle}{i}", "author": handle, "name": name, "verified": True, "likes": 10, "at": utcnow() - timedelta(hours=i),
                                         "text": "x", "url": f"https://x.com/{handle}/status/{i}"})

    for i in range(2):
        add("acme", "Acme_Official", "Acme Inc", i)       # 账号名带公司英文名 → 官方号
        add("acme", "quiet_analyst", "someone", i + 5)    # 名字里没有公司名、只涉及一家 → 媒体号
        add("acme", "newswire", "News Wire", i + 10)      # 涉及多家公司 → 共享账号
        add("beta", "newswire", "News Wire", i + 20)
    sug = {x["handle"]: x["suggest"] for x in twitterfeed.candidates(app)}
    assert sug["Acme_Official"] == "official:acme" and sug["newswire"] == "shared"
    assert sug["quiet_analyst"] == "accounts:acme"


def test_scope_tracked_limits_feed_and_routing_to_tracked_companies(app, feed, router):
    cfg = app.settings.sources.twitter
    cfg.feed_scope = "tracked"
    shared_posts(feed)
    msgs = twitterfeed.fetch(app, app.watchlist.companies)
    assert len(msgs) == 1 and "没有在追踪的公司" in msgs[0] and not router["calls"] and not feed["calls"]  # 没有任何追踪中的公司：什么都不抓
    app.db.set_tracker("acme", enabled=1)
    app.db.set_tracker("beta", enabled=0)  # 已停止追踪
    twitterfeed.fetch(app, app.watchlist.companies)
    digest = router["calls"][0]["system"][1]
    assert "acme｜" in digest and "beta｜" not in digest  # 分流只面向追踪中的公司
    assert twitter_items(app) == [("acme", "1")]  # 分给 beta 的帖子被丢弃
    assert {c[1] for c in feed["calls"] if c[0] == "search"} == {"Acme", "测试公司"}  # 只检索追踪中的公司
    cfg.feed_scope = "all"
    feed["calls"].clear()
    twitterfeed.fetch(app, app.watchlist.companies)
    assert {c[1] for c in feed["calls"] if c[0] == "search"} == {"Acme", "测试公司", "Betatech", "贝塔科技"}  # 范围改回全部：beta 也检索（英文叫法 + 中文名）


def test_translate_prompt_asks_for_short_keyword_queries():
    assert "2–3 个关键词" in twitterfeed.TRANSLATE_SYSTEM and "且" in twitterfeed.TRANSLATE_SYSTEM and "泛词" in twitterfeed.TRANSLATE_SYSTEM


# ---------------------------------------------------------------- 登记账号：研究 agent 也按已证实的消息

def test_account_roles_and_accounts_line(app):
    from stockagent.config import Company

    app.settings.sources.twitter.shared_accounts = ["wallstengine", "AcmeOfficial"]
    c = app.watchlist.get("acme")
    c.twitter_official, c.twitter_accounts = ["AcmeOfficial"], ["newswire", "Wallstengine"]
    roles = twitterfeed.account_roles(app, c)
    assert roles["acmeofficial"] == "官方号"  # 官方号 > 登记账号 > 全局快讯号
    assert roles["wallstengine"] == "媒体 / 快讯 / 关键人物账号" and roles["newswire"] == "媒体 / 快讯 / 关键人物账号"
    assert twitterfeed.account_roles(app, None) == {"wallstengine": "全局快讯号", "acmeofficial": "全局快讯号"}  # 没有公司（选股器）：只有全局快讯号
    line = twitterfeed.accounts_line(app, c)
    assert "官方号 @AcmeOfficial" in line and "媒体 / 快讯 / 关键人物账号 @newswire、@Wallstengine" in line and "全局快讯号 @wallstengine、@AcmeOfficial" in line
    assert "已证实的消息" in line and "twitter_user" in line
    bare = Company.model_validate({"id": "bare", "name": "裸公司", "listings": [{"market": "A", "code": "300009"}]})
    app.settings.sources.twitter.shared_accounts = []
    assert twitterfeed.accounts_line(app, bare) == ""  # 什么账号都没登记：不加这句


def test_tool_output_labels_registered_accounts_as_trusted(app, tw):
    app.settings.sources.twitter.shared_accounts = ["wallstengine"]
    c = app.watchlist.get("acme")
    c.twitter_official, c.twitter_accounts = ["AcmeOfficial"], ["newswire"]
    tw["out"] = {"ok": True, "data": [tweet(1, "Acme wins an order", author="AcmeOfficial"), tweet(2, "Acme margin outlook", author="NewsWire", verified=False),
                                      tweet(3, "Acme rumor", author="wallstengine"), tweet(4, "Acme to the moon", author="shill", verified=False)]}
    out = ToolBox(app, c).call("twitter_search", {"query": "Acme"})
    assert "@AcmeOfficial✓【可信账号·官方号】" in out and "@NewsWire【可信账号·媒体 / 快讯 / 关键人物账号】" in out and "@wallstengine✓【可信账号·全局快讯号】" in out
    assert "@shill【" not in out  # 其他账号：只作线索，不加标注
    assert "标【可信账号】的是使用者登记的账号，按已证实的消息" in out and "其余推文只作线索" in out
    # 读登记账号的动态：说明按已证实的消息；读别的账号：只作线索
    assert "使用者登记的官方号，按已证实的消息" in ToolBox(app, c).call("twitter_user", {"handle": "@acmeofficial"})
    assert "不是登记的账号，推文只作线索" in ToolBox(app, c).call("twitter_user", {"handle": "shill"})
    # 没有公司（选股器）：只认全局快讯号
    assert "【可信账号·全局快讯号】" in ToolBox(app, None).call("twitter_search", {"query": "Acme"}) and "官方号】" not in ToolBox(app, None).call("twitter_search", {"query": "Acme"})
    assert "【可信账号】" in twitter.HINT and "按已证实的消息" in twitter.HINT


def test_dossier_and_tracker_prompts_list_trusted_accounts(app, tw, monkeypatch):
    from types import SimpleNamespace
    from stockagent import dossier

    c = app.watchlist.get("acme")
    c.twitter_official = ["AcmeOfficial"]
    seen = []

    def run_agent(task, system, user, specs, call, **kw):
        seen.append((task, list(system)))
        return SimpleNamespace(text="（笔记）")

    monkeypatch.setattr(app.llm, "run_agent", run_agent)
    dossier.research(app, c, "k", "标题", "问题", "行情")
    task, system = seen[0]
    assert task == "research" and twitter.HINT in system and any("使用者登记的可信推特账号" in x and "官方号 @AcmeOfficial" in x for x in system)
    c.twitter_official = []
    app.settings.sources.twitter.shared_accounts = []
    seen.clear()
    dossier.research(app, c, "k", "标题", "问题", "行情")
    assert twitter.HINT in seen[0][1] and not any("使用者登记的可信推特账号" in x for x in seen[0][1])  # 没有登记账号：只有通用提示


def test_tracker_prompt_lists_trusted_accounts(app, tw, monkeypatch):
    from types import SimpleNamespace
    from stockagent import tracker

    c = app.watchlist.get("acme")
    c.twitter_accounts = ["newswire"]
    seen = []
    monkeypatch.setattr(app.llm, "run_agent", lambda task, system, user, specs, call, **kw: seen.append((task, list(system))) or SimpleNamespace(text="（修正笔记）"))
    tracker._research(app, c, "上下文", [], "")
    task, system = seen[0]
    assert task == "tracker" and twitter.HINT in system and any("媒体 / 快讯 / 关键人物账号 @newswire" in x for x in system)
