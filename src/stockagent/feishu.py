"""飞书应用机器人（APP ID / APP SECRET）：长连接收消息，对话启动后台任务，完成后把报告文件发回发起指令的会话。

- 收消息走飞书「长连接」（lark-oapi 的 WebSocket 客户端），不需要公网地址；监听线程随 stock web 启动（只有拿到 web 锁的进程启动）。
- 指令只能启动 jobs.TASKS 里登记的任务（深度研究 = 已在观察清单的公司跑 dossier，否则走 add_company 新增并初始化）；发送者必须在
  notify.feishu_allowed_users 里。
- 任务结果由 jobrun 包装进程在任务结束时调用 job_finished 推回（web 进程重启也不影响）。
- 发消息、传文件直接走开放平台 REST（requests），与 notify.py 的其他渠道一致，便于测试。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

from . import jobs, newco, tracker
from .config import Settings, env
from .utils import iso, log, truncate_bytes, utcnow

API = "https://open.feishu.cn/open-apis"
SOURCE = "feishu"
TOKEN_INVALID = {99991663, 99991661, 99991668}   # tenant_access_token 失效 / 缺失
TEXT_LIMIT = 28000                               # 单条文本消息的字节上限（官方 150 KB，留足余量）


class FeishuError(RuntimeError):
    pass


def credentials(settings: Settings) -> tuple[str, str] | None:
    n = settings.notify
    app_id, secret = env(n.feishu_app_id_env), env(n.feishu_app_secret_env)
    return (app_id, secret) if app_id and secret else None


def _home_file(settings: Settings) -> Path:
    return settings.data_dir / "feishu_home_chat.txt"


def home_chat(settings: Settings) -> str | None:
    """定时推送（日报、提醒）发到哪个会话：notify.feishu_chat_id，没填则取最近私聊过机器人的会话。"""
    if settings.notify.feishu_chat_id:
        return settings.notify.feishu_chat_id
    try:
        return _home_file(settings).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def set_home_chat(settings: Settings, chat_id: str) -> None:
    try:
        if home_chat(settings) != chat_id and not settings.notify.feishu_chat_id:
            _home_file(settings).write_text(chat_id, encoding="utf-8")
    except OSError as e:
        log.warning("无法保存飞书默认会话：%s", e)


class Client:
    """开放平台 REST：换 tenant_access_token（缓存到过期前 1 分钟）、发文本、上传并发送文件。"""

    def __init__(self, app_id: str, app_secret: str, session: requests.Session | None = None, timeout: float = 20):
        self.app_id, self.app_secret = app_id, app_secret
        self.http = session or requests.Session()
        self.timeout = timeout
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self, refresh: bool = False) -> str:
        with self._lock:
            if refresh or not self._token or time.time() >= self._expires:
                d = self._json(self.http.post(f"{API}/auth/v3/tenant_access_token/internal", timeout=self.timeout,
                                              json={"app_id": self.app_id, "app_secret": self.app_secret}))
                if d.get("code") != 0 or not d.get("tenant_access_token"):
                    raise FeishuError(f"获取 token 失败：{d.get('code')} {d.get('msg')}")
                self._token = d["tenant_access_token"]
                self._expires = time.time() + max(int(d.get("expire", 7200)) - 60, 60)
            return self._token

    @staticmethod
    def _json(r: requests.Response) -> dict:
        try:
            return r.json()
        except ValueError as e:
            raise FeishuError(f"飞书返回了非 JSON 内容：HTTP {r.status_code}") from e

    def call(self, method: str, path: str, **kw) -> dict:
        """调用接口并返回 data；token 失效时刷新后重试一次。kw.files 的文件对象在重试前需要重新定位，由调用方传入 reopen。"""
        reopen = kw.pop("reopen", None)
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self.token(refresh=attempt == 1)}"}
            if attempt and reopen:
                kw["files"] = reopen()
            try:
                d = self._json(self.http.request(method, f"{API}{path}", headers=headers, timeout=self.timeout, **kw))
            except requests.RequestException as e:
                raise FeishuError(f"请求飞书失败：{e}") from e
            if d.get("code") in TOKEN_INVALID and attempt == 0:
                continue
            if d.get("code") != 0:
                raise FeishuError(f"{path} 失败：{d.get('code')} {d.get('msg')}")
            return d.get("data") or {}
        raise FeishuError(f"{path} 失败：token 无效")  # 不可达，满足类型检查

    def _send(self, msg_type: str, content: dict, chat_id: str | None, reply_to: str | None) -> None:
        body = {"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)}
        if reply_to:
            self.call("POST", f"/im/v1/messages/{reply_to}/reply", json=body)
        elif chat_id:
            # 会话 ID（oc_）、用户 open_id（ou_）、union_id（on_）都可以作为接收方
            kind = {"ou_": "open_id", "on_": "union_id"}.get(chat_id[:3], "chat_id")
            self.call("POST", "/im/v1/messages", params={"receive_id_type": kind}, json={**body, "receive_id": chat_id})
        else:
            raise FeishuError("没有指定接收会话")

    def send_text(self, text: str, chat_id: str | None = None, reply_to: str | None = None) -> None:
        self._send("text", {"text": truncate_bytes(text, TEXT_LIMIT)}, chat_id, reply_to)

    def send_file(self, path: Path, chat_id: str | None = None, reply_to: str | None = None) -> None:
        def files():
            return {"file": (path.name, open(path, "rb"))}
        up = files()
        try:
            data = self.call("POST", "/im/v1/files", data={"file_type": "stream", "file_name": path.name}, files=up, reopen=files)
        finally:
            for _, fh in up.values():
                fh.close()
        self._send("file", {"file_key": data["file_key"]}, chat_id, reply_to)


def client(settings: Settings) -> Client | None:
    cred = credentials(settings)
    return Client(*cred) if cred else None


# ---------------------------------------------------------------- 指令解析

@dataclass(frozen=True)
class Command:
    kind: str                  # research | report | check | status | help
    market: str | None = None  # research 的市场提示：HK / A / US
    query: str = ""


_MENTION = re.compile(r"@_user_\d+|@\S+?\s")      # 飞书文本里的 @ 占位符（@_user_1）
_MARKETS = [("美股", "US"), ("港股", "HK"), ("a股", "A"), ("沪深", "A"), ("沪市", "A"), ("深市", "A"), ("us", "US"), ("hk", "HK")]
_RESEARCH = ("深度研究", "深度报告", "研究")
_REPORT = ("发送报告", "发报告", "报告")
_STATUS = ("任务状态", "状态", "进度")
_CHECK = ("立即检查", "检查")
_HELP = ("帮助", "help", "?", "？", "使用说明")

HELP = ("可用指令：\n"
        "· 深度研究 美股AAPL / 深度研究 港股700 / 深度研究 A股600519 / 深度研究 公司名（市场前缀可省略）\n"
        "  已在观察清单的公司直接生成深度报告；不在清单的会先写入观察清单并开始追踪。完成后把 HTML 发到这里\n"
        "· 发送报告 公司名：直接发该公司最新一版报告（不重新生成）\n"
        "· 立即检查 [公司名]：对追踪中的公司抓取、分级后按新依据修正估值（不写公司名 = 全部追踪中的公司），结果发回这里\n"
        "· 任务状态：查看正在运行和排队的任务\n"
        "深度报告每家要数分钟，同一时间只运行一个，其余排队。")


def parse(text: str) -> Command | None:
    t = _MENTION.sub(" ", (text or "").replace("　", " ")).strip()
    if not t:
        return None
    low = t.lower()
    if low in _HELP:
        return Command("help")
    if low in _STATUS:
        return Command("status")
    for kind, words in (("research", _RESEARCH), ("report", _REPORT), ("check", _CHECK)):
        for w in words:
            if low.startswith(w):
                rest = t[len(w):].strip(" :：")
                market = None
                for label, m in _MARKETS:
                    tail = rest[len(label):len(label) + 1]
                    # 英文前缀要后面是空格或数字，免得把 USB 之类的代码拆开
                    if rest.lower().startswith(label) and (not label.isascii() or label == "a股" or not tail or tail.isdigit() or tail == " "):
                        market, rest = m, rest[len(label):].strip(" :：")
                        break
                return Command(kind, market if kind == "research" else None, rest)
    return None


# ---------------------------------------------------------------- 指令处理

@dataclass(frozen=True)
class Incoming:
    message_id: str
    chat_id: str
    chat_type: str             # p2p | group
    sender: str                # open_id
    text: str
    mentioned: bool = False
    sender_ids: tuple[str, ...] = ()   # 发送者的其他 ID（user_id、union_id；白名单里写哪种都认）


class Bot:
    """处理一条收到的消息：校验发送者 → 解析指令 → 启动任务或回答。holder 提供 .app 与 .config_path；client 可替换（测试）。"""

    def __init__(self, holder, client: Client):
        self.holder, self.client = holder, client
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = threading.Lock()

    @property
    def app(self):
        return self.holder.app

    def _fresh(self, message_id: str) -> bool:
        """飞书在未及时确认时会重发事件，同一条消息只处理一次。"""
        with self._seen_lock:
            if message_id in self._seen:
                return False
            self._seen[message_id] = None
            while len(self._seen) > 500:
                self._seen.popitem(last=False)
            return True

    def handle(self, msg: Incoming) -> None:
        if not self._fresh(msg.message_id):
            return
        if msg.chat_type != "p2p" and not msg.mentioned:
            return                                   # 群里只响应 @机器人 的消息
        reply = lambda text: self.client.send_text(text, reply_to=msg.message_id)  # noqa: E731
        try:
            allowed = self.app.settings.notify.feishu_allowed_users
            if not any(i in allowed for i in (msg.sender, *msg.sender_ids)):
                reply(f"没有权限。你的 open_id 是 {msg.sender}，请管理员把它加入 config.yaml 的 notify.feishu_allowed_users 后再试。")
                return
            if msg.chat_type == "p2p":
                set_home_chat(self.app.settings, msg.chat_id)       # 定时推送的默认会话 = 最近私聊过机器人的会话
            cmd = parse(msg.text)
            if cmd is None:
                reply("没看懂这条指令。\n" + HELP)
            elif cmd.kind == "help":
                reply(HELP)
            elif cmd.kind == "status":
                reply(self.status_text())
            elif cmd.kind == "report":
                self.send_report(msg, cmd.query)
            elif cmd.kind == "check":
                reply(self.start_check(msg, cmd.query))
            else:
                reply(self.start_research(msg, cmd))
        except (FeishuError, ValueError, jobs.JobError) as e:
            log.warning("飞书指令处理失败：%s", e)
            try:
                reply(f"✗ {e}")
            except FeishuError:
                pass
        except Exception as e:  # noqa: BLE001 - 单条消息出错不能让监听线程退出
            log.exception("飞书指令处理出错")
            try:
                reply(f"✗ 处理出错：{e}")
            except FeishuError:
                pass

    # -- 深度研究
    def start_research(self, msg: Incoming, cmd: Command) -> str:
        app = self.app
        query = cmd.query.strip()
        if not query:
            raise ValueError("请带上股票，例如：深度研究 美股AAPL")
        c = app.watchlist.get(query)
        if c is not None and cmd.market and not c.listing(cmd.market):
            c = None
        if c is not None:
            task, args, label = "dossier", {"companies": [c.id]}, f"{c.name}（观察清单中已有，直接生成深度报告）"
            market, code = None, None
        else:
            market, code, name = self._resolve(query, cmd.market)
            if hit := newco.existing(app, market, code):
                task, args, label = "dossier", {"companies": [hit.id]}, f"{hit.name}（观察清单中已有，直接生成深度报告）"
            else:
                data = newco.build(app, market, code)    # 联网查公司资料；查不到时抛 ValueError，不入队
                task, args = "add_company", {"listing": (market, code)}
                label = f"{newco.describe(data)}\n新公司：先写入观察清单，再抓取 → 年报 → 分级 → 画像 → 深度报告 → 开始追踪"
        steps, title, _ = jobs.build(app.watchlist, app.settings.llm.profiles, task, args.get("companies"), listing=args.get("listing"))
        for j in app.db.jobs(limit=200, statuses=("queued", "running")):
            if j["task"] == task and j["title"] == title:
                return f"「{title}」已经在{'运行' if j['status'] == 'running' else '排队'}（任务 #{j['id']}），完成后会把报告发回这里。"
        ahead = len([j for j in app.db.jobs(limit=200, statuses=("queued", "running"))
                     if (jobs.TASKS.get(j["task"]) and jobs.TASKS[j["task"]].group) == jobs.TASKS[task].group])
        jid = jobs.submit(app, task, args.get("companies"), source=SOURCE, note=f"飞书 {msg.sender}", listing=args.get("listing"))
        app.db.kv_set(f"feishu_origin:{jid}", json.dumps({"chat_id": msg.chat_id, "message_id": msg.message_id}))
        queue = f"前面还有 {ahead} 个深度报告类任务，将排队等待。" if ahead else "马上开始。"
        return f"已受理：深度研究 {label}\n任务 #{jid}，{queue}每家需要数分钟，完成后把 HTML 发到这里。"

    def _resolve(self, query: str, market: str | None) -> tuple[str, str, str]:
        if market and (hit := newco.infer(query)) is None:
            try:
                m, c = newco.normalize(market, query)     # 带市场前缀的代码（美股 AAPL）无需联网识别
                return m, c, query
            except ValueError:
                pass
        m, c, name = newco.resolve(self.app, query)
        if market and m != market:
            raise ValueError(f"「{query}」识别为 {m} {c}（{name}），与你指定的市场不一致")
        return m, c, name

    # -- 立即检查（标的页面的「立即检查」，jobs 里的 target_run）
    def start_check(self, msg: Incoming, query: str) -> str:
        app = self.app
        ids = []
        if query.strip():
            c = app.watchlist.get(query.strip())
            if c is None:
                raise ValueError(f"观察清单里找不到「{query.strip()}」")
            if not (t := app.db.tracker(c.id)) or not t["enabled"]:
                raise ValueError(f"{c.name} 不在追踪中，先发「深度研究 {c.name}」建立估值基线")
            ids = [c.id]
        elif not app.db.trackers():
            raise ValueError("没有追踪中的公司")
        _, title, _ = jobs.build(app.watchlist, app.settings.llm.profiles, "target_run", ids)
        for j in app.db.jobs(limit=200, statuses=("queued", "running")):
            if j["task"] == "target_run" and j["title"] == title:
                return f"「{title}」已经在{'运行' if j['status'] == 'running' else '排队'}（任务 #{j['id']}），完成后会把结果发回这里。"
        jid = jobs.submit(app, "target_run", ids, source=SOURCE, note=f"飞书 {msg.sender}")
        app.db.kv_set(f"feishu_origin:{jid}", json.dumps({"chat_id": msg.chat_id, "message_id": msg.message_id}))
        return f"已受理：{title}\n任务 #{jid}，抓取、分级后修正估值，完成后把结果发到这里。"

    # -- 发送已有报告
    def send_report(self, msg: Incoming, query: str) -> None:
        if not query.strip():
            raise ValueError("请带上公司，例如：发送报告 英伟达")
        c = self.app.watchlist.get(query.strip())
        if c is None:
            raise ValueError(f"观察清单里找不到「{query.strip()}」")
        d = self.app.db.latest_dossier(c.id)
        if not d or not d.get("html_path") or not Path(d["html_path"]).exists():
            raise ValueError(f"{c.name} 还没有深度报告，可以发「深度研究 {c.name}」生成")
        self.client.send_text(f"{c.name} 最新深度报告（{d['created_at'][:10]} 生成）", reply_to=msg.message_id)
        for p in report_files(d):
            self.client.send_file(p, reply_to=msg.message_id)

    # -- 任务状态
    def status_text(self) -> str:
        rows = self.app.db.jobs(limit=200, statuses=("running", "queued"))
        if not rows:
            return "当前没有运行或排队的任务。"
        return "\n".join(f"#{j['id']} {'运行中' if j['status'] == 'running' else '排队'}｜{j['title']}" for j in reversed(rows[:10]))


def report_files(dossier: dict) -> list[Path]:
    return [p for p in (Path(dossier[k]) for k in ("html_path", "pdf_path") if dossier.get(k)) if p.exists()]


# ---------------------------------------------------------------- 任务结束后推回结果

def _tail(path: str | None, lines: int = 12) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
    except OSError:
        return ""
    return "\n".join(x for x in text.strip().splitlines()[-lines:] if x.strip())


def job_finished(app, job_id: int, status: str) -> None:
    """jobrun 包装进程在任务结束时调用：来自飞书的任务把结果发回原会话。其他来源的任务什么也不做。"""
    raw = app.db.kv_get(f"feishu_origin:{job_id}")
    if not raw:
        return
    app.db.kv_delete(f"feishu_origin:{job_id}")
    cli = client(app.settings)
    if cli is None:
        return
    origin = json.loads(raw)
    job = app.db.job(job_id) or {}
    to = {"chat_id": origin["chat_id"], "reply_to": origin["message_id"]}
    try:
        if job.get("task") == "target_run" and status == "done":
            body = "\n".join(x for x in _tail(job.get("log_path"), 30).splitlines() if not x.startswith("【任务 #"))
            cli.send_text(f"✓ 立即检查完成（任务 #{job_id}）：{job.get('title', '')}\n{body}\n（{tracker.DISCLAIMER}）", **to)
            return
        if status != "done":
            word = {"failed": "失败", "cancelled": "已取消"}.get(status, status)
            cli.send_text(f"✗ 任务 #{job_id} {word}：{job.get('title', '')}\n{_tail(job.get('log_path'))}", **to)
            return
        d = _new_dossier(app, job)
        if d is None:
            cli.send_text(f"任务 #{job_id} 已结束，但没有生成新的深度报告：{job.get('title', '')}\n{_tail(job.get('log_path'))}", **to)
            return
        c = app.watchlist.get(d["company_id"])
        cli.send_text(f"✓ {c.name if c else d['company_id']} 深度报告已生成（任务 #{job_id}）。HTML 请下载后用浏览器打开。", **to)
        for p in report_files(d):
            cli.send_file(p, **to)
    except FeishuError as e:
        log.warning("任务 #%s 的结果推回飞书失败：%s", job_id, e)


def _new_dossier(app, job: dict) -> dict | None:
    """任务期间生成的最新深度报告：已有公司按任务的 company_id，新公司按任务标题里的市场与代码找。"""
    cid = job.get("company_id")
    if not cid:
        m = re.search(r"\b(HK|A|US)\s+([0-9A-Z.\-]+)\s*$", job.get("title", ""))
        if m and (c := newco.existing(app, m.group(1), m.group(2))):
            cid = c.id
    d = app.db.latest_dossier(cid) if cid else None
    return d if d and d["created_at"] >= (job.get("created_at") or "") else None


# ---------------------------------------------------------------- 长连接监听

class Listener:
    """stock web 里的后台线程：飞书长连接收到消息后交给线程池处理（事件回调必须立刻返回，否则飞书会重发）。"""

    def __init__(self, holder):
        self.holder = holder
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feishu")
        self.bot: Bot | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        cli = client(self.holder.app.settings)
        if cli is None:
            return False
        self.bot = Bot(self.holder, cli)
        self._thread = threading.Thread(target=self._run, args=credentials(self.holder.app.settings), name="feishu-ws", daemon=True)
        self._thread.start()
        log.warning("飞书机器人已启动（长连接）")
        return True

    def stop(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)   # 长连接线程是守护线程，随进程退出

    def _on_event(self, data) -> None:
        try:
            ev = data.event
            m, sid = ev.message, ev.sender.sender_id
            sender, others = sid.open_id, tuple(x for x in (sid.user_id, sid.union_id) if x)
            if m.message_type != "text":
                return
            text = json.loads(m.content).get("text", "")
            msg = Incoming(m.message_id, m.chat_id, m.chat_type, sender, text, bool(m.mentions), others)
        except (AttributeError, ValueError, TypeError):
            log.exception("无法解析飞书消息事件")
            return
        self.pool.submit(self.bot.handle, msg)

    def _run(self, app_id: str, app_secret: str) -> None:
        import asyncio  # noqa: PLC0415

        import lark_oapi as lark  # noqa: PLC0415 - 只有启用飞书机器人时才加载
        from lark_oapi.ws import client as ws_client  # noqa: PLC0415

        # SDK 在模块级持有一个事件循环；本线程不是主线程，换成线程自己的循环，免得与 uvicorn 的循环冲突
        ws_client.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ws_client.loop)
        handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._on_event).build()
        try:
            lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.WARNING).start()
        except Exception:  # noqa: BLE001 - 凭据错误等：记日志，不影响 web
            logging.getLogger("stockagent").exception("飞书长连接退出（检查 APP ID / APP SECRET，以及应用是否已启用机器人并订阅 im.message.receive_v1）")
