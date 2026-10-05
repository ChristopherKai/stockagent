"""LLM 事件分级：相关性、类型、方向、重要性、是否新信息、相对预期。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from pydantic import BaseModel, field_validator

from .app import SOURCE_LABEL, App
from .sources import twitter
from .config import Company
from .llm import LLMError
from .utils import fmt_local, iso, log, today_local, utcnow

EVENT_TYPES = {
    "earnings": "业绩/财报", "guidance": "业绩预告/指引", "orders_customers": "订单/客户",
    "product_tech": "产品/技术", "capacity": "产能/扩产", "financing": "融资/配售/增发",
    "buyback_dividend": "回购/分红", "insider_trade": "股东/高管增减持", "lockup": "解禁",
    "index_connect": "指数/港股通调整", "policy_regulation": "政策/监管", "litigation": "诉讼/合规",
    "governance": "治理/人事", "mna": "并购/分拆", "industry_chain": "行业/产业链",
    "macro_geo": "宏观/地缘", "market_move": "行情/资金面", "analyst": "研报/评级",
    "routine": "例行公告", "other": "其他",
}
DIRECTIONS = {"positive": "利好", "negative": "利空", "neutral": "中性", "mixed": "多空交织", "unclear": "不明"}
NOVELTY = {"new": "新信息", "update": "进展更新", "repeat": "旧闻复述"}
HORIZONS = {"intraday": "日内", "days": "数天", "weeks": "数周", "months": "数月以上"}

TRIAGE_SYSTEM = """你是一名覆盖港股、A股和美股的买方研究员，负责给自选股的新闻和公告做"事件分级"。
使用者的交易风格是波段（持有数天到数周）和事件驱动，所以你要判断：这条信息是不是新的、会不会影响股价、方向和持续时间如何、市场是否已经反映。

判断规则：
1. 只依据提供的材料，不要编造材料里没有的数字或事实；材料不足时，在 expectation 中写明缺少什么。
2. 区分新信息和复述。只报道涨跌、资金流向、板块排名而没有新的基本面信息的，归为 market_move，重要性一般不超过 2。
3. 同一事件的多条报道（包括港交所与巨潮的同一公告、不同媒体的同一新闻）：保留信息最完整的一条，其余的 duplicate_of 填那一条的 id。
4. 公司只是被顺带提及（板块综述、ETF 成分股列表、同行对比）且没有与本公司相关的新信息的，relevant 为 false。
5. 重要性 materiality（1–5）：
   5 = 可能改变投资逻辑或引发 10% 以上波动：业绩大幅超预期或盈利警告、大客户得失、大比例折价配售/供股、重大诉讼结果、纳入或剔除港股通/重要指数、重大监管处罚
   4 = 显著影响，可能引发 5–10% 波动：重要订单、业绩指引变化、大股东或高管大额增减持、解禁临近、关键产品或认证突破
   3 = 值得跟踪：产能进展、行业政策、竞争对手或上下游的重要变化、有新信息的研报
   2 = 次要信息
   1 = 噪音或例行公告（章程修订、董事名单、会议通知、翌日披露报表等，除非内容本身有实质变化）
