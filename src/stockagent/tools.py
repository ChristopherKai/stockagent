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

from . import fcf as fcf_mod
from . import imafeed, technical, twitterfeed
from .app import FX_HKDCNH, FX_USDCNH, PRICE_SOURCES, SOURCE_LABEL, App, _is_down, _trip, _try, ccass_snapshot, klines
from .config import Company, Listing
from .docs import DOC_TYPES, read_document_text
from .sources import ccass, eastmoney, emdata, ids, ima, nasdaq, sec, tencent, twitter, web
from .triage import DIRECTIONS, event_label
from .utils import CST, big_num, fmt_local, iso, lock_holder, log, num, pct, to_simplified, today_local, utcnow



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


MATRIX_BASES = ("equity", "ev")


def infer_basis(multiple_label: str) -> str:
    """倍数口径：名称里有 EV / 企业价值的是 EV 倍数（倍数 × 驱动 = 企业价值，要加净现金），其余（PE、PB、PS）是股权倍数。"""
    return "ev" if re.search(r"EV|企业价值", multiple_label or "", re.I) else "equity"


def matrix_price(driver: float, multiple: float, shares: float, net_cash: float = 0.0, fx: float = 1.0,
                 basis: str = "ev") -> float:
    """敏感性矩阵的一个格点。EV 口径：隐含股价 =（倍数 × 驱动指标 + 净现金）÷ 股本 × 汇率；
    股权口径（PE、PB 等）：倍数 × 驱动指标已是股权价值，不再加减净现金。估值模型（valuation.py）用同一公式。"""
    return (multiple * driver + (net_cash if basis == "ev" else 0.0)) / shares * fx


def matrix_formula(driver_label: str, basis: str, net_cash: float, shares: float, fx: float) -> str:
    if basis == "ev":
        return (f"隐含股价 =（倍数 × {driver_label} + 净现金 {fmt_number(net_cash)}）÷ 股本 {fmt_number(shares)} × 汇率 {fmt_number(fx)}"
                "（EV 口径：倍数 × 驱动指标 = 企业价值，加净现金得股权价值）")
    return (f"隐含股价 = 倍数 × {driver_label} ÷ 股本 {fmt_number(shares)} × 汇率 {fmt_number(fx)}"
            "（股权口径：PE、PB 等倍数 × 驱动指标已是股权价值，净负债已体现在倍数里，不再加减净现金"
            + (f"；传入的净现金 {fmt_number(net_cash)} 未计入" if net_cash else "") + "）")


def matrix_table(driver_label: str, driver_values: list[float], multiple_label: str, multiples: list[float],
                 shares: float, net_cash: float = 0.0, fx: float = 1.0, current_price: float | None = None,
                 basis: str = "ev") -> str:
    if shares <= 0:
        raise ValueError("shares 必须为正")
    head = f"| {driver_label} ↓ / {multiple_label} → | " + " | ".join(fmt_number(m) + "×" for m in multiples) + " |"
    rows = [head, "|---|" + "---|" * len(multiples)]
    for d in driver_values:
        cells = []
        for m in multiples:
            price = matrix_price(d, m, shares, net_cash, fx, basis)
            cell = f"{price:,.2f}"
            if current_price:
                cell += f"（{pct(price / current_price - 1, 0)}）"
            cells.append(cell)
        rows.append(f"| {fmt_number(d)} | " + " | ".join(cells) + " |")
    note = matrix_formula(driver_label, basis, net_cash, shares, fx)
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


def listings_for(app: App, company: Company | None, ticker: str | None, markets: tuple[str, ...] = ("A", "HK")) -> list[Listing]:
    """结构化数据默认只支持 A 股与港股（markets 可加 "US"）：不填 ticker 表示本公司（不针对某家公司的 agent 必须填 ticker）。"""
    if not ticker:
        if company is None:
            raise ValueError("需要指定 ticker（A 股 6 位、港股 5 位代码" + ("、美股字母代码" if "US" in markets else "") + "）")
        return [l for l in company.listings if l.market in markets]
    c = app.watchlist.get(ticker)
    if c is not None:
        return [l for l in c.listings if l.market in markets]
    t = ticker.strip().upper()
    if m := re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", t):
        return [Listing(market="A", code=m.group(1), exchange=m.group(2))]
    if m := re.fullmatch(r"(\d{1,5})(?:\.HK)?", t):
        return [Listing(market="HK", code=m.group(1))]
    if "US" in markets and re.fullmatch(r"[A-Z][A-Z.\-]{0,7}", t):
        return [Listing(market="US", code=t)]
    raise ValueError(f"{ticker}：结构化财务与估值数据只支持 A 股（6 位）和港股（5 位）代码"
                     + ("，以及美股字母代码" if "US" in markets else ""))


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
    n = fcf_mod.payback_years(ev, fcf, growth, cap)
    if n == 0:
        return "EV 已为负"
    if n is None:
        return "FCF 为负，不适用"
    return f"约 {n} 年" if n <= cap else f"超过 {cap} 年"


def _cny_per(app: App, ccy: str) -> float | None:
    """1 单位 ccy 折合多少人民币（最新收盘；库里没有就抓取）。"""
    if ccy == "CNY":
        return 1.0
    secid = {"HKD": FX_HKDCNH, "USD": FX_USDCNH}.get(ccy)
    bars = ensure_bars(app, secid) if secid else None
    return float(bars["close"].iloc[-1]) if bars is not None and not bars.empty else None


def convert(app: App, amount: float, from_ccy: str, to_ccy: str) -> tuple[float, str] | None:
    """按最新汇率换算金额，返回 (换算后金额, 说明)；缺汇率时返回 None。"""
    if from_ccy == to_ccy:
        return amount, ""
    a, b = _cny_per(app, from_ccy), _cny_per(app, to_ccy)
    if not a or not b:
        return None
    return amount * a / b, f"按 1 {from_ccy} = {a / b:.4f} {to_ccy} 换算"


