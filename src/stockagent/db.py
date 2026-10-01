"""SQLite 存储。所有时间字段为 UTC ISO 字符串。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from .utils import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    uid           TEXT UNIQUE NOT NULL,
    company_id    TEXT NOT NULL,
    listing       TEXT,
    kind          TEXT NOT NULL,          -- announcement | news | data（程序发现的数据异动，如 CCASS） | manual
    source        TEXT NOT NULL,          -- hkex | cninfo | sec | eastmoney | google_news | ima | ccass | manual
    source_name   TEXT,                   -- 媒体名 / 公告类别
    title         TEXT NOT NULL,
    title_norm    TEXT NOT NULL,
    snippet       TEXT,
    url           TEXT,
    published_at  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    raw_json      TEXT,
    triage_attempts INTEGER NOT NULL DEFAULT 0,
    triaged_at    TEXT,
    triage_model  TEXT,
    relevant      INTEGER,
    duplicate_of  TEXT,
    event_type    TEXT,
    direction     TEXT,
    materiality   INTEGER,
    novelty       TEXT,
    horizon       TEXT,
    summary_zh    TEXT,
    expectation   TEXT,
    follow_up     TEXT,
    alerted_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_company_time ON items(company_id, published_at);
CREATE INDEX IF NOT EXISTS idx_items_triage ON items(triaged_at);

CREATE TABLE IF NOT EXISTS bars (
    secid    TEXT NOT NULL,
    date     TEXT NOT NULL,
    open     REAL, close REAL, high REAL, low REAL,
    volume   REAL, amount REAL, turnover REAL,
    PRIMARY KEY (secid, date)
);

CREATE TABLE IF NOT EXISTS quotes (
    secid     TEXT PRIMARY KEY,
    taken_at  TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    company_id   TEXT NOT NULL,
    doc_type     TEXT NOT NULL,
    period       TEXT,
    title        TEXT,
    source       TEXT NOT NULL,           -- manual | hkex | cninfo | sec
    source_url   TEXT,
    file_path    TEXT NOT NULL,
    text_path    TEXT NOT NULL,
    sha256       TEXT UNIQUE NOT NULL,
    pages        INTEGER,
    chars        INTEGER,
    published_at TEXT,
    added_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_company ON documents(company_id, doc_type);

CREATE TABLE IF NOT EXISTS manual_data (
    id          INTEGER PRIMARY KEY,
    company_id  TEXT NOT NULL DEFAULT '',  -- 空字符串表示行业/宏观指标
    metric      TEXT NOT NULL,
    period      TEXT NOT NULL,
    value       REAL,
    unit        TEXT,
    note        TEXT,
    source      TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE (company_id, metric, period)
);

CREATE TABLE IF NOT EXISTS profiles (
    company_id TEXT PRIMARY KEY,
    profile_md TEXT NOT NULL,
    doc_ids    TEXT NOT NULL,             -- JSON 数组：生成画像时用到的文档
    model      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ccass (            -- 港股中央结算系统（CCASS）持股快照：每只港股每个交易日一行
    code          TEXT NOT NULL,              -- 港股代码（5 位）
    date          TEXT NOT NULL,              -- 持股日期（港交所日终数据，YYYY-MM-DD）
    issued        INTEGER,                    -- 已发行股份（港交所最近更新数）
    summary_json  TEXT NOT NULL,              -- 各类参与者合计：{类别: {shares, participants, pct}}
    participants_json TEXT NOT NULL,          -- 持股最多的参与者 [[编号, 名称, 股数]]（只存前 sources.ccass_keep 名）
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS ima_seen (         -- ima 知识库中已处理过的条目（按公司检索到的；收录与否都记）
    media_id    TEXT NOT NULL,
    company_id  TEXT NOT NULL,
    kb_id       TEXT NOT NULL,
    title       TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (media_id, company_id)
);

CREATE TABLE IF NOT EXISTS dossiers (
    id            INTEGER PRIMARY KEY,
    company_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    model         TEXT,
    report_md     TEXT NOT NULL,
    card_json     TEXT NOT NULL,          -- 跟踪卡：论点、情景、证伪条件、催化剂、跟踪计划
    notes_json    TEXT NOT NULL,          -- 各研究子任务的笔记
    tool_log_json TEXT NOT NULL,          -- 工具调用记录（研究过程与出处）
    html_path     TEXT,
    pdf_path      TEXT
);

CREATE TABLE IF NOT EXISTS triggers (
    id           INTEGER PRIMARY KEY,
    company_id   TEXT NOT NULL,
    dossier_id   INTEGER,
    description  TEXT NOT NULL,
    kind         TEXT NOT NULL,           -- price（代码判定）| fundamental | event（LLM 判定）
    metric       TEXT, listing TEXT, operator TEXT, threshold REAL,
    action       TEXT NOT NULL,           -- review 复核 | reduce 减仓 | exit 退出 | upgrade 上调判断
    direction    TEXT NOT NULL DEFAULT 'down',  -- down 情况比判断更差 | up 情况比判断更好（与立场相反的方向才是证伪）
    active       INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'ok',   -- ok | warning | triggered
    evidence     TEXT,
    checked_at   TEXT, triggered_at TEXT, alerted_at TEXT
);

CREATE TABLE IF NOT EXISTS catalysts (
    id          INTEGER PRIMARY KEY,
    company_id  TEXT NOT NULL,
    dossier_id  INTEGER,
    when_text   TEXT,
    due_date    TEXT,                     -- 能解析出的最早日期（YYYY-MM-DD），否则为空
    event       TEXT NOT NULL,
    kpi         TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    company_id  TEXT PRIMARY KEY,
    dossier_id  INTEGER,
    plan_json   TEXT NOT NULL,            -- 产业链相关标的、新闻关键词、需跟踪的指标
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_requests (
    id          INTEGER PRIMARY KEY,
    company_id  TEXT NOT NULL,
    dossier_id  INTEGER,
    item        TEXT NOT NULL,            -- 缺失的数据项
    why         TEXT,                     -- 为什么关键
    ask         TEXT,                     -- 请使用者补充什么、建议来源、补充方式
    status      TEXT NOT NULL DEFAULT 'open',   -- open | done（使用者已补充）| dismissed | resolved（新报告不再需要）
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    UNIQUE (company_id, item)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY,
    task          TEXT, profile TEXT, model TEXT,
    input_tokens  INTEGER, output_tokens INTEGER,
    cache_read    INTEGER, cache_write INTEGER,
    started_at    TEXT, duration_s REAL,
    ok            INTEGER, error TEXT
);

CREATE TABLE IF NOT EXISTS trackers (
    company_id   TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    dossier_id   INTEGER,                 -- 当前估值基线来自哪份深度报告
    revision_id  INTEGER,                 -- 最新一条修正记录（含基线）
    checked_at   TEXT,                    -- 上次检查时间
    last_msg     TEXT                     -- 上次检查的结果（一句话）
);

CREATE TABLE IF NOT EXISTS revisions (
    id              INTEGER PRIMARY KEY,
    company_id      TEXT NOT NULL,
    dossier_id      INTEGER,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,        -- baseline 基线 | revision 修正 | review 已评估、维持不变
    origin          TEXT,                 -- cli | run | web | scheduler | dossier
    price           REAL,                 -- 当时定价上市地的最新收盘价
    price_date      TEXT,
    fx              REAL,
    target          REAL,                 -- 目标价（情景加权估值，按 tracker.target 口径）
    prev_target     REAL,
    bull REAL, base REAL, bear REAL,
    stance          TEXT,
    thesis_status   TEXT,                 -- intact | strengthened | weakened | broken
    summary         TEXT,
    narrative_md    TEXT,
    changes_json    TEXT NOT NULL DEFAULT '[]',   -- 已应用的假设变化（原值、新值、操作、理由、依据）
    rejected_json   TEXT NOT NULL DEFAULT '[]',   -- 未采纳的修改及原因
    events_json     TEXT NOT NULL DEFAULT '[]',   -- 本次看过的依据及评估
    catalysts_json  TEXT NOT NULL DEFAULT '[]',   -- 催化剂评估（含文字快照：新报告会删除催化剂）
    triggers_json   TEXT NOT NULL DEFAULT '{}',   -- 证伪条件状态快照 {id: status}
    flags_json      TEXT NOT NULL DEFAULT '[]',
    unverified_json TEXT NOT NULL DEFAULT '[]',   -- 修正说明中找不到出处的数字
    notes_md        TEXT,                         -- 修正研究笔记
    tool_log_json   TEXT NOT NULL DEFAULT '[]',
    model_json      TEXT NOT NULL,                -- 修正后的估值模型快照
    llm_model       TEXT
);
CREATE INDEX IF NOT EXISTS idx_revisions_company ON revisions(company_id, id);

CREATE TABLE IF NOT EXISTS tracker_refs (
    company_id   TEXT NOT NULL,
    ref          TEXT NOT NULL,           -- item:<uid> | trig:<id>:<status> | doc:<id> | man:<id>:<imported_at> | cat:<dossier>:<hash>
    revision_id  INTEGER,
    impact       TEXT NOT NULL,           -- none | minor | material | unassessed | superseded | hit | miss | partial
    created_at   TEXT NOT NULL,
    PRIMARY KEY (company_id, ref)
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    task         TEXT NOT NULL,
    argv_json    TEXT NOT NULL,           -- 各步骤的子命令参数（不含 -c 配置路径）
    title        TEXT NOT NULL,
    company_id   TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed | cancelled | interrupted
    source       TEXT NOT NULL,           -- web | scheduler | tracker
    note         TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    pid          INTEGER,
    exit_code    INTEGER,
    log_path     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);

CREATE TABLE IF NOT EXISTS chats (          -- 报告研读对话（只供理解报告，不作为追踪修正或深度报告的依据）
    id           INTEGER PRIMARY KEY,
    company_id   TEXT NOT NULL,
    dossier_id   INTEGER NOT NULL,
    revision_id  INTEGER,                 -- 在修正页发起的对话：讨论的是这次修正
    title        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chats_company ON chats(company_id, updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id             INTEGER PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    role           TEXT NOT NULL,         -- user | assistant
    content        TEXT NOT NULL DEFAULT '',
    quote          TEXT,                  -- 使用者引用的报告段落
    status         TEXT NOT NULL DEFAULT 'done',  -- pending | done | error（assistant）
    progress       TEXT,                  -- 回答进行中：最近在做什么（调用了哪个工具）
    checks_json    TEXT NOT NULL DEFAULT '{}',    -- 代码核对：报告引文是否在原文中、数字能否溯源
    tool_log_json  TEXT NOT NULL DEFAULT '[]',
    trace_json     TEXT NOT NULL DEFAULT '[]',    -- 回答过程：思考、工具调用及结果、中间说明（按顺序）
    model          TEXT,
    profile        TEXT,
    usage_json     TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id, id);

CREATE TABLE IF NOT EXISTS consensus (        -- 一致预期快照：程序每天记录（A 股、美股）或使用者录入（港股）；比较时取业绩发布前最近一份
    id          INTEGER PRIMARY KEY,
    company_id  TEXT NOT NULL,
    source      TEXT NOT NULL,                -- em_a 东方财富机构预测（A 股）| nasdaq | manual 使用者录入
    listing     TEXT,                         -- 数据对应的上市地（A:300308 / US:XPEV / HK:09868）
    taken_on    TEXT NOT NULL,                -- 数据日期（北京时间 YYYY-MM-DD）；录入时可填数据源上的日期
    fiscal_year INTEGER NOT NULL,
    metric      TEXT NOT NULL,                -- eps 每股收益 | profit 归母净利（元）| revenue 营业收入（元）
    value       REAL NOT NULL,
    currency    TEXT,                         -- CNY / HKD / USD
    per_ads     INTEGER NOT NULL DEFAULT 0,   -- 1 = 每 ADS 口径（Nasdaq 的中概股）
    n           INTEGER,                      -- 机构数
    high        REAL,
    low         REAL,
    note        TEXT,                         -- 来源说明（如「富途」「彭博」）
    created_at  TEXT NOT NULL,
    UNIQUE (company_id, source, taken_on, fiscal_year, metric)
);
CREATE INDEX IF NOT EXISTS idx_consensus_company ON consensus(company_id, fiscal_year, metric, taken_on);

CREATE TABLE IF NOT EXISTS earnings (         -- 业绩 vs 发布前一致预期（consensus.py 代码计算）
    company_id  TEXT NOT NULL,
    period      TEXT NOT NULL,                -- 报告期截止日（YYYY-MM-DD）
    stage       TEXT NOT NULL,                -- forecast 业绩预告 | express 业绩快报 | report 定期报告
    notice_date TEXT,                         -- 发布日（北京时间）
    data_json   TEXT NOT NULL,                -- 对比结果（含所用一致预期的来源、家数、日期与标注）
    item_uid    TEXT,                         -- 生成的「一致预期对比」条目；没有可比的一致预期时为空
    created_at  TEXT NOT NULL,
    PRIMARY KEY (company_id, period, stage)
);
"""

