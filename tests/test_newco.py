"""新增公司：按市场与代码查公司概况生成观察清单条目、stock add、一键添加的后台任务与页面、WebUI 自动加载被任务修改的观察清单。"""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from stockagent import cli, jobs, newco
from stockagent.app import App
from stockagent.sources import eastmoney, emdata
from stockagent.web import yamledit
from test_web import post, web  # noqa: F401 - fixture

XPENG = {"name": "小鹏集团-W", "names": ["小鹏集团-W", "小鹏集团"], "name_en": "XPeng Inc.",
         "industry": "非日常生活消费品-汽车与汽车零部件-汽车-汽车制造商", "business": "主要从事设计、开发、生产及营销智能电动汽车。",
         "listings": [("HK", "09868", None), ("US", "XPEV", "NYSE")]}


@pytest.fixture
def profile(monkeypatch):
    calls = []

    def fake(session, market, code):
        calls.append((market, code))
        return XPENG if (market, code) in {("HK", "09868"), ("US", "XPEV")} else None

    monkeypatch.setattr(emdata, "company_profile", fake)
    return calls


def test_build_entry_from_profile(app, profile):
    data = newco.build(app, "hk", "9868")
    assert profile == [("HK", "09868")]
    assert data["id"] == "xpeng" and data["name"] == "小鹏集团" and data["name_en"] == "XPeng"
    assert data["aliases"] == ["小鵬集團"]
    assert data["listings"] == [{"market": "HK", "code": "09868", "exchange": None, "benchmark": None},
                                {"market": "US", "code": "XPEV", "exchange": "NYSE", "benchmark": None}]
    assert data["sector"] == "汽车 · 汽车制造商" and data["notes"].startswith("主要从事设计")
    with pytest.raises(ValueError, match="查不到"):
        newco.build(app, "HK", "1")
    with pytest.raises(ValueError, match="已在观察清单中：测试公司"):
        newco.build(app, "A", "300001")  # 不联网就能判断
    assert len(profile) == 2
    with pytest.raises(ValueError, match="6 位数字"):
        newco.normalize("A", "30000")
    assert newco.normalize("hk", "00100.HK") == ("HK", "00100")


SUGGEST = {
    "小鹏": [{"code": "XPEV", "name": "小鹏汽车", "mkt_num": "106", "type": "美股"},
             {"code": "09868", "name": "小鹏集团-W", "mkt_num": "116", "type": "港股"},
             {"code": "89868", "name": "小鹏集团-WR", "mkt_num": "116", "type": "港股"},        # 人民币柜台
             {"code": "13027", "name": "小鹏摩利六乙购C", "mkt_num": "116", "type": "港股"}],  # 窝轮
    "小鹏集团": [{"code": "XPEV", "name": "小鹏集团", "mkt_num": "106", "type": "美股"},
                 {"code": "09868", "name": "小鹏集团-W", "mkt_num": "116", "type": "港股"},
                 {"code": "XPEX", "name": "二倍做多小鹏ETF", "mkt_num": "107", "type": "美股"}],
}


def test_resolve_name_or_code(app, monkeypatch):
    calls = []
    monkeypatch.setattr(eastmoney, "suggest", lambda s, q, count=10: calls.append(q) or SUGGEST.get(q, []))
    assert newco.resolve(app, "2577") == ("HK", "02577", "2577") and newco.resolve(app, "300308")[:2] == ("A", "300308")
    assert calls == []  # 纯数字不联网
    assert newco.resolve(app, "小鹏集团") == ("HK", "09868", "小鹏集团-W")  # 同一家公司两地上市：去掉后缀后同名
    with pytest.raises(ValueError, match="匹配到多家公司"):
        newco.resolve(app, "小鹏")  # 小鹏汽车 与 小鹏集团 名称不同，不替使用者猜
    with pytest.raises(ValueError, match="查不到"):
        newco.resolve(app, "不存在")
    for bad in ["-rf", "a;b", "x" * 31, ""]:
        with pytest.raises(ValueError, match="请输入股票名称或代码"):
            newco.check_query(bad)
    assert newco.existing_name(app, "測試公司").id == "acme"