def fcf_snapshot(app: App, company: Company, ticker: str | None = None, growth: float = 0.0,
                 forward_fcf: float | None = None, forward_label: str = "") -> str:
    """EV、自由现金流与 FCF Yield（代码计算）。报表优先用 A 股（A+H 公司），市值按定价上市地的股价；
    多地上市的公司另列其他上市地股价下的 EV 与 FCF Yield（各地股价不同，FCF Yield 也不同）。"""
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
    target = company if not ticker else app.watchlist.get(ticker)
    priced = ([target.pricing_listing] + [x for x in target.listings if x.key != target.pricing_listing.key]
              if target is not None else [l])
    caps = []  # (上市地, 报告币种市值, 说明)
    for i, pl in enumerate(priced):
        secid = eastmoney.secid_for(pl) if pl.market != "US" else app.secid(pl)
        q = latest_quote(app, secid) if i == 0 else (app.db.quote(secid) or {})  # 其他上市地只用库里的快照（stock run 每天更新）
        if not q.get("market_cap"):
            continue
        conv = convert(app, q["market_cap"], pl.currency, cur)
        if conv is None:
            continue
        caps.append((pl, conv[0], f"{pl.display} 总市值，最新价 {num(q.get('price'))} {pl.currency}" + (f"，{conv[1]}" if conv[1] else "")))
    if not caps:
        return f"{l.display}：没有取到市值（或缺少换算汇率）"
    main, mcap, note = caps[0]
    b = data["balance"]
    m = fcf_mod.metrics(data, mcap)
    ev, flows, avg = m["ev"], m["flows"], m["fcf_avg3"]
    yi = 1e8

    def f(v: float) -> str:
        return f"{v / yi:,.2f}"

    lines = [f"{l.display} EV 与自由现金流（程序按东方财富现金流量表/资产负债表计算；金额：亿 {cur}；资产负债表截至 {b['date']}"
             + (f"；市值按定价上市地 {main.display}" if len(caps) > 1 or main.key != l.key else "") + "）",
             f"- 市值 {f(mcap)}（{note}）",
             f"- 现金及存款 {f(b['cash'])}"
             + (f"（{'、'.join(f'{k} {f(v)}' for k, v in b['cash_items'].items())}）" if len(b.get("cash_items") or {}) > 1 else "")
             + f"，短期投资 {f(b['invest'])}"
             + (f"；受限制现金 {f(b['restricted'])} 未计入（多为应付票据等的保证金）" if b.get("restricted") else "")
             + f"；有息负债 {f(b['debt'])}"
             + (f"（{'、'.join(f'{k} {f(v)}' for k, v in b['debt_items'].items())}）" if b["debt_items"] else "")
             + f"；少数股东权益 {f(b['minority'])}",
             f"- EV = 市值 + 有息负债 + 少数股东权益 − 现金及短期投资 = {f(ev)}",
             f"- 净现金 {f(m['net_cash'])}，占市值 {pct(m['net_cash_ratio'], sign=False)}；Negative EV（净现金超过市值）：{'是' if m['negative_ev'] else '否'}",
             "", "| 期间 | 经营现金流 | 资本开支 | FCF | FCF Yield（FCF ÷ EV） |", "|---|---|---|---|---|"]
    for x in flows:
        lines.append(f"| {x['label']} | {f(x['ocf'])} | {f(x['capex'])} | {f(x['fcf'])} | "
                     + (f"{pct(x['fcf'] / ev, sign=False)} |" if ev > 0 else "EV 为负 |"))
    if avg is not None:
        lines.append(f"| 近 3 个年度平均 | | | {f(avg)} | " + (f"{pct(avg / ev, sign=False)} |" if ev > 0 else "EV 为负 |"))
    base = m["fcf_base"] or 0
    if m["capex_proxy"]:
        lines.append(f"- 注意：{'、'.join(m['capex_proxy'])} 的资本开支科目缺失或归类异常（标准化报表里接近 0），"
                     "上表按投资活动现金净流出估算资本开支（偏保守），请用年报现金流量表核对")
    for other, cap2, note2 in caps[1:]:
        m2 = fcf_mod.metrics(data, cap2)
        lines.append(f"- 按 {other.display} 股价：市值 {f(cap2)}（{note2}），EV {f(m2['ev'])}，"
                     + (f"{flows[0]['label']} FCF Yield {pct(m2['fcf_base'] / m2['ev'], sign=False)}"
                        if flows and m2["ev"] > 0 and m2["fcf_base"] is not None else "FCF Yield 不适用"))
    lines.append("")
    lines.append(f"- FCF 累计覆盖 EV 的年数（FCF 年增速假设 {pct(growth)}）：按 {flows[0]['label'] if flows else '—'} "
                 f"{_payback_years(ev, base, growth)}" + (f"；按 3 年平均 {_payback_years(ev, avg, growth)}" if avg is not None else ""))
    if forward_fcf is not None:
        fwd = float(forward_fcf) * yi
        lines.append(f"- 前瞻 FCF Yield（{forward_label or '前瞻年度'} FCF {forward_fcf:g} 亿）：" + (pct(fwd / ev, sign=False) if ev > 0 else "EV 为负"))
    if (ev2 := m["ev_conservative"]) is not None:
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


RATING_ZH = {"Strong Buy": "强烈买入", "Buy": "买入", "Hold": "持有", "Sell": "卖出", "Strong Sell": "强烈卖出"}


