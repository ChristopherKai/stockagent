"""晋级：把候选写进观察清单（watchlist.yaml，保留注释、备份后写入），之后走现有的新公司初始化流程
（抓取 → 下载年报 → 分级 → 画像 → 深度报告 → 开始追踪）。由使用者点选，不自动晋级。"""

from __future__ import annotations

from pathlib import Path

from ..app import App
from ..config import Company
from ..utils import today_local
from . import store
from .buckets import POOL_LABEL, TIER_LABEL


class PromoteError(ValueError):
    pass


def locate(app: App, key: str) -> dict:
    if key.count(".") == 1 and key.split(".")[0].isdigit():  # secid
        u = store.stock(app.db, key)
        if u:
            return u
    rows = store.find(app.db, key)
    if not rows:
        raise PromoteError(f"股票池中找不到 {key}（先运行 stock screen refresh）")
    if len({r["secid"] for r in rows}) > 1 and not key.upper().startswith(("HK:", "A:")):
        raise PromoteError(f"{key} 对应多只股票：" + "、".join(f"{r['market']}:{r['code']} {r['name']}" for r in rows)
                           + "；请写成 HK:代码 或 A:代码")
    return rows[0]


def entry(app: App, u: dict) -> dict:
    """观察清单条目：A+H 公司两地都列上；备注写明入选来源与快筛要核实的问题。"""
    listings = [u]
    if u.get("org_code"):
        listings += [x for x in store.universe(app.db) if x["org_code"] == u["org_code"] and x["secid"] != u["secid"]]
    listings.sort(key=lambda x: x["market"] != "A")  # 只是排列顺序：定价上市地默认港股（Company.pricing_listing）
    last = next((h for h in reversed(store.all_hits(app.db)) if h["secid"] in {x["secid"] for x in listings}), None)
    note = f"由选股器加入（{today_local():%Y-%m-%d}）"
    watch = []
    if last:
        note += f"：{POOL_LABEL[last['pool']]}，{TIER_LABEL.get(last['tier'], last['tier'])}，入选价 {last['price']:.3g} {last['currency']}"
        q = last.get("quick") or {}
        if q.get("summary"):
            note += f"。快筛：{q['summary']}"
        watch = [x for x in q.get("questions") or []][:5]
    cid = f"{u['market'].lower()}{u['code']}"
    data = {"id": cid, "name": u["name"], "name_en": None, "aliases": [],
            "listings": [{"market": x["market"], "code": x["code"], "exchange": None, "benchmark": None} for x in listings],
            "sector": u.get("industry"), "notes": note, "watch_items": watch, "news_queries": {}}
    Company.model_validate(data)
    return data


def promote(app: App, key: str, watchlist_path: Path | None = None) -> tuple[str, str]:
    """返回 (公司 id, 说明)。已在观察清单里的只记一笔，不重复添加。"""
    from ..web import yamledit

    u = locate(app, key)
    existing = app.watchlist.get(u["code"])
    if existing is not None:
        store.mark_promoted(app.db, u["secid"], existing.id)
        return existing.id, f"{u['name']} 已在观察清单中（id：{existing.id}）"
    data = entry(app, u)
    path = watchlist_path or app.root / "watchlist.yaml"
    try:
        yamledit.upsert_company(path, data, None, app.data_dir / "backups", is_new=True)
    except yamledit.EditError as e:
        raise PromoteError(str(e)) from e
    (app.data_dir / "inbox" / data["id"]).mkdir(parents=True, exist_ok=True)
    store.mark_promoted(app.db, u["secid"], data["id"])
    return data["id"], f"已把 {u['name']}（{' / '.join(l['market'] + ':' + l['code'] for l in data['listings'])}）加入观察清单，id：{data['id']}"
