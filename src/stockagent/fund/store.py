"""模拟基金的数据表（与核心表同库，首次使用时由本模块建表；核心模块不读写这些表）。

fund 基金定义与账户状态 / market_regime 每日市场状态 / signal_snapshot 每次打分结果 / position 持仓 /
suggestion 操作建议 / trade 模拟成交 / nav_daily 每日净值 / user_action 使用者对建议的回执（已执行 / 忽略）。

user_action 只记使用者自己的操作，与模拟账本分开：模拟持仓、净值照旧按规则计算，「实际持仓」由回执推算，两者可以对照。
"""

from __future__ import annotations

import json

from ..db import DB
from ..utils import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS fund (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    market          TEXT NOT NULL,            -- HK | A
    strategy        TEXT NOT NULL,
    style           TEXT NOT NULL,            -- value | trend（市场状态对应的仓位上限档位）
    currency        TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    max_holdings    INTEGER NOT NULL,
    max_weight      REAL NOT NULL,
    sector_cap      REAL NOT NULL,
    min_weight      REAL NOT NULL,
    dd1 REAL NOT NULL, dd2 REAL NOT NULL, dd3 REAL NOT NULL,   -- 回撤熔断阈值
    rebalance       TEXT NOT NULL,            -- weekly | monthly | quarterly
    benchmark       TEXT NOT NULL,
    status          TEXT NOT NULL,            -- paper 模拟 | paused 暂停（来自 funds.yaml）
    params_json     TEXT NOT NULL DEFAULT '{}',
    cash            REAL NOT NULL,
    breaker         INTEGER NOT NULL DEFAULT 0,   -- 0 正常 | 1 新仓减半 | 2 停止开新仓、降到 50% | 3 暂停（需手动恢复）
    halted_at       TEXT,
    inception       TEXT NOT NULL,            -- 开始记账的日期（北京时间）
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_regime (
    market       TEXT NOT NULL,
    date         TEXT NOT NULL,               -- 所用收盘数据的日期
    state        TEXT NOT NULL,               -- attack 进攻 | neutral 中性 | defend 防守
    index_secid  TEXT,
    index_close  REAL,
    index_ma     REAL,
    vol_pct      REAL,                        -- 20 日波动率在近一年中的分位
    breadth      REAL,                        -- 日线股票池中站上 50 日线的比例
    breadth_n    INTEGER,
    signals_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (market, date)
);

CREATE TABLE IF NOT EXISTS signal_snapshot (
    id            INTEGER PRIMARY KEY,
    fund_id       TEXT NOT NULL,
    date          TEXT NOT NULL,
    secid         TEXT NOT NULL,
    code          TEXT,
    name          TEXT,
    rank          INTEGER,
    score         REAL,
    target_weight REAL,                       -- 策略给出的目标权重（风控前）
    stop          REAL,
    entry_json    TEXT NOT NULL DEFAULT '{}', -- 入场条件
    scores_json   TEXT NOT NULL DEFAULT '{}', -- 各项得分与指标
    reason        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fs_fund ON signal_snapshot(fund_id, date);

CREATE TABLE IF NOT EXISTS position (
    fund_id       TEXT NOT NULL,
    secid         TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    sector        TEXT,
    org_code      TEXT,                       -- 发行人代码（A+H 合并计算跨基金敞口）
    shares        REAL NOT NULL,
    avg_cost      REAL NOT NULL,              -- 含买入费用的平均成本
    entry_price   REAL NOT NULL,              -- 首批成交价
    opened_on     TEXT NOT NULL,
    stop          REAL,
    high_close    REAL,                       -- 建仓以来最高收盘价（移动止损）
    tranches      INTEGER NOT NULL DEFAULT 1, -- 已建批次
    target_weight REAL,                       -- 计划的满仓权重
    score         REAL,
    thesis        TEXT,
    flags_json    TEXT NOT NULL DEFAULT '{}', -- half_off 已跌破 50 日线减半 / review 已提示复核 / lot_unknown …
    last_trade_on TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (fund_id, secid)
);

CREATE TABLE IF NOT EXISTS suggestion (
    id            INTEGER PRIMARY KEY,
    fund_id       TEXT NOT NULL,
    date          TEXT NOT NULL,              -- 适用的交易日
    secid         TEXT NOT NULL,
    code          TEXT,
    name          TEXT,
    action        TEXT NOT NULL,              -- buy 建仓 | add 加仓 | trim 减仓 | sell 清仓 | review 复核提醒
    kind          TEXT NOT NULL,              -- open 按当日开盘价成交 | conditional 盘中条件触发 | note 只提醒
    cur_weight    REAL,
    target_weight REAL,
    shares        REAL,
    limit_price   REAL,
    trigger_json  TEXT NOT NULL DEFAULT '{}',
    stop          REAL,
    reason        TEXT,
    priority      INTEGER,                    -- 1 止损 2 论点失效 3 风控降仓 4 止盈 5 替换 / 调仓 6 建仓加仓
    valid_until   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending 待执行 | filled 已模拟成交 | expired 已失效 | unfilled 未能成交 | cancelled
    note          TEXT,
    trade_id      INTEGER,
    alerted_at    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_fsg_fund ON suggestion(fund_id, date);
CREATE INDEX IF NOT EXISTS idx_fsg_status ON suggestion(status, date);

CREATE TABLE IF NOT EXISTS trade (
    id            INTEGER PRIMARY KEY,
    fund_id       TEXT NOT NULL,
    suggestion_id INTEGER,
    secid         TEXT NOT NULL,
    code          TEXT,
    name          TEXT,
    side          TEXT NOT NULL,              -- buy | sell
    shares        REAL NOT NULL,
    price         REAL NOT NULL,              -- 含滑点的成交价
    amount        REAL NOT NULL,
    fees          REAL NOT NULL,
    realized      REAL,                       -- 卖出的已实现盈亏（扣费，按含费平均成本）
    date          TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'paper',
    reason        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ft_fund ON trade(fund_id, date);

CREATE TABLE IF NOT EXISTS nav_daily (
    fund_id         TEXT NOT NULL,
    date            TEXT NOT NULL,
    nav             REAL NOT NULL,            -- 单位净值（初始 1.0）
    equity          REAL NOT NULL,            -- 总资产（本币）
    cash            REAL NOT NULL,
    exposure        REAL NOT NULL,            -- 持仓市值 ÷ 总资产
    peak            REAL NOT NULL,            -- 历史最高单位净值
    drawdown        REAL NOT NULL,            -- 从最高点的回撤（正数）
    positions       INTEGER NOT NULL,
    benchmark_close REAL,
    benchmark_nav   REAL,                     -- 基准按建仓日归一
    breaker         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (fund_id, date)
);

CREATE TABLE IF NOT EXISTS user_action (
    id            INTEGER PRIMARY KEY,
    suggestion_id INTEGER NOT NULL UNIQUE,    -- 每条建议一条回执，改主意时覆盖
    fund_id       TEXT NOT NULL,
    secid         TEXT NOT NULL,
    code          TEXT,
    name          TEXT,
    status        TEXT NOT NULL,              -- executed 已执行 | ignored 忽略
    side          TEXT,                       -- buy | sell（已执行时）
    shares        REAL,                       -- 实际成交股数（未填写时按建议数量）
    price         REAL,                       -- 实际成交价（未填写时按参考价）
    estimated     INTEGER NOT NULL DEFAULT 0, -- 价格或数量是推算的（使用者没有填写）
    note          TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fua_fund ON user_action(fund_id, created_at);

CREATE TABLE IF NOT EXISTS fund_flow (          -- 追加 / 取回资金：按当时的单位净值申购或赎回份额，净值不受影响
    id          INTEGER PRIMARY KEY,
    fund_id     TEXT NOT NULL,
    date        TEXT NOT NULL,              -- 北京时间日期
    amount      REAL NOT NULL,              -- 正数追加，负数取回（本币）
    nav         REAL NOT NULL,              -- 当时的单位净值
    units       REAL NOT NULL,              -- 变动的份额（amount ÷ nav）
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ff_fund ON fund_flow(fund_id, id);
"""

JSON_FIELDS = ("params_json", "signals_json", "entry_json", "scores_json", "flags_json", "trigger_json")


def ensure(db: DB) -> DB:
    if not getattr(db, "_fund_schema", False):
        with db.lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()
        db._fund_schema = True
    return db


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    for k in JSON_FIELDS:
        if k in row:
            row[k[:-5]] = json.loads(row[k]) if isinstance(row[k], str) else {}
    return row


def _dump(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def now() -> str:
    return iso(utcnow())


# ---------------------------------------------------------------- 基金

DEF_COLS = ("name", "market", "strategy", "style", "currency", "initial_capital", "max_holdings", "max_weight", "sector_cap",
            "min_weight", "dd1", "dd2", "dd3", "rebalance", "benchmark", "status")


def def_row(d) -> dict:
    """FundDef → fund 表的定义列。"""
    dd1, dd2, dd3 = d.drawdown
    return {"name": d.name, "market": d.market, "strategy": d.strategy, "style": d.style, "currency": d.currency,
            "initial_capital": d.initial_capital, "max_holdings": d.max_holdings, "max_weight": d.max_weight,
            "sector_cap": d.sector_cap, "min_weight": d.min_weight, "dd1": dd1, "dd2": dd2, "dd3": dd3,
            "rebalance": d.rebalance, "benchmark": d.benchmark, "status": d.status, "params": d.params}


def sync(db: DB, defs: list, inception: str, dry_run: bool = False) -> list[str]:
    """把 funds.yaml 同步到 fund 表：新基金按初始资金建账；已有基金只更新定义，不动现金、持仓与熔断状态。
    已有成交、净值或资金进出的基金，初始资金不能改（会让净值失真；加减资金用「追加 / 取回资金」），其他定义照常更新；
    还没有任何记录的基金可以改初始资金（现金同步重置）。"""
    ensure(db)
    msgs = []
    have = {f["id"]: f for f in funds(db)}
    for d in defs:
        row = def_row(d)
        old = have.pop(d.id, None)
        if old is None:
            msgs.append(f"新建 {d.id}（{d.name}，{d.market}，{d.strategy}，初始资金 {d.initial_capital:,.0f} {d.currency}）")
            if not dry_run:
                db._exec(f"INSERT INTO fund (id, {', '.join(DEF_COLS)}, params_json, cash, inception, created_at, updated_at) "
                         f"VALUES (?, {', '.join('?' * len(DEF_COLS))}, ?, ?, ?, ?, ?)",
                         (d.id, *[row[k] for k in DEF_COLS], _dump(row["params"]), d.initial_capital, inception, now(), now()))
            continue
        reset_cash = False
        if abs(old["initial_capital"] - d.initial_capital) > 1e-6:
            if has_history(db, d.id):
                msgs.append(f"✗ {d.id}：已有成交或净值记录，初始资金不能修改（{old['initial_capital']:,.0f} → {d.initial_capital:,.0f}），"
                            "加减资金请用「追加 / 取回资金」；其他修改照常同步")
                row["initial_capital"] = old["initial_capital"]
            else:
                reset_cash = True
        changed = [k for k in DEF_COLS if old[k] != row[k]] + (["params"] if old["params"] != row["params"] else [])
        if changed:
            msgs.append(f"更新 {d.id}：{'、'.join(changed)}")
            if not dry_run:
                db._exec(f"UPDATE fund SET {', '.join(f'{k}=?' for k in DEF_COLS)}, params_json=?, updated_at=? WHERE id=?",
                         (*[row[k] for k in DEF_COLS], _dump(row["params"]), now(), d.id))
                if reset_cash:
                    db._exec("UPDATE fund SET cash=? WHERE id=?", (row["initial_capital"], d.id))
    for fid in have:
        msgs.append(f"{fid} 已不在 funds.yaml 中：保留账户与历史，不再生成建议（如需删除请手动处理）")
    return msgs or ["基金定义没有变化"]


def funds(db: DB) -> list[dict]:
    ensure(db)
    return [_decode(r) for r in db._all("SELECT * FROM fund ORDER BY market, id")]


def fund(db: DB, fund_id: str) -> dict | None:
    ensure(db)
    return _decode(db._one("SELECT * FROM fund WHERE id=?", (fund_id,)))


def set_fund(db: DB, fund_id: str, **fields) -> None:
    db._exec(f"UPDATE fund SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? WHERE id=?", (*fields.values(), now(), fund_id))


# ---------------------------------------------------------------- 市场状态

def save_regime(db: DB, r: dict) -> None:
    ensure(db)
    db._exec("INSERT OR REPLACE INTO market_regime (market, date, state, index_secid, index_close, index_ma, vol_pct, breadth, "
             "breadth_n, signals_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (r["market"], r["date"], r["state"], r.get("index_secid"), r.get("index_close"), r.get("index_ma"), r.get("vol_pct"),
              r.get("breadth"), r.get("breadth_n"), _dump(r.get("signals") or {}), now()))


def regime(db: DB, market: str) -> dict | None:
    ensure(db)
    return _decode(db._one("SELECT * FROM market_regime WHERE market=? ORDER BY date DESC LIMIT 1", (market,)))


# ---------------------------------------------------------------- 打分快照

def add_snapshot(db: DB, fund_id: str, day: str, targets: list) -> None:
    ensure(db)
    with db.transaction() as c:
        c.execute("DELETE FROM signal_snapshot WHERE fund_id=? AND date=?", (fund_id, day))
        for i, t in enumerate(targets, 1):
            c.execute("INSERT INTO signal_snapshot (fund_id, date, secid, code, name, rank, score, target_weight, stop, entry_json, "
                      "scores_json, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (fund_id, day, t.secid, t.code, t.name, i, t.score, t.weight, t.stop, _dump(t.entry), _dump(t.metrics),
                       t.reason, now()))


def latest_snapshot(db: DB, fund_id: str) -> list[dict]:
    ensure(db)
    r = db._one("SELECT MAX(date) AS d FROM signal_snapshot WHERE fund_id=?", (fund_id,))
    if not r or not r["d"]:
        return []
    return [_decode(x) for x in db._all("SELECT * FROM signal_snapshot WHERE fund_id=? AND date=? ORDER BY rank", (fund_id, r["d"]))]


# ---------------------------------------------------------------- 持仓、建议、成交、净值

def positions(db: DB, fund_id: str | None = None) -> list[dict]:
    ensure(db)
    if fund_id is None:
        return [_decode(r) for r in db._all("SELECT * FROM position ORDER BY fund_id, secid")]
    return [_decode(r) for r in db._all("SELECT * FROM position WHERE fund_id=? ORDER BY secid", (fund_id,))]


POS_COLS = ("code", "name", "sector", "org_code", "shares", "avg_cost", "entry_price", "opened_on", "stop", "high_close", "tranches",
            "target_weight", "score", "thesis", "last_trade_on")


def save_positions(c, fund_id: str, positions_: dict[str, dict]) -> None:
    """在事务内整体替换某基金的持仓（c 为 transaction() 给出的连接）。"""
    c.execute("DELETE FROM position WHERE fund_id=?", (fund_id,))
    for secid, p in positions_.items():
        if p["shares"] <= 0:
            continue
        c.execute(f"INSERT INTO position (fund_id, secid, {', '.join(POS_COLS)}, flags_json, updated_at) "
                  f"VALUES (?, ?, {', '.join('?' * len(POS_COLS))}, ?, ?)",
                  (fund_id, secid, *[p.get(k) for k in POS_COLS], _dump(p.get("flags") or {}), now()))


SUG_COLS = ("fund_id", "date", "secid", "code", "name", "action", "kind", "cur_weight", "target_weight", "shares", "limit_price",
            "stop", "reason", "priority", "valid_until", "status", "note")


def add_suggestions(db: DB, rows: list[dict]) -> list[int]:
    ensure(db)
    ids = []
    with db.transaction() as c:
        for s in rows:
            cur = c.execute(f"INSERT INTO suggestion ({', '.join(SUG_COLS)}, trigger_json, created_at) "
                            f"VALUES ({', '.join('?' * len(SUG_COLS))}, ?, ?)",
                            (*[s.get(k) for k in SUG_COLS[:15]], s.get("status") or "pending", s.get("note"),
                             _dump(s.get("trigger") or {}), now()))
            s["id"] = cur.lastrowid
            ids.append(cur.lastrowid)
    return ids


def suggestions(db: DB, fund_id: str | None = None, day: str | None = None, statuses: tuple[str, ...] | None = None) -> list[dict]:
    ensure(db)
    sql, params = "SELECT * FROM suggestion WHERE 1=1", []
    if fund_id:
        sql += " AND fund_id=?"
        params.append(fund_id)
    if day:
        sql += " AND date=?"
        params.append(day)
    if statuses:
        sql += f" AND status IN ({', '.join('?' * len(statuses))})"
        params += list(statuses)
    return [_decode(r) for r in db._all(sql + " ORDER BY date, priority, id", params)]


def cancel_pending(c, fund_id: str, before_or_on: str, reason: str) -> None:
    """新计划生成前，旧的待执行建议一律作废（每天的计划只以当天的为准）。"""
    c.execute("UPDATE suggestion SET status='expired', note=?, updated_at=? WHERE fund_id=? AND status='pending' AND date<=?",
              (reason, now(), fund_id, before_or_on))


def mark_alerted(db: DB, suggestion_id: int) -> None:
    db._exec("UPDATE suggestion SET alerted_at=? WHERE id=?", (now(), suggestion_id))


def trades(db: DB, fund_id: str | None = None, since: str = "") -> list[dict]:
    ensure(db)
    if fund_id is None:
        return db._all("SELECT * FROM trade WHERE date>=? ORDER BY date, id", (since,))
    return db._all("SELECT * FROM trade WHERE fund_id=? AND date>=? ORDER BY date, id", (fund_id, since))


def navs(db: DB, fund_id: str, since: str = "") -> list[dict]:
    ensure(db)
    return db._all("SELECT * FROM nav_daily WHERE fund_id=? AND date>=? ORDER BY date", (fund_id, since))


def last_nav(db: DB, fund_id: str, before: str | None = None) -> dict | None:
    ensure(db)
    if before:
        return db._one("SELECT * FROM nav_daily WHERE fund_id=? AND date<? ORDER BY date DESC LIMIT 1", (fund_id, before))
    return db._one("SELECT * FROM nav_daily WHERE fund_id=? ORDER BY date DESC LIMIT 1", (fund_id,))


def resume(db: DB, fund_id: str) -> dict | None:
    """恢复因回撤三档熔断而暂停的基金：熔断清零（回撤仍按历史最高净值计算）。命令行与 WebUI 共用。"""
    f = fund(db, fund_id)
    if f is not None:
        set_fund(db, fund_id, breaker=0, halted_at=None)
    return f


# ---------------------------------------------------------------- 使用者回执（不改动模拟账本）

def suggestion(db: DB, suggestion_id: int) -> dict | None:
    ensure(db)
    return _decode(db._one("SELECT * FROM suggestion WHERE id=?", (suggestion_id,)))


def record_action(db: DB, s: dict, status: str, shares: float | None = None, price: float | None = None,
                  estimated: bool = False, note: str | None = None) -> None:
    ensure(db)
    side = None if status != "executed" else ("buy" if s["action"] in ("buy", "add") else "sell")
    db._exec("INSERT INTO user_action (suggestion_id, fund_id, secid, code, name, status, side, shares, price, estimated, note, "
             "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
             "ON CONFLICT(suggestion_id) DO UPDATE SET status=excluded.status, side=excluded.side, shares=excluded.shares, "
             "price=excluded.price, estimated=excluded.estimated, note=excluded.note, updated_at=excluded.updated_at",
             (s["id"], s["fund_id"], s["secid"], s.get("code"), s.get("name"), status, side, shares if status == "executed" else None,
              price if status == "executed" else None, int(estimated), note, now(), now()))


def clear_action(db: DB, suggestion_id: int) -> bool:
    ensure(db)
    return db._exec("DELETE FROM user_action WHERE suggestion_id=?", (suggestion_id,)).rowcount > 0


def actions(db: DB, fund_id: str | None = None) -> list[dict]:
    ensure(db)
    if fund_id is None:
        return db._all("SELECT * FROM user_action ORDER BY created_at, id")
    return db._all("SELECT * FROM user_action WHERE fund_id=? ORDER BY created_at, id", (fund_id,))


def actual_holdings(db: DB, fund_id: str) -> dict[str, dict]:
    """由「已执行」回执推算的实际持仓（平均成本法）：secid → {code, name, shares, avg_cost, realized}。"""
    out: dict[str, dict] = {}
    for a in actions(db, fund_id):
        if a["status"] != "executed" or not a["shares"] or a["price"] is None:
            continue
        h = out.setdefault(a["secid"], {"secid": a["secid"], "code": a["code"], "name": a["name"], "shares": 0.0, "avg_cost": 0.0,
                                        "realized": 0.0, "estimated": False})
        h["estimated"] = h["estimated"] or bool(a["estimated"])
        if a["side"] == "buy":
            total = h["shares"] + a["shares"]
            h["avg_cost"] = (h["shares"] * h["avg_cost"] + a["shares"] * a["price"]) / total
            h["shares"] = total
        else:
            n = min(a["shares"], h["shares"])
            h["realized"] += n * (a["price"] - h["avg_cost"])
            h["shares"] -= n
    return {k: v for k, v in out.items() if v["shares"] > 0 or v["realized"]}


# ---------------------------------------------------------------- 资金进出（份额制）

def flows(db: DB, fund_id: str) -> list[dict]:
    ensure(db)
    return db._all("SELECT * FROM fund_flow WHERE fund_id=? ORDER BY id", (fund_id,))


def units(db: DB, f: dict) -> float:
    """总份额：建账时按单位净值 1 申购 initial_capital 份，之后每笔资金进出按当时净值增减份额。"""
    return f["initial_capital"] + sum(x["units"] for x in flows(db, f["id"]))


def add_flow(db: DB, f: dict, amount: float, nav: float, day: str, note: str | None = None) -> dict:
    """追加（amount > 0）或取回（< 0）资金：现金同步增减；份额 = 金额 ÷ 当时的单位净值，所以单位净值与收益率不受影响。"""
    ensure(db)
    if nav <= 0:
        raise ValueError("单位净值无效，无法折算份额")
    if amount < 0 and -amount > f["cash"] + 1e-6:
        raise ValueError(f"取回金额超过现金 {f['cash']:,.0f}（需要先减仓，模拟基金不会为取回资金自动卖出）")
    row = {"fund_id": f["id"], "date": day, "amount": amount, "nav": nav, "units": amount / nav, "note": note}
    with db.transaction() as c:
        c.execute("INSERT INTO fund_flow (fund_id, date, amount, nav, units, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (f["id"], day, amount, nav, row["units"], note, now()))
        c.execute("UPDATE fund SET cash=cash+?, updated_at=? WHERE id=?", (amount, now(), f["id"]))
    return row


def has_history(db: DB, fund_id: str) -> bool:
    """有过模拟成交、净值或资金进出的基金不能删除（只能暂停），以免丢掉记录。"""
    ensure(db)
    return any(db._one(f"SELECT 1 AS x FROM {t} WHERE fund_id=? LIMIT 1", (fund_id,)) for t in ("trade", "nav_daily", "fund_flow", "position"))


def delete_fund(db: DB, fund_id: str) -> None:
    """删除没有历史的基金（以及它的计划、打分与回执）。"""
    if has_history(db, fund_id):
        raise ValueError("这只基金已有成交或净值记录，只能暂停，不能删除")
    with db.transaction() as c:
        for t in ("suggestion", "signal_snapshot", "user_action"):
            c.execute(f"DELETE FROM {t} WHERE fund_id=?", (fund_id,))
        c.execute("DELETE FROM fund WHERE id=?", (fund_id,))