6. expectation：判断相对市场预期是超预期、符合还是低于预期，并说明依据（与历史数据、公司指引、行业趋势比较）；无法判断就写"无法判断"并说明缺什么。
7. 港股特别注意：配售/供股（通常短期利空，关注折让幅度与用途）、解禁、港股通纳入/剔除、恒指等指数调整、盈利警告/正面盈利预告、停牌复牌、大股东质押。A股注意：业绩预告、减持计划、解禁、股权激励、定增、问询函。
8. 产业链信息（客户、供应商、竞争对手、下游需求）如果会影响本公司，relevant 为 true，event_type 用 industry_chain。
9. 定期报告、业绩公告等如果只有标题、看不到具体数字，重要性按 3 处理，在 follow_up 中写明需要阅读原文核实哪些数字；有数字时再按超预期程度打分。
10. 摘要里带有「[跟踪词：…]」的条目，是按深度报告给出的产业链线索检索到的，要判断它与本公司的关联。
11. 来源为「一致预期对比」的条目（摘要以「[程序计算]」开头）是程序算好的「实际业绩 vs 发布前一致预期」：数字可直接引用，expectation 以它为准（写明偏离幅度与口径）；event_type 用 earnings 或 guidance。它不是任何公告的重复，duplicate_of 留空。重要性按偏离幅度：年报偏离或中报/季报外推全年偏离 ≥30% → 5；≥15%，或与市场预期方向相反 → 4；±5% 以内且没有其他新信息 → 3；带「样本少」「外推仅供参考」等标注时酌情降一级。
12. 来源为「推特/X」且摘要以「[推特·未经证实]」开头的条目是社交媒体帖子（按公司名、跟踪词检索到的任意作者），只当线索、不当事实：个人观点、喊单、情绪、营销图文、无信息量的转发，relevant 为 false 或重要性 1；发布了具体事件（交付量、财报、发布会、合作公告）的可按内容分级，但 expectation 要写明「社交媒体消息，未经证实」；与公告或其他媒体报道的是同一件事时，duplicate_of 指向非推特的那一条。
13. 摘要以「[共享账号·已分流]」开头的帖子来自使用者指定的财经快讯号，按已证实的新闻处理（不要写「未经证实」），重要性按规则 5 正常打分。程序已用一次模型调用把它从全部自选股里粗筛到「可能与本公司有关」（宁可多选），请结合本公司画像（业务、产业链上下游、竞争对手、所属行业的政策与价格、对汇率利率关税大宗价格等宏观变量的敏感度）再判断：对本公司确无实质影响的 relevant 为 false；不要因为它是快讯号发的或数字醒目就判为相关。
14. 摘要以「[官方账号]」开头的帖子是本公司自己的官方推特账号发布的，按公司的一手披露处理（不要写「未经证实」）：交付量、订单、合作、融资、人事、业绩等具体经营信息按规则 5 正常打分；营销图文、产品宣传、活动预告等没有新的经营信息的，relevant 为 false 或重要性 1。
15. 摘要以「[登记账号]」开头的帖子来自使用者登记在本公司名下的媒体 / 快讯号 / 关键人物账号，按已证实的新闻处理（不要写「未经证实」），重要性按规则 5 正常打分。这些账号发帖面很广、程序没有按公司筛选，请结合本公司画像判断：对本公司确无实质影响的 relevant 为 false；关键人物（管理层、行业人士）的公开表态按其内容与影响力判断。

