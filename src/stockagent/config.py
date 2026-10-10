"""配置与自选股清单（config.yaml / watchlist.yaml / .env）。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import cron

Effort = Literal["low", "medium", "high", "xhigh", "max"]


# ---------------------------------------------------------------- LLM

class LLMProfile(BaseModel):
    """一个可调用的模型端点。protocol 决定走哪套 SDK。"""

    protocol: Literal["anthropic", "openai", "mock"] = "anthropic"
    base_url: str | None = None
    model: str
    # 从哪个环境变量读 key；为空时 anthropic 协议交给 SDK 自己解析凭据
    api_key_env: str | None = None
    # anthropic 协议的认证头：api_key → x-api-key；bearer → Authorization: Bearer（部分中转站需要）
    auth: Literal["api_key", "bearer"] = "api_key"
    max_tokens: int = 32000  # 单次输出上限（调用方的需求会被它截断）
    timeout: float = 600
    max_retries: int = 3  # openai 协议只对很快就失败的瞬时故障（限流、502/503）重试，网关超时（504）与读超时不重试，直接换备用模型
    # ---- anthropic 协议专用
    effort: Effort | None = None
    thinking: Literal["adaptive", "off"] = "adaptive"
    structured_output: bool = True
    refusal_fallback: bool = False
    prompt_cache: bool = True
    # ---- openai 兼容协议专用
    json_mode: bool = True
    # 流式请求：网关常给非流式请求设总时限（实测 2 分钟，长输出必超时）；服务不支持 stream 时改为 false
    stream: bool = True
    max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    extra_body: dict = Field(default_factory=dict)
    # agent 调用工具的方式：native 用接口的 tools 参数；text 用 <tool_call> 文本协议（服务端不支持工具调用时）
    tool_mode: Literal["native", "text"] = "native"
    # 单次请求最多送入多少字的资料；不填则不限制（只受 report.profile_max_doc_chars 控制）
    max_input_chars: int | None = None
    # 本模型不可用时（连接失败、5xx、限流、上游拒绝）自动改用的 profile
    fallback: str | None = None
    # 两种协议都可用；Claude 新模型不接受 temperature，默认不发送
    temperature: float | None = None


class TaskRoute(BaseModel):
    profile: str
    effort: Effort | None = None
    max_tokens: int | None = None


class LLMConfig(BaseModel):
    profiles: dict[str, LLMProfile]
    default_profile: str | None = None
    tasks: dict[str, str | TaskRoute] = Field(default_factory=dict)
    concurrency: int = 4
    max_continuations: int = 3  # 文本输出达到 max_tokens 时，最多让模型从中断处续写几次

    @model_validator(mode="after")
    def _check(self) -> "LLMConfig":
        if not self.profiles:
            raise ValueError("llm.profiles 至少需要一个配置")
        if self.default_profile is None:
            self.default_profile = next(iter(self.profiles))
        names = set(self.profiles)
        if self.default_profile not in names:
            raise ValueError(f"default_profile '{self.default_profile}' 不在 profiles 中")
        for task, route in self.tasks.items():
            p = route if isinstance(route, str) else route.profile
            if p not in names:
                raise ValueError(f"任务 {task} 引用了不存在的 profile '{p}'")
        for n, prof in self.profiles.items():
            if prof.fallback and prof.fallback not in names:
                raise ValueError(f"profile {n} 的 fallback '{prof.fallback}' 不存在")
        return self

    def route(self, task: str) -> tuple[str, LLMProfile, TaskRoute]:
        r = self.tasks.get(task, self.default_profile)
        if isinstance(r, str):
            r = TaskRoute(profile=r)
        return r.profile, self.profiles[r.profile], r


# ---------------------------------------------------------------- 其他配置

class ImaConfig(BaseModel):
    """腾讯 ima 知识库（需要 .env 的 IMA_CLIENT_ID 与 IMA_API_KEY）。"""

    enabled: bool = True
    knowledge_bases: list[str] = Field(default_factory=list)  # 知识库名称或 ID
    queries_per_company: int = 4  # 每家公司每次用几个检索词（公司名、代码、英文名），结果取并集
    download: bool = True         # 下载新条目原文：PDF/Word 入库为资料，图片 OCR
    ocr: bool = True              # 图片（新闻截图）用本地 tesseract 识别文字
    max_download_mb: int = 40
    max_downloads_per_day: int = 25  # 每天最多取几次原文（ima 对取原文有未公开的每日上限，实测约几十次；agent 的 ima_read 也计入）
    in_alert: bool = False        # 盘中 stock alert 是否也检索（默认只在 stock run / fetch 时）


class TwitterConfig(BaseModel):
    """推特 / X（经 twitter-cli；需要 .env 的 TWITTER_AUTH_TOKEN 与 TWITTER_CT0）。两个开关互相独立：
    agent_tools = 研究 agent 与标的追踪可以按需调用 twitter_search / twitter_user；
    news_feed = stock run 时自动抓取（公司的账号白名单 + 检索词），新帖子进入事件分级。"""

    agent_tools: bool = True
    news_feed: bool = True
    # agent 工具
    max_results: int = 20   # 单次工具调用最多返回几条
    days: int = 7           # twitter_search 默认只看最近几天
    min_likes: int = 3      # twitter_search 默认的最低点赞数（过滤 0 互动的刷屏）
    # 新闻源
    shared_accounts: list[str] = Field(default_factory=list)  # 全局共享账号（财经快讯号等）：帖子不做关键词匹配，用一次模型调用按全部自选股分流，再由分到的公司逐条分级
    shared_max_per_account: int = 100  # 共享账号每次读取最近几条（最多 200）：快讯号发帖很密（FirstSquawk 约 8 条/小时），读少了两次运行之间的帖子会漏掉
    shared_route_max: int = 6         # 共享账号的一条帖子最多分给几家公司（宏观快讯可能与很多公司沾边，按相关度取前几家）
    # 检索语种：英文优先、适当用中文（推特以英文为主，中文帖子多是个人评论）
    feed_queries_per_company: int = 3   # 公司的英文叫法（美股代号 $XPEV、英文名）最多几个，并成一条 OR 检索式（只看英文帖子）
    feed_chinese_name: bool = True      # 再用中文公司名检索一次（只看中文帖子）
    feed_plan_keywords: int = 2         # 每家公司用深度报告跟踪计划里的前几个新闻跟踪词的英文译文检索（只看英文帖子；0 = 不用）；命中不要求提到公司名，交给分级判断关联
    feed_plan_keywords_zh: int = 1      # 再用前几个跟踪词的中文原词检索（只看中文帖子；0 = 不用）
    feed_scope: Literal["all", "tracked"] = "all"  # 新闻源覆盖的公司：all 全部自选股；tracked 只有标的追踪中的公司（调用预算有限，追踪中的公司刷新更勤）
    feed_max_calls: int = 40            # 每次运行最多调用推特几次（读账号 + 检索；0 = 不限）：超出的公司留到下次运行、轮流覆盖。X 的网页接口对检索大约每 15 分钟只给 50 次左右，超了返回 429（会熔断 60 分钟）；共享账号每读 100 条约 5 次请求
    feed_min_likes: int = 10            # 新闻源只收录至少这么多赞的帖子
    feed_max_per_query: int = 10        # 每个检索词、每个账号每次最多取几条
    feed_languages: list[str] = Field(default_factory=lambda: ["en", "zh"])  # 只收录这些语种（推特的 lang 代码）的帖子；留空 = 不限
    discover: bool = True               # 账号发现：新闻源检索时记下反复出现的作者，在设置页列为候选账号，由你决定是否加入
    discover_days: int = 30             # 候选账号统计最近几天
    discover_min_posts: int = 2         # 至少出现几次才列为候选
    feed_max_materiality: int = 2       # 推特帖子分级后重要性的上限（1–5）：社交内容未经证实，默认不进入标的追踪与重要事件

    @field_validator("shared_accounts", mode="before")
    @classmethod
    def _handles(cls, v):
        return _clean_handles(v)


def _clean_handles(v):
    out = []
    for h in v or []:
        h = str(h).strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h):
            raise ValueError(f"推特账号名不合法：{h}（只能是字母、数字、下划线，最长 15 个字符，不带 @）")
        if h.lower() not in [x.lower() for x in out]:
            out.append(h)
    return out


class SourcesConfig(BaseModel):
    announcements_lookback_days: int = 30
    news_lookback_days: int = 7
    price_history_days: int = 400
    price_sources: list[Literal["eastmoney", "tencent", "sina", "yahoo"]] = ["eastmoney", "tencent", "sina"]  # sina、yahoo 只覆盖美股
    request_delay: float = 0.6
    eastmoney_news: bool = True
    eastmoney_news_page_size: int = 20
    google_news: bool = True
    google_news_locales: list[Literal["zh-CN", "zh-HK", "en-US"]] = ["zh-CN", "zh-HK", "en-US"]
    google_news_max_per_query: int = 40
    google_news_resolve_max: int = 30  # 每家公司每次最多读多少条新条目的原文页取真实发布时间（0=不读，用 Google 的收录时间）
    plan_keywords_max: int = 6  # 深度报告给出的产业链跟踪词，每次最多检索几个
    blocked_news_sources: list[str] = Field(default_factory=list)
    blocked_title_patterns: list[str] = Field(default_factory=list)
    auto_download_reports: bool = True
    ima: ImaConfig = Field(default_factory=ImaConfig)
    twitter: TwitterConfig = Field(default_factory=TwitterConfig)
    sec_user_agent: str | None = None  # SEC 要求 User-Agent 带联系方式（「姓名 邮箱」）；美股公告与 XBRL 财报都要用
    ccass: bool = True           # 每天记录自选港股的 CCASS（中央结算系统）持股快照
    ccass_alert_pct: float = 1.0  # 相邻两份快照间某参与者持股变化 ≥ 此值（占已发行股份的百分点）时生成一条「数据异动」进入分级
    ccass_keep: int = 60          # 每份快照保存持股最多的前几名参与者
    # 大股东 / 董监高持股变动（港股：港交所权益披露 DI；美股：Form 4 内部人交易、Schedule 13D / 13G）；程序提取数字，事件交给分级
    holdchg: bool = True
    holdchg_lookback_days: int = 30     # 第一次抓取往回看多少天；之后从上次水位线再往回重叠几天（申报常晚几天才公布）
    holdchg_hk_min_pct: float = 0.5     # 港股：大股东 / 董事的好仓比例相对上次报告的水平变化 ≥ 此值（占已发行股份的百分点）才生成事件；被动基金的日常借还、小幅增减达不到
    holdchg_hk_streak: int = 5          # 港股：同一申报人连续这么多份同方向买入 / 卖出申报，净额达到上面阈值的一半就生成事件（控股股东每天小额增持，单日不起眼，连起来是信号）
    holdchg_hk_insider_value: float = 1_000_000  # 港股：董事 / 最高行政人员一次买卖（同方向合并）金额 ≥ 此值（港元；缺成交价用最近收盘价）也生成事件，不看比例
    holdchg_us_insider_value: float = 500_000    # 美股：内部人公开市场买卖（Form 4）金额 ≥ 此值（美元）才生成事件；期权行权、授予、税款代扣不算
    holdchg_us_filer_pct: float = 1.0   # 美股：13G 申报人的持股比例较上次变化 ≥ 此值（百分点）或降到 5% 以下才生成事件；13D 与首次 13G 一律生成
    sec_forms: list[str] = Field(
        default_factory=lambda: [
            "8-K", "10-Q", "10-K", "6-K", "20-F", "S-1", "S-3", "424B4", "424B5",
            "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A", "DEF 14A",
        ]
    )


class NotifyConfig(BaseModel):
    feishu_webhook_env: str | None = None
    feishu_secret_env: str | None = None
    wecom_webhook_env: str | None = None
    telegram_token_env: str | None = None
    telegram_chat_id: str | None = None
    feishu_app_id_env: str | None = None       # 飞书应用机器人（APP ID / APP SECRET）：长连接收消息、对话启动任务、发文件
    feishu_app_secret_env: str | None = None
    feishu_chat_id: str | None = None          # 应用机器人定时推送的会话（oc_ 开头）；不填则发到最近私聊过机器人的会话（data/feishu_home_chat.txt）
    feishu_allowed_users: list[str] = Field(default_factory=list)   # 允许下指令的用户 open_id（ou_ 开头）或 user_id；空 = 谁也不能下指令
    alert_min_materiality: int = 4


class AgentConfig(BaseModel):
    """研究 agent 的通用参数（与具体模型无关；模型相关的限制写在各自的 profile 里）。"""

    max_steps: int = 12              # 每个研究子任务最多调用工具的轮数
    valuation_extra_steps: int = 8   # 估值情景子任务（阶段 2，读完前序笔记后用矩阵 / SOTP 建模）在 max_steps 之上多给的轮数
    gap_agents: int = 3              # 补缺检索最多同时跑几个 agent（缺口分组并行）；1 = 不并行；parallel_sections 关闭时也不并行
    tool_result_chars: int = 4000    # 单次工具结果最多返回给模型的字数
    # 网页搜索源：搜索 API 优先——exa（经 agent-reach 的 mcporter 调用，装了就启用）、bocha / tavily / serper（.env 填了 key 才启用）；
    # 免费源中前三个轮流使用（分摊请求量、减少被反爬拦截），其余在它们都失败时依次备用
    web_search: list[Literal["exa", "bocha", "tavily", "serper", "so360", "toutiao", "gnews", "baidu", "bing", "duckduckgo"]] = [
        "exa", "bocha", "tavily", "serper", "so360", "toutiao", "gnews", "baidu", "bing", "duckduckgo"]
    parallel_sections: bool = True   # 深度报告的研究子任务并行执行


class ReportConfig(BaseModel):
    lookback_hours: int = 36
    min_materiality: int = 2
    max_items_per_company: int = 12
    triage_batch_size: int = 20
    profile_max_doc_chars: int = 180_000
    auto_profile: bool = True
    dossier_stale_days: int = 30  # 深度报告超过多少天提示更新
    review: bool = True           # 深度报告撰写后做一轮反方审稿（task review，可路由到另一个模型）
    color_scheme: Literal["red_up", "green_up"] = "red_up"


class TrackerConfig(BaseModel):
    """标的追踪：按新事件修正估值假设；目标价（情景加权估值）由代码按模型计算。"""

    model_config = ConfigDict(extra="forbid")

    min_materiality: int = 3            # 触发修正的最低事件重要性（1–5）
    max_events: int = 25                # 每次修正最多处理的依据条数，其余留到下次
    max_steps: int = 8                  # 修正 agent 最多调用工具的轮数
    web_search: bool = True             # 修正 agent 可用网页搜索（深度报告运行期间自动暂停，避免争抢搜索源）
    target: Literal["expected", "base"] = "expected"   # 目标价口径：expected 概率加权 / base 基准情景
    notify_min_change: float = 0.05     # 目标价变动达到该比例时推送
    flag_change: float = 0.25           # 单项假设变动超过该比例时标注「大幅修正」
    auto_rebaseline: bool = True        # 有新的深度报告时自动重建估值基线
    redo_materiality: int = 5           # 重要性达到该值且修正判为实质影响的事件，自动排队重跑深度报告
    redo_on_broken: bool = True         # 论点被判失效时自动排队重跑深度报告
    redo_cooldown_days: int = 3         # 同一公司自动重跑深度报告的冷却天数
    stance_band: float = 0.15           # 参考立场：加权空间超过 ±该比例才算偏多 / 偏空（见 valuation.reference_stance）
    anchor_band: float = 0.25           # 程序参考价：基准情景较「一致预期 EPS × 历史中位 PE」偏离超过 ±该比例时标注 ⚠（见 anchor.py）


class ConsensusConfig(BaseModel):
    """业绩 vs 一致预期（consensus.py，代码计算）：A 股按研报还原发布前的一致预期，美股每天存快照，港股靠手动录入。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    window_days: int = 120      # A 股：发布前多少天内的研报计入一致预期（每家机构取最新一篇）
    lookback_days: int = 10     # 第一次检查某家公司时，只对比多少天内发布的业绩（避免把历史业绩一次性推进分级）
    min_estimates: int = 3      # 少于几家机构时标注「样本少」
    stale_days: int = 90        # 一致预期（最新一篇研报或快照）早于发布日多少天时标注「可能过时」


