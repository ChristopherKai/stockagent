"""飞书应用机器人：指令解析、REST 客户端、指令处理（启动任务）、任务结束后推回结果、推送渠道。全部离线。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stockagent import feishu, newco, notify
from stockagent.utils import iso, utcnow


# ---------------------------------------------------------------- 指令解析

@pytest.mark.parametrize("text, expect", [
    ("深度研究 美股AAPL", feishu.Command("research", "US", "AAPL")),
    ("@_user_1 深度研究 港股700", feishu.Command("research", "HK", "700")),
    ("深度研究：A股 600519", feishu.Command("research", "A", "600519")),
    ("深度研究 US AAPL", feishu.Command("research", "US", "AAPL")),
    ("深度研究 USB", feishu.Command("research", None, "USB")),       # 代码以 US 开头时不能被拆成市场前缀
    ("深度研究 英伟达", feishu.Command("research", None, "英伟达")),
    ("深度研究", feishu.Command("research", None, "")),
    ("发送报告 英伟达", feishu.Command("report", None, "英伟达")),
    ("任务状态", feishu.Command("status")),
    ("立即检查", feishu.Command("check", None, "")),
    ("立即检查 测试公司", feishu.Command("check", None, "测试公司")),
    ("帮助", feishu.Command("help")),
])
def test_parse(text, expect):
    assert feishu.parse(text) == expect


def test_parse_unknown():
    assert feishu.parse("今天天气怎么样") is None
    assert feishu.parse("   ") is None


# ---------------------------------------------------------------- REST 客户端

class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


class FakeHttp:
    """按路径返回预设响应，记录请求。"""

    def __init__(self, token_codes=()):
        self.calls: list[tuple[str, str, dict]] = []
        self.tokens = 0
        self.invalid = list(token_codes)   # 依次让第 n 次业务调用返回 token 失效

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def request(self, method, url, **kw):
        path = url.split("/open-apis")[1]
        if path.startswith("/auth/"):
            self.tokens += 1
            return FakeResp({"code": 0, "tenant_access_token": f"t{self.tokens}", "expire": 7200})
        self.calls.append((method, path, kw))
        if self.invalid and self.invalid.pop(0):
            return FakeResp({"code": 99991663, "msg": "token invalid"})
        if path == "/im/v1/files":
            return FakeResp({"code": 0, "data": {"file_key": "fk1"}})
        return FakeResp({"code": 0, "data": {}})


def test_client_text_and_file(tmp_path):
    http = FakeHttp()
    cli = feishu.Client("id", "secret", session=http)
    cli.send_text("你好", reply_to="om_1")
    cli.send_text("定时", chat_id="oc_1")
    cli.send_text("私聊", chat_id="ou_abc")              # 填的是用户 open_id 也能发
    f = tmp_path / "r.html"
    f.write_text("<html></html>", encoding="utf-8")
    cli.send_file(f, chat_id="oc_1")
    assert http.tokens == 1                                     # token 缓存
    (m1, p1, k1), (m2, p2, k2), (_, _, k2b), (m3, p3, k3), (m4, p4, k4) = http.calls
    assert k2b["params"] == {"receive_id_type": "open_id"}
    assert p1 == "/im/v1/messages/om_1/reply" and json.loads(k1["json"]["content"]) == {"text": "你好"}
    assert p2 == "/im/v1/messages" and k2["params"] == {"receive_id_type": "chat_id"} and k2["json"]["receive_id"] == "oc_1"
    assert p3 == "/im/v1/files" and k3["data"] == {"file_type": "stream", "file_name": "r.html"}
    assert p4 == "/im/v1/messages" and k4["json"]["msg_type"] == "file" and json.loads(k4["json"]["content"]) == {"file_key": "fk1"}


def test_client_refreshes_invalid_token():
    http = FakeHttp(token_codes=[True, False])
    cli = feishu.Client("id", "secret", session=http)
    cli.send_text("hi", chat_id="oc_1")
    assert http.tokens == 2 and len(http.calls) == 2


def test_client_error_and_no_target():
    class Bad(FakeHttp):
        def request(self, method, url, **kw):
            if "/auth/" in url:
                return super().request(method, url, **kw)
            return FakeResp({"code": 230002, "msg": "bot not in chat"})

    with pytest.raises(feishu.FeishuError, match="230002"):
        feishu.Client("id", "secret", session=Bad()).send_text("hi", chat_id="oc_1")
    with pytest.raises(feishu.FeishuError, match="没有指定接收会话"):
        feishu.Client("id", "secret", session=FakeHttp()).send_text("hi")


# ---------------------------------------------------------------- 指令处理

class FakeClient:
    def __init__(self):
        self.sent: list[tuple] = []

    def send_text(self, text, chat_id=None, reply_to=None):
        self.sent.append(("text", text, chat_id, reply_to))

    def send_file(self, path, chat_id=None, reply_to=None):
        self.sent.append(("file", path.name, chat_id, reply_to))


@pytest.fixture
def bot(app, project):
    app.settings.notify.feishu_allowed_users = ["ou_me"]
    holder = SimpleNamespace(app=app, config_path=project / "config.yaml")
    return feishu.Bot(holder, FakeClient())


def say(bot, text, mid="om_1", sender="ou_me", chat_type="p2p", mentioned=False):
    bot.handle(feishu.Incoming(mid, "oc_1", chat_type, sender, text, mentioned))
    return bot.client.sent[-1][1] if bot.client.sent else None


def test_unauthorized_gets_open_id_and_no_job(bot, app):
    out = say(bot, "深度研究 测试公司", sender="ou_stranger")
    assert "ou_stranger" in out and "feishu_allowed_users" in out
    assert app.db.jobs(statuses=("queued",)) == []


def test_user_id_in_whitelist_is_accepted(bot, app):
    app.settings.notify.feishu_allowed_users = ["16e9ff2b"]
    assert say(bot, "帮助", sender="ou_other") is not None and "没有权限" in bot.client.sent[-1][1]
    bot.handle(feishu.Incoming("om_7", "oc_1", "p2p", "ou_other", "帮助", False, ("16e9ff2b",)))
    assert "可用指令" in bot.client.sent[-1][1]


def test_private_chat_becomes_home_chat(bot, app, monkeypatch):
    assert feishu.home_chat(app.settings) is None
    say(bot, "帮助", chat_type="group", mentioned=True)
    assert feishu.home_chat(app.settings) is None               # 群聊不改默认会话
    say(bot, "帮助", mid="om_2")
    assert feishu.home_chat(app.settings) == "oc_1"
    app.settings.notify.feishu_chat_id = "oc_cfg"                # 配置的会话优先
    assert feishu.home_chat(app.settings) == "oc_cfg"


def test_check_command(bot, app):
    assert "没有追踪中的公司" in say(bot, "立即检查")
    assert "不在追踪中" in say(bot, "立即检查 acme", mid="om_2")
    assert "找不到" in say(bot, "立即检查 不存在", mid="om_3")
    app.db.set_tracker("acme")
    assert "任务 #" in say(bot, "立即检查 acme", mid="om_4")
    j = app.db.jobs(statuses=("queued",))[0]
    assert j["task"] == "target_run" and j["source"] == "feishu" and j["company_id"] == "acme"
    assert "已经在排队" in say(bot, "立即检查 测试公司", mid="om_5")
    assert "任务 #" in say(bot, "立即检查", mid="om_6")            # 全部追踪中的公司：另一条任务
    assert len(app.db.jobs(statuses=("queued",))) == 2


def test_check_result_sent_back(app, tmp_path, sent):
    log = tmp_path / "t.log"
    log.write_text("【任务 #1】标的追踪（开始）\n测试公司：目标价 12.3\n【任务 #1】完成（退出码 0）\n", encoding="utf-8")
    jid = app.db.add_job("target_run", [["target", "run"]], "标的追踪：按新事件修正估值", "feishu")
    app.db.update_job(jid, log_path=str(log))
    app.db.kv_set(f"feishu_origin:{jid}", json.dumps({"chat_id": "oc_1", "message_id": "om_1"}))
    feishu.job_finished(app, jid, "done")
    text = sent[0][1]
    assert "立即检查完成" in text and "目标价 12.3" in text and "【任务" not in text.split("\n", 1)[1] and "不构成投资建议" in text


def test_group_needs_mention_and_duplicates_ignored(bot):
    assert say(bot, "帮助", chat_type="group") is None
    assert "可用指令" in say(bot, "帮助", mid="om_2", chat_type="group", mentioned=True)
    n = len(bot.client.sent)
    say(bot, "帮助", mid="om_2", chat_type="group", mentioned=True)      # 飞书重发同一事件
    assert len(bot.client.sent) == n


def test_unknown_command_shows_help(bot):
    assert "没看懂" in say(bot, "你好")


def test_research_existing_company_submits_dossier(bot, app):
    out = say(bot, "深度研究 测试公司")
    j = app.db.jobs(statuses=("queued",))[0]
    assert j["task"] == "dossier" and j["source"] == "feishu" and j["company_id"] == "acme"
    assert f"#{j['id']}" in out and "马上开始" in out
    assert json.loads(app.db.kv_get(f"feishu_origin:{j['id']}")) == {"chat_id": "oc_1", "message_id": "om_1"}
    # 同一公司已在排队：不重复入队
    assert "已经在排队" in say(bot, "深度研究 acme", mid="om_2")
    assert len(app.db.jobs(statuses=("queued",))) == 1


def test_research_new_company_goes_through_add_company(bot, app, monkeypatch):
    seen = {}
    monkeypatch.setattr(newco, "build", lambda a, m, c: seen.update(m=m, c=c) or {
        "name": "苹果", "listings": [{"market": "US", "code": "AAPL", "exchange": None}], "name_en": "Apple", "aliases": [], "sector": None, "id": "apple"})
    out = say(bot, "深度研究 美股AAPL")
    j = app.db.jobs(statuses=("queued",))[0]
    assert seen == {"m": "US", "c": "AAPL"}
    assert j["task"] == "add_company" and j["title"].endswith("US AAPL") and j["source"] == "feishu"
    assert j["argv"][0][-3:] == ["US", "AAPL", "--onboard"]
    assert "新公司" in out


def test_research_resolution_errors(bot, app, monkeypatch):
    assert "请带上股票" in say(bot, "深度研究")
    monkeypatch.setattr(newco, "resolve", lambda a, q: ("A", "600519", "贵州茅台"))
    assert "与你指定的市场不一致" in say(bot, "深度研究 美股茅台", mid="om_3")
    monkeypatch.setattr(newco, "build", lambda a, m, c: (_ for _ in ()).throw(ValueError("东方财富查不到 US ZZZZ 的公司资料")))
    assert "东方财富查不到" in say(bot, "深度研究 美股ZZZZ", mid="om_4")
    assert app.db.jobs(statuses=("queued",)) == []


def test_status_and_send_report(bot, app, tmp_path):
    assert "没有运行或排队" in say(bot, "任务状态")
    say(bot, "深度研究 acme", mid="om_2")
    assert "排队｜深度报告" in say(bot, "任务状态", mid="om_3")
    assert "还没有深度报告" in say(bot, "发送报告 acme", mid="om_4")
    html, pdf = tmp_path / "acme.html", tmp_path / "acme.pdf"
    html.write_text("<html></html>", encoding="utf-8")
    pdf.write_bytes(b"%PDF")
    did = app.db.save_dossier("acme", "mock", "# 报告", {}, {}, [], [], [], {})
    app.db.set_dossier_paths(did, str(html), str(pdf))
    bot.client.sent.clear()
    say(bot, "发送报告 测试公司", mid="om_5")
    assert [s[:2] for s in bot.client.sent] == [("text", bot.client.sent[0][1]), ("file", "acme.html"), ("file", "acme.pdf")]
    assert all(s[3] == "om_5" for s in bot.client.sent)


# ---------------------------------------------------------------- 任务结束后推回

def fake_job(app, tmp_path, *, status_log="", company="acme"):
    log = tmp_path / "job.log"
    log.write_text(status_log, encoding="utf-8")
    jid = app.db.add_job("dossier", [["dossier", company]], "深度报告：测试公司", "feishu", company_id=company)
    app.db.update_job(jid, log_path=str(log))
    app.db.kv_set(f"feishu_origin:{jid}", json.dumps({"chat_id": "oc_1", "message_id": "om_1"}))
    return jid


@pytest.fixture
def sent(app, monkeypatch):
    cli = FakeClient()
    monkeypatch.setattr(feishu, "client", lambda settings: cli)
    return cli.sent


def test_job_finished_sends_new_report(app, tmp_path, sent):
    jid = fake_job(app, tmp_path)
    html = tmp_path / "acme.html"
    html.write_text("<html></html>", encoding="utf-8")
    did = app.db.save_dossier("acme", "mock", "# 报告", {}, {}, [], [], [], {})
    app.db.set_dossier_paths(did, str(html), None)
    feishu.job_finished(app, jid, "done")
    assert [s[0] for s in sent] == ["text", "file"] and "测试公司 深度报告已生成" in sent[0][1]
    assert sent[1][1:] == ("acme.html", "oc_1", "om_1")
    assert app.db.kv_get(f"feishu_origin:{jid}") is None
    feishu.job_finished(app, jid, "done")                 # 只推一次
    assert len(sent) == 2


def test_job_finished_without_new_report_or_failed(app, tmp_path, sent):
    html = tmp_path / "old.html"
    html.write_text("x", encoding="utf-8")
    old = app.db.save_dossier("acme", "mock", "# 旧报告", {}, {}, [], [], [], {})
    app.db.set_dossier_paths(old, str(html), None)
    jid = fake_job(app, tmp_path, status_log="第一行\n✗ 测试公司：模型不可用\n")
    app.db.update_job(jid, **{"created_at": iso(utcnow().replace(year=utcnow().year + 1))})  # 旧报告早于任务创建
    feishu.job_finished(app, jid, "done")
    assert len(sent) == 1 and "没有生成新的深度报告" in sent[0][1] and "模型不可用" in sent[0][1]
    jid = fake_job(app, tmp_path, status_log="Traceback\nboom")
    feishu.job_finished(app, jid, "failed")
    assert sent[-1][1].startswith(f"✗ 任务 #{jid} 失败") and "boom" in sent[-1][1]


def test_job_finished_ignores_other_sources(app, tmp_path, sent):
    jid = app.db.add_job("dossier", [["dossier"]], "x", "web")
    feishu.job_finished(app, jid, "done")
    assert sent == []


def test_new_company_report_found_by_listing(app, tmp_path, sent):
    log = tmp_path / "j.log"
    log.write_text("", encoding="utf-8")
    jid = app.db.add_job("add_company", [["add", "A", "300001", "--onboard"]], "新增公司并初始化：A 300001", "feishu")
    app.db.update_job(jid, log_path=str(log))
    app.db.kv_set(f"feishu_origin:{jid}", json.dumps({"chat_id": "oc_1", "message_id": "om_1"}))
    html = tmp_path / "new.html"
    html.write_text("x", encoding="utf-8")
    did = app.db.save_dossier("acme", "mock", "# 报告", {}, {}, [], [], [], {})
    app.db.set_dossier_paths(did, str(html), None)
    feishu.job_finished(app, jid, "done")
    assert [s[0] for s in sent] == ["text", "file"]


# ---------------------------------------------------------------- 推送渠道与配置

def test_notify_channel_and_push(app, monkeypatch):
    n = app.settings.notify
    n.feishu_app_id_env, n.feishu_app_secret_env = "T_FS_ID", "T_FS_SECRET"
    assert notify.channels(app.settings) == []                   # 没有会话可推
    feishu.set_home_chat(app.settings, "oc_9")
    monkeypatch.setenv("T_FS_ID", "id")
    monkeypatch.setenv("T_FS_SECRET", "secret")
    assert notify.channels(app.settings) == ["飞书应用"]
    assert any("feishu_allowed_users 为空" in m for m in notify.missing(app.settings))
    cli = FakeClient()
    monkeypatch.setattr(feishu, "client", lambda settings: cli)
    assert notify.push(app.settings, "标题", "正文") == ["已推送到飞书应用"]
    assert cli.sent == [("text", "标题\n正文", "oc_9", None)]
    monkeypatch.delenv("T_FS_SECRET")
    assert notify.channels(app.settings) == [] and any("缺少 APP SECRET" in m for m in notify.missing(app.settings))


def test_dossier_requests_are_no_longer_pushed():
    import inspect

    from stockagent import cli
    assert "需要你补充的数据】" not in inspect.getsource(cli)


def test_listener_parses_sdk_event(app, project, monkeypatch):
    """用 SDK 自己的事件模型验证字段路径（message_id / chat_id / sender.open_id / content.text / mentions）。"""
    from lark_oapi.api.im.v1 import (EventMessage, EventSender, MentionEvent, P2ImMessageReceiveV1, P2ImMessageReceiveV1Data,
                                     UserId)

    got = []
    ln = feishu.Listener(SimpleNamespace(app=app, config_path=project / "config.yaml"))
    ln.bot = SimpleNamespace(handle=got.append)
    ln.pool = SimpleNamespace(submit=lambda fn, *a: fn(*a))
    ev = P2ImMessageReceiveV1(); ev.event = P2ImMessageReceiveV1Data()
    ev.event.sender = EventSender(); ev.event.sender.sender_id = UserId(); ev.event.sender.sender_id.open_id = "ou_me"
    ev.event.message = EventMessage(); m = ev.event.message
    m.message_id, m.chat_id, m.chat_type, m.message_type = "om_9", "oc_9", "group", "text"
    m.content = json.dumps({"text": "@_user_1 深度研究 美股AAPL"}); m.mentions = [MentionEvent()]
    ln._on_event(ev)
    assert got == [feishu.Incoming("om_9", "oc_9", "group", "ou_me", "@_user_1 深度研究 美股AAPL", True)]
    m.message_type = "image"
    ln._on_event(ev)
    assert len(got) == 1                                        # 非文本消息忽略


def test_listener_without_credentials_does_not_start(app, project):
    assert feishu.Listener(SimpleNamespace(app=app, config_path=project / "config.yaml")).start() is False