字段说明：
- event_type：earnings 业绩/财报；guidance 业绩预告/指引；orders_customers 订单/客户；product_tech 产品/技术；capacity 产能/扩产；financing 融资/配售/增发；buyback_dividend 回购/分红；insider_trade 股东/高管增减持；lockup 解禁；index_connect 指数/港股通调整；policy_regulation 政策/监管；litigation 诉讼/合规；governance 治理/人事；mna 并购/分拆；industry_chain 行业/产业链；macro_geo 宏观/地缘；market_move 行情/资金面；analyst 研报/评级；routine 例行公告；other 其他
- direction：positive / negative / neutral / mixed / unclear
- novelty：new 新信息 / update 已知事件的进展 / repeat 旧闻复述
- horizon：intraday / days / weeks / months（影响持续多久）
- summary_zh：简体中文一句话，写清谁、做了什么、关键数字，不超过 80 字
- follow_up：接下来需要跟踪或验证什么；没有就留空字符串
- duplicate_of：不重复时填空字符串"""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "duplicate_of": {"type": "string"},
                    "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
                    "direction": {"type": "string", "enum": list(DIRECTIONS)},
                    "materiality": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "novelty": {"type": "string", "enum": list(NOVELTY)},
                    "horizon": {"type": "string", "enum": list(HORIZONS)},
                    "summary_zh": {"type": "string"},
                    "expectation": {"type": "string"},
                    "follow_up": {"type": "string"},
                },
                "required": ["id", "relevant", "duplicate_of", "event_type", "direction", "materiality",
                             "novelty", "horizon", "summary_zh", "expectation", "follow_up"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


class TriageItem(BaseModel):
    id: str
    relevant: bool
    duplicate_of: str = ""
    event_type: str = "other"
    direction: str = "unclear"
    materiality: int = 1
    novelty: str = "new"
    horizon: str = "days"
    summary_zh: str
    expectation: str = ""
    follow_up: str = ""

    @field_validator("materiality", mode="before")
    @classmethod
    def _clamp(cls, v):
        return min(5, max(1, int(v)))

    @field_validator("event_type")
    @classmethod
    def _et(cls, v):
        return v if v in EVENT_TYPES else "other"

    @field_validator("direction")
    @classmethod
    def _dir(cls, v):
        return v if v in DIRECTIONS else "unclear"

    @field_validator("novelty")
    @classmethod
    def _nov(cls, v):
        return v if v in NOVELTY else "new"

    @field_validator("horizon")
    @classmethod
    def _hor(cls, v):
        return v if v in HORIZONS else "days"


class TriageBatch(BaseModel):
    items: list[TriageItem]


def _validate(data: dict) -> dict:
    return TriageBatch.model_validate(data).model_dump()


def short_id(uid: str) -> str:
    return uid[:12]


def _describe(item: dict, i: int) -> str:
    kind = "程序计算" if item["source"] == "consensus" else \
        {"announcement": "公告", "news": "新闻", "data": "数据异动", "manual": "手动录入"}.get(item["kind"], item["kind"])
    src = SOURCE_LABEL.get(item["source"], item["source"])
    media = item.get("source_name") or ""
    head = f"[{i}] id={short_id(item['uid'])} | {kind} · {src}{' · ' + media if media and media != src else ''} | {fmt_local(item['published_at'])}"
    body = f"标题：{item['title']}"
    if item.get("snippet") and item["snippet"] != item.get("source_name"):
        body += f"\n    摘要：{item['snippet'][:900 if item['source'] == 'consensus' else 300]}"  # 业绩对比的口径与标注要完整
    return f"{head}\n    {body}"


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


_NUM_RE = re.compile(r"\d+")
_POLAR = [("增", "减"), ("涨", "跌"), ("买", "卖"), ("升", "降"), ("盈", "亏"), ("多", "空"), ("上调", "下调"), ("扩", "缩")]


def same_event(a: str, b: str, ga: set[str], gb: set[str], threshold: float = 0.8) -> bool:
    """保守的标题去重：高度相似、数字一致、且没有方向相反的关键字（如"增持"与"减持"）。"""
    if len(ga & gb) / len(ga | gb) < threshold:
        return False
    if set(_NUM_RE.findall(a)) != set(_NUM_RE.findall(b)):
        return False
    for x, y in _POLAR:
        if (x in a) != (x in b) or (y in a) != (y in b):
            return False
    return True


def dedupe_titles(app: App, company: Company) -> int:
    """与近 3 天更早的新闻几乎相同的新闻标记为重复，不送模型（语义层面的去重由模型在批内完成）。
    公告不参与：港交所"翌日披露報表"等标题天天相同但内容不同。"""
    since = iso(utcnow() - timedelta(days=3))
    kept: list[tuple[str, str, set[str]]] = []
    n = 0
    for r in app.db.recent_titles(company.id, since):
        if r["kind"] != "news":
            continue
        norm = r["title_norm"]
        grams = _bigrams(norm)
        first = next((uid for uid, t, g in kept if same_event(norm, t, grams, g)), None)
        if first is None:
            kept.append((r["uid"], norm, grams))
            continue
        if r["triaged_at"] is None:
            app.db.save_triage(r["uid"], {
                "relevant": True, "duplicate_of": first, "event_type": "other", "direction": "unclear",
                "materiality": 1, "novelty": "repeat", "horizon": "days",
                "summary_zh": "与另一条标题相同，已自动去重", "expectation": "", "follow_up": "",
            }, "dedupe")
            n += 1
    return n


def triage_company(app: App, company: Company) -> tuple[int, list[str]]:
    dedupe_titles(app, company)
    items = app.db.untriaged(company.id)
    if not items:
        return 0, []
    system = [TRIAGE_SYSTEM, app.company_context(company)]
    market = app.market_line(company)
    size = app.settings.report.triage_batch_size
    done, errors = 0, []
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        # 近 3 天已分级的重要事件：新条目与其中某条是同一事件时标为重复（跨批次去重）
        known = [e for e in app.db.items_since(company.id, iso(utcnow() - timedelta(days=3)), min_materiality=3)
                 if e["triaged_at"]][:30]
        known_text = ""
        if known:
            known_text = "近 3 天已分级的事件（新条目若与其中某条是同一事件，duplicate_of 填该条 id）：\n" + "\n".join(
                f"- id={short_id(e['uid'])}｜{fmt_local(e['published_at'])}｜{e['summary_zh'] or e['title']}" for e in known) + "\n\n"
        user = (
            f"今天是 {today_local():%Y-%m-%d}（北京时间）。以下是{company.name}的 {len(batch)} 条待分级信息。\n\n"
            f"最新行情：\n{market}\n\n{known_text}待分级：\n"
            + "\n".join(_describe(it, i) for i, it in enumerate(batch, 1))
            + "\n\n请逐条输出待分级条目的结果：id 原样返回，条目数与待分级条目一致。"
        )
        try:
            res = app.llm.complete("triage", system, user, schema=TRIAGE_SCHEMA, validate=_validate, max_tokens=16000)
        except LLMError as e:
            app.db.bump_triage_attempts([it["uid"] for it in batch])
            errors.append(f"✗ {company.name} 分级失败：{e}")
            continue
        by_short = {short_id(it["uid"]): it for it in batch}
        dup_targets = {**{short_id(e["uid"]): e for e in known}, **by_short}
        returned = set()
        for r in res.data["items"]:
            it = by_short.get(r["id"].strip()[:12])
            if it is None or it["uid"] in returned:
                continue
            dup = dup_targets.get((r.get("duplicate_of") or "").strip()[:12])
            r["duplicate_of"] = dup["uid"] if dup and dup["uid"] != it["uid"] else ""
            if it["source"] == "consensus":  # 程序计算的业绩对比：不是公告的重复，必须保留
                r["duplicate_of"], r["relevant"] = "", True
            if it["source"] == "twitter" and (it.get("snippet") or "").startswith(twitter.TAG):
                # 未经证实的社交内容（检索词 / 跟踪词 / 媒体账号来源）：重要性由代码封顶（默认不进入标的追踪）；共享账号与官方号按已证实的消息，不封顶
                r["materiality"] = min(int(r["materiality"]), app.settings.sources.twitter.feed_max_materiality)
            app.db.save_triage(it["uid"], r, res.model)
            returned.add(it["uid"])
        missing = [it["uid"] for it in batch if it["uid"] not in returned]
        if missing:
            app.db.bump_triage_attempts(missing)
            log.warning("%s：模型漏掉了 %d 条，下次重试", company.name, len(missing))
        done += len(returned)
    return done, errors


def run(app: App, companies: list[Company]) -> list[str]:
    reason = app.llm.unavailable_reason("triage")
    if reason:
        return [f"跳过事件分级：{reason}"]
    msgs: list[str] = []
    with ThreadPoolExecutor(max_workers=app.settings.llm.concurrency) as pool:
        for company, (n, errors) in zip(companies, pool.map(lambda c: triage_company(app, c), companies)):
            if n:
                msgs.append(f"{company.name} 分级 {n} 条")
            msgs += errors
    return msgs or ["没有需要分级的新信息"]


def event_label(item: dict) -> str:
    return EVENT_TYPES.get(item.get("event_type") or "", "—")


def direction_label(item: dict) -> str:
    return DIRECTIONS.get(item.get("direction") or "", "—")


def listing_market(app: App, company: Company, item: dict) -> str:
    """事件用哪个上市地的行情衡量价格反应：公告用其所属上市地，新闻用定价上市地。"""
    if item.get("listing"):
        return item["listing"].split(":")[0]
    return company.pricing_listing.market
