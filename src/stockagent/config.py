"""配置与自选股清单（config.yaml / watchlist.yaml / .env）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

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
    max_retries: int = 3
    # ---- anthropic 协议专用
    effort: Effort | None = None
    thinking: Literal["adaptive", "off"] = "adaptive"
    structured_output: bool = True
    refusal_fallback: bool = False
    prompt_cache: bool = True
    # ---- openai 兼容协议专用
    json_mode: bool = True
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

class SourcesConfig(BaseModel):
    announcements_lookback_days: int = 30
    news_lookback_days: int = 7
    price_history_days: int = 400
    price_sources: list[Literal["eastmoney", "tencent"]] = ["eastmoney", "tencent"]
    request_delay: float = 0.6
    eastmoney_news: bool = True
    eastmoney_news_page_size: int = 20
    google_news: bool = True
    google_news_locales: list[Literal["zh-CN", "zh-HK", "en-US"]] = ["zh-CN", "zh-HK", "en-US"]
    google_news_max_per_query: int = 40
    plan_keywords_max: int = 6  # 深度报告给出的产业链跟踪词，每次最多检索几个
    blocked_news_sources: list[str] = Field(default_factory=list)
    blocked_title_patterns: list[str] = Field(default_factory=list)
    auto_download_reports: bool = True
    sec_user_agent: str | None = None
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
    alert_min_materiality: int = 4


class AgentConfig(BaseModel):
    """研究 agent 的通用参数（与具体模型无关；模型相关的限制写在各自的 profile 里）。"""

    max_steps: int = 12              # 每个研究子任务最多调用工具的轮数
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
    color_scheme: Literal["red_up", "green_up"] = "red_up"


class Settings(BaseModel):
    data_dir: Path = Path("data")
    llm: LLMConfig
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)


# ---------------------------------------------------------------- 自选股

class Listing(BaseModel):
    market: Literal["HK", "A", "US"]
    code: str
    exchange: str | None = None  # A: SH/SZ/BJ；US: NASDAQ/NYSE/AMEX
    benchmark: str | None = None  # 计算超额收益用的指数 secid；不填用该市场的第一个基准

    @model_validator(mode="after")
    def _normalize(self) -> "Listing":
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
    sector: str | None = None
    notes: str | None = None
    watch_items: list[str] = Field(default_factory=list)
    # 可按语言指定新闻检索词；未指定时用 name / 第一个别名 / name_en
    news_queries: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not v or not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"company id 只能包含字母、数字、-、_：{v}")
        return v

    def listing(self, market: str) -> Listing | None:
        return next((l for l in self.listings if l.market == market), None)

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
