"""ima 知识库接入：每次抓取按公司检索标题（公司名、代码、英文名），新出现的条目进入分级，研报等 PDF 原文入库为资料、
新闻截图用本地 tesseract OCR；研究 agent 另有 ima_search / ima_read 工具按需检索。

为什么用检索而不是按目录浏览：目录接口与取原文接口各有每日次数上限（实测各约几十到一百次，超出返回 220021），
检索没有。检索只匹配标题、每个词最多返回约 100 条且不按时间排序，所以每家公司用几个不同的词取并集，
命中接近上限时提示可能漏掉新条目。原文按每日预算下载（sources.ima.max_downloads_per_day），配额用完的条目只收录标题。

知识条目不带发布时间：日期取标题里的日期，其次是图片 OCR 文字里的日期，再没有就用首次见到的时间。
某家公司第一次检索时只收录有日期的近期条目，其余只记为已见过（避免把旧内容当新消息）。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from .app import App, _item, _try
from .config import Company
from .docs import IMAGE_SUFFIXES, ocr_image, read_document_text, store_document
from .sources import ima
from .utils import CST, days_ago, iso, log, to_simplified, today_local, utcnow

SOURCE = "ima"
SEARCH_CAP = 95  # 检索结果达到这个数时可能被截断（服务端上限约 100）
_TRANSCRIPT = re.compile(r"纪要|紀要|调研|調研|电话会|電話會|交流|闭门会|閉門會|路演|业绩会|業績會")
_PAGE_MARK = re.compile(r"\[第\d+页\]")
_LATIN = re.compile(r"[\w .&-]+", re.A)

AGENT_HINT = ("ima 知识库（使用者订阅）收录外资与内资券商研报、调研纪要、彭博/路透等付费新闻：卖方观点、目标价、盈利预测、"
              "管理层交流优先用 ima_search 检索（按标题匹配：公司名、代码如 2577.HK / 300308、机构名），用 ima_read 读原文，"
              "入库后用 search_filings / read_filing 按页引用，出处写〔研报：机构 日期 第N页〕；搜不到再用 web_search。")


def enabled(app: App) -> bool:
    cfg = app.settings.sources.ima
    return cfg.enabled and bool(cfg.knowledge_bases) and ima.credentials() is not None


def knowledge_bases(app: App) -> list[dict]:
    """配置里的知识库（名称或 ID）解析成 [{id, name}]；ID 缓存在 kv，避免每次列出全部知识库。"""
    out, listed = [], None
    for name in app.settings.sources.ima.knowledge_bases:
        key = f"ima_kb:{name}"
        cached = app.db.kv_get(key)
        if cached:
            out.append(json.loads(cached))
            continue
        if listed is None:
            listed = ima.knowledge_bases(app.session)
        found = next((x for x in listed if name in (x["id"], x["name"])), None) or next((x for x in listed if name in x["name"]), None)
        if not found:
            raise ima.ImaError(-1, f"找不到知识库「{name}」（stock ima list 查看可访问的知识库）")
        kb = {"id": found["id"], "name": found["name"]}
        app.db.kv_set(key, json.dumps(kb, ensure_ascii=False))
        out.append(kb)
    return out


# ---------------------------------------------------------------- 检索词与标题匹配

def queries(company: Company, n: int) -> list[str]:
    """公司名、代码（2577.HK 这种写法、A 股 6 位代码、美股代码）、英文名；按顺序去重后取前 n 个。"""
    out = [company.name]
    for l in company.listings:
        out.append(f"{int(l.code)}.HK" if l.market == "HK" else l.code)
    out += [x for x in (company.name_en, *company.aliases) if x and _LATIN.fullmatch(x)]
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    return uniq[:n]


class Matcher:
    """标题是否提到这家公司：名称、别名、英文名，或代码（2577.HK / 0100.HK、300308、XPEV）。检索是子串匹配，要再核对一次。"""

    def __init__(self, company: Company):
        pats = []
        for n in company.names():
            s = to_simplified(n).strip()
            if _LATIN.fullmatch(s):
                if len(s) >= 4:  # 太短的英文名容易误配
                    pats.append(re.compile(rf"(?<![A-Za-z]){re.escape(s)}(?![A-Za-z])", re.I))
            elif len(s) >= 2:
                pats.append(re.compile(re.escape(s), re.I))
        for l in company.listings:
            if l.market == "HK":
                pats.append(re.compile(rf"(?<!\d)0*{int(l.code)}\.HK(?![A-Za-z])", re.I))
            elif l.market == "A":
                pats.append(re.compile(rf"(?<!\d){l.code}(?!\d)"))
            elif len(l.code) >= 3:
                pats.append(re.compile(rf"(?<![A-Za-z]){re.escape(l.code)}(?![A-Za-z])"))
        self.pats = pats

    def __call__(self, title: str) -> bool:
        t = to_simplified(title)
        return any(p.search(t) for p in self.pats)


# ---------------------------------------------------------------- 原文

def _budget(app: App) -> dict:
    """今天（北京时间）已用的原文获取次数；ima 返回配额用完后当天不再尝试。"""
    raw = app.db.kv_get("ima_downloads")
    b = json.loads(raw) if raw else {}
    return b if b.get("date") == today_local().isoformat() else {"date": today_local().isoformat(), "n": 0, "exhausted": False}


def downloads_left(app: App) -> int:
    b = _budget(app)
    return 0 if b["exhausted"] else max(0, app.settings.sources.ima.max_downloads_per_day - b["n"])


def _spend(app: App, exhausted: bool = False) -> None:
    b = _budget(app)
    b["n"] += 0 if exhausted else 1
    b["exhausted"] = b["exhausted"] or exhausted
    app.db.kv_set("ima_downloads", json.dumps(b))


def _clean_title(title: str) -> str:
    stem, _, suffix = title.rpartition(".")
    return stem.strip() if stem and suffix.lower() in {"pdf", "docx", "doc", "md", "txt", "html", "png", "jpg", "jpeg", "webp", "note", "xlsx"} else title.strip()


def read_entry(app: App, company: Company, entry: dict, published: date | None = None) -> tuple[dict | None, str]:
    """下载原文：PDF/Word 等入库为该公司资料（返回 (资料, 全文)）；图片 OCR（返回 (None, 文字)）。"""
    cfg = app.settings.sources.ima
    mt = entry["media_type"]
    if mt not in ima.DOWNLOADABLE or (mt == 9 and not cfg.ocr):
        raise ima.ImaError(-1, f"{ima.MEDIA_TYPES.get(mt, mt)}类条目无法通过接口读取原文，只能参考标题")
    if downloads_left(app) <= 0:
        raise ima.ImaError(ima.QUOTA, "今天的 ima 原文获取次数已用完（ima 每日配额或 sources.ima.max_downloads_per_day），明天再试")
    try:
        got = ima.media_url(app.session, entry["media_id"])
    except ima.ImaError as e:
        if e.code == ima.QUOTA:
            _spend(app, exhausted=True)
        raise
    _spend(app)
    if not got:
        raise ima.ImaError(ima.UNAVAILABLE, "ima 不提供该条目的原文（只能在 ima 客户端查看）")
    data = ima.download(app.session, got[0], got[1], cfg.max_download_mb << 20)
    title = _clean_title(entry["title"])
    suffix = Path(entry["title"]).suffix.lower()
    if not (mt == 9 and suffix in IMAGE_SUFFIXES):
        suffix = ima.DOWNLOADABLE[mt]
    tmp_dir = app.data_dir / "tmp" / "ima"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / (re.sub(r"[^\w.\-]+", "_", title)[:80] + suffix)
    tmp.write_bytes(data)
    try:
        if mt == 9:
            return None, ocr_image(tmp)
        published = published or ima.parse_date(entry["title"])
        doc, _ = store_document(app.db, app.data_dir, company.id, tmp,
                                doc_type="transcript" if _TRANSCRIPT.search(title) else "research",
                                period=published.isoformat() if published else None, title=title, source=SOURCE,
                                published_at=iso(_day(published)) if published else None)
        return doc, read_document_text(doc)
    finally:
        tmp.unlink(missing_ok=True)


def excerpt(text: str, n: int = 500) -> str:
    return re.sub(r"\s+", " ", _PAGE_MARK.sub(" ", text)).strip()[:n]


def _day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=CST)


# ---------------------------------------------------------------- 抓取

def ingest(app: App, company: Company, entry: dict, first: bool) -> tuple[int, int]:
    """一条新出现的条目：定日期、取原文、写入 items。返回 (新增条目数, 入库资料数)。"""
    cfg = app.settings.sources.ima
    cutoff = days_ago(app.settings.sources.news_lookback_days)
    pub = ima.parse_date(entry["title"])
    if (pub and _day(pub) < cutoff) or (pub is None and first):
        return 0, 0
    doc, text, note = None, "", ""
    if cfg.download and entry["media_type"] in ima.DOWNLOADABLE:
        try:
            doc, text = read_entry(app, company, entry, pub)
        except Exception as e:  # 取不到原文也收录标题
            log.info("ima 原文读取失败 %s：%s", entry["title"][:40], e)
            note = "（今日原文配额已用完，可用 ima_read 补读）" if isinstance(e, ima.ImaError) and e.code == ima.QUOTA else "（原文未能获取）"
    if pub is None and text:
        pub = ima.parse_date(text[:3000])
    published = _day(pub) if pub else utcnow()
    if published < cutoff:
        return 0, 0
    x = {"title": _clean_title(entry["title"]), "published_at": published, "source_id": entry["media_id"],
         "snippet": (f"[原文已入库：资料 #{doc['id']}] " if doc else note) + excerpt(text), "url": None,
         "media_type": entry["media_type"]}
    return int(app.db.add_item(_item(company, None, "news", SOURCE, x, ima.source_name(entry["title"])))), int(doc is not None)


def fetch(app: App, companies: list[Company]) -> list[str]:
    """按公司检索各知识库，新出现的条目写入 items。"""
    if not enabled(app):
        return []
    cfg = app.settings.sources.ima
    msgs = []
    for kb in knowledge_bases(app):
        calls = fresh = added = docs = 0
        capped, stopped, firsts = [], None, []
        for c in companies:
            key = f"fetched:ima:{kb['id']}:{c.id}"
            first = app.db.kv_get(key) is None
            match, hits = Matcher(c), {}
            for q in queries(c, cfg.queries_per_company):
                calls += 1
                try:
                    found = _try(app, SOURCE, ima.search, app.session, kb["id"], q)
                except ima.ImaError as e:
                    stopped = e.msg
                    break
                if found is None:
                    stopped = "连接失败"
                    break
                if len(found) >= SEARCH_CAP:
                    capped.append(q)
                hits.update((x["media_id"], x) for x in found if x["media_type"] != ima.FOLDER and match(x["title"]))
            if stopped:
                break
            seen = app.db.ima_seen_ids(c.id, list(hits))
            for e in (x for x in hits.values() if x["media_id"] not in seen):
                fresh += 1
                a, d = ingest(app, c, e, first)
                added, docs = added + a, docs + d
                app.db.ima_mark_seen(e["media_id"], c.id, kb["id"], e["title"])
            app.db.kv_set(key, iso(utcnow()))
            if first:
                firsts.append(c.name)
        line = f"ima「{kb['name']}」：检索 {calls} 次，新条目 {fresh}，新增 {added} 条，原文入库 {docs} 份（今日原文额度剩 {downloads_left(app)}）"
        if firsts:
            line += f"（{'、'.join(firsts)} 首次检索：只收录标题带日期的近 {app.settings.sources.news_lookback_days} 天内容）"
        if capped:
            line += f"；「{'」「'.join(capped)}」命中数已达上限，最新条目可能被截掉"
        msgs.append(f"✗ {line}；{stopped}，其余公司下次继续" if stopped else line)
    return msgs