class ChatConfig(BaseModel):
    """报告研读对话（WebUI 报告页）：讲解深度报告，不改报告结论，也不作为追踪修正的依据。"""

    model_config = ConfigDict(extra="forbid")

    web_search: bool = True             # 对话可用网页搜索（深度报告运行期间自动暂停，避免争抢搜索源）
    max_steps: int = 8                  # 每次回答最多调用工具的轮数
    max_turns: int = 20                 # 追问时带上最近多少轮问答（另受 profile 的 max_input_chars 限制）


class ScreenConfig(BaseModel):
    """选股器（stock screen）：全市场初筛 → 代码打分 → 模型快筛 → 人工点选送深研。输出是研究候选，不是买卖建议。"""

    model_config = ConfigDict(extra="forbid")

    markets: list[Literal["HK", "A"]] = ["HK", "A"]
    # 港股的筛选范围：all 全部港股（主板 + 创业板）| connect 只看港股通标的。股票池总是存全部港股并标出是否在港股通内，
    # 运行时可用 stock screen run --hk-scope 临时改变（WebUI 选股页的「港股范围」）
    hk_scope: Literal["all", "connect"] = "all"
    request_delay: float = 0.3             # 批量请求间隔（秒）；数据中心接口，与单公司查询共用熔断
    refresh_days: int = 6                  # 报表缓存多少天后重新抓取（已披露很久的报告期只抓一次）
    # ---- L1 硬过滤
    min_mcap: dict[Literal["HK", "A"], float] = {"HK": 2e9, "A": 3e9}          # 总市值下限（港元 / 人民币）
    min_avg_amount: dict[Literal["HK", "A"], float] = {"HK": 1e7, "A": 3e7}    # 20 日成交额中位数下限
    participation: float = 0.1             # 估算可容纳资金：成交额中位数 × 该参与率 × 5 个交易日
    min_listing_days: int = 365
    exclude_st: bool = True
    # FCF 口径不适用的行业（名称包含即排除出现金地板桶；银行、保险另按机构类型排除）
    fcf_exclude_industries: list[str] = ["银行", "保险", "证券", "多元金融", "房地产开发", "信托"]
    # ---- 桶 1 现金地板：入选门槛与粗算赔率的假设（都在页面上写明）
    min_fcf_yield: float = 0.08            # 保守 FCF Yield（TTM 与 3 年平均取低）达到即入选
    min_net_cash_ratio: float = 0.5        # 或净现金 ≥ 市值 × 该比例
    fair_fcf_yield: float = 0.08           # 合理估值：保守 FCF ÷ 该收益率 + 净现金
    bear_fcf_yield: float = 0.2            # 地板的盈利部分：最差一期 FCF ÷ 该收益率
    cash_haircut: float = 0.3              # 地板的现金部分：净现金打折（现金可能滞留子公司、不分配）；净负债全额扣减
    min_downside: float = 0.15             # 下行至少按该幅度计（地板高于现价时也有市场风险），避免赔率失真
    # ---- 桶 5 质量红利（高胜率）：稳定现金牛
    quality_min_dividend_yield: float = 0.04   # 近 12 个月股息率下限（且须被 FCF 覆盖）
    quality_min_div_years: int = 3             # 连续派息年数下限
    quality_fcf_stability: float = 0.4         # 近 3 个年度最差 FCF ≥ 3 年平均 × 该比例
    quality_min_fcf_yield: float = 0.05        # 保守 FCF Yield 下限
    # ---- 桶 4 预期修正
    min_profit_growth: float = 30.0        # 最新一期归母净利同比（%）
    min_revenue_growth: float = 10.0
    forecast_days: int = 45                # A 股业绩预告回看天数
    # ---- 桶 2 事件催化
    event_days: int = 30                   # 事件池回看天数
    event_lookback_days: int = 14          # 首次抓取全市场公告回看天数
    # ---- 分层（赔率 = (合理价值 − 现价) ÷ (现价 − 地板)）：A 高胜率 + 高赔率 / B 高胜率 / C 高赔率 / D 观察
    odds_a: float = 3.0                    # A 类：赔率 ≥ 该值、下行 ≤ max_downside_a、有催化剂、无严重风险标记
    max_downside_a: float = 0.2
    odds_win: float = 1.0                  # B 类（高胜率）：质量红利入选或「催化剂 + 下行 ≤ max_downside_a」，且赔率 ≥ 该值
    odds_b: float = 3.0                    # C 类（高赔率）：赔率 ≥ 该值，胜率证据不足
    event_odds_a: float = 2.0              # 事件池 A 类的赔率门槛
    top_n: int = 40                        # 每个桶进入候选的数量（按代码评分）
    quick_top_n: int = 15                  # 每个池送模型快筛的数量
    themes_file: str = "themes.yaml"       # 产业链主题（桶 3），相对 config.yaml 所在目录


