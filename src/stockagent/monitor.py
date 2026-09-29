"""论点跟踪：每天检查深度报告里的证伪条件和催化剂。

- 价格类条件（收盘价、距高点回撤、20 日涨跌、AH 溢价）由代码判定；
- 基本面/事件类条件由 LLM 对照自上次检查以来的新事件判定；
- 状态只升级不回退（ok → warning → triggered），已触发的条件等新一版深度报告生成后重置。
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel

from .app import SOURCE_LABEL, App
from .config import Company
from .dossier import ACTION_LABEL, METRIC_LABEL
from .llm import LLMError
from .triage import DIRECTIONS
from .utils import fmt_local, iso, num, pct, utcnow

RANK = {"ok": 0, "warning": 1, "triggered": 2}

EVENT_CHECK_SYSTEM = """你是风控研究员。给你一只股票的投资论点、若干条「证伪条件」，以及最近新发生的事件。
逐条判断：新事件是否表明某条证伪条件已经发生（triggered）、正在接近或出现苗头（warning），还是无关（ok）。
- 只依据给出的事件，不要推测；证据不足时判 ok 或 warning，不要轻易判 triggered。
- evidence 写明依据的事件（日期、来源、关键事实），判 ok 时写空字符串。"""

EVENT_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "status": {"type": "string", "enum": ["ok", "warning", "triggered"]},
                           "evidence": {"type": "string"}},
            "required": ["id", "status", "evidence"], "additionalProperties": False}},
    },
    "required": ["results"],
    "additionalProperties": False,
}


class _Result(BaseModel):
    id: int
    status: str
    evidence: str = ""


class _Results(BaseModel):
    results: list[_Result]


def _listing(company: Company, key: str | None):
    return next((l for l in company.listings if l.key == key), company.listings[0])


def price_value(app: App, company: Company, trig: dict) -> float | None:
    metric = trig["metric"]
    if metric == "ah_premium":
        ah = app.ah_premium(company)
        return ah["current"] if ah else None
    s = app.listing_snapshot(_listing(company, trig.get("listing")))
    if not s:
        return None
    if metric == "close":
        return s["close"]
    if metric == "drawdown_from_high":
        return 1 - s["close"] / s["high_250"]
    if metric == "ret_20d":
        return s["ret_20d"]
    return None


def _fmt(metric: str, v: float) -> str:
    return num(v) if metric == "close" else pct(v, 1, sign=metric in ("ret_20d", "ah_premium"))


def check_price(app: App, company: Company) -> list[str]:
    msgs = []
    for t in app.db.triggers(company.id):
        if t["kind"] != "price" or t["status"] == "triggered":
            continue
        v = price_value(app, company, t)
        if v is None:
            continue
        hit = v < t["threshold"] if t["operator"] == "<" else v > t["threshold"]
        evidence = f"{t['listing']} {METRIC_LABEL.get(t['metric'], t['metric'])} 当前 {_fmt(t['metric'], v)}，阈值 {t['operator']} {_fmt(t['metric'], t['threshold'])}"
        if app.db.update_trigger(t["id"], "triggered" if hit else "ok", evidence):
            msgs.append(f"{company.name}：证伪条件触发【{ACTION_LABEL[t['action']]}】{t['description']}（{evidence}）")
    return msgs


def check_events(app: App, company: Company) -> list[str]:
    triggers = [t for t in app.db.triggers(company.id) if t["kind"] != "price" and t["status"] != "triggered"]
    key = f"monitor_checked:{company.id}"
    since = app.db.kv_get(key) or iso(utcnow() - timedelta(days=3))
    events = [e for e in app.db.items_since(company.id, since, min_materiality=3)]
    if not triggers or not events:
        app.db.kv_set(key, iso(utcnow()))
        return []
    if reason := app.llm.unavailable_reason("monitor"):
        return [f"跳过 {company.name} 的事件类条件检查：{reason}"]
    dossier = app.db.latest_dossier(company.id)
    user = (
        f"投资论点：{dossier['card']['thesis'] if dossier else '（无）'}\n\n证伪条件：\n"
        + "\n".join(f"- id={t['id']}（{'基本面' if t['kind'] == 'fundamental' else '事件'}）{t['description']}" for t in triggers)
        + "\n\n新事件：\n"
        + "\n".join(f"- {fmt_local(e['published_at'])}｜{SOURCE_LABEL.get(e['source'], e['source'])}｜重要性{e['materiality']}｜"
                    f"{DIRECTIONS.get(e['direction'], '')}｜{e['summary_zh'] or e['title']}" for e in events[:40])
    )
    try:
        res = app.llm.complete("monitor", [EVENT_CHECK_SYSTEM], user, schema=EVENT_CHECK_SCHEMA,
                               validate=lambda d: _Results.model_validate(d).model_dump(), max_tokens=4000)
    except LLMError as e:
        return [f"✗ {company.name} 事件类条件检查失败：{e}"]
    by_id = {t["id"]: t for t in triggers}
    msgs = []
    for r in res.data["results"]:
        t = by_id.get(r["id"])
        if t is None or r["status"] not in RANK or RANK[r["status"]] <= RANK[t["status"]]:
            continue  # 只升级不回退
        if app.db.update_trigger(t["id"], r["status"], r["evidence"]):
            msgs.append(f"{company.name}：证伪条件触发【{ACTION_LABEL[t['action']]}】{t['description']}（{r['evidence']}）")
        elif r["status"] == "warning":
            msgs.append(f"{company.name}：证伪条件出现苗头 · {t['description']}（{r['evidence']}）")
    app.db.kv_set(key, iso(utcnow()))
    return msgs


def run(app: App, companies: list[Company]) -> list[str]:
    msgs = []
    for c in companies:
        if not app.db.triggers(c.id):
            continue
        msgs += check_price(app, c)
        msgs += check_events(app, c)
    return msgs


def upcoming(app: App, company: Company, days: int = 30) -> list[dict]:
    """未来 days 天内到期、或 7 天内刚过期的催化剂（没有具体日期的不列入）。"""
    today = date.today()
    out = []
    for c in app.db.catalysts(company.id):
        if c["due_date"] is None:
            continue
        d = date.fromisoformat(c["due_date"])
        if today - timedelta(days=7) <= d <= today + timedelta(days=days):
            out.append(c)
    return out


def trigger_alerts(app: App) -> list[tuple[dict, Company | None]]:
    rows = app.db.pending_trigger_alerts()
    return [(r, app.watchlist.get(r["company_id"])) for r in rows]
