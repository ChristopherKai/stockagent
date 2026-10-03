"""股票池、日线与指标。

两阶段，避免触发东方财富限流：
1. 第一阶段只用选股器已入库的全市场批量数据（screen_universe：港股通 + 全部 A 股）：排除 ST、退市整理、上市不满一年、
   市值低于下限的，再按市值取前 N 只（bars_universe）。不发任何请求。
2. 第二阶段只对这 N 只用日线：晚间任务（refresh_bars）逐只限速请求、出错退避、可中断续跑，结果存进 bars 表；
   盘前计划只读库，不联网。流动性下限（20 日成交额中位数）、相对强度排名都在这一阶段按缓存的日线计算。
不使用行情列表接口（push2 clist）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from .. import app as appmod
from ..app import FX_HKDCNH, App
from ..sources import tencent
from ..utils import log, make_session, today_local
from . import calendar
from .config import FundsFile

# ---------------------------------------------------------------- 第一阶段：批量数据过滤


def stage1(app: App, cfg: FundsFile, market: str, limit: int | None = None) -> tuple[list[dict], dict[str, int]]:
    """选股器全市场表 → 排除 → 按市值取前 N。返回 (股票, 剔除原因计数)。"""
    from ..screen import store as sc_store

    uc = cfg.universe
    dropped: dict[str, int] = {}
    out = []
    for u in sc_store.universe(app.db, market):
        why = exclude_reason(u, uc.min_mcap[market], uc.min_listing_days)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        out.append(u)
    out.sort(key=lambda u: -(u.get("mcap") or 0))
    n = limit if limit is not None else uc.bars_universe[market]
    if len(out) > n:
        dropped["市值排名在前 N 之外"] = len(out) - n
    return out[:n], dropped


def exclude_reason(u: dict, min_mcap: float, min_listing_days: int) -> str | None:
    name = (u.get("name") or "").upper()
    if u.get("st") or "ST" in name:
        return "ST"
    if "退" in name:
        return "退市整理"
    if not u.get("mcap") or not u.get("price"):
        return "缺少市值或价格"
    if u["mcap"] < min_mcap:
        return "市值低于下限"
    if u.get("listing_date"):
        try:
            if (today_local() - date.fromisoformat(u["listing_date"][:10])).days < min_listing_days:
                return "上市不满一年"
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- 日线（只读库）


def load_bars(app: App, secids: list[str], days: int = 400) -> dict[str, pd.DataFrame]:
    """一次查询取多只股票最近的日线（按 secid 分组），只读库。"""
    out: dict[str, pd.DataFrame] = {}
    since = (today_local() - timedelta(days=int(days * 1.6) + 10)).isoformat()
    for i in range(0, len(secids), 400):
        chunk = secids[i:i + 400]
        rows = app.db._all(f"SELECT * FROM bars WHERE secid IN ({', '.join('?' * len(chunk))}) AND date>=? ORDER BY secid, date",
                           (*chunk, since))
        if not rows:
            continue
        df = pd.DataFrame(rows)
        for sid, g in df.groupby("secid", sort=False):
            out[sid] = g.reset_index(drop=True)
    return out


def amounts(bars: pd.DataFrame, market: str) -> pd.Series:
    """每日成交额：优先用成交额字段；腾讯备用源没有时 A 股按成交量（手）× 100 × 收盘价估算，港股按成交量 × 收盘价。"""
    lot = 100 if market == "A" else 1
    est = bars["volume"] * bars["close"] * lot
    return bars["amount"].where(bars["amount"].notna() & (bars["amount"] > 0), est)


def indicators(bars: pd.DataFrame, market: str) -> dict | None:
    """趋势与流动性指标（全部由日线计算）。日线不足 2 根返回 None。"""
    if bars is None or len(bars) < 2:
        return None
    c, h, lo, v = (bars[k].astype(float) for k in ("close", "high", "low", "volume"))
    n = len(bars)
    last = float(c.iloc[-1])

    def ma(k: int, back: int = 0) -> float | None:
        return float(c.iloc[n - back - k:n - back].mean()) if n - back >= k else None

    def ret(k: int) -> float | None:
        return float(last / c.iloc[-1 - k] - 1) if n > k else None

    prev = c.shift(1)
    tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean()) if n > 14 else None
    amt = amounts(bars, market).tail(20)
    avg_vol50 = float(v.iloc[-51:-1].mean()) if n > 51 else (float(v.iloc[:-1].mean()) if n > 2 else None)
    pivot = float(h.iloc[-21:-1].max()) if n > 21 else None        # 突破位：之前 20 个交易日的最高价（不含当天）
    rets = [ret(k) for k in (63, 126, 189, 252)]
    rs = (0.4 * rets[0] + 0.2 * rets[1] + 0.2 * rets[2] + 0.2 * rets[3]) if all(x is not None for x in rets) else None
    return {
        "date": str(bars["date"].iloc[-1]), "close": last, "open": float(bars["open"].iloc[-1]),
        "high": float(h.iloc[-1]), "low": float(lo.iloc[-1]), "prev_close": float(c.iloc[-2]),
        "bars": n, "ma50": ma(50), "ma150": ma(150), "ma200": ma(200), "ma200_prev": ma(200, 20),
        "atr": atr, "volume": float(v.iloc[-1]), "avg_vol50": avg_vol50,
        "vol_ratio": float(v.iloc[-1] / avg_vol50) if avg_vol50 else None,
        "pivot": pivot, "high_250": float(h.tail(250).max()), "low_250": float(lo.tail(250).min()),
        "ret_20d": ret(20), "rs_raw": rs,
        "turnover_med20": float(amt.median()) if amt.notna().sum() >= 5 else None,
        "zero_days20": int((v.tail(20).fillna(0) <= 0).sum()),
    }


def liquidity_capacity(ind: dict | None, participation: float) -> float | None:
    """可容纳资金（与选股器 screen.run.liquidity 同一口径）：20 日成交额中位数 × 参与率 × 5 个交易日。"""
    if not ind or not ind.get("turnover_med20"):
        return None
    return ind["turnover_med20"] * participation * 5


def expected_bar(market: str, as_of: date, after_close: bool = False) -> date:
    """计划所依据的最新收盘日：盘前用上一个交易日，收盘后用当天（当天休市则用之前最近的交易日）。"""
    if after_close and calendar.is_trading_day(market, as_of):
        return as_of
    return calendar.prev_trading_day(market, as_of)


def suspended(ind: dict | None, expected: date) -> bool:
    """最新一根日线早于应有的收盘日，或那天没有成交：视为停牌（或数据没更新），不开新仓。"""
    if not ind:
        return True
    return date.fromisoformat(ind["date"][:10]) < expected or ind["volume"] <= 0


def fx_hkdcny(app: App, fallback: float) -> tuple[float, bool]:
    """(1 港元 = ? 人民币, 是否来自行情)。只读库。"""
    b = app.bars(FX_HKDCNH, limit=5)
    if b is not None and not b.empty:
        return float(b["close"].iloc[-1]), True
    return fallback, False


# ---------------------------------------------------------------- 晚间刷新（联网）


@dataclass
class RefreshStats:
    requested: int = 0
    ok: int = 0
    failed: int = 0
    full: int = 0                   # 首次回填或复权变化后重取全量的只数
    seconds: float = 0.0
    aborted: str | None = None
    lots: int = 0
    msgs: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float | None:
        return self.requested / self.seconds if self.seconds > 0 else None


def _fetch(app: App, session, secid: str, limit: int) -> list[dict]:
    for name in app.settings.sources.price_sources:
        bars = appmod._try(app, name, appmod.PRICE_SOURCES[name].fetch_klines, session, secid, limit=limit)
        if bars:
            return bars
    return []


def refresh_bars(app: App, cfg: FundsFile, secids: list[str], *, limit: int | None = None,
                 progress: Callable[[str], None] = lambda m: None, sleep: Callable[[float], None] = time.sleep) -> RefreshStats:
    """逐只刷新日线：库里没有或太旧的取 history 根，其余取最近 incremental 根；与库里重叠的收盘价对不上（发生了复权）时重取全量。
    按最旧的先刷新，所以中断后下次会接着做。连续失败 max_errors 只就停止（指数退避，上限 backoff_max 秒）。"""
    bc = cfg.bars
    st = RefreshStats()
    session = make_session(bc.request_delay)
    last = {r["secid"]: r["d"] for r in app.db._all(
        f"SELECT secid, MAX(date) AS d FROM bars WHERE secid IN ({', '.join('?' * len(secids))}) GROUP BY secid", secids)} if secids else {}
    counts = {r["secid"]: r["n"] for r in app.db._all(
        f"SELECT secid, COUNT(*) AS n FROM bars WHERE secid IN ({', '.join('?' * len(secids))}) GROUP BY secid", secids)} if secids else {}
    todo = sorted(secids, key=lambda s: last.get(s) or "")
    today = today_local()
    todo = [s for s in todo if (last.get(s) or "") < today.isoformat()]
    if limit is not None:
        todo = todo[:limit]
    t0, errors = time.monotonic(), 0
    for i, sid in enumerate(todo, 1):
        have_n, last_d = counts.get(sid, 0), last.get(sid)
        stale = not last_d or (today - date.fromisoformat(last_d)).days > 20
        n = bc.history if stale or have_n < bc.history * 0.8 else bc.incremental
        st.requested += 1
        bars = _fetch(app, session, sid, n)
        if bars and n == bc.incremental and not _overlap_ok(app, sid, bars):
            st.requested += 1
            bars = _fetch(app, session, sid, bc.history)
            n = bc.history
        if not bars:
            st.failed += 1
            errors += 1
            if errors >= bc.max_errors:
                st.aborted = f"连续 {errors} 只取不到日线，停止（行情源可能限流或熔断，下次从这里继续）"
                break
            sleep(min(2 ** errors, bc.backoff_max))
            continue
        errors = 0
        st.ok += 1
        st.full += n == bc.history
        app.db.upsert_bars(sid, bars)
        if i % 50 == 0:
            progress(f"日线 {i}/{len(todo)}（失败 {st.failed}）")
    st.seconds = time.monotonic() - t0
    return st


def _overlap_ok(app: App, secid: str, bars: list[dict]) -> bool:
    """新取的日线与库里同日期的收盘价是否一致（不一致说明前复权基准变了，需要重取全量）。"""
    old = app.bars(secid, limit=len(bars) + 5)
    if old is None:
        return False
    m = old.set_index("date")["close"]
    for b in bars[:-1]:  # 最后一根可能是盘中或刚收盘，不比
        if b["date"] in m.index and abs(float(m[b["date"]]) / b["close"] - 1) > 1e-3:
            return False
    return True


def lot_key(code: str) -> str:
    return f"hk_lot:{code}"


def cached_lot(app: App, code: str) -> float | None:
    v = app.db.kv_get(lot_key(code))
    try:
        return float(v.partition("@")[0]) if v else None
    except ValueError:
        return None


def refresh_lots(app: App, cfg: FundsFile, codes: list[str], sleep_session=None) -> int:
    """港股每手股数（腾讯快照第 61 个字段），缓存 lot_days 天。只对候选与持仓查询。"""
    session = sleep_session or make_session(cfg.bars.request_delay)
    n = 0
    today = today_local()
    for code in codes:
        v = app.db.kv_get(lot_key(code))
        if v and "@" in v:
            day = v.split("@", 1)[1]
            try:
                if (today - date.fromisoformat(day)).days < cfg.bars.lot_days:
                    continue
            except ValueError:
                pass
        q = appmod._try(app, "tencent", tencent.fetch_quote, session, f"116.{code}")
        if q and q.get("lot"):
            app.db.kv_set(lot_key(code), f"{q['lot']:g}@{today.isoformat()}")
            n += 1
        else:
            log.info("没有取到 %s 的每手股数", code)
    return n
