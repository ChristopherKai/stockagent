"""研究 agent 的工具箱：计算与估值、公司资料检索、行情、事件、手动数据、网页搜索与抓取。

工具返回给模型读的纯文本；每次调用都记在 ToolBox.log 里，报告附录据此列出研究过程与出处。
"""

from __future__ import annotations

import ast
import json
import math
import operator
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from . import imafeed, technical
from .app import FX_HKDCNH, PRICE_SOURCES, SOURCE_LABEL, App, _try, klines
from .config import Company, Listing
from .docs import DOC_TYPES, read_document_text
from .sources import eastmoney, emdata, ima, tencent, web
from .triage import DIRECTIONS, event_label
from .utils import big_num, fmt_local, iso, num, pct, to_simplified, utcnow



@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------- 安全计算器

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "len": len, "sqrt": math.sqrt,
          "log": math.log, "log10": math.log10, "exp": math.exp, "pow": pow, "ceil": math.ceil, "floor": math.floor}
_CONSTS = {"pi": math.pi, "e": math.e}


def safe_calc(code: str) -> tuple[object, dict]:
    """只允许数字、四则运算、乘方、白名单函数、列表和简单赋值。不能导入、访问属性、做字符串操作。"""
    tree = ast.parse(code.strip(), mode="exec")
    env: dict[str, object] = {}
    last = None

    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in _CONSTS:
                return _CONSTS[node.id]
            raise ValueError(f"未定义的变量：{node.id}")
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if not all(isinstance(x, (int, float)) for x in (left, right)):
                raise ValueError("只能对数字做运算（列表请用 sum/min/max）")
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("指数过大")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        if isinstance(node, (ast.List, ast.Tuple)):
            return [ev(x) for x in node.elts]
        raise ValueError(f"不支持的写法：{type(node).__name__}")

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            last = env[stmt.targets[0].id] = ev(stmt.value)
        elif isinstance(stmt, ast.Expr):
            last = ev(stmt.value)
        else:
            raise ValueError("只支持表达式和简单赋值，例如 a = 12.3")
    return last, env


