"""WebUI：页面渲染（不联网）、安全措施（Host、CSRF、响应头、净化、文件访问）、观察清单与配置的编辑、任务提交、上传。"""

from __future__ import annotations

import textwrap

import pytest
import requests
from fastapi.testclient import TestClient

from stockagent import tracker
from stockagent.web import charts, render, yamledit
from stockagent.web.server import create_app
from test_tracker import CARD, MATRIX, REPORT, _bars, seed_event

WATCHLIST = """
# 自选股清单（测试）
companies:
  - id: dongyue
    name: 东岳集团
    listings:
      - {market: HK, code: "00189", benchmark: "100.HSI"}   # 港股
    sector: 氟硅化工
    notes: >
      山东氟硅化工企业。
    watch_items:
      - R32 价格

benchmarks:
  HK:
    - {secid: "100.HSI", name: 恒生指数}
"""


@pytest.fixture
def web(project, monkeypatch):
    (project / "watchlist.yaml").write_text(textwrap.dedent(WATCHLIST), encoding="utf-8")
    app = create_app(project / "config.yaml", allowed_hosts=["testserver"], start_background=False)
    a = app.state.holder.app
    _bars(a, "116.00189", [11.05] * 30)
    _bars(a, "100.HSI", [24000.0] * 30)
    _bars(a, "133.HKDCNH", [0.85541] * 10)
    a.db.save_quote("116.00189", {"price": 11.05, "market_cap": 19146460000.0})
    from stockagent.tools import matrix_table
    log = [{"tool": "valuation_matrix", "args": MATRIX, "result": matrix_table(
        MATRIX["driver_label"], MATRIX["driver_values"], "PE", MATRIX["multiples"], MATRIX["shares"], MATRIX["net_cash"], MATRIX["fx"])}]
    a.db.save_dossier("dongyue", "mock", REPORT + "\n\n<script>alert(1)</script>", CARD, {}, log,
                      [{"description": "R32 跌破 6 万元/吨", "kind": "event", "action": "reduce"}], [], {})
    tracker.add(a, a.watchlist.get("dongyue"))
    seed_event(a, "Q4 长协价上调 <b>5%</b>")

    def no_network(*args, **kwargs):  # 页面只读库：任何联网请求都算错误
        raise AssertionError(f"页面渲染时不应联网：{args[1:3]}")

    monkeypatch.setattr(requests.Session, "request", no_network)
    client = TestClient(app)
    client.holder = app.state.holder
    client.get("/")  # 拿到 CSRF cookie
    return client


def token(client) -> str:
    return client.cookies.get("stock_csrf")


def post(client, url, data=None, files=None, **kw):
    return client.post(url, data={**(data or {}), "csrf_token": token(client)}, files=files, follow_redirects=False, **kw)


# ---------------------------------------------------------------- 页面

def test_pages_render_without_network(web):
    rid = web.holder.app.db.latest_revision("dongyue")["id"]
    for url in ["/", "/companies", "/companies/new", "/companies/dongyue/edit", "/c/dongyue", "/c/dongyue?range=all",
                f"/c/dongyue/revisions/{rid}", "/c/dongyue/report", "/events?days=30&min=1", "/reports", "/jobs", "/status",
                "/settings/llm", "/settings/llm/profile/mock", "/settings/llm/profile/_new", "/settings/schedule",
                "/settings/tracker", "/settings/general", "/settings/secrets", "/settings/raw", "/settings/raw?file=watchlist"]:
        r = web.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    page = web.get("/c/dongyue").text
    assert "<svg" in page and "目标价（情景加权估值）" in page and "16.17" in page and "&lt;b&gt;5%&lt;/b&gt;" in page
    report = web.get("/c/dongyue/report").text
    assert "<script>alert(1)</script>" not in report and "基线" in report


def test_host_and_security_headers(web):
    assert web.get("/", headers={"host": "evil.example"}).status_code == 400  # DNS rebinding
    r = web.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"] and "form-action 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["referrer-policy"] == "same-origin"


