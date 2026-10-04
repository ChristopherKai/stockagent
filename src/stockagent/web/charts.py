"""标的详情页的走势图（服务端生成 SVG，不依赖前端图表库）：收盘价、目标价阶梯线、牛熊价格带、事件与修正节点。

横轴按交易日排列（不留周末空档）；事件、修正落在当天或之前最近的交易日上。
修正节点画在目标价上，事件节点画在当日收盘价上（历史价格经前复权会被改写，所以不用存下来的价格定位）。
所有文字都经 html.escape：标题与摘要来自新闻和 LLM。
"""

from __future__ import annotations

import bisect
from html import escape

from markupsafe import Markup

from ..utils import fmt_local

W, H = 960, 320
PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 16, 28
DIRECTION_CLASS = {"positive": "ev-pos", "negative": "ev-neg"}


def _fmt(v: float) -> str:
    return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"


def timeline_svg(bars, revisions: list[dict], events: list[dict], currency: str = "") -> Markup:
    """bars：DataFrame（date, close）；revisions：按时间升序（created_at, target, bull, bear, id, kind, summary）；
    events：（published_at, materiality, direction, uid, title）。没有行情时返回空。"""
    if bars is None or len(bars) < 2:
        return Markup('<p class="muted">暂无行情数据，无法绘制走势图（运行一次「抓取」任务）。</p>')
    dates = [str(d) for d in bars["date"]]
    closes = [float(c) for c in bars["close"]]
    n = len(dates)

    def idx(iso_ts: str) -> int | None:
        day = fmt_local(iso_ts, with_time=False)  # 按北京时间落到交易日
        i = bisect.bisect_right(dates, day) - 1
        return i if i >= 0 else None

    revs = [(idx(r["created_at"]), r) for r in revisions]
    revs = [(i, r) for i, r in revs if i is not None and r.get("target")]
    # 起点之前最后一次修正也要画（阶梯线从图的左端开始）
    before = [r for r in revisions if r.get("target") and idx(r["created_at"]) is None]
    if before:
        revs.insert(0, (0, before[-1]))
    values = closes + [v for _, r in revs for v in (r.get("target"), r.get("bull"), r.get("bear")) if v]
    lo, hi = min(values), max(values)
    span = (hi - lo) or abs(hi) or 1.0
    lo, hi = lo - span * 0.06, hi + span * 0.06

    right = (n - 1) + max(3, round(n * 0.06))  # 右侧留白：当前目标价与牛熊区间画成一段，刚开始追踪时也看得见

    def x(i: float) -> float:
        return PAD_L + (W - PAD_L - PAD_R) * (i / right)

    def y(v: float) -> float:
        return PAD_T + (H - PAD_T - PAD_B) * (1 - (v - lo) / (hi - lo))

    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="走势与估值修正">']
    for k in range(5):  # 纵轴刻度
        v = lo + (hi - lo) * k / 4
        out.append(f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                   f'<text class="axis" x="{PAD_L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{_fmt(v)}</text>')
    last_month = None
    for i, d in enumerate(dates):  # 横轴：每月第一个交易日
        if d[:7] != last_month:
            if last_month is not None:
                out.append(f'<text class="axis" x="{x(i):.1f}" y="{H - 8}" text-anchor="middle">{d[2:7]}</text>')
            last_month = d[:7]
    if revs:  # 牛熊价格带与目标价阶梯线
        band_top, band_bot, line = [], [], []
        for k, (i, r) in enumerate(revs):
            j = revs[k + 1][0] if k + 1 < len(revs) else right
            if r.get("bull") and r.get("bear"):
                band_top += [(x(i), y(r["bull"])), (x(j), y(r["bull"]))]
                band_bot += [(x(i), y(r["bear"])), (x(j), y(r["bear"]))]
            line += [(x(i), y(r["target"])), (x(j), y(r["target"]))]
        if band_top:
            pts = band_top + list(reversed(band_bot))
            out.append('<polygon class="band" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in pts) + '"/>')
        out.append('<polyline class="target" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in line) + '"/>')
        last = revs[-1][1]
        out.append(f'<text class="tlabel" x="{W - PAD_R}" y="{y(last["target"]) - 6:.1f}" text-anchor="end">{_fmt(last["target"])}</text>')
    out.append('<polyline class="close" points="' + " ".join(f"{x(i):.1f},{y(c):.1f}" for i, c in enumerate(closes)) + '"/>')
    for e in events:
        i = idx(e["published_at"])
        if i is None:
            continue
        cls = DIRECTION_CLASS.get(e.get("direction") or "", "ev-neu")
        r = 2.5 + (e.get("materiality") or 1) * 1.1
        tip = escape(f"{fmt_local(e['published_at'])} ★{e.get('materiality')} {e.get('title', '')}")
        out.append(f'<a href="#ev-{escape(e["uid"][:12])}"><circle class="ev {cls}" cx="{x(i):.1f}" cy="{y(closes[i]):.1f}" '
                   f'r="{r:.1f}"><title>{tip}</title></circle></a>')
    for i, r in revs:
        if r.get("kind") == "review" and not r.get("changed"):
            continue
        cx, cy = x(i), y(r["target"])
        tip = escape(f"{fmt_local(r['created_at'])} {r.get('label', '')} 目标价 {_fmt(r['target'])} {currency}：{r.get('summary', '')}")
        out.append(f'<a href="#rev-{r["id"]}"><path class="rev rev-{escape(r.get("kind", ""))}" '
                   f'd="M{cx:.1f},{cy - 6:.1f} L{cx + 6:.1f},{cy:.1f} L{cx:.1f},{cy + 6:.1f} L{cx - 6:.1f},{cy:.1f} Z">'
                   f'<title>{tip}</title></path></a>')
    out.append(f'<text class="axis" x="{PAD_L}" y="{PAD_T - 4}">{escape(currency)}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def nav_svg(navs: list[dict], height: int = 220) -> Markup:
    """模拟基金净值走势（服务端 SVG）：上半部分单位净值与基准（按建账日归一），下半部分从最高点的回撤。
    navs：nav_daily 的行（date, nav, benchmark_nav, drawdown），按日期升序。"""
    if len(navs) < 2:
        return Markup('<p class="muted small">净值记录不足两天，暂不画图（每个交易日盘后结算后增加一天）。</p>')
    w, h = W, height
    top_h = h * 0.68
    dates = [str(r["date"]) for r in navs]
    nav = [float(r["nav"]) for r in navs]
    bench = [float(r["benchmark_nav"]) if r.get("benchmark_nav") else None for r in navs]
    dd = [float(r["drawdown"] or 0.0) for r in navs]
    vals = nav + [b for b in bench if b is not None]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 0.02
    lo, hi = lo - span * 0.08, hi + span * 0.08
    dmax = max(max(dd), 0.02)
    n = len(navs)

    def x(i: int) -> float:
        return PAD_L + (w - PAD_L - PAD_R) * (i / (n - 1))

    def y(v: float) -> float:
        return PAD_T + (top_h - PAD_T) * (1 - (v - lo) / (hi - lo))

    def yd(v: float) -> float:
        return top_h + 14 + (h - PAD_B - top_h - 14) * (v / dmax)

    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="单位净值与回撤">']
    for k in range(4):
        v = lo + (hi - lo) * k / 3
        out.append(f'<line class="grid" x1="{PAD_L}" x2="{w - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                   f'<text class="axis" x="{PAD_L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.3f}</text>')
    out.append(f'<text class="axis" x="{PAD_L - 6}" y="{yd(dmax) + 4:.1f}" text-anchor="end">-{dmax * 100:.0f}%</text>'
               f'<line class="grid" x1="{PAD_L}" x2="{w - PAD_R}" y1="{yd(0):.1f}" y2="{yd(0):.1f}"/>')
    last_month = None
    for i, d in enumerate(dates):
        if d[:7] != last_month:
            if last_month is not None:
                out.append(f'<text class="axis" x="{x(i):.1f}" y="{h - 8}" text-anchor="middle">{escape(d[2:7])}</text>')
            last_month = d[:7]
    bpts = [(x(i), y(b)) for i, b in enumerate(bench) if b is not None]
    if len(bpts) >= 2:
        out.append('<polyline class="target" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in bpts) + '"><title>基准</title></polyline>')
    out.append('<polyline class="close" points="' + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(nav)) + '"><title>单位净值</title></polyline>')
    area = [(x(0), yd(0))] + [(x(i), yd(v)) for i, v in enumerate(dd)] + [(x(n - 1), yd(0))]
    out.append('<polygon class="dd" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in area) + '"><title>回撤</title></polygon>')
    out.append(f'<text class="tlabel" x="{w - PAD_R}" y="{y(nav[-1]) - 6:.1f}" text-anchor="end">{nav[-1]:.4f}</text>')
    out.append(f'<text class="axis" x="{PAD_L}" y="{PAD_T - 4}">单位净值（实线）· 基准（虚线）· 下方为回撤</text>')
    out.append("</svg>")
    return Markup("".join(out))
