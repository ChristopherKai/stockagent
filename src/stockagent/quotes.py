"""行情刷新（无模型）：只取追踪中公司各上市地的实时快照（价格、昨收、涨跌幅），不拉日线。

- 只刷新当前开盘（或刚收盘 REFRESH_GRACE_MIN 分钟内，取收盘价）的市场；全部休市时不联网、立即结束。
- 取数走 sources.price_sources 的顺序，经 app._try（熔断）；`app.fetch_prices` 存快照时会合并南向持股 southbound_*，
  快照是整行替换，所以这里把旧快照里新快照没有的字段（southbound_*、PE / 市值 / 每手股数等）合并保留，不能抹掉。
- 刷新后检查情景切换（scenario_watch.check）。
- 由内置调度器在 stock web 进程内直接执行（run_scheduled：不落 jobs 记录、不起子进程），也可 `stock quotes refresh` 手动运行。
"""

from __future__ import annotations

from datetime import datetime

from . import marketclock, scenario_watch
from .app import PRICE_SOURCES, App, _try
from .config import Company
from .tracker import tracked
from .utils import log


def merge_quote(old: dict | None, new: dict) -> dict:
    """新快照为准；旧快照里新快照没有的字段保留（taken_at 是库里加的，不带进去）。"""
    kept = {k: v for k, v in (old or {}).items() if k != "taken_at" and k not in new}
    return {**kept, **new}


def fetch_quote(app: App, secid: str) -> dict | None:
    for name in app.settings.sources.price_sources:
        q = _try(app, name, PRICE_SOURCES[name].fetch_quote, app.session, secid)
        if q and q.get("price"):
            return q
    return None


def refresh(app: App, companies: list[Company], push: bool = True, ts: datetime | None = None) -> list[str]:
    """刷新追踪中公司的快照并检查情景切换。返回日志；没有开盘的市场时为空列表。"""
    ts = ts or marketclock.now()
    work = [(c, l) for c in tracked(app, companies) for l in c.listings if marketclock.in_refresh_window(l.market, ts)]
    if not work:
        return []
    msgs, ok, fail, done = [], 0, [], []
    for c, l in work:
        secid = app.secid(l)
        q = fetch_quote(app, secid)
        if q is None:
            fail.append(l.display)
            continue
        app.db.save_quote(secid, merge_quote(app.db.quote(secid), q), taken_at=ts)
        ok += 1
        if c not in done:
            done.append(c)
    msgs.append(f"行情刷新：{ok}/{len(work)} 个上市地已更新" + (f"；失败：{'、'.join(fail)}" if fail else ""))
    msgs += scenario_watch.check(app, done, push=push)
    return msgs


def run_scheduled(app: App, companies: list[Company], flags: list[str]) -> list[str]:
    """调度器进程内入口（参数与 stock quotes refresh 相同）。拿不到 quotes 锁（上一轮还没结束）时跳过。"""
    with app.lock("quotes") as got:
        if not got:
            return []
        msgs = refresh(app, companies, push="--no-push" not in flags)
    for m in msgs:
        log.info("%s", m)
    return msgs