def test_csrf_and_origin(web):
    assert web.post("/c/dongyue/note", data={"title": "x"}).status_code == 403
    assert post(web, "/c/dongyue/note", {"title": "渠道调研：订单饱满"}, headers={"origin": "http://evil.example"}).status_code == 403
    assert post(web, "/c/dongyue/note", {"title": "渠道调研：订单饱满"}, headers={"origin": "null"}).status_code == 403
    r = post(web, "/c/dongyue/note", {"title": "渠道调研：订单饱满", "url": "javascript:alert(1)"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = post(web, "/c/dongyue/note", {"title": "渠道调研：订单饱满"}, headers={"origin": "http://testserver"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert any(i["title"] == "渠道调研：订单饱满" for i in web.holder.app.db.items_since("dongyue", "2000"))


def test_sanitize_markdown_and_chart_text():
    html = str(render.md_html("**粗体** <script>alert(1)</script> [x](javascript:alert(1)) [ok](https://a.b/c) <img src=x onerror=1>"))
    assert "<strong>粗体</strong>" in html and "<script" not in html and "javascript:" not in html and "onerror" not in html
    assert 'href="https://a.b/c"' in html
    assert render.safe_url("javascript:alert(1)") == "" and render.safe_url("https://x.y") == "https://x.y"
    import pandas as pd
    bars = pd.DataFrame({"date": ["2026-09-25", "2026-09-28"], "close": [10.0, 11.0]})
    svg = str(charts.timeline_svg(bars, [], [{"published_at": "2026-09-28T02:00:00+00:00", "materiality": 4, "direction": "positive",
                                             "uid": "abc", "title": "<script>x</script>"}]))
    assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_files_route(web):
    rep = web.holder.app.data_dir / "reports"
    rep.mkdir(parents=True, exist_ok=True)
    (rep / "2026-09-29_0830.html").write_text("<script>alert(1)</script>简报", encoding="utf-8")
    r = web.get("/files/reports/2026-09-29_0830.html")
    assert r.status_code == 200 and r.headers["content-security-policy"].startswith("sandbox")
    for bad in ["/files/reports/..%2Fstock.db", "/files/other/x.html", "/files/reports/x.exe", "/files/reports/missing.html"]:
        assert web.get(bad).status_code == 404


# ---------------------------------------------------------------- 观察清单

def test_watchlist_crud_keeps_comments_and_quotes(web):
    path = web.holder.watchlist_path
    etag = yamledit.digest(path.read_text(encoding="utf-8"))
    form = {"id": "tencent", "name": "腾讯控股", "aliases": "騰訊控股、Tencent", "listing0_market": "HK", "listing0_code": "700",
            "listing0_benchmark": "100.HSI", "notes": "游戏与广告", "watch_items": "游戏版号\n广告收入", "q_zh-CN": '"腾讯控股"', "etag": etag}
    r = post(web, "/companies/new", form)
    assert r.status_code == 303, r.text[:500]
    text = path.read_text(encoding="utf-8")
    assert '# 自选股清单（测试）' in text and '# 港股' in text and 'code: "00700"' in text and "- id: tencent" in text
    assert web.holder.app.watchlist.get("tencent").listings[0].key == "HK:00700"
    assert (web.holder.app.data_dir / "inbox" / "tencent").is_dir()
    # 校验失败：文件不变
    bad = post(web, "/companies/new", {**form, "id": "bad", "listing0_market": "A", "listing0_code": "12345", "etag": ""})
    assert bad.status_code == 400 and "A 股代码" in bad.text and path.read_text(encoding="utf-8") == text
    # 文件在页面打开后被改过：拒绝覆盖
    stale = post(web, "/companies/tencent/edit", {**form, "name": "腾讯", "etag": etag})
    assert stale.status_code == 400 and "被修改过" in stale.text
    etag = yamledit.digest(text)
    ok = post(web, "/companies/tencent/edit", {**form, "name": "腾讯", "listing0_code": "00700", "etag": etag})
    assert ok.status_code == 303 and web.holder.app.watchlist.get("tencent").name == "腾讯"
    etag = yamledit.digest(path.read_text(encoding="utf-8"))
    assert post(web, "/companies/tencent/delete", {"etag": etag}).status_code == 303
    assert web.holder.app.watchlist.get("tencent") is None and "# 港股" in path.read_text(encoding="utf-8")
    assert list((web.holder.app.data_dir / "backups").glob("watchlist.yaml.*"))


def test_bulk_track_and_confirm_flow(web):
    r = post(web, "/companies/track", {"ids": ["dongyue"], "action": "untrack"})
    assert r.status_code == 303 and not web.holder.app.db.tracker("dongyue")["enabled"]
    r = post(web, "/companies/track", {"ids": ["dongyue"], "action": "track"})
    assert "恢复追踪" in requests.utils.unquote(r.headers["location"]) and web.holder.app.db.tracker("dongyue")["enabled"]


# ---------------------------------------------------------------- 配置

def test_llm_profile_edit_and_key_is_never_shown(web):
    path = web.holder.config_path
    etag = yamledit.digest(path.read_text(encoding="utf-8"))
    secret = "sk-test-SECRET-1234567890"
    r = post(web, "/settings/llm/profile/_new", {"new_name": "gw", "profile.protocol": "openai", "profile.base_url": "http://127.0.0.1:18080/v1",
                                                  "profile.model": "qwen", "profile.api_key_env": "GW_API_KEY", "profile.json_mode": "on",
                                                  "profile.tool_mode": "native", "profile.max_tokens": "8000", "profile.timeout": "60",
                                                  "profile.max_retries": "2", "profile.max_tokens_param": "max_tokens",
                                                  "profile.auth": "api_key", "profile.thinking": "adaptive", "api_key": secret, "etag": etag})
    assert r.status_code == 303 and "ok=" in r.headers["location"], r.headers["location"]
    assert "GW_API_KEY=sk-test-SECRET-1234567890" in (path.parent / ".env").read_text(encoding="utf-8")
    cfg = web.holder.app.settings.llm.profiles["gw"]
    assert cfg.model == "qwen" and cfg.max_tokens == 8000 and cfg.timeout == 60
    for url in ["/settings/llm", "/settings/llm/profile/gw", "/settings/secrets"]:
        page = web.get(url).text
        assert secret not in page and "sk-…7890" in page
    # 被任务或默认引用的 profile 不能删除
    etag = yamledit.digest(path.read_text(encoding="utf-8"))
    r = post(web, "/settings/llm/profile/mock/delete", {"etag": etag})
    assert "仍被使用" in requests.utils.unquote(r.headers["location"])
    r = post(web, "/settings/llm/profile/gw/delete", {"etag": etag})
    assert "gw" not in web.holder.app.settings.llm.profiles


def test_dangerous_env_names_are_rejected(project):
    with pytest.raises(yamledit.EditError):
        yamledit.set_env(project / ".env", "PATH", "/tmp/evil")
    with pytest.raises(yamledit.EditError):
        yamledit.set_env(project / ".env", "X_API_KEY", "a\nPATH=/tmp")
    yamledit.set_env(project / ".env", "X_API_KEY", "value with space")
    from dotenv import dotenv_values
    assert dotenv_values(project / ".env")["X_API_KEY"] == "value with space"


def test_schedule_and_section_settings(web):
    path = web.holder.config_path
    etag = yamledit.digest(path.read_text(encoding="utf-8"))
    r = post(web, "/settings/schedule", {"rows": "0", "enabled": "on", "catchup_minutes": "90", "name_0": "收盘日报",
                                         "task_0": "run", "args_0": "--push", "cron_0": "30 17 * * 1-5", "enabled_0": "on", "etag": etag})
    assert r.status_code == 303 and "ok=" in r.headers["location"], requests.utils.unquote(r.headers["location"])
    s = web.holder.app.settings.scheduler
    assert s.catchup_minutes == 90 and s.jobs[0].name == "收盘日报" and s.jobs[0].args == ["--push"]
    before = path.read_text(encoding="utf-8")
    r = post(web, "/settings/schedule", {"rows": "0", "name_0": "坏", "task_0": "run", "cron_0": "99 * * * *",
                                         "etag": yamledit.digest(before)})
    assert "err=" in r.headers["location"] and path.read_text(encoding="utf-8") == before
    r = post(web, "/settings/section/tracker", {"tracker.min_materiality": "4", "tracker.max_events": "10", "tracker.max_steps": "6",
                                                "tracker.target": "base", "tracker.notify_min_change": "0.1", "tracker.flag_change": "0.3",
                                                "tracker.redo_materiality": "5", "tracker.redo_cooldown_days": "5", "tracker.web_search": "on",
                                                "etag": yamledit.digest(before)})
    assert r.status_code == 303
    t = web.holder.app.settings.tracker
    assert t.min_materiality == 4 and t.target == "base" and t.auto_rebaseline is False and t.web_search is True


# ---------------------------------------------------------------- 任务与上传

def test_job_submit_log_and_cancel(web):
    r = post(web, "/jobs", {"task": "fetch", "companies": ["dongyue"], "flags": ["--no-news"]})
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    jid = int(r.headers["location"].split("/")[2].split("?")[0])
    job = web.holder.app.db.job(jid)
    assert job["argv"] == [["fetch", "--no-news", "dongyue"]] and job["status"] == "queued"
    assert web.get(f"/jobs/{jid}/log?offset=0").json()["status"] == "queued"
    assert "err=" in post(web, "/jobs", {"task": "fetch", "flags": ["--evil"]}).headers["location"]
    r = post(web, f"/jobs/{jid}/cancel")
    assert web.holder.app.db.job(jid)["status"] == "cancelled"


def test_upload_document_and_limits(web):
    r = post(web, "/c/dongyue/upload", {"doc_type": "research", "title": "券商点评"},
             files={"file": ("点评.txt", "毛利率提升。" * 400, "text/plain")})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    docs = web.holder.app.db.documents("dongyue")
    assert docs and docs[0]["doc_type"] == "research" and docs[0]["title"] == "券商点评"
    r = post(web, "/c/dongyue/upload", {}, files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert "err=" in r.headers["location"]
    web.holder.app.settings.web.upload_max_mb = 0  # 上限 1 MB（留 1 MB 给表单字段）
    r = post(web, "/c/dongyue/upload", {}, files={"file": ("big.txt", b"a" * (2 * 1024 * 1024), "text/plain")})
    assert r.status_code == 413


# ---------------------------------------------------------------- 报告研读对话

class SyncPool:
    """测试里同步执行回答（生产中是后台线程池）。"""

    def submit(self, fn, *args):
        fn(*args)


def test_report_chat_flow(web, monkeypatch):
    from stockagent.llm import LLMResult

    web.app.state.chat_pool = SyncPool()
    app = web.holder.app
    did = app.db.latest_dossier("dongyue")["id"]
    page = web.get("/c/dongyue/report").text
    assert "data-chat" in page and "研读对话" in page and "data-report-body" in page and "引文会与原文逐字核对" not in page
    url = "/c/dongyue/chats"
    body = {"dossier_id": did, "question": "估值怎么算？"}
    assert web.post(url, json=body).status_code == 403  # 没有 CSRF token
    hdr = {"X-CSRF-Token": token(web)}
    assert web.post(url, json=body, headers={**hdr, "origin": "http://evil.example"}).status_code == 403
    assert web.post(url, json={**body, "question": " "}, headers=hdr).json()["error"] == "问题不能为空"

    seen = {}

    def fake(task, system, user, tools, call_tool, **kw):
        seen["tools"] = [t["name"] for t in tools]
        kw["on_event"]({"type": "thinking", "text": "先看报告"})
        call_tool("calculate", {"code": "1 + 1"})
        kw["on_event"]({"type": "text", "text": "<script>x</script>"})
        return LLMResult(text="【报告·核心洞见】「配额是行政性供给刚性」<script>alert(1)</script>净利 99.9 亿",
                         data=None, model="fake", profile="mock", usage={})

    monkeypatch.setattr(app.llm, "run_agent", fake)
    r = web.post(url, json={**body, "quote": "配额是行政性供给刚性", "web_search": False}, headers=hdr)
    assert r.status_code == 200, r.text
    assert "web_search" not in seen["tools"] and "fetch_url" not in seen["tools"]
    chat_id, mid = r.json()["chat_id"], r.json()["message_id"]
    m = web.get(f"/chats/msg/{mid}").json()
    assert m["status"] == "done" and "<script" not in m["html"] and m["tools"] == 1
    assert [s["type"] for s in m["trace"]] == ["note", "thinking", "tool", "text"]
    sse = web.get(f"/chats/msg/{mid}/events")  # 回答已结束：补发全部事件，最后是 final
    assert sse.headers["content-type"].startswith("text/event-stream")
    assert '"type": "tool"' in sse.text and sse.text.rstrip().split("\n\n")[-1].startswith("id: ")
    assert "event: final" in sse.text and sse.text.index('"type": "thinking"') < sse.text.index("event: final")
    assert "event: final" in web.get(f"/chats/msg/{mid}/events", headers={"Last-Event-ID": "99"}).text
    assert m["checks"]["quotes"] == [{"text": "配额是行政性供给刚性", "source": "report", "found": True}] and "99.9亿" in m["checks"]["unverified"]
    detail = web.get(f"/chats/{chat_id}").json()
    assert [x["role"] for x in detail["messages"]] == ["user", "assistant"] and detail["messages"][0]["quote"] == "配额是行政性供给刚性"
    assert web.get(f"/c/dongyue/chats?dossier={did}").json()["chats"][0]["id"] == chat_id
    assert "估值怎么算？" in web.get("/c/dongyue/report").text  # 历史对话出现在下拉框

    app.db.add_chat_message(chat_id, "assistant", status="pending")
    assert web.post(f"/chats/{chat_id}/ask", json={"question": "追问"}, headers=hdr).status_code == 409
    assert web.post(f"/chats/{chat_id}/delete", json={}, headers=hdr).json() == {"ok": True}
    assert web.get(f"/chats/{chat_id}").status_code == 404 and app.db.chat_messages(chat_id) == []
    assert web.get("/chats/msg/999999").status_code == 404


def test_revision_page_layout_and_chat(web):
    from test_chat import EVENTS

    web.app.state.chat_pool = SyncPool()
    app = web.holder.app
    rid = app.db.latest_revision("dongyue")["id"]
    app.db._exec("UPDATE revisions SET events_json=?, notes_md=? WHERE id=?",
                 (__import__("json").dumps(EVENTS, ensure_ascii=False), "研究笔记正文", rid))
    page = web.get(f"/c/dongyue/revisions/{rid}").text
    assert "研讨对话" in page and f'data-revision="{rid}"' in page and 'data-page="revision"' in page
    assert page.index("修正研究笔记") < page.index("本次评估的依据")
    assert page.index('id="ev-ccc"') < page.index('id="ev-bbb"') < page.index('id="ev-K7"') < page.index('id="ev-aaa"')
    assert "不影响估值的依据（1 条）" in page and "引文会与原文逐字核对" not in page
    hdr = {"X-CSRF-Token": token(web)}
    r = web.post("/c/dongyue/chats", json={"dossier_id": 0, "revision_id": rid, "question": "改了什么？"}, headers=hdr)
    assert r.status_code == 200, r.text
    assert web.get(f"/c/dongyue/chats?revision={rid}").json()["chats"][0]["id"] == r.json()["chat_id"]
    assert "改了什么？" in web.get(f"/c/dongyue/revisions/{rid}").text
    assert "改了什么？" not in web.get("/c/dongyue/report").text  # 修正页的对话不出现在报告页