SCHEMA_VERSION = 4  # PRAGMA user_version；以后给已有表加列时在 DB.__init__ 里按版本号执行 ALTER
MIGRATIONS = {  # 版本号 → 升到该版本要补的列（表, 列, 定义）；新库由 SCHEMA 直接建好，已有的列跳过
    2: [("chat_messages", "trace_json", "TEXT NOT NULL DEFAULT '[]'")],
    3: [("chats", "revision_id", "INTEGER")],
    4: [("triggers", "direction", "TEXT NOT NULL DEFAULT 'down'")],
}

TRIAGE_FIELDS = (
    "relevant", "duplicate_of", "event_type", "direction", "materiality",
    "novelty", "horizon", "summary_zh", "expectation", "follow_up",
)


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # LLM 调用在线程池中执行并记录用量，因此允许跨线程使用，写操作由锁串行化。
        # WebUI 进程与子进程任务会同时写库：等锁最多 30 秒（默认 5 秒）
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                for v in range(version + 1, SCHEMA_VERSION + 1):
                    for table, col, ddl in MIGRATIONS.get(v, []):
                        if col not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _all(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params=()) -> dict | None:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    @contextmanager
    def transaction(self):
        """多语句写入：BEGIN IMMEDIATE 一开始就拿写锁，避免读后再写时与其他进程冲突（SQLITE_BUSY_SNAPSHOT 不会等待）。"""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.rollback()
                raise
            self.conn.commit()

    # ------------------------------------------------------------ kv

    def kv_get(self, key: str) -> str | None:
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else None

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def kv_delete(self, key: str) -> bool:
        return self._exec("DELETE FROM kv WHERE key=?", (key,)).rowcount > 0

    def kv_prefix(self, prefix: str) -> dict[str, str]:
        rows = self._all("SELECT key, value FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key", (len(prefix), prefix))
        return {r["key"]: r["value"] for r in rows}

    # ------------------------------------------------------------ items

    def add_item(self, item: dict) -> bool:
        """按 uid 去重插入，返回是否为新条目。"""
        cols = ("uid", "company_id", "listing", "kind", "source", "source_name", "title",
                "title_norm", "snippet", "url", "published_at", "raw_json")
        values = [item.get(c) for c in cols]
        cur = self._exec(
            f"INSERT OR IGNORE INTO items({', '.join(cols)}, fetched_at) VALUES({', '.join('?' * len(cols))}, ?)",
            (*values, iso(utcnow())),
        )
        return cur.rowcount > 0

    def untriaged(self, company_id: str, max_attempts: int = 3) -> list[dict]:
        return self._all(
            "SELECT * FROM items WHERE company_id=? AND triaged_at IS NULL AND triage_attempts < ? ORDER BY published_at",
            (company_id, max_attempts),
        )

    def bump_triage_attempts(self, uids: list[str]) -> None:
        with self.lock:
            self.conn.executemany("UPDATE items SET triage_attempts = triage_attempts + 1 WHERE uid=?", [(u,) for u in uids])
            self.conn.commit()

    def save_triage(self, uid: str, result: dict, model: str) -> None:
        sets = ", ".join(f"{f}=?" for f in TRIAGE_FIELDS)
        vals = [result.get(f) for f in TRIAGE_FIELDS]
        vals[0] = int(bool(vals[0]))
        self._exec(f"UPDATE items SET {sets}, triaged_at=?, triage_model=? WHERE uid=?", (*vals, iso(utcnow()), model, uid))

    def items_since(self, company_id: str, since: str, min_materiality: int | None = None) -> list[dict]:
        sql = "SELECT * FROM items WHERE company_id=? AND published_at>=?"
        params: list = [company_id, since]
        if min_materiality is not None:
            sql += " AND relevant=1 AND (duplicate_of IS NULL OR duplicate_of='') AND materiality>=?"
            params.append(min_materiality)
        return self._all(sql + " ORDER BY published_at DESC", params)

    def items_triaged_since(self, company_id: str, since: str, min_materiality: int) -> list[dict]:
        """某个时间之后完成分级的重要事件（按分级时间而不是发布时间：补抓、补分级的条目不会漏掉）。"""
        return self._all(
            "SELECT * FROM items WHERE company_id=? AND triaged_at>? AND relevant=1 AND (duplicate_of IS NULL OR duplicate_of='') "
            "AND materiality>=? ORDER BY published_at DESC",
            (company_id, since, min_materiality),
        )

    def recent_titles(self, company_id: str, since: str) -> list[dict]:
        return self._all(
            "SELECT uid, kind, title_norm, published_at, triaged_at FROM items WHERE company_id=? AND published_at>=? ORDER BY published_at",
            (company_id, since),
        )

    def pending_alerts(self, min_materiality: int) -> list[dict]:
        return self._all(
            "SELECT * FROM items WHERE alerted_at IS NULL AND relevant=1 AND (duplicate_of IS NULL OR duplicate_of='') "
            "AND materiality>=? ORDER BY materiality DESC, published_at DESC",
            (min_materiality,),
        )

    def mark_alerted(self, uids: list[str]) -> None:
        now = iso(utcnow())
        with self.lock:
            self.conn.executemany("UPDATE items SET alerted_at=? WHERE uid=?", [(now, u) for u in uids])
            self.conn.commit()

    def mark_all_alerted(self) -> None:
        self._exec("UPDATE items SET alerted_at=? WHERE alerted_at IS NULL AND triaged_at IS NOT NULL", (iso(utcnow()),))

    # ------------------------------------------------------------ 行情

    def upsert_bars(self, secid: str, bars: list[dict]) -> int:
        rows = [(secid, b["date"], b["open"], b["close"], b["high"], b["low"], b["volume"], b["amount"], b.get("turnover"))
                for b in bars]
        with self.lock:
            self.conn.executemany(
                "INSERT INTO bars(secid, date, open, close, high, low, volume, amount, turnover) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(secid, date) DO UPDATE SET open=excluded.open, close=excluded.close, high=excluded.high, "
                "low=excluded.low, volume=excluded.volume, amount=excluded.amount, turnover=excluded.turnover",
                rows,
            )
            self.conn.commit()
        return len(rows)

    def bars(self, secid: str, limit: int = 400) -> pd.DataFrame:
        rows = self._all(
            "SELECT * FROM (SELECT * FROM bars WHERE secid=? ORDER BY date DESC LIMIT ?) ORDER BY date", (secid, limit)
        )
        return pd.DataFrame(rows)

    def save_quote(self, secid: str, data: dict) -> None:
        self._exec(
            "INSERT INTO quotes(secid, taken_at, data_json) VALUES(?,?,?) "
            "ON CONFLICT(secid) DO UPDATE SET taken_at=excluded.taken_at, data_json=excluded.data_json",
            (secid, iso(utcnow()), json.dumps(data, ensure_ascii=False)),
        )

    def quote(self, secid: str) -> dict | None:
        r = self._one("SELECT * FROM quotes WHERE secid=?", (secid,))
        return {**json.loads(r["data_json"]), "taken_at": r["taken_at"]} if r else None

    # ------------------------------------------------------------ CCASS 持股快照

    def save_ccass(self, code: str, snap: dict, keep: int) -> None:
        rows = [[p["id"], p["name"], p["shares"]] for p in snap["participants"][:keep]]
        self._exec(
            "INSERT INTO ccass(code, date, issued, summary_json, participants_json, fetched_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(code, date) DO UPDATE SET issued=excluded.issued, summary_json=excluded.summary_json, "
            "participants_json=excluded.participants_json, fetched_at=excluded.fetched_at",
            (code, snap["date"], snap.get("issued"), json.dumps(snap.get("summary") or {}, ensure_ascii=False),
             json.dumps(rows, ensure_ascii=False), iso(utcnow())),
        )

    def ccass(self, code: str, on_or_before: str | None = None) -> dict | None:
        """on_or_before 当日或之前最近的一份快照；不填为最新一份。形状与 sources.ccass.parse 相同。"""
        r = (self._one("SELECT * FROM ccass WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1", (code, on_or_before))
             if on_or_before else self._one("SELECT * FROM ccass WHERE code=? ORDER BY date DESC LIMIT 1", (code,)))
        if not r:
            return None
        return {"date": r["date"], "issued": r["issued"], "summary": json.loads(r["summary_json"]),
                "participants": [{"id": i, "name": n, "shares": sh} for i, n, sh in json.loads(r["participants_json"])]}

    # ------------------------------------------------------------ ima 知识库

    def ima_seen_ids(self, company_id: str, media_ids: list[str]) -> set[str]:
        if not media_ids:
            return set()
        rows = self._all(f"SELECT media_id FROM ima_seen WHERE company_id=? AND media_id IN ({','.join('?' * len(media_ids))})",
                         (company_id, *media_ids))
        return {r["media_id"] for r in rows}

    def ima_mark_seen(self, media_id: str, company_id: str, kb_id: str, title: str) -> None:
        self._exec("INSERT OR IGNORE INTO ima_seen(media_id, company_id, kb_id, title, seen_at) VALUES(?, ?, ?, ?, ?)",
                   (media_id, company_id, kb_id, title, iso(utcnow())))

    # ------------------------------------------------------------ 文档

    def doc_by_sha(self, sha: str) -> dict | None:
        return self._one("SELECT * FROM documents WHERE sha256=?", (sha,))

    def add_document(self, doc: dict) -> int:
        cols = ("company_id", "doc_type", "period", "title", "source", "source_url", "file_path",
                "text_path", "sha256", "pages", "chars", "published_at")
        cur = self._exec(
            f"INSERT INTO documents({', '.join(cols)}, added_at) VALUES({', '.join('?' * len(cols))}, ?)",
            (*[doc.get(c) for c in cols], iso(utcnow())),
        )
        return cur.lastrowid

    def documents(self, company_id: str | None = None) -> list[dict]:
        if company_id:
            return self._all("SELECT * FROM documents WHERE company_id=? ORDER BY COALESCE(published_at, added_at) DESC", (company_id,))
        return self._all("SELECT * FROM documents ORDER BY company_id, COALESCE(published_at, added_at) DESC")

    def has_document(self, company_id: str, doc_type: str, period: str) -> bool:
        return self._one(
            "SELECT 1 FROM documents WHERE company_id=? AND doc_type=? AND period=?", (company_id, doc_type, period)
        ) is not None

    # ------------------------------------------------------------ 手动数据

    def upsert_manual(self, company_id: str, metric: str, period: str, value, unit=None, note=None, source=None) -> None:
        self._exec(
            "INSERT INTO manual_data(company_id, metric, period, value, unit, note, source, imported_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(company_id, metric, period) DO UPDATE SET value=excluded.value, unit=excluded.unit, "
            "note=excluded.note, source=excluded.source, imported_at=excluded.imported_at",
            (company_id or "", metric, period, value, unit, note, source or "manual", iso(utcnow())),
        )

    # ------------------------------------------------------------ 一致预期与业绩对比

    def save_consensus(self, company_id: str, source: str, taken_on: str, rows: list[dict], listing: str | None = None) -> int:
        """保存一致预期快照；同一来源、日期、财年、指标的旧值被覆盖。rows：[{fiscal_year, metric, value, currency, per_ads, n, high, low, note}]"""
        now = iso(utcnow())
        with self.lock:
            self.conn.executemany(
                "INSERT INTO consensus(company_id, source, listing, taken_on, fiscal_year, metric, value, currency, per_ads, n, high, low, "
                "note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(company_id, source, taken_on, fiscal_year, metric) DO UPDATE SET listing=excluded.listing, "
                "value=excluded.value, currency=excluded.currency, per_ads=excluded.per_ads, n=excluded.n, high=excluded.high, "
                "low=excluded.low, note=excluded.note, created_at=excluded.created_at",
                [(company_id, source, listing, taken_on, int(r["fiscal_year"]), r["metric"], float(r["value"]), r.get("currency"),
                  int(bool(r.get("per_ads"))), r.get("n"), r.get("high"), r.get("low"), r.get("note"), now) for r in rows],
            )
            self.conn.commit()
        return len(rows)

    def consensus_rows(self, company_id: str, *, fiscal_year: int | None = None, metric: str | None = None,
                       on_or_before: str | None = None, sources: tuple[str, ...] | None = None) -> list[dict]:
        """一致预期快照，按日期倒序。"""
        sql, params = "SELECT * FROM consensus WHERE company_id=?", [company_id]
        if fiscal_year is not None:
            sql += " AND fiscal_year=?"
            params.append(int(fiscal_year))
        if metric:
            sql += " AND metric=?"
            params.append(metric)
        if on_or_before:
            sql += " AND taken_on<=?"
            params.append(on_or_before)
        if sources:
            sql += f" AND source IN ({','.join('?' * len(sources))})"
            params += list(sources)
        return self._all(sql + " ORDER BY taken_on DESC, id DESC", tuple(params))

    def consensus_latest(self, company_id: str) -> list[dict]:
        """每个（来源、财年、指标）的最新一份快照。"""
        out, seen = [], set()
        for r in self.consensus_rows(company_id):
            key = (r["source"], r["fiscal_year"], r["metric"])
            if key not in seen:
                seen.add(key)
                out.append(r)
        return sorted(out, key=lambda r: (r["fiscal_year"], r["metric"], r["source"]))

    def delete_consensus(self, company_id: str, row_id: int) -> bool:
        """删除一条使用者录入的一致预期（程序记录的快照不删）。"""
        cur = self._exec("DELETE FROM consensus WHERE id=? AND company_id=? AND source='manual'", (row_id, company_id))
        return cur.rowcount > 0

    def save_earnings(self, company_id: str, period: str, stage: str, notice_date: str | None, data: dict,
                      item_uid: str | None) -> None:
        self._exec(
            "INSERT INTO earnings(company_id, period, stage, notice_date, data_json, item_uid, created_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(company_id, period, stage) DO UPDATE SET notice_date=excluded.notice_date, data_json=excluded.data_json, "
            "item_uid=excluded.item_uid, created_at=excluded.created_at",
            (company_id, period, stage, notice_date, json.dumps(data, ensure_ascii=False, default=str), item_uid, iso(utcnow())),
        )

    def has_earnings(self, company_id: str, period: str, stage: str) -> bool:
        return self._one("SELECT 1 AS x FROM earnings WHERE company_id=? AND period=? AND stage=?", (company_id, period, stage)) is not None

    def earnings(self, company_id: str, limit: int = 8) -> list[dict]:
        """业绩对比记录，按发布日倒序。"""
        rows = self._all("SELECT * FROM earnings WHERE company_id=? ORDER BY COALESCE(notice_date, created_at) DESC, created_at DESC LIMIT ?",
                         (company_id, limit))
        for r in rows:
            r["data"] = json.loads(r.pop("data_json") or "{}")
        return rows

    def manual_data(self, company_id: str | None) -> list[dict]:
        return self._all(
            "SELECT * FROM manual_data WHERE company_id=? ORDER BY metric, period DESC", (company_id or "",)
        )

    # ------------------------------------------------------------ 公司画像

    def save_profile(self, company_id: str, md: str, doc_ids: list[int], model: str) -> None:
        self._exec(
            "INSERT INTO profiles(company_id, profile_md, doc_ids, model, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(company_id) DO UPDATE SET profile_md=excluded.profile_md, doc_ids=excluded.doc_ids, "
            "model=excluded.model, updated_at=excluded.updated_at",
            (company_id, md, json.dumps(doc_ids), model, iso(utcnow())),
        )

    def profile(self, company_id: str) -> dict | None:
        r = self._one("SELECT * FROM profiles WHERE company_id=?", (company_id,))
        if r:
            r["doc_ids"] = json.loads(r["doc_ids"])
        return r

    # ------------------------------------------------------------ 深度报告与跟踪

    def save_dossier(self, company_id: str, model: str, report_md: str, card: dict, notes: dict, tool_log: list,
                     triggers: list[dict], catalysts: list[dict], plan: dict) -> int:
        """保存报告；同时用新报告的证伪条件、催化剂、跟踪计划替换旧的。"""
        now = iso(utcnow())
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO dossiers(company_id, created_at, model, report_md, card_json, notes_json, tool_log_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (company_id, now, model, report_md, json.dumps(card, ensure_ascii=False),
                 json.dumps(notes, ensure_ascii=False), json.dumps(tool_log, ensure_ascii=False)),
            )
            did = cur.lastrowid
            self.conn.execute("UPDATE triggers SET active=0 WHERE company_id=?", (company_id,))
            self.conn.executemany(
                "INSERT INTO triggers(company_id, dossier_id, description, kind, metric, listing, operator, threshold, action, "
                "direction) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(company_id, did, t["description"], t["kind"], t.get("metric"), t.get("listing"), t.get("operator"),
                  t.get("threshold"), t["action"], t.get("direction") or "down") for t in triggers],
            )
            self.conn.execute("DELETE FROM catalysts WHERE company_id=?", (company_id,))
            self.conn.executemany(
                "INSERT INTO catalysts(company_id, dossier_id, when_text, due_date, event, kpi) VALUES(?,?,?,?,?,?)",
                [(company_id, did, c.get("when"), c.get("due_date"), c["event"], c.get("kpi")) for c in catalysts],
            )
            self.conn.execute(
                "INSERT INTO plans(company_id, dossier_id, plan_json, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(company_id) DO UPDATE SET dossier_id=excluded.dossier_id, plan_json=excluded.plan_json, "
                "updated_at=excluded.updated_at",
                (company_id, did, json.dumps(plan, ensure_ascii=False), now),
            )
            self.conn.commit()
        return did

    def set_dossier_paths(self, dossier_id: int, html_path: str | None, pdf_path: str | None) -> None:
        self._exec("UPDATE dossiers SET html_path=?, pdf_path=? WHERE id=?", (html_path, pdf_path, dossier_id))

    def latest_dossier(self, company_id: str) -> dict | None:
        r = self._one("SELECT * FROM dossiers WHERE company_id=? ORDER BY id DESC LIMIT 1", (company_id,))
        if r:
            r["card"] = json.loads(r.pop("card_json"))
        return r

    def triggers(self, company_id: str, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM triggers WHERE company_id=?" + (" AND active=1" if active_only else "") + " ORDER BY id"
        return self._all(sql, (company_id,))

    def update_trigger(self, trigger_id: int, status: str, evidence: str) -> bool:
        """更新状态；由非触发变为触发时记录触发时间，返回是否为新触发。"""
        now = iso(utcnow())
        row = self._one("SELECT status FROM triggers WHERE id=?", (trigger_id,))
        newly = row is not None and row["status"] != "triggered" and status == "triggered"
        self._exec(
            "UPDATE triggers SET status=?, evidence=?, checked_at=?, triggered_at=CASE WHEN ? THEN ? ELSE triggered_at END "
            "WHERE id=?",
            (status, evidence, now, int(newly), now, trigger_id),
        )
        return newly

    def pending_trigger_alerts(self) -> list[dict]:
        return self._all("SELECT * FROM triggers WHERE active=1 AND status='triggered' AND alerted_at IS NULL")

    def mark_trigger_alerted(self, ids: list[int]) -> None:
        now = iso(utcnow())
        with self.lock:
            self.conn.executemany("UPDATE triggers SET alerted_at=? WHERE id=?", [(now, i) for i in ids])
            self.conn.commit()

    def catalysts(self, company_id: str) -> list[dict]:
        return self._all("SELECT * FROM catalysts WHERE company_id=? ORDER BY COALESCE(due_date, '9999'), id", (company_id,))

    def plan(self, company_id: str) -> dict | None:
        r = self._one("SELECT plan_json FROM plans WHERE company_id=?", (company_id,))
        return json.loads(r["plan_json"]) if r else None

    def dossier(self, dossier_id: int) -> dict | None:
        r = self._one("SELECT * FROM dossiers WHERE id=?", (dossier_id,))
        if r:
            r["card"] = json.loads(r.pop("card_json"))
        return r

    def dossiers(self, company_id: str | None = None) -> list[dict]:
        """报告列表（不含正文）。"""
        sql = "SELECT id, company_id, created_at, model, html_path, pdf_path FROM dossiers"
        return self._all(sql + (" WHERE company_id=?" if company_id else "") + " ORDER BY id DESC",
                         (company_id,) if company_id else ())

    # ------------------------------------------------------------ 标的追踪

    def tracker(self, company_id: str) -> dict | None:
        return self._one("SELECT * FROM trackers WHERE company_id=?", (company_id,))

    def trackers(self, enabled_only: bool = True) -> list[dict]:
        return self._all("SELECT * FROM trackers" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY company_id")

    def set_tracker(self, company_id: str, **fields) -> None:
        """新建或更新追踪器（只更新给出的字段）。"""
        now = iso(utcnow())
        with self.lock:
            if self.conn.execute("SELECT 1 FROM trackers WHERE company_id=?", (company_id,)).fetchone() is None:
                self.conn.execute("INSERT INTO trackers(company_id, created_at, updated_at) VALUES(?,?,?)", (company_id, now, now))
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self.conn.execute(f"UPDATE trackers SET {sets}, updated_at=? WHERE company_id=?", (*fields.values(), now, company_id))
            self.conn.commit()

    REVISION_JSON = ("changes", "rejected", "events", "catalysts", "triggers", "flags", "unverified", "tool_log", "model")

    def add_revision(self, rev: dict, refs: list[tuple[str, str]] = (), tracker: dict | None = None) -> int:
        """保存一条修正记录，同时关联看过的依据（ref, impact）并把追踪器指向它；在一个事务里完成。"""
        rev = dict(rev)
        for k in self.REVISION_JSON:
            if k in rev:
                rev[f"{k}_json"] = json.dumps(rev.pop(k), ensure_ascii=False)
        rev.setdefault("created_at", iso(utcnow()))
        cols = list(rev)
        with self.transaction() as conn:
            rid = conn.execute(f"INSERT INTO revisions({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                               [rev[c] for c in cols]).lastrowid
            conn.executemany("INSERT OR IGNORE INTO tracker_refs(company_id, ref, revision_id, impact, created_at) VALUES(?,?,?,?,?)",
                             [(rev["company_id"], ref, rid, impact, rev["created_at"]) for ref, impact in refs])
            fields = {"revision_id": rid, **(tracker or {})}
            conn.execute(f"UPDATE trackers SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? WHERE company_id=?",
                         (*fields.values(), rev["created_at"], rev["company_id"]))
        return rid

    @classmethod
    def _revision_row(cls, r: dict | None) -> dict | None:
        if r:
            for k in cls.REVISION_JSON:
                raw = r.pop(f"{k}_json", None)
                r[k] = json.loads(raw) if raw else None
        return r

    def revision(self, revision_id: int) -> dict | None:
        return self._revision_row(self._one("SELECT * FROM revisions WHERE id=?", (revision_id,)))

    def revisions(self, company_id: str, limit: int | None = None, since_id: int = 0) -> list[dict]:
        """修正记录，新的在前。"""
        sql = "SELECT * FROM revisions WHERE company_id=? AND id>=? ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
        return [self._revision_row(r) for r in self._all(sql, (company_id, since_id))]

    def latest_revision(self, company_id: str, kind: str | None = None) -> dict | None:
        sql = "SELECT * FROM revisions WHERE company_id=?" + (" AND kind=?" if kind else "") + " ORDER BY id DESC LIMIT 1"
        return self._revision_row(self._one(sql, (company_id, kind) if kind else (company_id,)))

    def recent_revisions(self, limit: int = 20, kinds: tuple[str, ...] = ("revision", "review", "baseline")) -> list[dict]:
        rows = self._all(f"SELECT * FROM revisions WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY id DESC LIMIT ?",
                         (*kinds, limit))
        return [self._revision_row(r) for r in rows]

    def link_refs(self, company_id: str, refs: list[tuple[str, str]], revision_id: int | None = None) -> None:
        now = iso(utcnow())
        with self.lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO tracker_refs(company_id, ref, revision_id, impact, created_at) VALUES(?,?,?,?,?)",
                [(company_id, ref, revision_id, impact, now) for ref, impact in refs])
            self.conn.commit()

    def refs(self, company_id: str, prefix: str = "") -> dict[str, dict]:
        rows = self._all("SELECT * FROM tracker_refs WHERE company_id=? AND substr(ref, 1, ?)=?", (company_id, len(prefix), prefix))
        return {r["ref"]: r for r in rows}

    def pending_items(self, company_id: str, triaged_after: str, published_after: str, min_materiality: int,
                      limit: int | None = None) -> list[dict]:
        """尚未被追踪器处理的重要事件（按分级时间取窗口，按重要性降序、时间升序）。"""
        sql = ("SELECT i.* FROM items i WHERE i.company_id=? AND i.triaged_at>? AND i.published_at>=? AND i.relevant=1 "
               "AND (i.duplicate_of IS NULL OR i.duplicate_of='') AND i.materiality>=? "
               "AND NOT EXISTS (SELECT 1 FROM tracker_refs r WHERE r.company_id=i.company_id AND r.ref='item:' || i.uid) "
               "ORDER BY i.materiality DESC, i.published_at ASC")
        return self._all(sql + (f" LIMIT {int(limit)}" if limit else ""), (company_id, triaged_after, published_after, min_materiality))

    def documents_added_since(self, company_id: str, since: str) -> list[dict]:
        return self._all("SELECT * FROM documents WHERE company_id=? AND added_at>? ORDER BY added_at", (company_id, since))

    def manual_since(self, company_id: str, since: str) -> list[dict]:
        """某个时间之后补充的手动数据（本公司与行业/宏观）。"""
        return self._all("SELECT * FROM manual_data WHERE company_id IN (?, '') AND imported_at>? ORDER BY imported_at",
                         (company_id, since))

    # ------------------------------------------------------------ 后台任务（WebUI 与调度器）

    def add_job(self, task: str, argv: list[list[str]], title: str, source: str, company_id: str | None = None,
                note: str | None = None) -> int:
        return self._exec(
            "INSERT INTO jobs(task, argv_json, title, company_id, source, note, created_at) VALUES(?,?,?,?,?,?,?)",
            (task, json.dumps(argv, ensure_ascii=False), title, company_id, source, note, iso(utcnow())),
        ).lastrowid

    def job(self, job_id: int) -> dict | None:
        r = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if r:
            r["argv"] = json.loads(r.pop("argv_json"))
        return r

    def jobs(self, limit: int = 50, statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql, params = "SELECT * FROM jobs", []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params += list(statuses)
        rows = self._all(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        for r in rows:
            r["argv"] = json.loads(r.pop("argv_json"))
        return rows

    def update_job(self, job_id: int, **fields) -> None:
        self._exec(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), job_id))

    def claim_job(self, job_id: int, pid: int, log_path: str) -> bool:
        """把排队中的任务标为运行中（只有仍在排队时才成功，避免重复启动）。"""
        return self._exec("UPDATE jobs SET status='running', pid=?, log_path=?, started_at=? WHERE id=? AND status='queued'",
                          (pid, log_path, iso(utcnow()), job_id)).rowcount > 0

    def recent_job(self, task: str, company_id: str, since: str) -> dict | None:
        """某公司某类任务在 since 之后的最近一次（排队、运行或已完成；取消的不算）。"""
        return self._one("SELECT * FROM jobs WHERE task=? AND company_id=? AND created_at>=? AND status NOT IN ('cancelled') "
                         "ORDER BY id DESC LIMIT 1", (task, company_id, since))

    # ------------------------------------------------------------ 报告研读对话

    def create_chat(self, company_id: str, dossier_id: int, title: str, revision_id: int | None = None) -> int:
        now = iso(utcnow())
        return self._exec("INSERT INTO chats(company_id, dossier_id, revision_id, title, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                          (company_id, dossier_id, revision_id, title, now, now)).lastrowid

    def chat(self, chat_id: int) -> dict | None:
        return self._one("SELECT * FROM chats WHERE id=?", (chat_id,))

    def chats(self, company_id: str, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM chats WHERE company_id=? ORDER BY updated_at DESC, id DESC LIMIT ?", (company_id, limit))

    def delete_chat(self, chat_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM chat_messages WHERE chat_id=?", (chat_id,))
            self.conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
            self.conn.commit()

    @staticmethod
    def _chat_message(r: dict | None) -> dict | None:
        if r:
            r["checks"] = json.loads(r.pop("checks_json") or "{}")
            r["tool_log"] = json.loads(r.pop("tool_log_json") or "[]")
            r["usage"] = json.loads(r.pop("usage_json") or "null")
            r["trace"] = json.loads(r.pop("trace_json", None) or "[]")
        return r

    def chat_messages(self, chat_id: int) -> list[dict]:
        return [self._chat_message(r) for r in self._all("SELECT * FROM chat_messages WHERE chat_id=? ORDER BY id", (chat_id,))]

    def chat_message(self, message_id: int) -> dict | None:
        return self._chat_message(self._one("SELECT * FROM chat_messages WHERE id=?", (message_id,)))

    def add_chat_message(self, chat_id: int, role: str, content: str = "", quote: str | None = None,
                         status: str = "done") -> int:
        now = iso(utcnow())
        with self.lock:
            mid = self.conn.execute(
                "INSERT INTO chat_messages(chat_id, role, content, quote, status, created_at) VALUES(?,?,?,?,?,?)",
                (chat_id, role, content, quote, status, now)).lastrowid
            self.conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (now, chat_id))
            self.conn.commit()
        return mid

    def update_chat_message(self, message_id: int, **fields) -> None:
        for k in ("checks", "tool_log", "usage", "trace"):
            if k in fields:
                fields[f"{k}_json"] = json.dumps(fields.pop(k), ensure_ascii=False)
        self._exec(f"UPDATE chat_messages SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), message_id))

    def fail_pending_chat_messages(self, reason: str) -> int:
        """上次进程退出时尚未写完的回答（后台线程随进程结束）。"""
        return self._exec("UPDATE chat_messages SET status='error', progress=NULL, content=? WHERE status='pending'",
                          (reason,)).rowcount

    # ------------------------------------------------------------ 向使用者请求的数据

    def add_requests(self, company_id: str, dossier_id: int | None, items: list[dict]) -> list[int]:
        """新增或刷新请求（同一数据项已存在时更新说明并重新打开）。返回请求编号。"""
        now = iso(utcnow())
        ids = []
        with self.lock:
            for it in items:
                self.conn.execute(
                    "INSERT INTO data_requests(company_id, dossier_id, item, why, ask, created_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(company_id, item) DO UPDATE SET dossier_id=excluded.dossier_id, why=excluded.why, "
                    "ask=excluded.ask, status='open', closed_at=NULL",
                    (company_id, dossier_id, it["item"], it.get("why"), it.get("ask"), now),
                )
                ids.append(self.conn.execute("SELECT id FROM data_requests WHERE company_id=? AND item=?",
                                             (company_id, it["item"])).fetchone()[0])
            self.conn.commit()
        return ids

    def requests(self, company_id: str | None = None, status: str = "open") -> list[dict]:
        sql = "SELECT * FROM data_requests WHERE status=?"
        params: list = [status]
        if company_id:
            sql += " AND company_id=?"
            params.append(company_id)
        return self._all(sql + " ORDER BY company_id, id", params)

    def resolve_requests(self, company_id: str, keep: list[str]) -> int:
        """新报告生成后，旧报告提出而这次不再列出的请求（已查到或不再关键）标记为 resolved。"""
        sql = "UPDATE data_requests SET status='resolved', closed_at=? WHERE company_id=? AND status='open'"
        if keep:
            sql += f" AND item NOT IN ({','.join('?' * len(keep))})"
        return self._exec(sql, (iso(utcnow()), company_id, *keep)).rowcount

    def supplied_since(self, company_id: str, since: str) -> dict[str, int]:
        """某个时间之后使用者补充了什么：手动数据条数、手动导入的文件数、标记为已补充的请求数。"""
        def count(sql: str) -> int:
            return self._one(sql, (company_id, since))["n"]
        return {"data": count("SELECT COUNT(*) n FROM manual_data WHERE company_id IN (?, '') AND imported_at >= ?"),
                "docs": count("SELECT COUNT(*) n FROM documents WHERE company_id=? AND source='manual' AND added_at >= ?"),
                "done": count("SELECT COUNT(*) n FROM data_requests WHERE company_id=? AND status='done' AND closed_at >= ?")}

    def close_request(self, request_id: int, status: str = "done") -> bool:
        cur = self._exec("UPDATE data_requests SET status=?, closed_at=? WHERE id=? AND status='open'",
                         (status, iso(utcnow()), request_id))
        return cur.rowcount > 0

    # ------------------------------------------------------------ LLM 用量

    def log_llm_call(self, rec: dict) -> None:
        cols = ("task", "profile", "model", "input_tokens", "output_tokens", "cache_read", "cache_write",
                "started_at", "duration_s", "ok", "error")
        self._exec(f"INSERT INTO llm_calls({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                   [rec.get(c) for c in cols])

    def llm_usage(self, since: str) -> list[dict]:
        return self._all(
            "SELECT model, COUNT(*) AS calls, SUM(ok) AS ok_calls, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write "
            "FROM llm_calls WHERE started_at>=? GROUP BY model",
            (since,),
        )

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict:
        return {
            "items": self._one("SELECT COUNT(*) AS n, SUM(triaged_at IS NOT NULL) AS triaged FROM items"),
            "items_by_source": self._all("SELECT source, COUNT(*) AS n, MAX(published_at) AS latest FROM items GROUP BY source"),
            "bars": self._all("SELECT secid, COUNT(*) AS n, MAX(date) AS latest FROM bars GROUP BY secid"),
            "documents": self._one("SELECT COUNT(*) AS n FROM documents"),
            "manual": self._one("SELECT COUNT(*) AS n FROM manual_data"),
            "profiles": self._all("SELECT company_id, updated_at FROM profiles"),
            "dossiers": self._one("SELECT COUNT(*) AS n FROM dossiers"),
            "trackers": self._one("SELECT COUNT(*) AS n, SUM(enabled) AS enabled FROM trackers"),
            "revisions": self._one("SELECT COUNT(*) AS n, SUM(kind='revision') AS changed FROM revisions"),
            "jobs": self._one("SELECT COUNT(*) AS n, SUM(status='queued') AS queued, SUM(status='running') AS running FROM jobs"),
        }

    def llm_usage_by(self, since: str) -> list[dict]:
        """按任务与模型汇总调用次数、失败次数、tokens 与平均耗时（状态页）。"""
        return self._all(
            "SELECT task, model, COUNT(*) AS calls, SUM(ok=0) AS failed, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, AVG(duration_s) AS avg_s FROM llm_calls WHERE started_at>=? "
            "GROUP BY task, model ORDER BY calls DESC",
            (since,),
        )