def us_consensus(app: App, listing: Listing) -> str:
    """美股卖方一致预期：Nasdaq 的目标价、评级分布与年度 EPS 预测。"""
    c = _try(app, "nasdaq", nasdaq.consensus, app.session, listing.code)
    if not c:
        return f"{listing.display}：Nasdaq 没有分析师一致预期数据"
    t, r = c["target"], c["ratings"]
    line = (f"{listing.display} 卖方一致预期（Nasdaq 分析师数据）：评级 买入 {r['buy']} / 持有 {r['hold']} / 卖出 {r['sell']}"
            + (f"（综合评级 {RATING_ZH.get(c['mean_rating'], c['mean_rating'])}"
               + (f"，Nasdaq 列出的覆盖机构 {c['brokers']} 家" if c["brokers"] else "") + "）" if c["mean_rating"] else "")
            + (f"；目标价均值 {num(t['avg'])} 美元，最高 {num(t['high'])}，最低 {num(t['low'])}" if t["avg"] else ""))
    price = latest_quote(app, app.secid(listing)).get("price")
    if price and t["avg"]:
        line += f"；相对现价 {num(price)} 美元，均值 {pct(t['avg'] / price - 1)}"
    if c["eps"]:
        line += "\n年度 EPS 一致预期（每股或每 ADS，美元）：\n| 财年 | 均值 | 最高 | 最低 | 家数 | 近 4 周上调/下调 |\n|---|---|---|---|---|---|\n"
        line += "\n".join(f"| {e['year']} | {num(e['avg'])} | {num(e['high'])} | {num(e['low'])} | {e['n'] or '—'} | {e['up'] or 0}/{e['down'] or 0} |"
                           for e in c["eps"])
    return line


def _cell(label: str, v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}" if "每股" in label else f"{v / 1e8:,.2f}"


def _us_stale_note(app: App, listing: Listing, cik: int, ua: str, data: dict, kind: str, periods: int,
                   annual_only: bool) -> str | dict | None:
    """XBRL 最新一期落后于已申报的 10-K / 10-Q（companyfacts 比申报晚更新）：先绕过缓存重取一次，
    仍落后就返回提示（说明最新一期要看申报文件原文），追上了返回新的报表数据；检查失败不影响报表本身。"""
    try:
        now = utcnow()
        filed = sec.reports(app.session, cik, ua, now - timedelta(days=200), now)
        newest = max((x["period_end"] for x in filed if not annual_only or x["doc_type"] == "annual_report"), default=None)
        if newest is None or newest <= data["periods"][0]["date"]:
            return None
        fresh = sec.statement_from_facts(sec.company_facts(app.session, cik, ua, fresh=True), kind, periods, annual_only)
        if fresh and fresh["rows"] and fresh["periods"][0]["date"] >= newest:
            return fresh
        return (f"XBRL 数据最新只到 {data['periods'][0]['date']}，但 SEC 已有截至 {newest} 的"
                f"{'年报' if annual_only else '10-K/10-Q'}（结构化数据尚未更新，该期数字请查申报原文 documents）")
    except Exception as e:  # 校验是附加提示，失败只记日志
        log.warning("%s XBRL 新鲜度检查失败：%s", listing.display, e)
        return None


