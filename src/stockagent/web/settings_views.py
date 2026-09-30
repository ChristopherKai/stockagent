"""设置页：LLM 连接（profile、任务路由、key、模型列表、连接测试）、日程、标的追踪与通用设置、密钥、原始配置。

所有修改都写回 config.yaml / watchlist.yaml / .env（保留注释，写前完整校验并备份），随后重新加载配置。
API key 只写不读：页面只显示是否已设置与掩码。
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .. import jobs, scheduler
from ..cli import LLM_TASKS
from ..config import LLMProfile, Settings
from ..cron import Cron
from ..utils import CST
from . import forms, yamledit
from .security import verify_csrf
from .server import page
from .views import back, holder

router = APIRouter(prefix="/settings", dependencies=[Depends(verify_csrf)])

TASK_LABEL = {"triage": "事件分级", "stock_brief": "个股研判", "market_brief": "今日要点", "profile": "公司画像",
              "research": "深度报告·研究 agent", "dossier": "深度报告·撰写与跟踪卡", "monitor": "证伪条件检查",
              "tracker": "标的追踪·估值修正", "chat": "报告研读对话"}
PROFILE_LABELS = {
    "protocol": ("协议", "openai：OpenAI 兼容接口（本地网关、DeepSeek、通义、Ollama 等）；anthropic：Claude 官方 SDK；mock：离线模拟"),
    "base_url": ("接口地址", "如 http://127.0.0.1:18080/v1；anthropic 协议留空为官方地址"),
    "model": ("模型名", ""),
    "api_key_env": ("key 所在的环境变量名", "key 本身写在 .env，不写进 config.yaml"),
    "tool_mode": ("工具调用方式", "native：接口原生工具调用；text：服务不支持时用 <tool_call> 文本协议"),
    "json_mode": ("JSON 模式", "服务不支持 response_format 时关闭"),
    "max_tokens": ("单次输出上限", "达到上限时文本会自动续写"),
    "max_input_chars": ("单次输入字数上限", "留空 = 不限制"),
    "timeout": ("超时（秒）", ""),
    "max_retries": ("SDK 重试次数", ""),
    "fallback": ("备用 profile", "本模型不可用（连接失败、5xx、限流、上游拒绝）时自动改用"),
    "temperature": ("temperature", "留空 = 不发送（Claude 新模型不接受）"),
    "max_tokens_param": ("输出上限参数名", "部分接口要求 max_completion_tokens"),
    "auth": ("认证头（anthropic）", "中转站要求 Authorization: Bearer 时选 bearer"),
    "effort": ("effort（anthropic）", ""),
    "thinking": ("thinking（anthropic）", "claude-haiku-4-5 需设为 off"),
    "structured_output": ("结构化输出（anthropic）", "中转站不支持 output_config 时关闭"),
    "refusal_fallback": ("拒答自动回退（anthropic）", "中转站不支持时关闭"),
    "prompt_cache": ("提示缓存（anthropic）", ""),
}
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,40}$")
SEARCH_KEYS = {"EXA_API_KEY": "Exa 搜索", "BOCHA_API_KEY": "博查搜索", "TAVILY_API_KEY": "Tavily 搜索", "SERPER_API_KEY": "Serper 搜索"}
SECTIONS = {"sources": "数据源", "report": "简报", "agent": "研究 agent", "notify": "推送", "web": "WebUI", "chat": "报告研读对话"}
for k, v in PROFILE_LABELS.items():
    forms.LABELS[f"profile.{k}"] = v


def _config(request: Request):
    h = holder(request)
    data, etag = yamledit.load(h.config_path)
    return h, data, etag


def _save(request: Request, h, data, etag, ok: str, url: str) -> RedirectResponse:
    try:
        yamledit.save_config(h.config_path, data, etag, h.backup_dir)
        h.reload()
    except yamledit.EditError as e:
        return back(url, err=str(e))
    return back(url, ok=ok)


def _env_value(h, name: str | None) -> str | None:
    if not name:
        return None
    return (dotenv_values(h.env_path).get(name) if h.env_path.exists() else None) or os.environ.get(name)


@router.get("")
def settings_home():
    return RedirectResponse("/settings/llm", status_code=303)


# ---------------------------------------------------------------- LLM

@router.get("/llm")
def llm_page(request: Request):
    h = holder(request)
    cfg = h.app.settings.llm
    _, etag = yamledit.load(h.config_path)
    rows = []
    for name, p in cfg.profiles.items():
        rows.append({"name": name, "p": p, "key": yamledit.mask(_env_value(h, p.api_key_env)) if p.api_key_env else "—",
                     "reason": h.app.llm._profile_reason(name, p),
                     "used": [t for t in LLM_TASKS if cfg.route(t)[0] == name] + (["默认"] if cfg.default_profile == name else [])})
    routes = {t: (cfg.tasks.get(t).profile if hasattr(cfg.tasks.get(t), "profile") else cfg.tasks.get(t)) for t in LLM_TASKS}
    return page(request, "settings_llm.html", rows=rows, routes=routes, task_label=TASK_LABEL, default=cfg.default_profile,
                profiles=list(cfg.profiles), etag=etag)


@router.post("/llm/routing")
async def llm_routing(request: Request):
    h, data, _ = _config(request)
    form = await request.form()
    llm = data["llm"]
    names = set(h.app.settings.llm.profiles)
    default = form.get("default_profile")
    if default not in names:
        return back("/settings/llm", err="默认 profile 不存在")
    llm["default_profile"] = default
    tasks = llm.setdefault("tasks", CommentedMap())
    for t in LLM_TASKS:
        v = form.get(f"task_{t}") or ""
        if v and v not in names:
            return back("/settings/llm", err=f"{TASK_LABEL[t]}：profile {v} 不存在")
        if not v:
            tasks.pop(t, None)
        elif isinstance(tasks.get(t), dict):
            tasks[t]["profile"] = v  # 保留 effort / max_tokens 等覆盖项
        else:
            tasks[t] = v
    return _save(request, h, data, form.get("etag"), "已保存任务路由", "/settings/llm")


def _profile_fields(p: LLMProfile) -> list[dict]:
    return [f for f in forms.fields("profile", p) if f["name"] != "fallback"]


@router.get("/llm/profile/{name}")
def profile_page(request: Request, name: str):
    h = holder(request)
    profiles = h.app.settings.llm.profiles
    is_new = name == "_new"
    if not is_new and name not in profiles:
        raise HTTPException(404, f"没有名为 {name} 的 profile")
    p = profiles.get(name) or LLMProfile(protocol="openai", model="", api_key_env=None)
    _, etag = yamledit.load(h.config_path)
    return page(request, "settings_profile.html", name=name, is_new=is_new, p=p, fields=_profile_fields(p),
                others=[n for n in profiles if n != name], key=yamledit.mask(_env_value(h, p.api_key_env)) if p.api_key_env else "未设置",
                etag=etag)


@router.post("/llm/profile/{name}")
async def profile_save(request: Request, name: str):
    h, data, _ = _config(request)
    form = await request.form()
    profiles = data["llm"].setdefault("profiles", CommentedMap())
    is_new = name == "_new"
    if is_new:
        name = (form.get("new_name") or "").strip()
        if not PROFILE_NAME_RE.match(name):
            return back("/settings/llm/profile/_new", err="profile 名只能包含字母、数字、-、_")
        if name in profiles:
            return back("/settings/llm/profile/_new", err=f"已有名为 {name} 的 profile")
    current = h.app.settings.llm.profiles.get(name) or LLMProfile(model="x")
    try:
        values = forms.parse("profile", current, form)
    except ValueError as e:
        return back(f"/settings/llm/profile/{name if not is_new else '_new'}", err=str(e))
    fb = (form.get("fallback") or "").strip() or None
    if fb is not None and (fb == name or fb not in profiles):
        return back(f"/settings/llm/profile/{name}", err="备用 profile 不存在或指向自己")
    values["fallback"] = fb
    if values.get("api_key_env") and not yamledit.SECRET_NAME_RE.match(values["api_key_env"]):
        return back(f"/settings/llm/profile/{name}", err="key 的环境变量名需以 _KEY / _TOKEN / _SECRET 结尾，如 LOCAL_LLM_API_KEY")
    defaults = {k: f.default for k, f in LLMProfile.model_fields.items() if not f.is_required()}
    target = profiles.get(name)
    if target is None:
        target = profiles[name] = CommentedMap()
    yamledit.update_map(target, values, defaults)
    target.setdefault("protocol", values["protocol"])
    key = form.get("api_key") or ""
    env_name = values.get("api_key_env")
    try:
        if key.strip() or form.get("clear_key"):
            if not env_name:
                raise yamledit.EditError("要保存 key，先填写「key 所在的环境变量名」")
            yamledit.set_env(h.env_path, env_name, None if form.get("clear_key") else key.strip())
    except yamledit.EditError as e:
        return back(f"/settings/llm/profile/{name}", err=str(e))
    return _save(request, h, data, form.get("etag"), f"已保存 profile {name}", f"/settings/llm/profile/{name}")


@router.post("/llm/profile/{name}/delete")
async def profile_delete(request: Request, name: str):
    h, data, _ = _config(request)
    form = await request.form()
    cfg = h.app.settings.llm
    if name not in cfg.profiles:
        return back("/settings/llm", err=f"没有名为 {name} 的 profile")
    refs = (["默认 profile"] if cfg.default_profile == name else []) + \
        [TASK_LABEL.get(t, t) for t in cfg.tasks if cfg.route(t)[0] == name] + \
        [f"{n} 的备用" for n, p in cfg.profiles.items() if p.fallback == name]
    if refs:
        return back(f"/settings/llm/profile/{name}", err=f"{name} 仍被使用（{'、'.join(refs)}），先改掉这些引用再删除")
    del data["llm"]["profiles"][name]
    return _save(request, h, data, form.get("etag"), f"已删除 profile {name}（.env 里的 key 保留）", "/settings/llm")


@router.post("/llm/profile/{name}/test")
def profile_test(request: Request, name: str):
    app = holder(request).app
    try:
        jid = jobs.submit(app, "llm_test", profile=name, source="web")
    except jobs.JobError as e:
        return back(f"/settings/llm/profile/{name}", err=str(e))
    return back(f"/jobs/{jid}", ok=f"已提交连接测试：{name}")


@router.get("/llm/profile/{name}/models")
def profile_models(request: Request, name: str):
    """列出接口上可用的模型（按按钮时才请求，超时 10 秒）。"""
    h = holder(request)
    p = h.app.settings.llm.profiles.get(name)
    if p is None:
        raise HTTPException(404, f"没有名为 {name} 的 profile")
    key = _env_value(h, p.api_key_env)
    try:
        if p.protocol == "openai":
            import openai  # noqa: PLC0415

            models = [m.id for m in openai.OpenAI(api_key=key or "not-needed", base_url=p.base_url, timeout=10, max_retries=0).models.list()]
        elif p.protocol == "anthropic":
            import anthropic  # noqa: PLC0415

            kwargs = {"base_url": p.base_url or "https://api.anthropic.com", "timeout": 10, "max_retries": 0}
            if key:
                kwargs["auth_token" if p.auth == "bearer" else "api_key"] = key
            models = [m.id for m in anthropic.Anthropic(**kwargs).models.list(limit=100)]
        else:
            models = ["mock"]
    except Exception as e:  # noqa: BLE001 - 任何错误都告诉页面
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:200]}"}, status_code=502)
    return JSONResponse({"models": sorted(models)})


# ---------------------------------------------------------------- 日程

@router.get("/schedule")
def schedule_page(request: Request):
    h = holder(request)
    _, etag = yamledit.load(h.config_path)
    return page(request, "settings_schedule.html", cfg=h.app.settings.scheduler, runs=scheduler.next_runs(h.app),
                tasks=jobs.TASKS, etag=etag, root=h.root)


@router.post("/schedule")
async def schedule_save(request: Request):
    h, data, _ = _config(request)
    form = await request.form()
    rows, errors = [], []
    for i in range(int(form.get("rows") or 0) + 1):
        name = (form.get(f"name_{i}") or "").strip()
        if not name or form.get(f"delete_{i}"):
            continue
        task, cron = form.get(f"task_{i}") or "", (form.get(f"cron_{i}") or "").strip()
        args = (form.get(f"args_{i}") or "").split()
        try:
            Cron(cron)
            flags, companies = scheduler.split_args(args)
            jobs.build(h.app.watchlist, h.app.settings.llm.profiles, task, companies, flags)
        except (ValueError, jobs.JobError) as e:
            errors.append(f"「{name}」：{e}")
            continue
        job = CommentedMap([("name", name), ("task", task), ("args", CommentedSeq(args)), ("cron", cron),
                            ("enabled", bool(form.get(f"enabled_{i}")))])
        job["args"].fa.set_flow_style()
        job.fa.set_flow_style()
        rows.append(job)
    if errors:
        return back("/settings/schedule", err="；".join(errors))
    if len({r["name"] for r in rows}) != len(rows):
        return back("/settings/schedule", err="日程名称不能重复")
    sched = data.setdefault("scheduler", CommentedMap())
    try:
        catchup = int(form.get("catchup_minutes") or 120)
    except ValueError:
        return back("/settings/schedule", err="补跑窗口必须是整数分钟")
    yamledit.update_map(sched, {"enabled": bool(form.get("enabled")), "catchup_minutes": catchup})
    sched["jobs"] = CommentedSeq(rows)
    resp = _save(request, h, data, form.get("etag"), "已保存日程", "/settings/schedule")
    now = datetime.now(CST).replace(second=0, microsecond=0)
    for r in rows:  # 保存时刻之前错过的时点不补跑
        h.app.db.kv_set(f"sched_last:{r['name']}", now.isoformat())
    return resp


# ---------------------------------------------------------------- 标的追踪与通用设置

def _section_models(settings: Settings) -> dict:
    return {s: getattr(settings, s) for s in ("tracker", *SECTIONS)}


@router.get("/tracker")
def tracker_settings(request: Request):
    h = holder(request)
    _, etag = yamledit.load(h.config_path)
    return page(request, "settings_form.html", title="标的追踪", sections=[("tracker", "标的追踪", forms.fields("tracker", h.app.settings.tracker))],
                etag=etag, active="tracker")


@router.get("/general")
def general_settings(request: Request):
    h = holder(request)
    _, etag = yamledit.load(h.config_path)
    models = _section_models(h.app.settings)
    return page(request, "settings_form.html", title="通用设置", etag=etag, active="general",
                sections=[(s, label, forms.fields(s, models[s])) for s, label in SECTIONS.items()])


@router.post("/section/{section}")
async def section_save(request: Request, section: str):
    h, data, _ = _config(request)
    models = _section_models(h.app.settings)
    if section not in models:
        raise HTTPException(404, "没有这一节设置")
    form = await request.form()
    url = "/settings/tracker" if section == "tracker" else "/settings/general"
    try:
        values = forms.parse(section, models[section], form)
    except ValueError as e:
        return back(url, err=str(e))
    model = models[section]
    defaults = {k: f.default for k, f in type(model).model_fields.items() if not f.is_required()}
    target = data.get(section)
    if target is None:
        target = data[section] = CommentedMap()
    yamledit.update_map(target, values, defaults)
    label = "标的追踪" if section == "tracker" else SECTIONS[section]
    return _save(request, h, data, form.get("etag"), f"已保存「{label}」设置", url)


# ---------------------------------------------------------------- 密钥

def _secret_names(h) -> dict[str, list[str]]:
    s = h.app.settings
    names: dict[str, list[str]] = {}
    for n, p in s.llm.profiles.items():
        if p.api_key_env:
            names.setdefault(p.api_key_env, []).append(f"模型 profile {n}")
    for field, label in (("feishu_webhook_env", "飞书 webhook"), ("feishu_secret_env", "飞书签名"),
                         ("wecom_webhook_env", "企业微信 webhook"), ("telegram_token_env", "Telegram token")):
        if env := getattr(s.notify, field):
            names.setdefault(env, []).append(label)
    for env, label in SEARCH_KEYS.items():
        names.setdefault(env, []).append(label)
    return names


@router.get("/secrets")
def secrets_page(request: Request):
    h = holder(request)
    rows = [{"name": n, "used": used, "mask": yamledit.mask(_env_value(h, n)), "valid": bool(yamledit.SECRET_NAME_RE.match(n))}
            for n, used in _secret_names(h).items()]
    return page(request, "settings_secrets.html", rows=rows, env_path=h.env_path)


@router.post("/secrets")
async def secrets_save(request: Request):
    h = holder(request)
    form = await request.form()
    allowed = _secret_names(h)
    changed = []
    try:
        for name in allowed:
            if form.get(f"clear_{name}"):
                yamledit.set_env(h.env_path, name, None)
                changed.append(f"清除 {name}")
            elif (v := (form.get(f"val_{name}") or "").strip()):
                yamledit.set_env(h.env_path, name, v)
                changed.append(f"设置 {name}")
    except yamledit.EditError as e:
        return back("/settings/secrets", err=str(e))
    h.reload()
    return back("/settings/secrets", ok=("已" + "、".join(changed) + "（写入 .env，新任务立即生效）") if changed else "没有改动")


# ---------------------------------------------------------------- 原始配置

def _raw_path(h, file: str):
    if file == "watchlist":
        return h.watchlist_path
    if file == "config":
        return h.config_path
    raise HTTPException(404, "只能编辑 config 或 watchlist")


@router.get("/raw")
def raw_page(request: Request, file: str = "config"):
    h = holder(request)
    path = _raw_path(h, file)
    text = path.read_text(encoding="utf-8")
    return page(request, "settings_raw.html", file=file, text=text, etag=yamledit.digest(text), path=path)


@router.post("/raw")
async def raw_save(request: Request):
    h = holder(request)
    form = await request.form()
    file = form.get("file") or "config"
    path = _raw_path(h, file)
    text = (form.get("text") or "").replace("\r\n", "\n")
    try:
        yamledit.save_raw(path, text, form.get("etag"), h.backup_dir, file)
        h.reload()
    except yamledit.EditError as e:
        return page(request, "settings_raw.html", status_code=400, file=file, text=text, etag=form.get("etag"), path=path, error=str(e))
    return back(f"/settings/raw?file={file}", ok=f"已保存 {path.name}（原文件已备份到 {h.backup_dir}）")
