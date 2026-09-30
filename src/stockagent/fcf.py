"""EV 与自由现金流（FCF）指标：fcf_yield 工具与选股器共用同一口径，全部由代码计算。

输入是 emdata.fcf_data 形状的报表数据（单位：元，报告币种）与同币种的市值：
- EV = 市值 + 有息负债 + 少数股东权益 − 现金及短期投资（受限制现金不计入）
- 保守口径 EV = 市值 + 总负债 − 现金（雪球「价值投资新经济」框架的算法）
- FCF = 经营现金流 − 购建长期资产支付的现金；TTM 由最新累计期推算，另取最近 3 个年报
"""

from __future__ import annotations

from .sources.emdata import ttm_flow


def payback_years(ev: float, fcf: float, growth: float = 0.0, cap: int = 50) -> int | None:
    """未来 FCF（按 growth 增长）累计多少年能覆盖当前 EV；EV ≤ 0 返回 0，FCF ≤ 0 返回 None，超过 cap 年返回 cap + 1。"""
    if ev <= 0:
        return 0
    if fcf <= 0:
        return None
    total = 0.0
    for n in range(1, cap + 1):
        total += fcf * (1 + growth) ** n
        if total >= ev:
            return n
    return cap + 1


def capex_checked(x: dict) -> dict:
    """港股标准化现金流量表里，部分公司的资本开支没有归到「购建固定资产 / 无形资产」（如记在「处置…」或「投资业务其他项目」），
    资本开支接近 0 会把 FCF 高估数倍。资本开支 < 投资活动净流出的 20% 且 < 经营现金流的 10% 时，改用投资活动现金净流出估算（偏保守）。"""
    inv, ocf = x.get("inv"), x.get("ocf")
    if inv is not None and inv < 0 and ocf and x["capex"] < 0.2 * -inv and x["capex"] < 0.1 * abs(ocf):
        return {**x, "capex": -inv, "capex_proxy": True}
    return x


def metrics(data: dict, mcap: float) -> dict:
    """由报表数据与市值（同为报告币种）计算 EV、净现金、各期 FCF 与 FCF Yield。"""
    data = {**data, "flows": [capex_checked(x) for x in data["flows"]]}
    b = data["balance"]
    cash_like = b["cash"] + b["invest"]
    ev = mcap + b["debt"] + b["minority"] - cash_like
    net_cash = cash_like - b["debt"]
    annual = [{**x, "label": f"{x['date'][:4]} 年报"} for x in data["flows"] if x["annual"] and x["ocf"] is not None][:3]
    ttm = ttm_flow(data["flows"])
    if ttm and any(x.get("capex_proxy") for x in data["flows"][:5]):  # TTM 由最近几期推算，其中有估算的也标出
        ttm["capex_proxy"] = True
    # 最新一期就是年报时，TTM 即该年报，不重复列出
    flows = annual if ttm and ttm["date"] == (annual[0]["date"] if annual else None) else [x for x in [ttm] if x] + annual
    for x in flows:
        x["fcf"] = x["ocf"] - x["capex"]
    avg3 = sum(x["fcf"] for x in annual) / 3 if len(annual) >= 3 else None
    base = flows[0]["fcf"] if flows else None
    ev_cons = mcap + b["liabilities"] - b["cash"] if b.get("liabilities") else None

    def yld(fcf: float | None, e: float | None) -> float | None:
        return fcf / e if fcf is not None and e and e > 0 else None

    return {
        "mcap": mcap, "ev": ev, "ev_conservative": ev_cons, "cash_like": cash_like, "debt": b["debt"],
        "minority": b["minority"], "net_cash": net_cash, "net_cash_ratio": net_cash / mcap if mcap else None,
        "negative_ev": ev < 0, "balance_date": b["date"], "flows": flows, "annual": annual,
        "fcf_base": base, "fcf_base_label": flows[0]["label"] if flows else None, "fcf_avg3": avg3,
        "yield_base": yld(base, ev), "yield_avg3": yld(avg3, ev), "yield_conservative": yld(base, ev_cons),
        "fcf_positive_years": sum(1 for x in annual if x["fcf"] > 0),
        "fcf_worst": min((x["fcf"] for x in flows), default=None),
        "capex_proxy": [x["label"] for x in flows if x.get("capex_proxy")],
    }
