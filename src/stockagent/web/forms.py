"""根据 pydantic 配置模型自动生成设置表单（布尔 / 数字 / 文本 / 单选 / 多选 / 多行列表），并把表单解析回配置值。
嵌套结构（字典、子模型）不在这里编辑，可到「原始配置」页修改。"""

from __future__ import annotations

import types
import typing
from typing import Literal, get_args, get_origin

LABELS: dict[str, tuple[str, str]] = {
    # 数据源
    "sources.announcements_lookback_days": ("首次抓取公告回溯天数", "之后按水位增量抓取；调大不会回补已抓过的区间"),
    "sources.news_lookback_days": ("新闻回溯天数", ""),
    "sources.price_history_days": ("日线历史长度（天）", ""),
    "sources.price_sources": ("行情源顺序", "前一个失败自动换下一个"),
    "sources.request_delay": ("同一网站请求间隔（秒）", "太小会被东方财富断开"),
    "sources.eastmoney_news": ("东方财富资讯", ""),
    "sources.eastmoney_news_page_size": ("东方财富资讯每次条数", ""),
    "sources.google_news": ("Google 新闻", "在中国大陆需要能访问 Google"),
    "sources.google_news_locales": ("Google 新闻语言", ""),
    "sources.google_news_max_per_query": ("每个检索词最多保留条数", ""),
    "sources.plan_keywords_max": ("每次检索的产业链跟踪词上限", "来自深度报告的跟踪计划"),
    "sources.blocked_news_sources": ("屏蔽的新闻来源", "每行一个"),
    "sources.blocked_title_patterns": ("屏蔽的标题（正则）", "每行一个"),
    "sources.auto_download_reports": ("自动下载年报 / 中期报告", ""),
    "sources.sec_user_agent": ("SEC User-Agent", "跟踪美股时必填：姓名 邮箱（美股公告与 XBRL 报表都要用）"),
    "sources.ccass": ("记录港股 CCASS 持股", "每天一份中央结算系统持股快照（港交所，T+1 可查）"),
    "sources.ccass_alert_pct": ("CCASS 异动阈值（百分点）", "某参与者持股变化占已发行股份的比例达到它时，生成一条数据异动进入分级"),
    "sources.ccass_keep": ("CCASS 快照保存前几名参与者", ""),
    "sources.sec_forms": ("抓取的 SEC 文件类型", "每行一个"),
    # 简报
    "report.lookback_hours": ("简报事件窗口（小时）", ""),
    "report.min_materiality": ("简报显示的最低重要性", "1–5"),
    "report.max_items_per_company": ("每家公司最多显示事件数", ""),
    "report.triage_batch_size": ("每批分级条数", ""),
    "report.profile_max_doc_chars": ("公司画像最多送入字数", ""),
    "report.auto_profile": ("资料更新后自动重做画像", ""),
    "report.dossier_stale_days": ("深度报告过期提示（天）", ""),
    "report.color_scheme": ("涨跌颜色", "red_up 红涨绿跌 / green_up 绿涨红跌"),
    # 研究 agent
    "agent.max_steps": ("研究子任务最多工具轮数", ""),
    "agent.tool_result_chars": ("单次工具结果最多字数", ""),
    "agent.web_search": ("网页搜索源（按顺序）", "API 类填了 key 才启用；免费源前三个轮流使用"),
    "agent.parallel_sections": ("研究子任务并行", "模型服务支持并发时开启"),
    # 推送
    "notify.feishu_webhook_env": ("飞书 webhook 环境变量名", "值在「密钥」页设置"),
    "notify.feishu_secret_env": ("飞书签名密钥环境变量名", ""),
    "notify.wecom_webhook_env": ("企业微信 webhook 环境变量名", ""),
    "notify.telegram_token_env": ("Telegram token 环境变量名", ""),
    "notify.telegram_chat_id": ("Telegram chat id", "先在 Telegram 里给你的 bot 发一条消息，再用浏览器打开 https://api.telegram.org/bot<token>/getUpdates，"
                                                    "返回里 message.chat.id 的数字就是它（群组为负数）"),
    "notify.alert_min_materiality": ("盘中提醒的最低重要性", "1–5"),
    # WebUI
    "web.port": ("端口", "修改后重启 stock web 生效"),
    "web.max_jobs": ("同时运行的后台任务数", "深度报告另有全局互斥：同一时间只跑一个"),
    "web.upload_max_mb": ("上传文档大小上限（MB）", ""),
    # 报告研读对话
    "chat.web_search": ("对话可用网页搜索", "深度报告运行期间自动暂停，避免争抢搜索源"),
    "chat.max_steps": ("每次回答最多工具轮数", ""),
    "chat.max_turns": ("追问时带上的历史轮数", "另受模型 profile 的 max_input_chars 限制"),
    # 标的追踪
    "tracker.min_materiality": ("触发修正的最低事件重要性", "1–5；分级低于它的事件只展示，不进入修正"),
    "tracker.max_events": ("每次修正最多处理的依据数", "其余留到下次"),
    "tracker.max_steps": ("修正 agent 最多工具轮数", ""),
    "tracker.web_search": ("修正 agent 可用网页搜索", "深度报告运行期间自动暂停，避免争抢搜索源"),
    "tracker.target": ("目标价口径", "expected 概率加权 / base 基准情景"),
    "tracker.notify_min_change": ("推送阈值：目标价变动比例", "0.05 表示 5%"),
    "tracker.flag_change": ("「大幅修正」标注阈值", "单项假设变动超过该比例时标注"),
    "tracker.auto_rebaseline": ("有新深度报告时自动重建基线", ""),
    "tracker.redo_materiality": ("自动重跑深度报告的事件重要性", "达到该值且修正判为实质影响时排队重跑"),
    "tracker.redo_on_broken": ("论点失效时自动重跑深度报告", ""),
    "tracker.redo_cooldown_days": ("自动重跑冷却天数", "同一公司在冷却期内不重复排队"),
}


