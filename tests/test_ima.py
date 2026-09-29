"""ima 知识库：标题解析、按公司检索的增量收录（日期、首次运行、原文入库、OCR、每日原文额度）、agent 工具。"""

from __future__ import annotations

from datetime import date

import pytest

from stockagent import imafeed
from stockagent.sources import ima
from stockagent.tools import ToolBox
from stockagent.utils import today_local


def test_parse_date_and_source_name():
    today = date(2026, 9, 30)
    assert ima.parse_date("20260929-高盛-英诺赛科（2577.HK）：800V", today) == date(2026, 9, 29)
    assert ima.parse_date("260310-GS-小鹏汽车：VLA 2.0 试驾体验", today) == date(2026, 3, 10)
    assert ima.parse_date("伯恩斯坦-小鹏汽车（9868.HK）：广州试驾要点-260309.pdf", today) == date(2026, 3, 9)
    assert ima.parse_date("中际旭创交流纪要20260909.note", today) == date(2026, 9, 9)
    assert ima.parse_date("洪灏：如何交易G2峰会26.9.22【爱分享】投研.pdf", today) == date(2026, 9, 22)
    assert ima.parse_date("Published: 8:46pm, 21 Jul 2026 | Updated", today) == date(2026, 7, 21)
    assert ima.parse_date("中际旭创（300308）2026年半年报", today) is None  # 股票代码与年份不是日期
    assert ima.parse_date("20261231-未来日期", today) is None
    assert ima.source_name("20260929_中金公司_奇梦岛（HERE.US）短期表现") == "中金公司"
    assert ima.source_name("深度个股-20260929-湘财证券-百济神州-688235-一体化") == "湘财证券"
    assert ima.source_name("彭博社：中际旭创已获得批准.png") == "彭博社"
    assert ima.source_name("【电报解读】MiniMax透露") == "电报解读"
    assert imafeed._clean_title("20260601-高盛-小鹏汽车(9868.HK)新模型;买入.pdf") == "20260601-高盛-小鹏汽车(9868.HK)新模型;买入"
    assert imafeed._clean_title("高盛-Minimax Model.xlsx") == "高盛-Minimax Model"


def test_queries_and_matcher(app):
    c = app.watchlist.get("acme")
    assert imafeed.queries(c, 5) == ["测试公司", "300001", "1234.HK", "Acme"]
    m = imafeed.Matcher(c)
    assert m("20260929-高盛-測試公司：订单")  # 繁体别名
    assert m("20260929-野村-创新科技(1234.HK)首次覆盖") and m("XX（01234.HK）") and not m("XX（21234.HK）")
    assert m("研报（300001）") and not m("研报（3000012）")
    assert m("Acme Corp update") and not m("Acmeist")


@pytest.fixture
def kb(app, monkeypatch):
    """启用 ima：凭证、一个知识库，检索/取原文/下载/OCR 全部替换成假的。"""
    monkeypatch.setenv(ima.ENV_CLIENT_ID, "cid")
    monkeypatch.setenv(ima.ENV_API_KEY, "key")
    app.settings.sources.ima.knowledge_bases = ["测试库"]
    monkeypatch.setattr(ima, "knowledge_bases", lambda s: [{"id": "kb1", "name": "测试库（共享）", "count": 9}])
    state = {"results": {}, "files": {}, "ocr": {}, "media_calls": 0, "quota": False, "searches": []}

    def search(session, kb_id, q):
        state["searches"].append(q)
        return list(state["results"].get(q, []))

    def media_url(session, media_id):
        state["media_calls"] += 1
        if state["quota"]:
            raise ima.ImaError(ima.QUOTA, "资料获取次数已达上限，请明天再尝试")
        return (f"https://skb.ima.myqcloud.com/{media_id}", {"X-IMA-Sign": "s"}) if media_id in state["files"] else None

    monkeypatch.setattr(ima, "search", search)
    monkeypatch.setattr(ima, "media_url", media_url)
    monkeypatch.setattr(ima, "download", lambda s, url, h, n: state["files"][url.rsplit("/", 1)[1]])
    monkeypatch.setattr(imafeed, "ocr_image", lambda path: state["ocr"][path.stem[:6]])
    return state


def entry(mid, title, mt=1):
    return {"media_id": mid, "title": title, "media_type": mt, "parent": ""}


