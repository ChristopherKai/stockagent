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
    "sources.holdchg": ("大股东 / 董监高持股变动", "港股取港交所权益披露（DI），美股取 Form 4 内部人交易与 Schedule 13D / 13G；stock run 时抓取，达到下列阈值的变动生成事件进入分级；盘中提醒不抓"),
    "sources.holdchg_lookback_days": ("持股变动首次回溯天数", "之后按水位增量抓取（每次再重叠 5 天，申报常晚几天才公布）"),
    "sources.holdchg_hk_min_pct": ("港股持股变化阈值（百分点）", "大股东 / 董事的好仓比例相对上次报告的水平变化达到它才生成事件；被动基金的日常借还、小幅增减不会触发"),
    "sources.holdchg_hk_streak": ("港股连续同向申报份数", "同一申报人连续这么多份同方向买入 / 卖出申报，净额达到持股变化阈值的一半就生成事件（控股股东每天小额增持，单日不起眼，连起来是信号）"),
    "sources.holdchg_hk_insider_value": ("港股董事买卖金额阈值（港元）", "董事 / 最高行政人员一次买卖（同方向合并）达到它就生成事件，不看比例；缺成交价用最近收盘价估算"),
    "sources.holdchg_us_insider_value": ("美股内部人买卖金额阈值（美元）", "只算公开市场买卖（Form 4 的 Buy / Sell），期权行权、授予、税款代扣不算；10b5-1 预设计划卖出在摘要里注明"),
    "sources.holdchg_us_filer_pct": ("美股 13G 持股变化阈值（百分点）", "13G 申报人的持股比例较上次变化达到它或降到 5% 以下才生成事件；13D 与首次 13G 一律生成"),
    "sources.sec_forms": ("抓取的 SEC 文件类型", "每行一个"),
    # 推特 / X
    "twitter.agent_tools": ("研究 agent 可用推特", "深度报告、补缺和标的追踪的 agent 可以按需调用 twitter_search / twitter_user；推文只作线索，不作事实依据"),
    "twitter.news_feed": ("推特新闻源", "stock run 时自动抓取：公司的推特账号（在公司编辑页填写）与检索词，新帖子进入事件分级；盘中提醒不抓"),
    "twitter.max_results": ("agent 单次工具调用最多条数", ""),
    "twitter.days": ("agent 检索默认回溯天数", ""),
    "twitter.min_likes": ("agent 检索默认最低点赞数", "过滤 0 互动的刷屏与闲聊"),
    "twitter.shared_accounts": ("全局共享账号", "每行一个账号名（不带 @），如财经快讯号。每个账号每次只读一次，帖子不做关键词匹配：新帖子连同全部自选股的简介交给模型一次调用批量分流，只有分到的公司才会逐条分级（模型用量小）。这里的账号按已证实的消息处理（不封顶重要性，也可成为标的追踪的依据）。公司自己的官方号、媒体 / 快讯号 / 关键人物账号在公司编辑页填写，同样按已证实处理"),
    "twitter.shared_max_per_account": ("共享账号每次读取条数", "最多 200；快讯号发帖很密，读少了两次运行之间的帖子会漏；分流过的帖子不再重复处理，新帖子每 20 条一次分流调用（并发）"),
    "twitter.shared_route_max": ("一条共享账号帖子最多分给几家公司", "宏观快讯可能与很多公司沾边，按相关度取前几家"),
    "twitter.feed_plan_keywords": ("跟踪词检索：英文个数", "取深度报告跟踪计划里的前几个新闻跟踪词，由模型译成英文后检索（只看英文帖子，译文永久缓存；译不出来时先用中文原词）；命中不要求提到公司名，由分级判断关联；0 = 不用"),
    "twitter.feed_plan_keywords_zh": ("跟踪词检索：中文原词个数", "再用前几个跟踪词的中文原词检索（只看中文帖子）；英文优先、适当用中文；0 = 不用"),
    "twitter.feed_chinese_name": ("再用中文公司名检索一次", "只看中文帖子；英文优先、适当用中文"),
    "twitter.feed_scope": ("新闻源覆盖范围", "all = 全部自选股；tracked = 只有标的追踪中的公司（调用预算有限，追踪中的公司刷新更勤；共享账号帖子也只分给它们）"),
    "twitter.feed_max_calls": ("每次运行最多调用推特次数", "读账号 + 检索；超出的公司留到下次运行、轮流覆盖。X 对检索大约每 15 分钟只给 50 次左右，超了会被限流（熔断 60 分钟）；0 = 不限"),
    "twitter.discover": ("账号发现", "新闻源检索时记下反复出现的作者，在下方列为候选账号，由你决定是否加入共享账号或某家公司"),
    "twitter.discover_days": ("账号发现统计天数", ""),
    "twitter.discover_min_posts": ("账号发现：最少出现次数", "同一账号在统计期内出现至少这么多次才列为候选"),
    "twitter.feed_queries_per_company": ("公司英文叫法个数", "美股代号（$XPEV）、英文名等最多几个，并成一条 OR 检索式（只看英文帖子）"),
    "twitter.feed_min_likes": ("新闻源最低点赞数", "低于它的帖子不收录；刚发出的帖子互动少，下次运行时若已达标仍会收录"),
    "twitter.feed_max_per_query": ("新闻源每个检索词 / 账号最多条数", ""),
    "twitter.feed_languages": ("新闻源收录的语种", "每行一个推特语言代码：en 英文、zh 中文、ja 日文……；留空 = 不限"),
    "twitter.feed_max_materiality": ("未经证实的推特帖子的重要性上限（1–5）", "只对公司名检索、跟踪词命中的任意作者封顶（默认 2：不进入标的追踪，需 ≥ 追踪的最低重要性，也不触发盘中提醒）；共享账号、官方号、登记账号按已证实的消息，不封顶"),
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
    "agent.valuation_extra_steps": ("估值情景子任务额外工具轮数", "在上一项之上追加，用于矩阵 / SOTP 建模"),
    "agent.gap_agents": ("补缺检索并行 agent 数", "缺口分组并行检索；1 = 不并行"),
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
    "notify.feishu_app_id_env": ("飞书应用 APP ID 环境变量名", "应用机器人：长连接收消息、对话启动任务、发文件；值在「密钥」页设置，修改后重启 stock web 生效"),
    "notify.feishu_app_secret_env": ("飞书应用 APP SECRET 环境变量名", ""),
    "notify.feishu_chat_id": ("飞书应用定时推送会话 ID", "oc_ 开头；日报、提醒也会通过应用机器人推送到该会话。不填则发到最近私聊过机器人的会话"),
    "notify.feishu_allowed_users": ("飞书允许下指令的用户 open_id（每行一个）", "open_id（ou_ 开头）或 user_id 均可；空 = 谁也不能下指令。先给机器人发一条消息，回复里会带上你的 open_id"),
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
    "tracker.stance_band": ("参考立场的空间阈值", "加权空间超过 ±该比例才算偏多 / 偏空，0.15 表示 15%"),
    "tracker.anchor_band": ("程序参考价的偏离阈值", "基准情景较「一致预期 EPS × 历史中位 PE」偏离超过 ±该比例时标注 ⚠，0.25 表示 25%"),
    # 业绩 vs 一致预期
    "consensus.enabled": ("业绩对比一致预期", "stock run 时检查新业绩，与发布前的一致预期对比"),
    "consensus.window_days": ("A 股一致预期窗口（天）", "发布前多少天内的研报计入（每家机构取最新一篇）"),
    "consensus.lookback_days": ("首次检查回看天数", "第一次检查某家公司时只对比这段时间内发布的业绩"),
    "consensus.min_estimates": ("样本少的阈值（家）", "机构数少于它时标注「样本少」"),
    "consensus.stale_days": ("一致预期过时阈值（天）", "最新一篇研报或快照早于发布日这么多天时标注"),
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