def _kind(ann) -> tuple[str, list[str]]:
    origin, args = get_origin(ann), get_args(ann)
    if ann is bool:
        return "bool", []
    if ann is int:
        return "int", []
    if ann is float:
        return "float", []
    if ann is str:
        return "str", []
    if origin is Literal:
        return "choice", [str(a) for a in args]
    if origin in (typing.Union, types.UnionType) and type(None) in args:
        inner = [a for a in args if a is not type(None)]
        if len(inner) == 1:
            k, opts = _kind(inner[0])
            return ("opt" + k, opts) if k in ("str", "int", "float", "choice") else ("skip", [])
    if origin is list and args:
        if get_origin(args[0]) is Literal:
            return "multi", [str(a) for a in get_args(args[0])]
        if args[0] is str:
            return "lines", []
    return "skip", []


def fields(section: str, model) -> list[dict]:
    out = []
    for name, f in type(model).model_fields.items():
        kind, options = _kind(f.annotation)
        if kind == "skip":
            continue
        label, help_ = LABELS.get(f"{section}.{name}", (name, ""))
        out.append({"name": name, "kind": kind, "options": options, "label": label, "help": help_,
                    "value": getattr(model, name)})
    return out


def parse(section: str, model, form) -> dict:
    """把表单解析成配置值（只包含本节可编辑的字段）。空的可选字段解析为 None（写回时删除该键）。"""
    values: dict = {}
    for f in fields(section, model):
        key, kind = f"{section}.{f['name']}", f["kind"]
        raw = form.get(key)
        try:
            if kind == "bool":
                values[f["name"]] = raw is not None
            elif kind == "multi":
                values[f["name"]] = [v for v in form.getlist(key) if v in f["options"]]
            elif kind == "lines":
                values[f["name"]] = [x.strip() for x in (raw or "").splitlines() if x.strip()]
            elif kind in ("int", "float"):
                values[f["name"]] = (int if kind == "int" else float)(str(raw).strip())
            elif kind == "optchoice":
                s = (raw or "").strip()
                values[f["name"]] = s if s in f["options"] else None
            elif kind == "choice":
                if raw not in f["options"]:
                    raise ValueError(f"取值必须是 {' / '.join(f['options'])}")
                values[f["name"]] = raw
            elif kind.startswith("opt"):
                s = (raw or "").strip()
                values[f["name"]] = None if not s else {"optint": int, "optfloat": float}.get(kind, str)(s)
            else:
                values[f["name"]] = (raw or "").strip()
        except ValueError as e:
            raise ValueError(f"{f['label']}：{e}") from e
    return values
