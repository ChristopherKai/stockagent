"""选股器的数据表（与核心表同库，由本模块在首次使用时建表；核心模块不读写这些表）。"""

from __future__ import annotations

import json

from ..db import DB
from ..utils import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS screen_universe (
    secid        TEXT PRIMARY KEY,         -- 东方财富 secid：116.00700 / 1.600519 / 0.000001
    market       TEXT NOT NULL,            -- HK | A
    code         TEXT NOT NULL,
    name         TEXT,
    org_code     TEXT,                     -- 发行人代码：A+H 公司两地相同
    industry     TEXT,
    org_type     TEXT,                     -- 一般企业 / 银行 / 保险 …（港股）
    st           INTEGER NOT NULL DEFAULT 0,
    listing_date TEXT,
    price        REAL,
    mcap         REAL,                     -- 全部股本按该上市地现价计的市值（上市地币种）
    currency     TEXT,                     -- 上市地币种 HKD / CNY
    report_ccy   TEXT,                     -- 报表币种（港股按指标反推）
    pe_ttm       REAL,
    pb           REAL,
    quote_date   TEXT,
    info_json    TEXT NOT NULL DEFAULT '{}',   -- 最新与上年同期主要指标、股息率等
    fcf_json     TEXT,                         -- emdata.fcf_data 形状的报表数据
    fcf_at       TEXT,
    active       INTEGER NOT NULL DEFAULT 1,   -- 最近一次刷新仍在股票池中
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_su_market ON screen_universe(market, active);

CREATE TABLE IF NOT EXISTS screen_bulk (
    key         TEXT PRIMARY KEY,          -- A:cf:2026-06-30 之类（某报告期的全市场报表，只存需要的列）
    fetched_at  TEXT NOT NULL,
    rows_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screen_events (
    uid          TEXT PRIMARY KEY,
    secid        TEXT NOT NULL,
    market       TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT,
    source       TEXT NOT NULL,            -- hkex | cninfo | forecast
    kind         TEXT NOT NULL,            -- 事件类型（见 events.KINDS）
    direction    TEXT NOT NULL,            -- positive | negative
    weight       REAL NOT NULL,
    title        TEXT NOT NULL,
    category     TEXT,
    url          TEXT,
    published_at TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_se_secid ON screen_events(secid, published_at);
CREATE INDEX IF NOT EXISTS idx_se_time ON screen_events(published_at);

CREATE TABLE IF NOT EXISTS screen_runs (
    id           INTEGER PRIMARY KEY,
    pool         TEXT NOT NULL,            -- long 长线池 | event 事件池
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',   -- running | done | failed
    params_json  TEXT NOT NULL DEFAULT '{}',
    stats_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS screen_hits (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL,
    pool          TEXT NOT NULL,
    secid         TEXT NOT NULL,
    market        TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    industry      TEXT,
    buckets       TEXT NOT NULL,           -- 逗号分隔：floor,quality,event,theme,revision
    score         REAL NOT NULL,
    tier          TEXT NOT NULL,           -- A 高胜率+高赔率 | B 高胜率 | C 高赔率 | D 观察 | X 快筛淘汰
    odds          REAL,                    -- 粗算赔率（代码按假设计算）
    upside        REAL,
    downside      REAL,
    price         REAL,                    -- 入选时的价格（前向检验的起点）
    price_date    TEXT,
    currency      TEXT,
    metrics_json  TEXT NOT NULL DEFAULT '{}',
    flags_json    TEXT NOT NULL DEFAULT '[]',
    events_json   TEXT NOT NULL DEFAULT '[]',
    quick_json    TEXT,                    -- 模型快筛结论
    created_at    TEXT NOT NULL,
    promoted_at   TEXT,
    company_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sh_run ON screen_hits(run_id, score);
CREATE INDEX IF NOT EXISTS idx_sh_secid ON screen_hits(secid, created_at);

CREATE TABLE IF NOT EXISTS screen_theme_ideas (
    id           INTEGER PRIMARY KEY,
    theme        TEXT NOT NULL,
    node         TEXT,
    secid        TEXT,
    market       TEXT,
    code         TEXT,
    name         TEXT,
    reason       TEXT,
    status       TEXT NOT NULL DEFAULT 'open',   -- open | accepted | dismissed
    created_at   TEXT NOT NULL
);
"""

JSON_FIELDS = ("info_json", "fcf_json", "params_json", "stats_json", "metrics_json", "flags_json", "events_json", "quick_json")


def ensure(db: DB) -> DB:
    if not getattr(db, "_screen_schema", False):
        with db.lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()
        db._screen_schema = True
    return db


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    for k in JSON_FIELDS:
        if k in row:
            row[k[:-5]] = json.loads(row[k]) if isinstance(row[k], str) else None
    return row


def _dump(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- 股票池

UNIVERSE_COLS = ("market", "code", "name", "org_code", "industry", "org_type", "st", "listing_date", "price", "mcap",
                 "currency", "report_ccy", "pe_ttm", "pb", "quote_date")


def upsert_universe(db: DB, rows: list[dict], market: str) -> int:
    """整体替换某个市场的股票池快照：本次没出现的标记为不活跃（保留历史，便于前向检验）。报表数据不动。"""
    ensure(db)
    now = iso(utcnow())
    with db.transaction() as c:
        c.execute("UPDATE screen_universe SET active=0 WHERE market=?", (market,))
        for r in rows:
            vals = [r.get(k) for k in UNIVERSE_COLS]
            c.execute(
                f"INSERT INTO screen_universe (secid, {', '.join(UNIVERSE_COLS)}, info_json, active, updated_at) "
                f"VALUES (?, {', '.join('?' * len(UNIVERSE_COLS))}, ?, 1, ?) "
                f"ON CONFLICT(secid) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in UNIVERSE_COLS)}, "
                "info_json=excluded.info_json, active=1, updated_at=excluded.updated_at",
                (r["secid"], *vals, _dump(r.get("info") or {}), now))
    return len(rows)


def set_fcf(db: DB, data: dict[str, dict]) -> None:
    now = iso(utcnow())
    with db.transaction() as c:
        c.executemany("UPDATE screen_universe SET fcf_json=?, fcf_at=? WHERE secid=?",
                      [(_dump(v), now, k) for k, v in data.items()])


def universe(db: DB, market: str | None = None, active_only: bool = True) -> list[dict]:
    ensure(db)
    sql, params = "SELECT * FROM screen_universe WHERE 1=1", []
    if market:
        sql += " AND market=?"
        params.append(market)
    if active_only:
        sql += " AND active=1"
    return [_decode(r) for r in db._all(sql + " ORDER BY market, code", params)]


def stock(db: DB, secid: str) -> dict | None:
    ensure(db)
    return _decode(db._one("SELECT * FROM screen_universe WHERE secid=?", (secid,)))


def find(db: DB, key: str) -> list[dict]:
    """按代码（600519、00700、700、HK:00700）或名称查找。"""
    ensure(db)
    k = key.strip().upper().removeprefix("HK:").removeprefix("A:").removesuffix(".HK")
    rows = db._all("SELECT * FROM screen_universe WHERE code=? OR code=? OR name=? ORDER BY active DESC",
                   (k, k.zfill(5) if k.isdigit() and len(k) <= 5 else k, key.strip()))
    return [_decode(r) for r in rows]


# ---------------------------------------------------------------- 批量报表缓存

def bulk_get(db: DB, key: str) -> tuple[str, list[dict]] | None:
    ensure(db)
    r = db._one("SELECT fetched_at, rows_json FROM screen_bulk WHERE key=?", (key,))
    return (r["fetched_at"], json.loads(r["rows_json"])) if r else None


def bulk_set(db: DB, key: str, rows: list[dict]) -> None:
    ensure(db)
    db._exec("INSERT OR REPLACE INTO screen_bulk (key, fetched_at, rows_json) VALUES (?, ?, ?)", (key, iso(utcnow()), _dump(rows)))


# ---------------------------------------------------------------- 事件

def add_events(db: DB, events: list[dict]) -> int:
    ensure(db)
    now = iso(utcnow())
    n = 0
    with db.transaction() as c:
        for e in events:
            cur = c.execute(
                "INSERT OR IGNORE INTO screen_events (uid, secid, market, code, name, source, kind, direction, weight, title, "
                "category, url, published_at, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (e["uid"], e["secid"], e["market"], e["code"], e.get("name"), e["source"], e["kind"], e["direction"],
                 e["weight"], e["title"], e.get("category"), e.get("url"), e["published_at"], now))
            n += cur.rowcount
    return n


def events_since(db: DB, since: str, secid: str | None = None) -> list[dict]:
    ensure(db)
    sql, params = "SELECT * FROM screen_events WHERE published_at>=?", [since]
    if secid:
        sql += " AND secid=?"
        params.append(secid)
    return db._all(sql + " ORDER BY published_at DESC", params)


# ---------------------------------------------------------------- 运行与候选

def start_run(db: DB, pool: str, params: dict) -> int:
    ensure(db)
    return db._exec("INSERT INTO screen_runs (pool, started_at, params_json) VALUES (?, ?, ?)",
                    (pool, iso(utcnow()), _dump(params))).lastrowid


def finish_run(db: DB, run_id: int, status: str, stats: dict) -> None:
    db._exec("UPDATE screen_runs SET status=?, finished_at=?, stats_json=? WHERE id=?", (status, iso(utcnow()), _dump(stats), run_id))


def runs(db: DB, pool: str | None = None, limit: int = 20) -> list[dict]:
    ensure(db)
    sql, params = "SELECT * FROM screen_runs", []
    if pool:
        sql += " WHERE pool=?"
        params.append(pool)
    return [_decode(r) for r in db._all(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])]


def latest_run(db: DB, pool: str) -> dict | None:
    ensure(db)
    return _decode(db._one("SELECT * FROM screen_runs WHERE pool=? AND status='done' ORDER BY id DESC LIMIT 1", (pool,)))


HIT_COLS = ("pool", "secid", "market", "code", "name", "industry", "buckets", "score", "tier", "odds", "upside", "downside",
            "price", "price_date", "currency")


def add_hits(db: DB, run_id: int, hits: list[dict]) -> None:
    now = iso(utcnow())
    with db.transaction() as c:
        for h in hits:
            cur = c.execute(
                f"INSERT INTO screen_hits (run_id, {', '.join(HIT_COLS)}, metrics_json, flags_json, events_json, quick_json, created_at) "
                f"VALUES (?, {', '.join('?' * len(HIT_COLS))}, ?, ?, ?, ?, ?)",
                (run_id, *[h.get(k) for k in HIT_COLS], _dump(h.get("metrics") or {}), _dump(h.get("flags") or []),
                 _dump(h.get("events") or []), _dump(h["quick"]) if h.get("quick") else None, now))
            h["id"] = cur.lastrowid


def hits(db: DB, run_id: int) -> list[dict]:
    ensure(db)
    return [_decode(r) for r in db._all("SELECT * FROM screen_hits WHERE run_id=? ORDER BY "
                                        "CASE tier WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 WHEN 'D' THEN 3 ELSE 4 END, score DESC", (run_id,))]


def hit(db: DB, hit_id: int) -> dict | None:
    ensure(db)
    return _decode(db._one("SELECT * FROM screen_hits WHERE id=?", (hit_id,)))


def all_hits(db: DB, since: str | None = None) -> list[dict]:
    ensure(db)
    return [_decode(r) for r in db._all("SELECT * FROM screen_hits WHERE created_at>=? ORDER BY id", (since or "",))]


def mark_promoted(db: DB, secid: str, company_id: str) -> None:
    """同一标的最近一次运行中的候选记为已送深研。"""
    db._exec("UPDATE screen_hits SET promoted_at=?, company_id=? WHERE id=(SELECT id FROM screen_hits WHERE secid=? "
             "ORDER BY id DESC LIMIT 1)", (iso(utcnow()), company_id, secid))


def promoted(db: DB) -> dict[str, str]:
    ensure(db)
    return {r["secid"]: r["company_id"] for r in db._all("SELECT secid, company_id FROM screen_hits WHERE company_id IS NOT NULL")}


# ---------------------------------------------------------------- 主题 agent 的建议

def add_theme_ideas(db: DB, ideas: list[dict]) -> int:
    ensure(db)
    now = iso(utcnow())
    n = 0
    with db.transaction() as c:
        for i in ideas:
            dup = c.execute("SELECT 1 FROM screen_theme_ideas WHERE theme=? AND secid=? AND status='open'",
                            (i["theme"], i.get("secid"))).fetchone()
            if dup:
                continue
            c.execute("INSERT INTO screen_theme_ideas (theme, node, secid, market, code, name, reason, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (i["theme"], i.get("node"), i.get("secid"), i.get("market"), i.get("code"), i.get("name"), i.get("reason"), now))
            n += 1
    return n


def theme_ideas(db: DB, status: str = "open") -> list[dict]:
    ensure(db)
    return db._all("SELECT * FROM screen_theme_ideas WHERE status=? ORDER BY theme, id", (status,))


def set_theme_idea(db: DB, idea_id: int, status: str) -> bool:
    ensure(db)
    return db._exec("UPDATE screen_theme_ideas SET status=? WHERE id=?", (status, idea_id)).rowcount > 0
