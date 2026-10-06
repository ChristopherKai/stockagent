"""取数缺口汇总：按公司列出行情 / 公告 / 申报原文 / 画像 / 一致预期 / 未分级条目哪些缺失（只读库，无网络、无 LLM）。

任务日志里只有「抛异常」的失败会打印；备用源兜底、步骤被取消、根本没有的数据都不会。这里把它们统一列出来。"""
from __future__ import annotations

from datetime import datetime, timedelta

from .app import App
from .config import Company
from .utils import utcnow

STALE_BAR_DAYS = 5
REPORT_TYPES = ("annual_report", "interim_report", "quarterly_report")
MAX_TRIAGE_ATTEMPTS = 3


def company_gaps(app: App, c: Company) -> list[str]:
    db, out = app.db, []
    for l in c.listings:
        secid = app.secid(l)
        bars = db.bars(secid, 1)
        if bars.empty:
            out.append(f"{l.display} 没有日线")
        else:
            last = datetime.fromisoformat(str(bars.iloc[-1]["date"])[:10])
            age = (utcnow().replace(tzinfo=None) - last).days
            if age > STALE_BAR_DAYS:
                out.append(f"{l.display} 日线停在 {last:%Y-%m-%d}（{age} 天前）")
        if not db.quote(secid):
            out.append(f"{l.display} 没有实时报价")
        if not db.kv_get(f"fetched:filings:{l.key}"):
            out.append(f"{l.display} 从未成功抓取公告")
    docs = db.documents(c.id)
    if not any(d["doc_type"] in REPORT_TYPES for d in docs):
        out.append("没有任何定期报告 / 申报原文（运行 stock docs fetch）")
    if not db.profile(c.id):
        out.append("没有公司画像（运行 stock profile）")
    if not db.consensus_rows(c.id):
        out.append("没有一致预期快照" + ("（港股需手动录入：stock consensus set）" if any(l.market == "HK" for l in c.listings) else ""))
    stuck = db._one(
        "SELECT COUNT(*) n FROM items WHERE company_id=? AND triaged_at IS NULL", (c.id,))["n"]
    if stuck:
        out.append(f"{stuck} 条事件未分级（含已放弃 ≥{MAX_TRIAGE_ATTEMPTS} 次者）")
    return out


def report(app: App, companies: list[Company]) -> list[str]:
    """每家有缺口的公司一行块；全部齐全时返回空列表。"""
    msgs: list[str] = []
    for c in companies:
        gaps = company_gaps(app, c)
        if gaps:
            msgs.append(f"⚠ {c.name} 数据缺口：")
            msgs += [f"    - {g}" for g in gaps]
    return msgs