def test_cli_add_writes_watchlist_and_onboards(app, project, profile, monkeypatch, capsys):
    ran = []
    orig = cli.main
    monkeypatch.setattr(cli, "main", lambda argv: ran.append(argv) if argv[2:3] != ["add"] else orig(argv))
    cfg = str(project / "config.yaml")
    orig(["-c", cfg, "add", "US", "XPEV", "--dry-run"])
    assert "id=xpeng" in capsys.readouterr().out and App.load(cfg).watchlist.get("xpeng") is None
    monkeypatch.setattr(eastmoney, "suggest", lambda s, q, count=10: SUGGEST.get(q, []))
    orig(["-c", cfg, "add", "小鹏集团", "--onboard"])  # 名称：先识别成 HK 09868
    c = App.load(cfg).watchlist.get("xpeng")
    assert c.name == "小鹏集团" and [l.display for l in c.listings] == ["09868.HK", "XPEV"]
    assert "测试公司" in (project / "watchlist.yaml").read_text(encoding="utf-8")  # 原有条目保留
    assert (app.data_dir / "inbox" / "xpeng").is_dir()
    assert [a[2:] for a in ran] == [[*s, "xpeng"] for s in jobs.TASKS["onboard"].steps]
    with pytest.raises(SystemExit, match="已在观察清单中"):
        orig(["-c", cfg, "add", "HK", "9868"])


def test_add_company_job_validation(app):
    steps, title, cid = jobs.build(app.watchlist, {}, "add_company", listing=("hk", "2577"))
    assert steps == [["add", "HK", "02577", "--onboard"]] and title == "新增公司并初始化：HK 02577" and cid is None
    assert jobs.build(app.watchlist, {}, "add_company", listing=("", "腾讯控股"))[0] == [["add", "腾讯控股", "--onboard"]]
    for bad in [None, ("HK", "--help"), ("XX", "1"), ("", "--onboard"), ("", "a b;c")]:
        with pytest.raises(jobs.JobError):
            jobs.build(app.watchlist, {}, "add_company", listing=bad)
    with pytest.raises(jobs.JobError, match="不接受市场与代码"):
        jobs.build(app.watchlist, {}, "run", listing=("HK", "2577"))


def test_quick_add_page_and_watchlist_reload(web):
    assert "一键添加并初始化" in web.get("/companies").text
    assert "新增公司并初始化" not in web.get("/jobs").text  # 只能从观察清单页提交
    r = post(web, "/companies/quick", {"market": "HK", "code": "189"})
    assert r.status_code == 303 and "已在观察清单中：东岳集团" in unquote(r.headers["location"])
    assert "A 股代码为 6 位数字" in unquote(post(web, "/companies/quick", {"market": "A", "code": "12"}).headers["location"])
    assert "已在观察清单中：东岳集团" in unquote(post(web, "/companies/quick", {"market": "", "code": "东岳集团"}).headers["location"])
    assert "已在观察清单中：东岳集团" in unquote(post(web, "/companies/quick", {"market": "", "code": "189"}).headers["location"])
    assert post(web, "/companies/quick", {"market": "", "code": "腾讯控股"}).headers["location"].startswith("/jobs/")
    assert web.holder.app.db.jobs(1)[0]["argv"] == [["add", "腾讯控股", "--onboard"]]
    r = post(web, "/companies/quick", {"market": "HK", "code": "2577"})
    assert r.headers["location"].startswith("/jobs/")
    job = web.holder.app.db.jobs(1)[0]
    assert job["task"] == "add_company" and job["argv"] == [["add", "HK", "02577", "--onboard"]]
    # 后台任务（另一个进程）改了 watchlist.yaml：WebUI 下次取 app 时自动重新加载
    path = web.holder.watchlist_path
    yamledit.upsert_company(path, {"id": "innoscience", "name": "英诺赛科", "listings": [{"market": "HK", "code": "02577"}],
                                   "aliases": [], "watch_items": [], "news_queries": {}}, None, web.holder.backup_dir, is_new=True)
    assert web.holder.app.watchlist.get("innoscience") is not None
    assert "英诺赛科" in web.get("/companies").text
    path.write_text("companies: [", encoding="utf-8")  # 改坏了：继续用旧的
    assert web.holder.app.watchlist.get("innoscience") is not None