def fmt_number(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(fmt_number(x) for x in v) + "]"
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return str(v)
    if abs(v) >= 1e15:
        return f"{v:.4e}"
    s = f"{v:,.6f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


# ---------------------------------------------------------------- 估值

def dcf_value(fcf: float, growth: float, discount_rate: float, terminal_growth: float, years: int = 5,
              net_cash: float = 0.0, shares: float | None = None) -> dict:
    if discount_rate <= terminal_growth:
        raise ValueError("折现率必须大于永续增长率")
    f, pv = fcf, 0.0
    for t in range(1, years + 1):
        f *= 1 + growth
        pv += f / (1 + discount_rate) ** t
    tv = f * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_tv = tv / (1 + discount_rate) ** years
    ev = pv + pv_tv
    equity = ev + net_cash
    return {"pv_explicit": pv, "pv_terminal": pv_tv, "enterprise_value": ev, "equity_value": equity,
            "per_share": equity / shares if shares else None, "terminal_share": pv_tv / ev if ev else None}


def implied_growth(market_value: float, fcf: float, discount_rate: float, terminal_growth: float,
                   years: int = 5, net_cash: float = 0.0) -> float | None:
    """当前市值隐含的显性期年增长率（二分法）；超出 -50%~300% 返回 None。"""
    if fcf <= 0:
        raise ValueError("fcf 必须为正；现金流为负时反向 DCF 不适用，可改用 valuation_matrix（收入 × 倍数）")
    target = market_value - net_cash

    def gap(g: float) -> float:
        return dcf_value(fcf, g, discount_rate, terminal_growth, years)["enterprise_value"] - target

    lo, hi = -0.5, 3.0
    if gap(lo) > 0 or gap(hi) < 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if gap(mid) < 0 else (lo, mid)
    return (lo + hi) / 2


def matrix_price(driver: float, multiple: float, shares: float, net_cash: float = 0.0, fx: float = 1.0) -> float:
    """敏感性矩阵的一个格点：隐含股价 =（倍数 × 驱动指标 + 净现金）÷ 股本 × 汇率。估值模型（valuation.py）用同一公式。"""
    return (multiple * driver + net_cash) / shares * fx


def matrix_table(driver_label: str, driver_values: list[float], multiple_label: str, multiples: list[float],
                 shares: float, net_cash: float = 0.0, fx: float = 1.0, current_price: float | None = None) -> str:
    if shares <= 0:
        raise ValueError("shares 必须为正")
    head = f"| {driver_label} ↓ / {multiple_label} → | " + " | ".join(fmt_number(m) + "×" for m in multiples) + " |"
    rows = [head, "|---|" + "---|" * len(multiples)]
    for d in driver_values:
        cells = []
        for m in multiples:
            price = matrix_price(d, m, shares, net_cash, fx)
            cell = f"{price:,.2f}"
            if current_price:
                cell += f"（{pct(price / current_price - 1, 0)}）"
            cells.append(cell)
        rows.append(f"| {fmt_number(d)} | " + " | ".join(cells) + " |")
    note = f"隐含股价 =（倍数 × {driver_label} + 净现金 {fmt_number(net_cash)}）÷ 股本 {fmt_number(shares)} × 汇率 {fmt_number(fx)}"
    if current_price:
        note += f"；括号内为相对现价 {fmt_number(current_price)} 的涨跌幅"
    return "\n".join(rows) + "\n" + note


def sotp_value(parts: list[dict], shares: float, net_cash: float = 0.0, fx: float = 1.0,
               holdco_discount: float = 0.0) -> tuple[list[dict], float, float]:
    """分部加总：Σ(分部价值 × 持股比例 × (1 − 分部折价)) + 净现金，再扣控股公司折价；返回 (明细, 股权价值, 每股价值)。"""
    if shares <= 0:
        raise ValueError("shares 必须为正")
    rows, total = [], 0.0
    for p in parts:
        stake = float(p.get("stake", 1.0))
        disc = float(p.get("discount", 0.0))
        if not (0 < stake <= 1) or not (0 <= disc < 1):
            raise ValueError(f"{p.get('name')}：stake 应在 (0,1]，discount 应在 [0,1)")
        attributable = float(p["value"]) * stake * (1 - disc)
        rows.append({**p, "stake": stake, "discount": disc, "attributable": attributable})
        total += attributable
    equity = (total + net_cash) * (1 - holdco_discount)
    return rows, equity, equity / shares * fx


def ddm_value(dividend: float, discount_rate: float, growth: float, high_growth: float | None = None,
              high_years: int = 0) -> float:
    """股息贴现（每股）：前 high_years 年按 high_growth 增长，之后按 growth 永续（Gordon）。dividend 为最近一年每股股息。"""
    if discount_rate <= growth:
        raise ValueError("折现率必须大于永续增长率")
    pv, d = 0.0, dividend
    for t in range(1, high_years + 1):
        d *= 1 + (high_growth if high_growth is not None else growth)
        pv += d / (1 + discount_rate) ** t
    terminal = d * (1 + growth) / (discount_rate - growth)
    return pv + terminal / (1 + discount_rate) ** high_years


def percentile_rank(values: list[float], x: float) -> float:
    return sum(1 for v in values if v <= x) / len(values) if values else float("nan")


def _stats_line(label: str, values: list[float], current: float | None) -> str:
    if not values or current is None:
        return f"{label}：数据不足"
    q = statistics.quantiles(values, n=4)
    return (f"{label}：当前 {current:.2f}，历史分位 {percentile_rank(values, current):.0%}；"
            f"区间 {min(values):.2f}–{max(values):.2f}，25%/50%/75% 分位 {q[0]:.2f}/{q[1]:.2f}/{q[2]:.2f}（样本 {len(values)} 个交易日）")


# ---------------------------------------------------------------- 资料检索

_PAGE_RE = re.compile(r"\[第(\d+)页\]\n")


def split_pages(text: str) -> list[tuple[int, str]]:
    parts = _PAGE_RE.split(text)
    if len(parts) == 1:  # 没有页码标记（TXT/DOCX 等）：每 3000 字算一页
        return [(i + 1, text[i * 3000 : (i + 1) * 3000]) for i in range(max(1, math.ceil(len(text) / 3000)))]
    return [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def _recent_series(bars: list[dict] | None) -> bool:
    """交易所后缀不对时，腾讯只返回 1 根当前价，或返回已换所旧代码的陈旧历史再拼上当前价；
    最近 3 根是连续的近期交易日才算识别成功。"""
    if not bars or len(bars) < 3:
        return False
    ds = [date.fromisoformat(b["date"]) for b in bars[-3:]]
    return (ds[-1] - ds[0]).days <= 10 and (date.today() - ds[-1]).days <= 10


def resolve_ticker(app: App, ticker: str) -> list[tuple[str, str, str]]:
    """把名称或代码解析成 [(东方财富 secid, 显示名, 币种)]；自选股返回全部上市地。"""
    c = app.watchlist.get(ticker)
    if c is not None:
        return [(app.secid(l), f"{c.name} {l.display}", l.currency) for l in c.listings]
    t = ticker.strip().upper()
    # 先认带交易所后缀的写法（189.HK、688337.SH），再认东方财富 secid（105.NVDA、116.02577），最后是纯数字
    if m := re.fullmatch(r"(\d{1,5})\.HK", t):
        l = Listing(market="HK", code=m.group(1))
        return [(eastmoney.secid_for(l), l.display, "HKD")]
    if m := re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", t):
        l = Listing(market="A", code=m.group(1), exchange=m.group(2))
        return [(eastmoney.secid_for(l), l.display, "CNY")]
    if re.fullmatch(r"\d{1,3}\.[A-Z0-9.]+", t):
        return [(t, t, "")]
    if re.fullmatch(r"\d{6}", t):
        l = Listing(market="A", code=t)
        return [(eastmoney.secid_for(l), l.display, "CNY")]
    if re.fullmatch(r"\d{1,5}", t):
        l = Listing(market="HK", code=t)
        return [(eastmoney.secid_for(l), l.display, "HKD")]
    if re.fullmatch(r"[A-Z][A-Z.\-]{0,7}", t):
        cached = app.db.kv_get(f"us_secid:{t}")
        if cached:
            return [(cached, t, "USD")]
        for prefix in ("105", "106", "107"):
            probe = _try(app, "tencent", tencent.fetch_klines, app.session, f"{prefix}.{t}", limit=5)
            if _recent_series(probe):
                app.db.kv_set(f"us_secid:{t}", f"{prefix}.{t}")
                return [(f"{prefix}.{t}", t, "USD")]
    raise ValueError(f"无法识别代码 {ticker}（港股 5 位数字、A 股 6 位数字、美股字母代码）")


def listings_for(app: App, company: Company, ticker: str | None) -> list[Listing]:
    """结构化数据只支持 A 股与港股：不填 ticker 表示本公司。"""
    if not ticker:
        return [l for l in company.listings if l.market in ("A", "HK")]
    c = app.watchlist.get(ticker)
    if c is not None:
        return [l for l in c.listings if l.market in ("A", "HK")]
    t = ticker.strip().upper()
    if m := re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", t):
        return [Listing(market="A", code=m.group(1), exchange=m.group(2))]
    if m := re.fullmatch(r"(\d{1,5})(?:\.HK)?", t):
        return [Listing(market="HK", code=m.group(1))]
    raise ValueError(f"{ticker}：结构化财务与估值数据只支持 A 股（6 位）和港股（5 位）代码")


def latest_quote(app: App, secid: str) -> dict:
    """库里的最新快照；没有时从行情源抓取一次。"""
    q = app.db.quote(secid)
    if not q:
        for name in app.settings.sources.price_sources:
            q = _try(app, name, PRICE_SOURCES[name].fetch_quote, app.session, secid)
            if q:
                app.db.save_quote(secid, q)
                break
    return q or {}


def hk_consensus(app: App, listing: Listing) -> str:
    """港股卖方一致预期：富途个股预测页（脚本渲染，经 Exa 读取）上的分析师数量与目标价均值/区间。"""
    text = web.exa_fetch(f"https://www.futunn.com/hk/stock/{listing.code}-HK/forecast", 4000)
    if not text:
        return (f"{listing.display}：港股一致预期需要用 Exa（agent-reach 配置的 mcporter）读取富途个股预测页，当前不可用；"
                "请用 web_search 搜「公司名 目标价 评级」，注明机构与日期")
    t = to_simplified(text)
    m = re.search(r"目标价预测平均价为([\d.]+)，最高价为([\d.]+)，最低价为([\d.]+)", t)
    if not m:
        return f"{listing.display}：富途个股预测页没有目标价数据"
    avg, hi, lo = (float(x) for x in m.groups())
    n = re.search(r"近三个月共(\d+)位分析师", t)
    dates = re.findall(r"更新时间[:：]\s*(\d{4}/\d{2}/\d{2})", t)
    price = latest_quote(app, f"116.{listing.code}").get("price")
    line = (f"{listing.display} 卖方一致预期（富途个股预测页{f'，更新于 {dates[-1]}' if dates else ''}，经 Exa 读取）："
            + (f"近三个月 {n.group(1)} 位分析师给出评级；" if n else "")
            + f"目标价均值 {avg:g} 港元，最高 {hi:g}，最低 {lo:g}")
    if price:
        line += f"；相对现价 {price:g} 港元，均值 {pct(avg / price - 1)}、最低 {pct(lo / price - 1)}"
    return line + "。各家机构的最新目标价与盈利预测请再用 web_search 核实日期。"


def _payback_years(ev: float, fcf: float, growth: float, cap: int = 50) -> str:
    """未来 FCF（按 growth 增长）累计多少年能覆盖当前 EV。"""
    if ev <= 0:
        return "EV 已为负"
    if fcf <= 0:
        return "FCF 为负，不适用"
    total = 0.0
    for n in range(1, cap + 1):
        total += fcf * (1 + growth) ** n
        if total >= ev:
            return f"约 {n} 年"
    return f"超过 {cap} 年"


def fcf_snapshot(app: App, company: Company, ticker: str | None = None, growth: float = 0.0,
                 forward_fcf: float | None = None, forward_label: str = "") -> str:
    """EV、自由现金流与 FCF Yield（代码计算；A+H 公司用 A 股报表与 A 股市值）。"""
    ls = listings_for(app, company, ticker)
    if not ls:
        return "EV 与自由现金流只支持 A 股和港股"
    l = next((x for x in ls if x.market == "A"), ls[0])
    data = emdata.fcf_data(app.session, l)
    if not data:
        return f"{l.display}：没有取到现金流量表或资产负债表"
    cur = "CNY"
    if l.market == "HK":
        rows = emdata.financials(app.session, l, 4)
        cur = rows[0].get("币种", "CNY") if rows else "CNY"
    secid = eastmoney.secid_for(l)
    q = latest_quote(app, secid)
    if not q.get("market_cap"):
        return f"{l.display}：没有取到市值"
    mcap, note = q["market_cap"], f"总股本 × 最新价 {num(q.get('price'))} {l.currency}"
    if l.market == "HK" and cur != "HKD":
        if cur == "CNY":
            fx = ensure_bars(app, FX_HKDCNH)
            if fx is None or fx.empty:
                return f"{l.display}：没有取到港元兑人民币汇率，无法换算市值"
            rate = float(fx["close"].iloc[-1])
            mcap *= rate
            note += f"，按 1 HKD = {rate:.4f} CNY 换算"
        else:
            mcap /= 7.8
            note += "，按 1 USD = 7.8 HKD 换算"
    b = data["balance"]
    cash_like = b["cash"] + b["invest"]
    ev = mcap + b["debt"] + b["minority"] - cash_like
    net_cash = cash_like - b["debt"]
    yi = 1e8

    def f(v: float) -> str:
        return f"{v / yi:,.2f}"

    flows = [x for x in [emdata.ttm_flow(data["flows"])] if x] + [
        {**x, "label": f"{x['date'][:4]} 年报"} for x in data["flows"] if x["annual"] and x["ocf"] is not None][:3]
    annual = [x for x in flows if x["label"].endswith("年报")]
    if flows and flows[0]["label"].endswith("年报") and len(annual) > 3:
        annual = annual[:3]
    lines = [f"{l.display} EV 与自由现金流（程序按东方财富现金流量表/资产负债表计算；金额：亿 {cur}；资产负债表截至 {b['date']}）",
             f"- 市值 {f(mcap)}（{note}）",
             f"- 现金及存款 {f(b['cash'])}"
             + (f"（{'、'.join(f'{k} {f(v)}' for k, v in b['cash_items'].items())}）" if len(b.get("cash_items") or {}) > 1 else "")
             + f"，短期投资 {f(b['invest'])}"
             + (f"；受限制现金 {f(b['restricted'])} 未计入（多为应付票据等的保证金）" if b.get("restricted") else "")
             + f"；有息负债 {f(b['debt'])}"
             + (f"（{'、'.join(f'{k} {f(v)}' for k, v in b['debt_items'].items())}）" if b["debt_items"] else "")
             + f"；少数股东权益 {f(b['minority'])}",
             f"- EV = 市值 + 有息负债 + 少数股东权益 − 现金及短期投资 = {f(ev)}",
             f"- 净现金 {f(net_cash)}，占市值 {pct(net_cash / mcap, sign=False)}；Negative EV（净现金超过市值）：{'是' if ev < 0 else '否'}",
             "", "| 期间 | 经营现金流 | 资本开支 | FCF | FCF Yield（FCF ÷ EV） |", "|---|---|---|---|---|"]
    for x in flows:
        fcf = x["ocf"] - x["capex"]
        lines.append(f"| {x['label']} | {f(x['ocf'])} | {f(x['capex'])} | {f(fcf)} | "
                     + (f"{pct(fcf / ev, sign=False)} |" if ev > 0 else "EV 为负 |"))
    avg = None
    if len(annual) >= 3:
        avg = sum(x["ocf"] - x["capex"] for x in annual[:3]) / 3
        lines.append(f"| 近 3 个年度平均 | | | {f(avg)} | " + (f"{pct(avg / ev, sign=False)} |" if ev > 0 else "EV 为负 |"))
    base = (flows[0]["ocf"] - flows[0]["capex"]) if flows else 0
    lines.append("")
    lines.append(f"- FCF 累计覆盖 EV 的年数（FCF 年增速假设 {pct(growth)}）：按 {flows[0]['label'] if flows else '—'} "
                 f"{_payback_years(ev, base, growth)}" + (f"；按 3 年平均 {_payback_years(ev, avg, growth)}" if avg is not None else ""))
    if forward_fcf is not None:
        fwd = float(forward_fcf) * yi
        lines.append(f"- 前瞻 FCF Yield（{forward_label or '前瞻年度'} FCF {forward_fcf:g} 亿）：" + (pct(fwd / ev, sign=False) if ev > 0 else "EV 为负"))
    if b["liabilities"]:
        ev2 = mcap + b["liabilities"] - b["cash"]
        lines.append(f"- 保守口径（EV = 市值 + 总负债 − 现金，雪球「价值投资新经济」框架的算法）：EV {f(ev2)}"
                     + (f"，{flows[0]['label']} FCF Yield {pct(base / ev2, sign=False)}" if flows and ev2 > 0 else ""))
    lines.append("- 说明：FCF = 经营现金流 − 购建长期资产支付的现金。周期股的 FCF 波动大，应看多年平均；资本开支扩张期 FCF 为负时 "
                 "FCF Yield 不适用，要看前瞻 FCF。现金未计入放在「其他流动资产」「债权投资」中的定期存款、大额存单，以报表附注为准。")
    return "\n".join(lines)


def ensure_bars(app: App, secid: str, max_age_days: int = 4, min_bars: int = 0):
    """库里没有或太旧时抓取日线，返回 DataFrame。"""
    bars = app.bars(secid, limit=max(400, min_bars))
    if (bars is None or (date.today() - date.fromisoformat(bars["date"].iloc[-1])).days > max_age_days
            or len(bars) < min_bars):
        fresh, _ = klines(app, secid, max(300, min_bars))
        if fresh:
            app.db.upsert_bars(secid, fresh)
            bars = app.bars(secid, limit=max(400, min_bars))
    return bars


# ---------------------------------------------------------------- 工具箱

class ToolBox:
    def __init__(self, app: App, company: Company, allow: list[str] | None = None):
        """allow：工具白名单。不在其中的工具既不提供给模型，模型硬要调用时也只返回错误（specs 只是告诉模型有哪些工具）。"""
        self.app = app
        self.company = company
        self.max_chars = app.settings.agent.tool_result_chars
        self.web_providers = list(app.settings.agent.web_search)
        self.log: list[dict] = []
        self._pages: dict[int, list[tuple[int, str, str]]] = {}  # doc_id → [(页码, 原文, 简体)]
        self._ima: dict[str, dict] = {}  # ima_search 结果编号 → 条目
        self.tools = {t.name: t for t in self._build() if allow is None or t.name in allow}

    def specs(self, names: list[str] | None = None) -> list[dict]:
        return [t.spec() for n, t in self.tools.items() if names is None or n in names]

    def call(self, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            result = f"错误：没有名为 {name} 的工具，可用：{', '.join(self.tools)}"
        else:
            missing = [k for k in tool.parameters.get("required", []) if k not in args]
            if missing:
                result = f"错误：缺少参数 {', '.join(missing)}"
            else:
                try:
                    allowed = set(tool.parameters.get("properties", {}))
                    result = tool.fn(**{k: v for k, v in args.items() if k in allowed})
                except Exception as e:  # 工具出错也要告诉模型，让它换个方式
                    result = f"错误：{type(e).__name__}: {str(e)[:300]}"
        if len(result) > self.max_chars:
            result = result[: self.max_chars] + f"\n……（结果过长已截断，共 {len(result):,} 字）"
        self.log.append({"tool": name, "args": args, "result": result, "at": iso(utcnow())})
        return result

    # ------------------------------------------------------------ 工具实现

    def _build(self) -> list[Tool]:
        return self._base_tools() + (self._ima_tools() if imafeed.enabled(self.app) else [])

    def _ima_tools(self) -> list[Tool]:
        return [
            Tool("ima_search",
                 "在使用者订阅的 ima 知识库中检索：外资与内资券商研报（高盛、摩根士丹利、中金等）、调研与电话会纪要、彭博/路透等付费新闻截图。"
                 "只按标题匹配：用公司名、代码（如 2577.HK、0100.HK、300308）、机构名或主题词；多个词用空格分隔时先搜第一个词、再按其余词筛选"
                 "（如「中际旭创 高盛」）。返回编号、日期、类型、标题，按日期倒序；用 ima_read 读取原文。",
                 _obj({"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"]),
                 self.ima_search),
            Tool("ima_read",
                 "读取 ima_search 结果中的一条（编号如 I3）：PDF/Word 入库为本公司资料，返回资料编号与开头内容，之后用 search_filings / read_filing "
                 "按页检索和引用；图片返回 OCR 文字（可能有错字）。笔记、录音、Excel 读不到原文。",
                 _obj({"ref": {"type": "string", "description": "ima_search 返回的编号，如 I3"}}, ["ref"]),
                 self.ima_read),
        ]

    def _base_tools(self) -> list[Tool]:
        return [
            Tool("calculate",
                 "计算器（Python 语法）：+ - * / ** %、括号、abs/round/min/max/sum/sqrt/log/exp 等函数、列表，支持多行简单赋值，"
                 "例如 \"a = 12.3\\nb = 45.6\\n(b / a) ** (1 / 4) - 1\"。所有计算都要用它，不要心算。",
                 _obj({"code": {"type": "string", "description": "要计算的表达式或多行赋值"}}, ["code"]),
                 self.calculate),
            Tool("dcf_valuation",
                 "两阶段 DCF：以 fcf（最近一年自由现金流或利润）为基数按 growth 增长 years 年，之后按 terminal_growth 永续增长，"
                 "用 discount_rate 折现，加净现金得股权价值；给出 shares 时算每股价值。比率用小数（10% 写 0.10），金额单位保持一致。",
                 _obj({"fcf": {"type": "number"}, "growth": {"type": "number"}, "discount_rate": {"type": "number"},
                       "terminal_growth": {"type": "number"}, "years": {"type": "integer"},
                       "net_cash": {"type": "number"}, "shares": {"type": "number"}},
                      ["fcf", "growth", "discount_rate", "terminal_growth"]),
                 self.dcf_valuation),
            Tool("reverse_dcf",
                 "反向 DCF：当前市值（market_value）隐含未来 years 年的年均增长率。fcf 为最近一年自由现金流或利润（须为正），"
                 "金额单位与 market_value、net_cash 一致（注意币种换算）。",
                 _obj({"market_value": {"type": "number"}, "fcf": {"type": "number"}, "discount_rate": {"type": "number"},
                       "terminal_growth": {"type": "number"}, "years": {"type": "integer"}, "net_cash": {"type": "number"}},
                      ["market_value", "fcf", "discount_rate", "terminal_growth"]),
                 self.reverse_dcf),
            Tool("valuation_matrix",
                 "敏感性矩阵：隐含股价 =（倍数 × 驱动指标 + 净现金）÷ 股本 × 汇率。例如驱动指标取 2027 年净利润、倍数取 PE；"
                 "亏损公司可取收入、倍数取 EV/Sales。fx 把财报货币换成股价货币，必须使用上下文给出的最新汇率（不要凭记忆），current_price 用于算相对现价涨跌。",
                 _obj({"driver_label": {"type": "string"}, "driver_values": {"type": "array", "items": {"type": "number"}},
                       "multiple_label": {"type": "string"}, "multiples": {"type": "array", "items": {"type": "number"}},
                       "shares": {"type": "number"}, "net_cash": {"type": "number"}, "fx": {"type": "number"},
                       "current_price": {"type": "number"}},
                      ["driver_label", "driver_values", "multiple_label", "multiples", "shares"]),
                 self.valuation_matrix),
            Tool("list_filings", "列出本公司已入库的资料（年报、中期报告、研报、笔记等）及编号、页数。", _obj({}, []), self.list_filings),
            Tool("search_filings",
                 "在本公司已入库资料中检索，返回最相关的页和原文片段（带页码）。query 用空格分隔关键词，如「毛利率 分部」「禁售 解禁」"
                 "「现金及现金等价物」；简繁体自动兼容。注意用词差异：港股年报常写「五大客户」「最大客户」「附属公司」「非控股权益」，"
                 "A 股年报常写「前五名客户」「子公司」「少数股东权益」；搜不到时换同义词再试。可用 doc_id 限定某份资料。",
                 _obj({"query": {"type": "string"}, "doc_id": {"type": "integer"}, "top_k": {"type": "integer"}}, ["query"]),
                 self.search_filings),
            Tool("read_filing", "读取某份资料的指定页原文（一次最多 4 页），用于核对表格与上下文。",
                 _obj({"doc_id": {"type": "integer"}, "start_page": {"type": "integer"}, "end_page": {"type": "integer"}},
                      ["doc_id", "start_page"]),
                 self.read_filing),
            Tool("market_data",
                 "查询任意标的行情与估值：自选股名称，或代码如 02577、00189.HK、300308、688337.SH、NVDA、106.COHR。"
                 "返回价格、区间涨跌、趋势、RSI、波动率、250 日高低点、PE/PB/市值（如有）；A+H 公司附 AH 溢价。",
                 _obj({"ticker": {"type": "string"}}, ["ticker"]),
                 self.market_data),
            Tool("financials",
                 "结构化主要财务指标（东方财富数据中心，A 股与港股）：最近多期的收入、毛利率、归母净利、同比、净利率、ROE、EPS、"
                 "每股净资产、经营现金流、负债率、周转天数等。核心财务数字优先用它，再用年报原文核对细节。ticker 不填表示本公司。",
                 _obj({"ticker": {"type": "string"}, "periods": {"type": "integer"}}, []),
                 self.financials),
            Tool("broker_research",
                 "卖方一致预期。A 股：东方财富研报库近几个月的研报列表，汇总评级分布与 EPS 预测中位数、目标价中位数；"
                 "港股：富途个股预测页的分析师数量与目标价均值/区间（经 Exa 读取）。单家机构的最新目标价请再用 web_search 核实。ticker 不填表示本公司。",
                 _obj({"ticker": {"type": "string"}, "months": {"type": "integer"}}, []),
                 self.broker_research),
            Tool("fcf_yield",
                 "EV 与自由现金流收益率（代码按东方财富现金流量表/资产负债表计算）：市值、现金、有息负债、少数股东权益、EV、"
                 "FCF（经营现金流 − 资本开支；TTM、近 3 个年度、3 年平均）、FCF Yield、净现金占市值、是否 Negative EV、FCF 累计覆盖 EV 的年数，"
                 "以及按总负债计算的保守口径。growth 为 FCF 年增速假设（小数，默认 0）；forward_fcf 为前瞻年度 FCF（亿元，报告币种）、"
                 "forward_label 为其年份，用于计算前瞻 FCF Yield。ticker 不填表示本公司，也可查可比公司（A 股 6 位、港股 5 位）。",
                 _obj({"ticker": {"type": "string"}, "growth": {"type": "number"}, "forward_fcf": {"type": "number"},
                       "forward_label": {"type": "string"}}, []),
                 self.fcf_yield),
            Tool("valuation_history",
                 "历史估值分位：当前 PE(TTM)/PB 在过去 years 年中的分位与区间（A 股用东方财富估值历史；港股用日收盘价 ÷ 最近已披露的 "
                 "EPS(TTM)/每股净资产计算，考虑披露滞后并按汇率换算）。亏损期 PE 无意义，看 PB。ticker 不填表示本公司。",
                 _obj({"ticker": {"type": "string"}, "years": {"type": "integer"}}, []),
                 self.valuation_history),
            Tool("southbound",
                 "港股通（南向资金）持股：最新持股占比，以及近 5/20/60 个交易日的增减持（股数、占比变化）。仅港股。ticker 不填表示本公司。",
                 _obj({"ticker": {"type": "string"}}, []),
                 self.southbound),
            Tool("sotp",
                 "分部加总估值（SOTP）：parts 为各业务/持股资产 [{name, value（100% 口径价值，单位与 net_cash 一致）, stake（持股比例 0–1）, "
                 "discount（该部分折价 0–1，可选）, method（估值依据，如「上市子公司市值」「8× 分部利润」）}]；再加净现金、扣控股公司折价，"
                 "除以股本并按 fx 换成股价货币。适合有上市子公司或多业务的公司。",
                 _obj({"parts": {"type": "array", "items": {"type": "object", "properties": {
                           "name": {"type": "string"}, "value": {"type": "number"}, "stake": {"type": "number"},
                           "discount": {"type": "number"}, "method": {"type": "string"}}, "required": ["name", "value"]}},
                       "shares": {"type": "number"}, "net_cash": {"type": "number"}, "fx": {"type": "number"},
                       "holdco_discount": {"type": "number"}, "current_price": {"type": "number"}},
                      ["parts", "shares"]),
                 self.sotp),
            Tool("ddm",
                 "股息贴现（每股）：dividend 为最近一年每股股息；前 high_years 年按 high_growth 增长，之后按 growth 永续；"
                 "适合稳定分红的公司。给出 current_price 时同时计算隐含涨跌与股息率。",
                 _obj({"dividend": {"type": "number"}, "discount_rate": {"type": "number"}, "growth": {"type": "number"},
                       "high_growth": {"type": "number"}, "high_years": {"type": "integer"}, "current_price": {"type": "number"}},
                      ["dividend", "discount_rate", "growth"]),
                 self.ddm),
            Tool("company_events", "查询本公司已分级的新闻与公告（重要性、方向、摘要、来源链接），按时间倒序。",
                 _obj({"days": {"type": "integer"}, "min_materiality": {"type": "integer"}}, []),
                 self.company_events),
            Tool("manual_data", "查询使用者手动补充的数据（公司指标，以及行业/宏观指标）。", _obj({}, []), self.manual_data),
            Tool("web_search",
                 "网页搜索，用于查行业数据、竞争对手、政策、卖方观点等。结果只是线索，重要数字要用 fetch_url 打开原文或与年报交叉核实。",
                 _obj({"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"]),
                 self.web_search),
            Tool("fetch_url", "抓取网页或 PDF（如交易所公告原文）的正文。",
                 _obj({"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
                 self.fetch_url),
        ]

    def calculate(self, code: str) -> str:
        last, env = safe_calc(code)
        out = f"结果：{fmt_number(last)}"
        if len(env) > 1 or (env and last is not list(env.values())[-1]):
            out += "\n变量：" + "，".join(f"{k} = {fmt_number(v)}" for k, v in env.items())
        return out

    def dcf_valuation(self, fcf, growth, discount_rate, terminal_growth, years=5, net_cash=0.0, shares=None) -> str:
        r = dcf_value(float(fcf), float(growth), float(discount_rate), float(terminal_growth), int(years), float(net_cash),
                      float(shares) if shares else None)
        out = (f"显性期现值 {fmt_number(r['pv_explicit'])}；终值现值 {fmt_number(r['pv_terminal'])}"
               f"（占企业价值 {pct(r['terminal_share'], 0, sign=False)}）；企业价值 {fmt_number(r['enterprise_value'])}；"
               f"股权价值 {fmt_number(r['equity_value'])}")
        if r["per_share"] is not None:
            out += f"；每股价值 {fmt_number(r['per_share'])}"
        return out + f"。假设：增长 {pct(growth, 1, sign=False)} × {years} 年，折现率 {pct(discount_rate, 1, sign=False)}，永续增长 {pct(terminal_growth, 1, sign=False)}"

    def reverse_dcf(self, market_value, fcf, discount_rate, terminal_growth, years=5, net_cash=0.0) -> str:
        g = implied_growth(float(market_value), float(fcf), float(discount_rate), float(terminal_growth), int(years), float(net_cash))
        if g is None:
            return "当前市值对应的隐含增长率超出 -50% ~ 300% 的范围，反向 DCF 不适用于这组参数"
        return (f"当前市值隐含未来 {years} 年年均增长 {pct(g, 1, sign=False)}"
                f"（折现率 {pct(discount_rate, 1, sign=False)}，永续增长 {pct(terminal_growth, 1, sign=False)}，净现金 {fmt_number(net_cash)}）")

    def valuation_matrix(self, driver_label, driver_values, multiple_label, multiples, shares, net_cash=0.0, fx=1.0,
                         current_price=None) -> str:
        return matrix_table(driver_label, [float(x) for x in driver_values], multiple_label, [float(x) for x in multiples],
                            float(shares), float(net_cash), float(fx), float(current_price) if current_price else None)

    def _docs(self) -> list[dict]:
        return self.app.db.documents(self.company.id)

    def _doc_pages(self, doc: dict) -> list[tuple[int, str, str]]:
        if doc["id"] not in self._pages:
            self._pages[doc["id"]] = [(p, t, to_simplified(t)) for p, t in split_pages(read_document_text(doc))]
        return self._pages[doc["id"]]

    def list_filings(self) -> str:
        docs = self._docs()
        if not docs:
            return "没有已入库资料"
        return "\n".join(f"#{d['id']}｜{DOC_TYPES.get(d['doc_type'], d['doc_type'])} {d['period'] or ''}｜{d['title']}｜"
                         f"{d['pages'] or '-'} 页｜{d['chars']:,} 字｜来源 {d['source']}" for d in docs)

    def search_filings(self, query: str, doc_id: int | None = None, top_k: int = 5) -> str:
        terms = [t for t in re.split(r"[\s,，;；、]+", query) if t]
        if not terms:
            return "错误：query 为空"
        keys = [to_simplified(t) for t in terms]
        pages = [(d, page, body, simple) for d in self._docs() if not doc_id or d["id"] == int(doc_id)
                 for page, body, simple in self._doc_pages(d)]
        # IDF 加权 + 词频饱和：越少见的关键词越重要（「东岳硅材」比「权益」更能定位），常见词出现再多次也有上限；全部命中再加分
        df = [sum(1 for *_, simple in pages if k in simple) for k in keys]
        idf = [math.log((len(pages) + 1) / (n + 1)) + 1 for n in df]
        hits = []
        for d, page, body, simple in pages:
            counts = [simple.count(k) for k in keys]
            if not any(counts):
                continue
            score = sum(w * c * 2.2 / (c + 1.2) for w, c in zip(idf, counts) if c)  # BM25 式词频饱和
            if all(counts):
                score *= 1.5
            hits.append((score, d, page, body, simple))
        if not hits:
            return f"没有找到与「{query}」相关的内容（资料：{len(self._docs())} 份）"
        hits.sort(key=lambda h: -h[0])
        out = []
        for _, d, page, body, simple in hits[: max(1, min(int(top_k), 10))]:
            # 片段围绕页内最罕见的命中词截取
            rare = max((k for k in keys if k in simple), key=lambda k: idf[keys.index(k)], default=keys[0])
            pos = max(0, simple.find(rare))
            src = body if len(body) == len(simple) else simple  # 简繁转换一般逐字对应，位置可以直接用于原文
            snippet = re.sub(r"\s+", " ", src[max(0, pos - 150) : pos + 450]).strip()
            out.append(f"〔#{d['id']} {d['title']} 第{page}页〕{snippet}")
        return "\n\n".join(out)

    def read_filing(self, doc_id: int, start_page: int, end_page: int | None = None) -> str:
        doc = next((d for d in self._docs() if d["id"] == int(doc_id)), None)
        if doc is None:
            return f"错误：本公司没有编号为 {doc_id} 的资料，先用 list_filings 查看"
        end = min(int(end_page or start_page), int(start_page) + 3)
        pages = [(p, t) for p, t, _ in self._doc_pages(doc) if int(start_page) <= p <= end]
        if not pages:
            return f"错误：该资料没有第 {start_page} 页"
        return "\n\n".join(f"〔#{doc['id']} {doc['title']} 第{p}页〕\n{t.strip()}" for p, t in pages)

    def market_data(self, ticker: str) -> str:
        out = []
        for secid, label, cur in resolve_ticker(self.app, ticker):
            s = technical.snapshot(ensure_bars(self.app, secid))
            if s is None:
                out.append(f"{label}（{secid}）：没有取到行情")
                continue
            q = latest_quote(self.app, secid)
            name = q.get("name") or label
            line = (f"{name}（{label}，{secid}）{s['date']} 收 {num(s['close'])} {cur}；1日 {pct(s['ret_1d'])}，5日 {pct(s['ret_5d'])}，"
                    f"20日 {pct(s['ret_20d'])}，60日 {pct(s['ret_60d'])}；趋势 {s['trend']}，RSI14 {num(s['rsi14'], 0)}，"
                    f"ATR {pct(s['atr_pct'], sign=False)}，20日年化波动 {pct(s['vol20'], sign=False)}；"
                    f"250日高/低 {num(s['high_250'])}/{num(s['low_250'])}（距高点 {pct(s['from_high'])}）")
            vals = [f"{k} {num(q.get(f), d)}" for k, f, d in (("PE(TTM)", "pe_ttm", 1), ("PE(静)", "pe_static", 1), ("PB", "pb", 2))
                    if q.get(f) is not None]
            if q.get("market_cap"):
                vals.append(f"总市值 {big_num(q['market_cap'])}")
            if vals:
                line += "；" + "，".join(vals)
            out.append(line)
        c = self.app.watchlist.get(ticker)
        if c is not None and (ah := self.app.ah_premium(c)):
            out.append(f"AH 溢价（A 相对 H，已按汇率换算）：{pct(ah['current'])}，20日均值 {pct(ah['avg_20'])}，"
                       f"60日区间 {pct(ah['min_60'])} ~ {pct(ah['max_60'])}")
        return "\n".join(out)

    def financials(self, ticker: str | None = None, periods: int = 8) -> str:
        out = []
        for l in listings_for(self.app, self.company, ticker):
            rows = emdata.financials(self.app.session, l, max(2, min(int(periods), 16)))
            if not rows:
                out.append(f"{l.display}：没有取到结构化财务数据")
                continue
            money = emdata.money_fields(l.market)
            cols = [k for k in rows[0] if not k.startswith("_") and k != "币种"]
            cur = rows[0].get("币种") or ""

            def cell(k, v, money=money):
                if v is None:
                    return "—"
                if k in money:
                    return f"{v / 1e8:,.2f}"
                return f"{v:,.2f}" if isinstance(v, float) else str(v)

            table = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            table += ["| " + " | ".join(cell(k, r.get(k)) for k in cols) + " |" for r in rows]
            note = "（港股报告币种由数据推断）" if l.market == "HK" else ""
            out.append(f"{l.display} 主要财务指标（东方财富数据中心；金额单位：亿 {cur}{note}；报告期倒序；"
                       f"港股半年报为半年累计、A 股季报为年初至今累计）：\n" + "\n".join(table))
        return "\n\n".join(out)

    def broker_research(self, ticker: str | None = None, months: int = 6) -> str:
        ls = listings_for(self.app, self.company, ticker)
        out = [hk_consensus(self.app, l) for l in ls if l.market == "HK"]
        a = [l for l in ls if l.market == "A"]
        if a:
            out.append(self._a_research(a[0], months))
        return "\n\n".join(out) or "只支持 A 股和港股"

    def fcf_yield(self, ticker: str | None = None, growth: float = 0.0, forward_fcf: float | None = None,
                  forward_label: str = "") -> str:
        return fcf_snapshot(self.app, self.company, ticker, growth, forward_fcf, forward_label)

    def _a_research(self, l: Listing, months: int) -> str:
        rows = emdata.research_reports(self.app.session, l.code, max(1, min(int(months), 24)))
        if not rows:
            return f"{l.display}：近 {months} 个月没有收录研报"
        y = date.today().year
        med = []
        for i in range(3):
            vals = [r["eps"][i] for r in rows if r["eps"][i] is not None]
            med.append(f"{y + i} 年 EPS 中位数 {statistics.median(vals):.2f}（{len(vals)} 家）" if vals else f"{y + i} 年无预测")
        ratings = Counter(r["rating"] or "未评级" for r in rows)
        targets = [r["target"] for r in rows if r["target"]]
        head = (f"{l.display} 近 {months} 个月研报 {len(rows)} 篇，机构 {len({r['org'] for r in rows})} 家；评级分布："
                + "，".join(f"{k} {v}" for k, v in ratings.most_common()) + "；" + "；".join(med)
                + (f"；目标价中位数 {statistics.median(targets):.2f}" if targets else "；未提供目标价"))
        lines = [f"{r['date']}｜{r['org']}｜{r['rating'] or '—'}｜EPS {'/'.join(f'{e:.2f}' if e is not None else '—' for e in r['eps'])}｜{r['title']}"
                 for r in rows[:15]]
        return head + "\n（EPS 依次为今年/明年/后年）\n" + "\n".join(lines)

    def valuation_history(self, ticker: str | None = None, years: int = 3) -> str:
        years = max(1, min(int(years), 8))
        out = []
        for l in listings_for(self.app, self.company, ticker):
            if l.market == "A":
                rows = emdata.valuation_history_a(self.app.session, l.code, days=years * 250)
                pe = [v for _, v, _ in rows if v is not None and v > 0]
                pb = [v for _, _, v in rows if v is not None and v > 0]
                cur_pe = rows[-1][1] if rows else None
                cur_pb = rows[-1][2] if rows else None
                src = "东方财富估值历史"
            else:
                pe, pb, cur_pe, cur_pb = self._hk_valuation_series(l, years)
                src = "日收盘价 ÷ 最近已披露 EPS(TTM)/每股净资产（半年报滞后 60 天、年报滞后 90 天视为可得；已按汇率换算）"
            cur_pe = cur_pe if cur_pe is not None and cur_pe > 0 else None
            out.append(f"{l.display} 近 {years} 年估值（{src}）：\n  " + _stats_line("PE(TTM)", pe, cur_pe)
                       + ("（当前亏损，PE 不适用）" if cur_pe is None else "") + "\n  " + _stats_line("PB", pb, cur_pb))
        return "\n".join(out)

    def _hk_valuation_series(self, l: Listing, years: int):
        reports = emdata.financials(self.app.session, l, periods=years * 2 + 3)
        if not reports:
            return [], [], None, None
        secid = eastmoney.secid_for(l)
        bars = ensure_bars(self.app, secid, min_bars=years * 250)
        if bars is None:
            return [], [], None, None
        fx = ensure_bars(self.app, FX_HKDCNH, min_bars=years * 250 + 30)
        fx_map = dict(zip(fx["date"], fx["close"])) if fx is not None else {}
        points = []  # (可得日期, EPS_TTM, BPS, 币种)
        for r in reports:
            d = date.fromisoformat(r["_report_date"])
            lag = 90 if d.month == 12 else 60
            points.append(((d + timedelta(days=lag)).isoformat(), r.get("EPS(TTM)"), r.get("每股净资产"), r.get("币种")))
        points.sort()
        pe, pb, last_fx = [], [], None
        cur_pe = cur_pb = None
        for d, close in zip(bars["date"], bars["close"]):
            last_fx = fx_map.get(d, last_fx)
            known = [p for p in points if p[0] <= d]
            if not known:
                continue
            _, eps, bps, cur = known[-1]
            rate = 1.0
            if cur == "CNY":  # 报表为人民币、股价为港元：人民币金额 ÷ (港元兑人民币汇率)
                if not last_fx:
                    continue
                rate = 1 / last_fx
            elif cur == "USD":  # 港元联系汇率
                rate = 7.8
            cur_pe = close / (eps * rate) if eps and eps > 0 else None
            cur_pb = close / (bps * rate) if bps and bps > 0 else None
            if cur_pe:
                pe.append(cur_pe)
            if cur_pb:
                pb.append(cur_pb)
        return pe, pb, cur_pe, cur_pb

    def southbound(self, ticker: str | None = None) -> str:
        hk = [l for l in listings_for(self.app, self.company, ticker) if l.market == "HK"]
        if not hk:
            return "南向资金数据只适用于港股"
        out = []
        for l in hk:
            s = emdata.southbound_summary(emdata.southbound_holdings(self.app.session, l.code))
            if not s:
                out.append(f"{l.display}：没有港股通持股数据（可能不在港股通名单内）")
                continue
            parts = [f"{l.display} 港股通持股（{s['date']}）：{s['shares'] / 1e4:,.0f} 万股，占已发行股份 {s['ratio']:.2f}%"]
            for n in (5, 20, 60):
                if f"chg_shares_{n}" in s:
                    parts.append(f"近 {n} 个交易日 {s[f'chg_shares_{n}'] / 1e4:+,.0f} 万股（{s[f'chg_ratio_{n}']:+.2f} 个百分点）")
            out.append("；".join(parts))
        return "\n".join(out)

    def sotp(self, parts, shares, net_cash=0.0, fx=1.0, holdco_discount=0.0, current_price=None) -> str:
        rows, equity, per_share = sotp_value(parts, float(shares), float(net_cash), float(fx), float(holdco_discount))
        table = ["| 部分 | 100%价值 | 持股 | 折价 | 归属价值 | 依据 |", "|---|---|---|---|---|---|"]
        table += [f"| {r['name']} | {fmt_number(float(r['value']))} | {r['stake']:.1%} | {r['discount']:.0%} | "
                  f"{fmt_number(r['attributable'])} | {r.get('method', '')} |" for r in rows]
        tail = (f"\n净现金 {fmt_number(float(net_cash))}；控股公司折价 {float(holdco_discount):.0%}；股权价值 {fmt_number(equity)}；"
                f"股本 {fmt_number(float(shares))}；汇率 {fmt_number(float(fx))} → 每股价值 {per_share:,.2f}")
        if current_price:
            tail += f"（相对现价 {float(current_price):,.2f}：{pct(per_share / float(current_price) - 1, 0)}）"
        return "\n".join(table) + tail

    def ddm(self, dividend, discount_rate, growth, high_growth=None, high_years=0, current_price=None) -> str:
        v = ddm_value(float(dividend), float(discount_rate), float(growth),
                      float(high_growth) if high_growth is not None else None, int(high_years))
        out = (f"每股价值 {v:,.2f}（最近股息 {float(dividend):,.4f}，折现率 {pct(discount_rate, 1, sign=False)}，"
               f"前 {int(high_years)} 年增长 {pct(high_growth if high_growth is not None else growth, 1, sign=False)}，"
               f"永续增长 {pct(growth, 1, sign=False)}）")
        if current_price:
            p = float(current_price)
            out += f"；相对现价 {p:,.2f}：{pct(v / p - 1, 0)}，当前股息率 {float(dividend) / p:.2%}"
        return out

    def company_events(self, days: int = 30, min_materiality: int = 3) -> str:
        since = iso(utcnow() - timedelta(days=int(days)))
        rows = self.app.db.items_since(self.company.id, since, min_materiality=int(min_materiality))
        if not rows:
            untriaged = [r for r in self.app.db.items_since(self.company.id, since) if r["triaged_at"] is None]
            if untriaged:
                return "近期信息尚未分级，以下为原始标题：\n" + "\n".join(
                    f"{fmt_local(r['published_at'])}｜{SOURCE_LABEL.get(r['source'], r['source'])}｜{r['title']}｜{r['url'] or ''}"
                    for r in untriaged[:25])
            return f"{days} 天内没有重要性≥{min_materiality} 的事件"
        return "\n".join(
            f"{fmt_local(r['published_at'])}｜重要性{r['materiality']}｜{DIRECTIONS.get(r['direction'], '')}｜{event_label(r)}｜"
            f"{r['summary_zh'] or r['title']}｜{SOURCE_LABEL.get(r['source'], r['source'])}｜{r['url'] or ''}"
            for r in rows[:40])

    def manual_data(self) -> str:
        rows = [("本公司", m) for m in self.app.db.manual_data(self.company.id)] + [("行业/宏观", m) for m in self.app.db.manual_data("")]
        if not rows:
            return "没有手动补充的数据"
        return "\n".join(f"{scope}｜{m['metric']}｜{m['period']}｜{m['value']}{m['unit'] or ''}｜{m['note'] or ''}｜{m['source'] or ''}"
                         for scope, m in rows)

    def web_search(self, query: str, max_results: int = 8) -> str:
        res, source = web.search(self.app.session, query, max(1, min(int(max_results), 10)), self.web_providers)
        if not res:
            return (f"没有可用的搜索结果（{source}）。如果是搜索源被拦截或无关，这是搜索服务的问题，不代表信息不存在："
                    "可以换检索式重试；仍然查不到的，写明「搜索不可用，未能核实」并列入需要使用者补充的数据。")
        return f"（搜索源：{web.LABELS.get(source, source)}）\n" + "\n".join(
            f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet'][:500]}" for i, r in enumerate(res, 1))

    def fetch_url(self, url: str, max_chars: int = 4000) -> str:
        return web.fetch_text(self.app.session, url, max(500, min(int(max_chars), self.max_chars)))

    def ima_search(self, query: str, max_results: int = 20) -> str:
        terms = [to_simplified(t).lower() for t in query.split() if t]
        if not terms:
            return "错误：query 为空"
        hits, seen = [], set()
        for kb in imafeed.knowledge_bases(self.app):
            for x in ima.search(self.app.session, kb["id"], query.split()[0]):
                key = re.sub(r"\W+", "", x["title"])
                if x["media_type"] == ima.FOLDER or key in seen or not all(t in to_simplified(x["title"]).lower() for t in terms[1:]):
                    continue
                seen.add(key)
                hits.append({**x, "date": ima.parse_date(x["title"])})
        if not hits:
            return f"ima 知识库中没有标题包含「{query}」的条目（只按标题匹配，可换公司简称、代码或英文名再试）"
        hits.sort(key=lambda x: x["date"] or date.min, reverse=True)
        lines = []
        for x in hits[: max(1, min(int(max_results), 50))]:
            ref = f"I{len(self._ima) + 1}"
            self._ima[ref] = x
            lines.append(f"{ref}｜{x['date'] or '日期不详'}｜{ima.MEDIA_TYPES.get(x['media_type'], x['media_type'])}｜{x['title']}")
        return f"共 {len(hits)} 条（显示 {len(lines)} 条，按日期倒序）：\n" + "\n".join(lines)

    def ima_read(self, ref: str) -> str:
        x = self._ima.get(ref.strip().upper())
        if x is None:
            return f"错误：没有编号 {ref}，先用 ima_search 检索"
        doc, text = imafeed.read_entry(self.app, self.company, x)
        if doc is None:
            return f"图片 OCR 文字（{x['title']}，可能有错字）：\n{text.strip()}"
        self._pages.pop(doc["id"], None)
        return (f"已入库为本公司资料 #{doc['id']}《{doc['title']}》（{doc['pages'] or '-'} 页，{doc['chars']:,} 字）。"
                f"用 search_filings（doc_id={doc['id']}）检索、read_filing 读取指定页。开头内容：\n{text[:2500]}")


def tool_log_markdown(log: list[dict], limit: int = 60) -> str:
    """把工具调用记录整理成报告附录。"""
    lines = []
    for i, x in enumerate(log[:limit], 1):
        args = json.dumps(x["args"], ensure_ascii=False)
        if len(args) > 160:
            args = args[:160] + "…"
        first = x["result"].strip().splitlines()[0] if x["result"].strip() else ""
        lines.append(f"{i}. `{x['tool']}` {args} → {first[:120]}")
    if len(log) > limit:
        lines.append(f"……共 {len(log)} 次调用")
    return "\n".join(lines)