def statement_table(app: App, listing: Listing, kind: str, periods: int, annual_only: bool) -> str:
    """一张报表的 Markdown 表：科目为行、期间为列（金额单位亿，每股指标为原值）。"""
    name = emdata.STATEMENT_KINDS[kind]
    if listing.market == "US":
        ua = app.settings.sources.sec_user_agent
        if not ua:
            return f"{listing.display}：美股报表来自 SEC XBRL，需要先在 config.yaml 设置 sources.sec_user_agent（「姓名 邮箱」）"
        cik = ids.sec_cik(app.db, app.session, listing, ua)
        if not cik:
            return f"{listing.display}：SEC 找不到这个代码"
        data = sec.statement_from_facts(sec.company_facts(app.session, cik, ua), kind, periods, annual_only)
        src, cur = "SEC XBRL（10-K/10-Q/20-F 申报数据）", (data or {}).get("currency", "")
        if data and data["rows"] and (stale := _us_stale_note(app, listing, cik, ua, data, kind, periods, annual_only)):
            if isinstance(stale, dict):  # 重新取过 companyfacts（绕过进程内缓存）后追上了最新申报
                data = stale
            else:
                src += f"；⚠ {stale}"
    else:
        data = emdata.statement(app.session, listing, kind, periods, annual_only)
        src = "东方财富数据中心"
        cur = "CNY"
        if listing.market == "HK":
            rows = emdata.financials(app.session, listing, 2)
            cur = (rows[0].get("币种") if rows else None) or "CNY"
            src += "（港股报告币种由数据推断）"
    if not data or not data["rows"]:
        return f"{listing.display}：没有取到{name}" + ("（A 股银行、保险、券商的报表格式不同，这里只整理一般工商业格式）" if listing.market == "A" else "")
    cols = [p["label"] for p in data["periods"]]
    lines = [f"{listing.display} {name}（{src}；金额单位：亿 {cur}，每股指标为原值；期间倒序"
             + ("；流量为年初至今累计" if kind != "balance" and listing.market != "US" else "") + "）：",
             "| 科目 | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    lines += [f"| {label} | " + " | ".join(_cell(label, v) for v in values) + " |" for label, values in data["rows"]]
    return "\n".join(lines)


def _holder_change(text: str) -> str:
    """东方财富的「较上期」字段：不变 / 新进 / 股数变化（如 -10186873）。"""
    try:
        return f"{float(text) / 1e4:+,.0f} 万股"
    except (TypeError, ValueError):
        return text or "—"


def _a_holders_text(app: App, l: Listing) -> str:
    out = [f"{l.display} 股东（东方财富数据中心）"]
    top: set[str] = set()
    for free, title in ((False, "十大股东"), (True, "十大流通股东")):
        rows = emdata.a_holders(app.session, l.code, free=free)
        if not rows:
            out.append(f"{title}：没有数据")
            continue
        dates = sorted({r["date"] for r in rows}, reverse=True)
        latest = [r for r in rows if r["date"] == dates[0]]
        prev = {r["name"] for r in rows if len(dates) > 1 and r["date"] == dates[1]}
        gone = [n for n in prev if n not in {r["name"] for r in latest}]
        if free:  # 十大流通股东多与十大股东重合，只列不同的
            extra = [r for r in latest if r["name"] not in top]
            out.append(f"\n{title}（{dates[0]}）：与十大股东重合 {len(latest) - len(extra)} 名"
                       + ("；另有 " + "；".join(f"{r['name']}（{r['type'] or '—'}）{(r['shares'] or 0) / 1e4:,.0f} 万股、占流通股 {num(r['ratio'])}%、"
                                              f"较上期 {'新进' if prev and r['name'] not in prev else _holder_change(r['change'])}" for r in extra)
                          if extra else ""))
            if gone:
                out.append("退出十大流通股东：" + "、".join(gone))
            continue
        top = {r["name"] for r in latest}
        out.append(f"\n{title}（{dates[0]}" + (f"，对比 {dates[1]}" if len(dates) > 1 else "") + "）：\n"
                   "| 名次 | 股东 | 持股（万股） | 占总股本% | 较上期 |\n|---|---|---|---|---|")
        out += [f"| {r['rank']} | {r['name']} | {(r['shares'] or 0) / 1e4:,.0f} | {num(r['ratio'])} | "
                f"{'新进' if prev and r['name'] not in prev else _holder_change(r['change'])} |" for r in latest]
        if gone:
            out.append("退出十大股东：" + "、".join(gone))
    counts = emdata.a_holder_counts(app.session, l.code)
    if counts:
        out.append("\n股东户数（户数减少、户均持股上升常见于筹码集中）：\n| 截止日 | 户数 | 较上期% | 户均流通股 |\n|---|---|---|---|")
        out += [f"| {c['date']} | {(c['holders'] or 0):,.0f} | {num(c['change_pct'])} | {(c['avg_shares'] or 0):,.0f} |" for c in counts]
    return "\n".join(out)


def _hk_holders_text(app: App, l: Listing) -> str:
    rows = emdata.hk_holders(app.session, l.code)
    if not rows:
        return f"{l.display}：没有主要股东披露数据"
    lines = [(f"{l.display} 主要股东与董事权益（东方财富数据中心，据 {rows[0]['date']} 期定期报告，{rows[0]['notice']} 披露；"
              "同一批股份常经多个受控法团重复披露，占比不能相加）："),
             "| 名称 | 身份 | 权益性质 | 股份类别 | 持股（万股） | 占已发行% |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['name']} | {r['role'] or '—'} | {r['identity'] or '—'} | {r['share_type'] or '—'} | {(r['shares'] or 0) / 1e4:,.0f} | {num(r['ratio'])} |"
              for r in rows[:15]]
    if len(rows) > 15:
        lines.append(f"（另有 {len(rows) - 15} 条持股更少的披露未列出）")
    return "\n".join(lines)


def ccass_text(app: App, l: Listing) -> str:
    """CCASS 持股分布与 5/20 个交易日（约 7/28 个自然日）前的对比；缺的快照当场向港交所查询并存库。"""
    code = l.code.zfill(5)
    snap = app.db.ccass(code)
    if snap is None or (today_local() - date.fromisoformat(snap["date"])).days > 4:
        snap = ccass_snapshot(app, code, today_local() - timedelta(days=1)) or snap
    if snap is None:
        return f"{l.display}：没有取到 CCASS 持股数据（港交所查询失败或代码不存在）"
    day = date.fromisoformat(snap["date"])
    base = {n: ccass_snapshot(app, code, day - timedelta(days=d)) for n, d in ((5, 7), (20, 28))}
    issued = snap["issued"]
    total = (snap["summary"].get("total") or {}).get("pct")
    head = f"{l.display} CCASS 持股（港交所中央结算系统，{snap['date']} 日终" + (f"；已发行股份 {issued:,} 股" if issued else "") + "）："
    parts = []
    if total is not None:
        parts.append(f"存于中央结算系统的合计占 {total:.2f}%，未存入的 {100 - total:.2f}% 多为控股股东实物持股或未上市股份")
    c5, c10 = ccass.concentration(snap, 5), ccass.concentration(snap, 10)
    if c5 is not None:
        parts.append(f"前 5 名参与者合计 {c5:.2f}%，前 10 名 {c10:.2f}%（港股通两个账户合并计）")
    lines = [head + "；".join(parts), ""]
    if not issued:
        return "\n".join(lines + ["（港交所未给出已发行股数，无法计算占比）"])
    olds = {n: {p["id"]: p["shares"] for p in b["participants"]} if b else None for n, b in base.items()}
    hdr = " | ".join(f"较 {b['date']}" if b else f"较约{n}日前" for n, b in base.items())
    lines += [f"| 参与者 | 持股（万股） | 占已发行% | {hdr} |", "|---|---|---|---|---|"]

    def delta(n: int, p: dict) -> str:
        if olds[n] is None:
            return "无数据"
        return f"{(p['shares'] - olds[n].get(p['id'], 0)) / issued * 100:+.2f} 个百分点"

    for p in snap["participants"][:15]:
        name = ccass.CONNECT_IDS.get(p["id"]) or to_simplified(p["name"])
        lines.append(f"| {name}（{p['id']}） | {p['shares'] / 1e4:,.0f} | {p['shares'] / issued * 100:.2f} | {delta(5, p)} | {delta(20, p)} |")
    if base[20]:
        moves = [m for m in ccass.changes(base[20], snap, 0.5) if m["id"] not in {p["id"] for p in snap["participants"][:15]}]
        if moves:
            lines.append("\n前 15 名以外、20 日内变化 ≥ 0.5 个百分点的参与者：" + "；".join(
                f"{to_simplified(m['name'])} {m['pct']:+.2f} 个百分点" for m in moves[:8]))
    return "\n".join(lines)


def valuation_series(app: App, l: Listing, years: int) -> tuple[list[float], list[float], float | None, float | None]:
    """A 股、港股近 years 年的每日 PE(TTM) 与 PB 序列（只保留正值）及当前值：(PE, PB, 当前 PE, 当前 PB)。"""
    if l.market == "A":
        rows = emdata.valuation_history_a(app.session, l.code, days=years * 250)
        pe = [v for _, v, _ in rows if v is not None and v > 0]
        pb = [v for _, _, v in rows if v is not None and v > 0]
        return pe, pb, (rows[-1][1] if rows else None), (rows[-1][2] if rows else None)
    if l.market == "HK":
        return hk_valuation_series(app, l, years)
    return [], [], None, None


def hk_valuation_series(app: App, l: Listing, years: int):
    """港股：日收盘价 ÷ 最近已披露 EPS(TTM)/每股净资产（半年报滞后 60 天、年报滞后 90 天视为可得；人民币报表按汇率换算）。"""
    reports = emdata.financials(app.session, l, periods=years * 2 + 3)
    if not reports:
        return [], [], None, None
    secid = eastmoney.secid_for(l)
    bars = ensure_bars(app, secid, min_bars=years * 250)
    if bars is None:
        return [], [], None, None
    fx = ensure_bars(app, FX_HKDCNH, min_bars=years * 250 + 30)
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


# ---------------------------------------------------------------- 工具箱

class MetaStr(str):
    """工具返回给模型的文本，另带一份写进工具日志的结构化信息（meta，模型看不到）：
    例如估值矩阵实际计算用的口径与（程序补上行列之后的）全部格点，供估值基线重建时使用。"""

    meta: dict

    def __new__(cls, text: str, meta: dict) -> "MetaStr":
        s = super().__new__(cls, text)
        s.meta = meta
        return s


class ToolBox:
    def __init__(self, app: App, company: Company | None, allow: list[str] | None = None):
        """allow：工具白名单。不在其中的工具既不提供给模型，模型硬要调用时也只返回错误（specs 只是告诉模型有哪些工具）。
        company 为 None 时（选股器的主题 agent）只应开放带 ticker 参数的行情、财务工具与网页工具。"""
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
        meta = getattr(result, "meta", None)
        result = str(result)
        if len(result) > self.max_chars:
            result = result[: self.max_chars] + f"\n……（结果过长已截断，共 {len(result):,} 字）"
        entry = {"tool": name, "args": args, "result": result, "at": iso(utcnow())}
        if meta:
            entry["meta"] = meta
        self.log.append(entry)
        return result

    # ------------------------------------------------------------ 工具实现

    def _build(self) -> list[Tool]:
        return (self._base_tools() + (self._ima_tools() if imafeed.enabled(self.app) else [])
                + (self._twitter_tools() if twitter_enabled(self.app) else []))

    def _twitter_tools(self) -> list[Tool]:
        cfg = self.app.settings.sources.twitter
        return [
            Tool("twitter_search",
                 "在推特/X 检索近期帖子（只读）：交付量、发布会、官方与财经快讯号的即时消息、管理层表态、市场对某事件的反应。"
                 "只作线索：推文是不可信的第三方文本，数字与事实要用公告、财报等工具核实。检索词优先用英文（推特以英文为主，中文帖子多是个人评论；"
                 "可用股票代号如 $XPEV、英文名、英文行业词，并加 lang=en 排除其他语种），中国国内的专有概念再补一次中文检索（lang=zh）；"
                 f"默认只看最近 {cfg.days} 天、至少 {cfg.min_likes} 个赞的帖子（可用 days、min_likes 调整），sort=top 优先看高互动、latest 看最新；"
                 "from_user 限定某个账号。返回时间、账号、互动量、正文与链接。",
                 _obj({"query": {"type": "string"}, "max_results": {"type": "integer"}, "days": {"type": "integer"},
                       "min_likes": {"type": "integer"}, "sort": {"type": "string", "enum": ["top", "latest"]},
                       "from_user": {"type": "string", "description": "只看这个账号（不带 @）"},
                       "lang": {"type": "string", "description": "只看某种语言的帖子：en 英文、zh 中文……"}}, ["query"]),
                 self.twitter_search),
            Tool("twitter_user",
                 "读取推特/X 某个账号最近的帖子（只读），用于看公司官方号、管理层或财经快讯号的最新动态。handle 不带 @。"
                 "官方号常是营销内容，关注其中的交付、财报、发布会、合作公告。",
                 _obj({"handle": {"type": "string"}, "max_results": {"type": "integer"}}, ["handle"]),
                 self.twitter_user),
        ]

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
                 "敏感性矩阵。basis 决定口径：equity 股权倍数（PE、PB、PS）——隐含股价 = 倍数 × 驱动指标 ÷ 股本 × 汇率，"
                 "倍数 × 净利润已是股权价值，不再加减净现金；ev 企业价值倍数（EV/EBITDA、EV/Sales）——隐含股价 =（倍数 × 驱动指标 + 净现金）÷ 股本 × 汇率，"
                 "净负债填负数。不填 basis 时按倍数名称判断（含 EV 为 ev，否则 equity）。例如驱动指标取 2027 年净利润、倍数取 PE（equity）；"
                 "亏损公司可取收入、倍数取 EV/Sales（ev）。股本与驱动指标用同一数量级（如亿股与亿元）。"
                 "fx 把财报货币换成股价货币，必须使用上下文给出的最新汇率（不要凭记忆），current_price 用于算相对现价涨跌。"
                 "程序会自动补上缺少的一致预期净利行与定价上市地历史 PE 分位列（能取到数据时）。",
                 _obj({"driver_label": {"type": "string"}, "driver_values": {"type": "array", "items": {"type": "number"}},
                       "multiple_label": {"type": "string"}, "multiples": {"type": "array", "items": {"type": "number"}},
                       "shares": {"type": "number"}, "net_cash": {"type": "number"}, "fx": {"type": "number"},
                       "current_price": {"type": "number"}, "basis": {"type": "string", "enum": list(MATRIX_BASES)}},
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
            Tool("statements",
                 "完整财务报表（程序整理，原始数字）：kind 取 income 利润表 / balance 资产负债表 / cashflow 现金流量表。"
                 "A 股与港股来自东方财富数据中心（A 股科目按一般工商业格式，港股为数据商的标准化科目），美股来自 SEC XBRL（10-K/10-Q/20-F，需要配置 sec_user_agent）。"
                 "financials 没有的明细用它：费用结构（销售、管理、研发、财务费用）、其他收益与投资收益、减值、少数股东损益、应收票据、合同负债、"
                 "在建工程、有息负债各科目、折旧摊销、分红与回购现金。各列标明截止日与期间长度（利润表与现金流量表为年初至今累计；美股 10-Q 利润表为单季）。"
                 "periods 默认 4 期；annual_only=true 只看年报。A+H 公司用 A 股报表。ticker 不填表示本公司。",
                 _obj({"kind": {"type": "string", "enum": ["income", "balance", "cashflow"]}, "ticker": {"type": "string"},
                       "periods": {"type": "integer"}, "annual_only": {"type": "boolean"}}, ["kind"]),
                 self.statements),
            Tool("shareholders",
                 "股东结构。A 股：十大股东与十大流通股东（最近两期，含增减变化与股东性质）、股东户数与户均持股趋势（户数减少常见于筹码集中）；"
                 "港股：定期报告披露的主要股东与董事权益（同一批股份可能经多个受控法团重复披露，不能简单相加）。"
                 "港股的券商/托管行持股分布用 ccass。ticker 不填表示本公司。",
                 _obj({"ticker": {"type": "string"}}, []),
                 self.shareholders),
            Tool("ccass",
                 "港股中央结算系统（CCASS）持股：前 15 名参与者（托管行、券商、港股通）的持股与占已发行股份比例，较约 5 个与 20 个交易日前的变化，"
                 "前 5/前 10 名集中度，以及未存入 CCASS 的比例（多为控股股东实物持股或未上市股份）。"
                 "用于看筹码集中度、大额转仓（配售交收、换托管行、质押）和港股通进出；参与者一增一减、数量相近多为转仓，不一定是买卖。仅港股。ticker 不填表示本公司。",
                 _obj({"ticker": {"type": "string"}}, []),
                 self.ccass),
            Tool("broker_research",
                 "卖方一致预期。A 股：东方财富研报库近几个月的研报列表，汇总评级分布与 EPS 预测中位数、目标价中位数；"
                 "港股：富途个股预测页的分析师数量与目标价均值/区间（经 Exa 读取）；美股：Nasdaq 的一致目标价、评级分布与年度 EPS 一致预期"
                 "（中概股为每 ADS 美元口径）。单家机构的最新目标价请再用 web_search 核实。ticker 不填表示本公司。",
                 _obj({"ticker": {"type": "string"}, "months": {"type": "integer"}}, []),
                 self.broker_research),
            Tool("earnings_vs_consensus",
                 "业绩 vs 一致预期（程序计算）：当前各年一致预期与近 30/90 天的修正趋势；近几个报告期「实际 vs 发布前一致预期」"
                 "（年报看偏差；中报、季报为年初至今累计，看完成全年一致预期的比例，并按近 3 年同期占比外推全年）。"
                 "A 股（A+H 公司用 A 股口径）按研报库回溯；港股用使用者录入的快照；美股另附 Nasdaq 季度业绩惊喜。"
                 "用于判断业绩是否超预期、公司历史上是否常超/不及预期。ticker 不填表示本公司；A 股可查任意代码。",
                 _obj({"ticker": {"type": "string"}, "periods": {"type": "integer"}}, []),
                 self.earnings_vs_consensus),
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
            Tool("report_notes",
                 "检索本公司深度报告的正文、背后的研究笔记，以及写报告时的工具调用记录（当时查到的原始数据、推理与出处），"
                 "用于回答「报告里这个数字、这个判断从哪来」。多个关键词用空格分隔；dossier_id 不填 = 最新一份报告。",
                 _obj({"query": {"type": "string"}, "dossier_id": {"type": "integer"}, "top_k": {"type": "integer"}}, ["query"]),
                 self.report_notes),
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
                         current_price=None, basis=None) -> str:
        basis = basis if basis in MATRIX_BASES else infer_basis(multiple_label)
        drivers, mults = [float(x) for x in driver_values], [float(x) for x in multiples]
        shares, net_cash, fx = float(shares), float(net_cash or 0), float(fx or 1)
        added: list[str] = []
        if self.company is not None and shares > 0:
            from . import anchor  # anchor → tools：放在函数里避免循环导入

            anc = anchor.get(self.app, self.company)
            if anc:
                drivers, mults, added = anchor.augment(anc, driver_label, drivers, multiple_label, mults, shares, fx, basis)
        table = matrix_table(driver_label, drivers, multiple_label, mults, shares, net_cash, fx,
                             float(current_price) if current_price else None, basis)
        return MetaStr(table + "".join(f"\n{x}" for x in added),
                       {"basis": basis, "driver_values": drivers, "multiples": mults})

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
        for label, p in self.app.cross_premiums(c) if c is not None else []:
            out.append(f"{label}，已按汇率{'与换股比例' if 'ADS' in label else ''}换算：{pct(p['current'])}，20日均值 {pct(p['avg_20'])}，"
                       f"60日区间 {pct(p['min_60'])} ~ {pct(p['max_60'])}")
        if c is not None and len(c.listings) > 1:
            out.append(f"定价上市地：{c.pricing_listing.display}（情景价格与目标价按它计算）")
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
        ls = listings_for(self.app, self.company, ticker, markets=("A", "HK", "US"))
        out = [hk_consensus(self.app, l) for l in ls if l.market == "HK"]
        a = [l for l in ls if l.market == "A"]
        if a:
            out.append(self._a_research(a[0], months))
        out += [us_consensus(self.app, l) for l in ls if l.market == "US"]
        return "\n\n".join(out) or "只支持 A 股、港股和美股"

    def statements(self, kind: str, ticker: str | None = None, periods: int = 4, annual_only: bool = False) -> str:
        if kind not in emdata.STATEMENT_KINDS:
            return "错误：kind 只能是 income、balance、cashflow"
        ls = listings_for(self.app, self.company, ticker, markets=("A", "HK", "US"))
        if not ls:
            return "本公司没有可查询报表的上市地"
        l = next(x for m in ("A", "HK", "US") for x in ls if x.market == m)  # A+H 用 A 股报表，港美两地用港股
        return statement_table(self.app, l, kind, max(1, min(int(periods), 12)), bool(annual_only))

    def shareholders(self, ticker: str | None = None) -> str:
        out = []
        for l in listings_for(self.app, self.company, ticker):
            out.append(_a_holders_text(self.app, l) if l.market == "A" else _hk_holders_text(self.app, l))
        return "\n\n".join(out) or "股东数据只支持 A 股和港股"

    def ccass(self, ticker: str | None = None) -> str:
        hk = [l for l in listings_for(self.app, self.company, ticker) if l.market == "HK"]
        if not hk:
            return "CCASS 持股只适用于港股"
        return "\n\n".join(ccass_text(self.app, l) for l in hk)

    def earnings_vs_consensus(self, ticker: str | None = None, periods: int = 6) -> str:
        from . import consensus  # consensus → valuation → tools：放在函数里避免循环导入

        n = max(1, min(int(periods), 12))
        if not ticker:
            if self.company is None:
                raise ValueError("需要指定 ticker（A 股 6 位代码或自选股名称）")
            return consensus.report_text(self.app, self.company, n=n)
        if (c := self.app.watchlist.get(ticker)) is not None:
            return consensus.report_text(self.app, c, n=n)
        l = listings_for(self.app, None, ticker, markets=("A", "HK", "US"))[0]
        if l.market != "A":
            return f"{l.display}：不在自选股里。港股、美股的一致预期只有自选股的快照；A 股可以直接查任意代码"
        return consensus.report_text(self.app, None, listing=l, n=n)

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
            pe, pb, cur_pe, cur_pb = valuation_series(self.app, l, years)
            src = ("东方财富估值历史" if l.market == "A" else
                   "日收盘价 ÷ 最近已披露 EPS(TTM)/每股净资产（半年报滞后 60 天、年报滞后 90 天视为可得；已按汇率换算）")
            cur_pe = cur_pe if cur_pe is not None and cur_pe > 0 else None
            out.append(f"{l.display} 近 {years} 年估值（{src}）：\n  " + _stats_line("PE(TTM)", pe, cur_pe)
                       + ("（当前亏损，PE 不适用）" if cur_pe is None else "") + "\n  " + _stats_line("PB", pb, cur_pb))
        return "\n".join(out)

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

    def report_notes(self, query: str, dossier_id: int | None = None, top_k: int = 5) -> str:
        d = self.app.db.dossier(int(dossier_id)) if dossier_id else self.app.db.latest_dossier(self.company.id)
        if d is None or d["company_id"] != self.company.id:
            return f"错误：本公司没有编号为 {dossier_id} 的深度报告" if dossier_id else "本公司还没有深度报告"
        terms = [to_simplified(t) for t in re.split(r"[\s,，;；、]+", query) if t]
        if not terms:
            return "错误：query 为空"
        chunks = []  # (出处, 原文)
        section = ""
        for para in re.split(r"\n\s*\n", d.get("report_md") or ""):
            if m := re.match(r"#+\s*(?:§\s*)?(.+)", para.strip()):
                section = m.group(1).strip()[:40]
            if para.strip():
                chunks.append((f"报告正文·{section}" if section else "报告正文", para.strip()))
        for note in json.loads(d.get("notes_json") or "{}").values():
            for para in re.split(r"\n\s*\n", note.get("text") or ""):
                if para.strip():
                    chunks.append((f"研究笔记·{note.get('title', '')}", para.strip()))
        for i, x in enumerate(json.loads(d.get("tool_log_json") or "[]"), 1):
            args = json.dumps(x.get("args") or {}, ensure_ascii=False)[:200]
            chunks.append((f"研究过程 #{i}·{x.get('section') or '—'}·{x.get('tool')}（{args}）", str(x.get("result") or "")))
        simple = [to_simplified(t) for _, t in chunks]
        df = [sum(1 for t in simple if k in t) for k in terms]
        idf = [math.log((len(chunks) + 1) / (n + 1)) + 1 for n in df]
        hits = []
        for (src, text), low in zip(chunks, simple):
            counts = [low.count(k) for k in terms]
            if not any(counts):
                continue
            score = sum(w * c * 2.2 / (c + 1.2) for w, c in zip(idf, counts) if c) * (1.5 if all(counts) else 1)
            hits.append((score, src, text, low))
        if not hits:
            return f"深度报告 #{d['id']} 的正文、研究笔记与研究过程中没有找到「{query}」"
        hits.sort(key=lambda h: -h[0])
        out = []
        for _, src, text, low in hits[: max(1, min(int(top_k), 10))]:
            rare = max((k for k in terms if k in low), key=lambda k: idf[terms.index(k)], default=terms[0])
            pos = max(0, low.find(rare))
            body = text if len(text) == len(low) else low
            out.append(f"〔深度报告 #{d['id']} {src}〕" + re.sub(r"\s+", " ", body[max(0, pos - 200): pos + 600]).strip())
        return "\n\n".join(out)

    def web_search(self, query: str, max_results: int = 8) -> str:
        res, source = web.search(self.app.session, query, max(1, min(int(max_results), 10)), self.web_providers)
        if not res:
            return (f"没有可用的搜索结果（{source}）。如果是搜索源被拦截或无关，这是搜索服务的问题，不代表信息不存在："
                    "可以换检索式重试；仍然查不到的，写明「搜索不可用，未能核实」并列入需要使用者补充的数据。")
        return f"（搜索源：{web.LABELS.get(source, source)}）\n" + "\n".join(
            f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet'][:500]}" for i, r in enumerate(res, 1))

    def fetch_url(self, url: str, max_chars: int = 4000) -> str:
        return web.fetch_text(self.app.session, url, max(500, min(int(max_chars), self.max_chars)))

    def _tweets(self, fn, *args, **kwargs) -> list[dict] | str:
        """调用推特适配器：失败时返回给模型的错误文字；凭证失效、被限流、命令不可用时熔断（各进程共享）。"""
        if _is_down(self.app, "twitter"):
            return "错误：推特暂不可用（凭证失效或被限流，稍后自动恢复），请改用 web_search"
        try:
            return fn(*args, **kwargs)
        except twitter.TwitterError as e:
            if e.trips_breaker:
                _trip(self.app, "twitter")
            hint = "；Cookie 可能已过期，需要重新导出并更新 .env 的 TWITTER_AUTH_TOKEN / TWITTER_CT0" if e.code == "auth" else ""
            return f"错误：{e}{hint}"

    def _tweet_lines(self, posts: list[dict], limit: int) -> str:
        roles = twitterfeed.account_roles(self.app, self.company)  # 使用者登记的可信账号：标出来，agent 可以按已证实的消息引用
        out = []
        for i, p in enumerate(posts[: max(1, limit)], 1):
            text = re.sub(r"\s+", " ", p["text"]).strip()
            if len(text) > 400:
                text = text[:400] + "…"
            stats = f"赞{p['likes']} 转{p['retweets']}" + (f" 浏览{p['views']:,}" if p["views"] else "")
            who = f"@{p['author']}" + ("✓" if p["verified"] else "")
            if role := roles.get(p["author"].lower()):
                who += f"【可信账号·{role}】"
            out.append(f"{i}. {p['at'].astimezone(CST):%m-%d %H:%M}｜{who}｜{stats}｜{'（转推）' if p['is_retweet'] else ''}{text}\n   {p['url']}")
        return "\n".join(out)

    def twitter_search(self, query: str, max_results: int = 10, days: int | None = None, min_likes: int | None = None,
                       sort: str = "latest", from_user: str = "", lang: str = "") -> str:
        cfg = self.app.settings.sources.twitter
        limit = max(1, min(int(max_results), cfg.max_results))
        days = cfg.days if days is None else int(days)
        min_likes = cfg.min_likes if min_likes is None else int(min_likes)
        posts = self._tweets(twitter.search, query, limit, days, min_likes, sort, from_user, lang.strip().lower())
        if isinstance(posts, str):
            return posts
        if not posts:
            return (f"推特上没有符合条件的帖子（最近 {days} 天、至少 {min_likes} 个赞）。可以放宽 days / min_likes，或换检索词"
                    f"（代号 $XPEV、英文名、中文名）；没有结果不代表没有这件事。")
        return (f"（推特检索「{query}」：最近 {days} 天、至少 {min_likes} 个赞，共 {len(posts)} 条；标【可信账号】的是使用者登记的账号，按已证实的消息，"
                f"可引用并写明账号与日期；其余推文只作线索，数字与事实须另行核实）\n" + self._tweet_lines(posts, limit))

    def twitter_user(self, handle: str, max_results: int = 10) -> str:
        limit = max(1, min(int(max_results), self.app.settings.sources.twitter.max_results))
        posts = self._tweets(twitter.user_posts, handle, limit)
        if isinstance(posts, str):
            return posts
        if not posts:
            return f"@{handle.lstrip('@')} 没有可读取的帖子"
        h = handle.lstrip("@")
        if role := twitterfeed.account_roles(self.app, self.company).get(h.lower()):
            note = f"使用者登记的{role}，按已证实的消息，可引用并写明账号与日期"
        else:
            note = "不是登记的账号，推文只作线索，数字与事实须另行核实"
        return f"（推特 @{h} 最近 {len(posts)} 条；{note}）\n" + self._tweet_lines(posts, limit)

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


def twitter_enabled(app: App) -> bool:
    return app.settings.sources.twitter.agent_tools and twitter.available()


def dossier_running(app: App) -> bool:
    """深度报告正在运行（持有 dossier 锁）：它的并行 agent 已占满免费搜索源，其他 agent 应暂停网页搜索。"""
    return lock_holder(app.lock_path("dossier")) is not None


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