class WebConfig(BaseModel):
    """WebUI（stock web）：只监听本机 127.0.0.1。"""

    model_config = ConfigDict(extra="forbid")

    port: int = 8765
    max_jobs: int = 2                   # 同时运行的后台任务数上限（深度报告另有全局互斥）
    upload_max_mb: int = 50             # 上传文档的大小上限


class ScheduleJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    task: str                           # 任务登记表中的名称（run / alert / target_run / dossier ...）
    args: list[str] = Field(default_factory=list)  # 开关（如 --push）与公司 id
    cron: str                           # 5 段 cron 表达式，按北京时间
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        return cron.validate(v)


class SchedulerConfig(BaseModel):
    """内置调度器（随 stock web 运行）。与系统 cron 二选一，不要同时启用。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    catchup_minutes: int = 120          # 电脑睡眠等原因错过的任务，醒来后在该时间窗内补跑一次
    jobs: list[ScheduleJob] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "SchedulerConfig":
        names = [j.name for j in self.jobs]
        if dup := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"日程名称重复：{dup}")
        return self


class Settings(BaseModel):
    data_dir: Path = Path("data")
    llm: LLMConfig
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    consensus: ConsensusConfig = Field(default_factory=ConsensusConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)


# ---------------------------------------------------------------- 自选股

PRICING_ORDER = ("HK", "A", "US")  # 未指定 pricing 时的定价上市地优先级（A+H、港美两地均以港股定价）


class Listing(BaseModel):
    market: Literal["HK", "A", "US"]
    code: str
    exchange: str | None = None  # A: SH/SZ/BJ；US: NASDAQ/NYSE/AMEX
    benchmark: str | None = None  # 计算超额收益用的指数 secid；不填用该市场的第一个基准
    ads_ratio: float | None = None  # 美股 ADS：每 1 份 ADS 对应的普通股数（如小鹏 XPEV 为 2）；用于 ADS 溢价与平价折算

    @model_validator(mode="after")
    def _normalize(self) -> "Listing":
        if self.ads_ratio is not None:
            if self.market != "US":
                raise ValueError(f"ads_ratio 只适用于美股：{self.market} {self.code}")
            if self.ads_ratio <= 0:
                raise ValueError(f"ads_ratio 必须为正：{self.code}")
        code = str(self.code).strip().upper()
        if self.market == "HK":
            code = code.removesuffix(".HK").zfill(5)
            self.exchange = "HKEX"
        elif self.market == "A":
            code = code.split(".")[0]
            if len(code) != 6 or not code.isdigit():
                raise ValueError(f"A 股代码应为 6 位数字：{self.code}")
            if not self.exchange:
                if code.startswith("92") or code[0] in "48":
                    self.exchange = "BJ"
                elif code[0] in "69":
                    self.exchange = "SH"
                else:
                    self.exchange = "SZ"
            self.exchange = self.exchange.upper()
        else:
            self.exchange = (self.exchange or "").upper() or None
        self.code = code
        return self

    @property
    def key(self) -> str:
        return f"{self.market}:{self.code}"

    @property
    def display(self) -> str:
        if self.market == "HK":
            return f"{self.code}.HK"
        if self.market == "A":
            return f"{self.code}.{self.exchange}"
        return self.code

    @property
    def currency(self) -> str:
        return {"HK": "HKD", "A": "CNY", "US": "USD"}[self.market]


class Company(BaseModel):
    id: str
    name: str
    name_en: str | None = None
    aliases: list[str] = Field(default_factory=list)
    listings: list[Listing]
    # 定价上市地：情景价格、目标价、价格类证伪条件按它计算；不填时按 PRICING_ORDER（港股优先）取
    pricing: Literal["HK", "A", "US"] | None = None
    sector: str | None = None
    notes: str | None = None
    watch_items: list[str] = Field(default_factory=list)
    # 可按语言指定新闻检索词；未指定时用 name / 第一个别名 / name_en
    news_queries: dict[str, list[str]] = Field(default_factory=dict)
    # 推特新闻源（sources.twitter.news_feed 开启时）：官方号的帖子全部收录；其他账号（媒体、财经快讯号、关键人物）只收录提到本公司的帖子
    twitter_official: list[str] = Field(default_factory=list)
    twitter_accounts: list[str] = Field(default_factory=list)

    @field_validator("twitter_official", "twitter_accounts", mode="before")
    @classmethod
    def _handles(cls, v):
        return _clean_handles(v)

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not v or not v[0].isalnum() or not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"company id 只能包含字母、数字、-、_，且以字母或数字开头：{v}")
        return v

    @model_validator(mode="after")
    def _pricing_ok(self) -> "Company":
        if self.pricing and self.listing(self.pricing) is None:
            raise ValueError(f"{self.id}：定价上市地 {self.pricing} 不在 listings 中")
        return self

    def listing(self, market: str) -> Listing | None:
        return next((l for l in self.listings if l.market == market), None)

    @property
    def pricing_listing(self) -> Listing:
        """定价上市地：pricing 指定的市场，否则按港股、A 股、美股的顺序取第一个存在的上市地。"""
        if self.pricing and (l := self.listing(self.pricing)):
            return l
        return next(l for m in PRICING_ORDER for l in self.listings if l.market == m)

    @property
    def title(self) -> str:
        return f"{self.name}（{' / '.join(l.display for l in self.listings)}）"

    def names(self) -> list[str]:
        out = [self.name, *self.aliases]
        if self.name_en:
            out.append(self.name_en)
        return [n for n in out if n]

    def queries(self, locale: str) -> list[str]:
        if locale in self.news_queries:
            return self.news_queries[locale]
        if locale == "zh-CN":
            return [f'"{self.name}"']
        if locale == "zh-HK":
            return [f'"{self.aliases[0] if self.aliases else self.name}"']
        return [f'"{self.name_en}"'] if self.name_en else []


class Quote(BaseModel):
    """指数、汇率或产业链相关标的（东方财富 secid）。"""

    secid: str
    name: str


class Watchlist(BaseModel):
    companies: list[Company]
    benchmarks: dict[Literal["HK", "A", "US"], list[Quote]] = Field(default_factory=dict)
    market_watch: list[Quote] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "Watchlist":
        ids = [c.id for c in self.companies]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"company id 重复：{dup}")
        return self

    def get(self, key: str) -> Company | None:
        """按 id、名称、别名或代码查找。"""
        k = key.strip()
        for c in self.companies:
            if k == c.id or k in c.names():
                return c
            for l in c.listings:
                if k.upper() in (l.code, l.display, l.key, l.code.lstrip("0")):
                    return c
        return None

    def select(self, keys: list[str] | None) -> list[Company]:
        if not keys:
            return list(self.companies)
        out = []
        for k in keys:
            c = self.get(k)
            if c is None:
                raise SystemExit(f"自选股中找不到：{k}")
            out.append(c)
        return out


# ---------------------------------------------------------------- 加载

class ConfigError(Exception):
    pass


def load(config_path: str | Path = "config.yaml", watchlist_path: str | Path | None = None) -> tuple[Settings, Watchlist, Path]:
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        raise ConfigError(f"找不到配置文件 {config_path}，先运行 `stock init`")
    root = config_path.parent
    load_dotenv(root / ".env", override=False)
    with open(config_path, encoding="utf-8") as f:
        settings = Settings.model_validate(yaml.safe_load(f) or {})
    if not settings.data_dir.is_absolute():
        settings.data_dir = (root / settings.data_dir).resolve()
    wl_path = Path(watchlist_path) if watchlist_path else root / "watchlist.yaml"
    if not wl_path.exists():
        raise ConfigError(f"找不到自选股清单 {wl_path}")
    with open(wl_path, encoding="utf-8") as f:
        watchlist = Watchlist.model_validate(yaml.safe_load(f) or {})
    return settings, watchlist, root


def env(name: str | None) -> str | None:
    return os.environ.get(name) if name else None