def test_fetch_first_run_then_incremental(app, kb):
    c = app.watchlist.get("acme")
    today = today_local()
    recent = entry("m1", f"{today:%Y%m%d}-高盛-测试公司（1234.HK）：订单超预期.txt", 13)
    kb["results"]["测试公司"] = [
        recent,
        entry("m2", "20200101-野村-测试公司旧报告.pdf"),                 # 超出回溯期
        entry("m3", "彭博社：测试公司获大单.png", 9),                    # 没有日期：首次运行只记为已见过
        entry("m4", "其他公司周报.pdf"),                                 # 标题没提到本公司（检索是子串匹配）
        entry("f1", "测试公司研究", ima.FOLDER),
    ]
    kb["results"]["1234.HK"] = [recent]
    kb["files"]["m1"] = "第一页：测试公司订单 5 亿元\n管理层上调全年指引".encode()
    msgs = imafeed.fetch(app, [c])
    assert "新增 1 条，原文入库 1 份" in msgs[0] and "首次检索" in msgs[0]
    assert kb["searches"] == ["测试公司", "300001", "1234.HK", "Acme"]
    items = app.db._all("SELECT * FROM items WHERE source='ima'")
    assert len(items) == 1 and items[0]["source_name"] == "高盛" and items[0]["kind"] == "news"
    doc = app.db.documents("acme")[0]
    assert (doc["source"], doc["doc_type"], doc["period"]) == ("ima", "research", today.isoformat())
    assert items[0]["snippet"].startswith(f"[原文已入库：资料 #{doc['id']}] 第一页：测试公司订单 5 亿元")
    assert kb["media_calls"] == 1  # 旧报告与无日期截图都没有取原文
    assert app.db.ima_seen_ids("acme", ["m1", "m2", "m3", "m4"]) == {"m1", "m2", "m3"}

    # 第二次：只处理新出现的条目；没有日期的截图按 OCR 文字里的日期，没有就按首次见到的时间
    kb["results"]["测试公司"] += [entry("m5", "路透社：测试公司扩产.png", 9), entry("m6", "南华早报：测试公司旧闻.png", 9)]
    kb["files"].update({"m5": b"png", "m6": b"png"})
    kb["ocr"].update({"路透社_测试": "路透社 测试公司宣布扩产", "南华早报_测": "Published: 8:46pm, 21 Jul 2020"})
    msgs = imafeed.fetch(app, [c])
    assert "新条目 2，新增 1 条" in msgs[0] and "首次检索" not in msgs[0]
    new = app.db._one("SELECT * FROM items WHERE title LIKE '路透社%'")
    assert new["source_name"] == "路透社" and new["snippet"] == "路透社 测试公司宣布扩产"
    assert imafeed._budget(app)["n"] == 3


def test_download_quota_keeps_title_and_stops_trying(app, kb):
    c = app.watchlist.get("acme")
    app.db.kv_set("fetched:ima:kb1:acme", "2026-01-01T00:00:00+00:00")  # 不是首次
    kb["quota"] = True
    kb["results"]["测试公司"] = [entry("q1", f"{today_local():%Y%m%d}-摩根大通-测试公司：目标价上调.pdf"),
                                 entry("q2", f"{today_local():%Y%m%d}-瑞银-测试公司：首次覆盖.pdf")]
    msgs = imafeed.fetch(app, [c])
    assert "新增 2 条，原文入库 0 份（今日原文额度剩 0）" in msgs[0]
    assert kb["media_calls"] == 1  # 配额用完后当天不再请求
    rows = app.db._all("SELECT snippet FROM items WHERE source='ima'")
    assert all("今日原文配额已用完" in r["snippet"] for r in rows)
    app.db.kv_set("ima_downloads", '{"date": "2000-01-01", "n": 99, "exhausted": true}')  # 第二天重新计数
    assert imafeed.downloads_left(app) == app.settings.sources.ima.max_downloads_per_day


def test_toolbox_ima_tools(app, kb, monkeypatch):
    c = app.watchlist.get("acme")
    kb["results"]["测试公司"] = [entry("a", "20260801-高盛-测试公司：旧观点.pdf"), entry("b", "20260920-高盛-测试公司：新观点.txt", 13),
                                 entry("c", "20260915-野村-测试公司：中性.pdf"), entry("d", "测试公司概念股.png", 9)]
    kb["files"]["b"] = "高盛：测试公司目标价 30 元".encode()
    box = ToolBox(app, c)
    out = box.call("ima_search", {"query": "测试公司 高盛"})
    assert out.splitlines()[1:] == ["I1｜2026-09-20｜TXT｜20260920-高盛-测试公司：新观点.txt", "I2｜2026-08-01｜PDF｜20260801-高盛-测试公司：旧观点.pdf"]
    out = box.call("ima_read", {"ref": "i1"})
    doc = app.db.documents("acme")[0]
    assert out.startswith(f"已入库为本公司资料 #{doc['id']}") and "目标价 30 元" in out
    assert "目标价 30" in box.call("search_filings", {"query": "目标价", "doc_id": doc["id"]})
    assert box.call("ima_read", {"ref": "I2"}).startswith("错误：ImaError: ima 接口错误 220030")
    assert box.call("ima_read", {"ref": "I9"}).startswith("错误：没有编号 I9")
    monkeypatch.delenv(ima.ENV_API_KEY)
    assert "ima_search" not in ToolBox(app, c).tools  # 没有凭证时不提供
