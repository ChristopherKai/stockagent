"""SQLite 存储。所有时间字段为 UTC ISO 字符串。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pandas as pd

from .utils import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    uid           TEXT UNIQUE NOT NULL,
    company_id    TEXT NOT NULL,
    listing       TEXT,
    kind          TEXT NOT NULL,          -- announcement | news | manual
    source        TEXT NOT NULL,          -- hkex | cninfo | sec | eastmoney | google_news | manual
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
    action       TEXT NOT NULL,           -- review 复核 | reduce 减仓 | exit 退出
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
"""

TRIAGE_FIELDS = (
    "relevant", "duplicate_of", "event_type", "direction", "materiality",
    "novelty", "horizon", "summary_zh", "expectation", "follow_up",
)


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # LLM 调用在线程池中执行并记录用量，因此允许跨线程使用，写操作由锁串行化
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
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

    # ------------------------------------------------------------ kv

    def kv_get(self, key: str) -> str | None:
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else None

    def kv_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

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
                "INSERT INTO triggers(company_id, dossier_id, description, kind, metric, listing, operator, threshold, action) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [(company_id, did, t["description"], t["kind"], t.get("metric"), t.get("listing"), t.get("operator"),
                  t.get("threshold"), t["action"]) for t in triggers],
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
        }
