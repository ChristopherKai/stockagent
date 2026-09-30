"""新增公司：按市场与代码查东方财富公司概况（名称、英文名、行业、主营、其他上市地），生成观察清单条目并写入 watchlist.yaml。

只填基础信息；公司画像、深度报告由之后的初始化步骤生成。notes / watch_items 可以在 WebUI 里再补。
"""

from __future__ import annotations

import re

from zhconv import convert

from .app import App
from .config import Company, Listing
from .sources import emdata

_NAME_SUFFIX = re.compile(r"-(?:W|SW|B|S|R|WR|SWR|U)$", re.I)  # 港股简称后缀：同股不同权、未盈利生物科技等
_EN_CUT = re.compile(r"\s*(?:,|\bCo\b\.?|\bInc\b\.?|\bLtd\b\.?|\bLimited\b|\bCorporation\b|\bCorp\b\.?|\bHoldings?\b|\bGroup\b|"
                     r"\bCompany\b|\bPLC\b)", re.I)
# 英文名的第一个词不适合当 id 时（地名、泛称），用「市场+代码」
_GENERIC = {"china", "chinese", "the", "hong", "new", "great", "shanghai", "beijing", "shenzhen", "guangzhou", "hangzhou",
            "jiangsu", "zhejiang", "shandong", "sichuan", "anhui", "fujian", "hubei", "hunan", "henan", "tianjin", "chongqing",
            "sino", "asia", "global", "international", "first", "united", "semiconductor", "semiconductors", "technology",
            "technologies", "industrial", "industries", "bank", "agricultural", "petroleum", "power", "energy", "national"}


def short_name_en(name: str | None) -> str | None:
    """「InnoScience (Suzhou) Technology Holding Co., Ltd.」→「InnoScience Technology」，「XPeng Inc.」→「XPeng」。"""
    if not name:
        return None
    s = re.sub(r"\s*\([^)]*\)", "", name).strip()
    m = _EN_CUT.search(s)
    s = (s[: m.start()] if m and m.start() > 0 else s).strip(" .,")
    return s or None


def make_id(name_en: str | None, market: str, code: str, taken: set[str]) -> str:
    first = re.sub(r"[^a-z0-9]", "", (name_en or "").split()[0].lower()) if name_en else ""
    base = first if len(first) >= 3 and first not in _GENERIC and first[0].isalpha() else f"{market.lower()}{code.lower()}"
    return base if base not in taken else f"{base}-{code.lower()}"


_CODE = {"HK": re.compile(r"\d{1,5}"), "A": re.compile(r"\d{6}"), "US": re.compile(r"[A-Z][A-Z.\-]{0,7}")}


def normalize(market: str, code: str) -> tuple[str, str]:
    """校验市场与代码（WebUI 与后台任务的参数都经过这里）：港股 1–5 位数字、A 股 6 位数字、美股字母代码。"""
    market, code = (market or "").strip().upper(), (code or "").strip().upper()
    if market.endswith(".HK"):
        market = "HK"
    if market not in _CODE:
        raise ValueError(f"市场只能是 HK / A / US：{market}")
    code = code.removesuffix(".HK") if market == "HK" else code.split(".")[0] if market == "A" else code
    if not _CODE[market].fullmatch(code):
        raise ValueError({"HK": "港股代码为 1–5 位数字", "A": "A 股代码为 6 位数字", "US": "美股代码为字母（如 XPEV）"}[market] + f"：{code}")
    return market, Listing(market=market, code=code).code


def existing(app: App, market: str, code: str) -> Company | None:
    """这个上市地是否已在观察清单中。"""
    key = Listing(market=market, code=code).key
    return next((c for c in app.watchlist.companies if any(l.key == key for l in c.listings)), None)


def build(app: App, market: str, code: str) -> dict:
    """查公司概况，生成观察清单条目（dict，写法与 WebUI 表单一致）；已在清单中或查不到时抛出 ValueError。"""
    market, code = normalize(market, code)
    if c := existing(app, market, code):
        raise ValueError(f"{market} {code} 已在观察清单中：{c.name}（{c.id}）")
    p = emdata.company_profile(app.session, market, code)
    if not p or not p.get("name"):
        raise ValueError(f"东方财富查不到 {market} {code} 的公司资料，请检查市场与代码（或在「新增公司」表单里手动填写）")
    for m, cd, _ in p["listings"]:
        if c := existing(app, m, cd):
            raise ValueError(f"{p['name']} 的 {m} 上市地 {cd} 已在观察清单中：{c.name}（{c.id}）")
    name = _NAME_SUFFIX.sub("", p["name"])
    name_en = short_name_en(p.get("name_en"))
    aliases = []
    for n in [*(_NAME_SUFFIX.sub("", x) for x in p["names"]), convert(name, "zh-hk")]:
        if n and n != name and n not in aliases:
            aliases.append(n)
    # 「白酒Ⅱ-白酒Ⅲ」这类分级后缀去掉后再去重
    segments = [s for s in dict.fromkeys(re.sub(r"[ⅠⅡⅢⅣ]+$", "", x.strip()) for x in (p.get("industry") or "").split("-")) if s]
    main_market, main_code, _ = next(x for x in p["listings"] if x[0] == market)
    data = {
        "id": make_id(name_en, main_market, main_code, {c.id for c in app.watchlist.companies}),
        "name": name, "name_en": name_en, "aliases": aliases,
        "listings": [{"market": m, "code": cd, "exchange": ex, "benchmark": None} for m, cd, ex in p["listings"]],
        "sector": " · ".join(segments[-2:]) or None,
        "notes": (re.sub(r"\s+", " ", p["business"]).strip()[:300] + "（东方财富公司概况，自动填写）") if p.get("business") else None,
        "watch_items": [], "news_queries": {},
    }
    Company.model_validate(data)  # 与 WebUI 表单相同的校验
    return data


def save(app: App, data: dict) -> None:
    from .web import yamledit  # 与 WebUI 共用：保留注释、代码写成带引号的字符串、先备份

    yamledit.upsert_company(app.root / "watchlist.yaml", data, None, app.data_dir / "backups", is_new=True)
    (app.data_dir / "inbox" / data["id"]).mkdir(parents=True, exist_ok=True)


def describe(data: dict) -> str:
    listings = " / ".join(Listing(**{k: v for k, v in l.items() if k != "benchmark"}).display for l in data["listings"])
    return (f"{data['name']}（{listings}）id={data['id']}"
            + (f"｜英文名 {data['name_en']}" if data["name_en"] else "")
            + (f"｜别名 {'、'.join(data['aliases'])}" if data["aliases"] else "")
            + (f"｜{data['sector']}" if data["sector"] else ""))
