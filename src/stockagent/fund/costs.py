"""交易成本、交易单位取整与 A 股涨跌停。全部是纯函数。"""

from __future__ import annotations

import math

from .config import CostConfig


def fees(cfg: CostConfig, side: str, amount: float) -> float:
    """一笔成交的费用（本币）：佣金（有最低额）+ 印花税 + 规费 + 结算 / 过户费。滑点体现在成交价里，不在这里。"""
    if amount <= 0:
        return 0.0
    commission = max(amount * cfg.commission, cfg.min_commission) if cfg.commission or cfg.min_commission else 0.0
    stamp = amount * (cfg.stamp_buy if side == "buy" else cfg.stamp_sell)
    if cfg.stamp_round_up and stamp > 0:
        stamp = math.ceil(stamp)
    return round(commission + stamp + amount * (cfg.exchange_fees + cfg.settlement_fee), 2)


def fill_price(cfg: CostConfig, side: str, price: float) -> float:
    """计入滑点后的成交价：买入上浮、卖出下浮。"""
    return price * (1 + cfg.slippage) if side == "buy" else price * (1 - cfg.slippage)


def a_limit_pct(code: str, name: str = "") -> float:
    """A 股涨跌幅限制：创业板 / 科创板 20%，北交所 30%，ST 5%，其余 10%。"""
    if "ST" in (name or "").upper():
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


def limit_prices(code: str, prev_close: float, name: str = "") -> tuple[float, float]:
    """(涨停价, 跌停价)，按交易所规则四舍五入到分。"""
    p = a_limit_pct(code, name)
    return round(prev_close * (1 + p) + 1e-9, 2), round(prev_close * (1 - p) + 1e-9, 2)


def lot_size(market: str, code: str, hk_lot: float | None = None) -> tuple[int, bool]:
    """(每手股数, 是否确知)。A 股 100 股（科创板起买 200 股，见 round_shares）；港股用行情里的每手股数，取不到时按 100 股并标出。"""
    if market == "A":
        return 100, True
    if hk_lot and hk_lot > 0:
        return int(hk_lot), True
    return 100, False


def round_shares(market: str, code: str, shares: float, lot: int) -> int:
    """向下取整到交易单位；科创板不足 200 股的买单取 0。"""
    n = math.floor(max(shares, 0) / lot + 1e-9) * lot
    if market == "A" and code.startswith(("688", "689")) and 0 < n < 200:
        return 0
    return n
