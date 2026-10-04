"""funds.yaml：模拟基金定义与共用风控参数（pydantic 校验，与 watchlist.yaml / themes.yaml 同样放在 config.yaml 所在目录）。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Market = Literal["HK", "A"]
Regime = Literal["attack", "neutral", "defend"]
FILE = "funds.yaml"
MARKET_CCY = {"HK": "HKD", "A": "CNY"}


class CostConfig(BaseModel):
    """交易成本（每边，按成交金额的比例）。港股：印花税买卖双方各 0.1%（不足 1 港元按 1 港元，向上取整到元）、
    交易费 0.00565%、证监会征费 0.0027%、会财局征费 0.00015%（港交所收费页，2026-10 核对）；结算费按券商收费表常见的 0.0042%（未在港交所页面核对）。
    A 股：印花税卖出单边 0.05%、经手费 0.00341%、证管费 0.002%、过户费 0.001%（双向）。佣金与滑点为假设值，按自己的券商调整。"""

    model_config = ConfigDict(extra="forbid")

    commission: float = Field(ge=0, le=0.01)        # 佣金费率
    min_commission: float = Field(ge=0)             # 每笔最低佣金（本币）
    stamp_buy: float = Field(ge=0, le=0.01)
    stamp_sell: float = Field(ge=0, le=0.01)
    stamp_round_up: bool = False                    # 印花税向上取整到元（港股）
    exchange_fees: float = Field(ge=0, le=0.01)     # 交易所与监管规费合计
    settlement_fee: float = Field(ge=0, le=0.01)    # 过户费 / 结算费
    slippage: float = Field(ge=0, le=0.05)          # 滑点：买入价上浮、卖出价下浮的比例


DEFAULT_COSTS = {
    "HK": CostConfig(commission=0.0003, min_commission=5, stamp_buy=0.001, stamp_sell=0.001, stamp_round_up=True,
                     exchange_fees=0.0000565 + 0.000027 + 0.0000015, settlement_fee=0.000042, slippage=0.001),
    "A": CostConfig(commission=0.00025, min_commission=5, stamp_buy=0.0, stamp_sell=0.0005,
                    exchange_fees=0.0000341 + 0.00002, settlement_fee=0.00001, slippage=0.0005),
}


class FundDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    name: str
    market: Market
    strategy: str                                   # 策略登记表中的名称（strategy.STRATEGIES）
    style: Literal["value", "trend"]                # 决定市场状态对应的仓位上限档位
    currency: Literal["HKD", "CNY"]
    initial_capital: float = Field(default=1_000_000, gt=0)
    max_holdings: int = Field(ge=1, le=50)
    max_weight: float = Field(default=0.15, gt=0, le=1)   # 单票上限
    sector_cap: float = Field(default=0.30, gt=0, le=1)   # 同一行业上限
    min_weight: float = Field(default=0.03, ge=0, le=1)   # 小于该权重的仓位不开
    drawdown: tuple[float, float, float] = (0.05, 0.10, 0.15)   # 熔断阈值：减半新仓 / 停止开新仓并降到 50% / 暂停
    rebalance: Literal["weekly", "monthly", "quarterly"]
    benchmark: str                                  # 基准指数 secid
    status: Literal["paper", "paused"] = "paper"
    params: dict = Field(default_factory=dict)      # 策略参数，由各策略的 Params 校验

    @model_validator(mode="after")
    def _check(self) -> FundDef:
        if self.currency != MARKET_CCY[self.market]:
            raise ValueError(f"{self.id}：{self.market} 基金的币种应为 {MARKET_CCY[self.market]}")
        a, b, c = self.drawdown
        if not 0 < a < b < c < 1:
            raise ValueError(f"{self.id}：回撤阈值须递增且在 0–1 之间")
        if self.min_weight > self.max_weight:
            raise ValueError(f"{self.id}：min_weight 不能大于 max_weight")
        from .strategy import STRATEGIES  # 避免循环导入

        s = STRATEGIES.get(self.strategy)
        if s is None:
            raise ValueError(f"{self.id}：未知策略 {self.strategy}（可选：{'、'.join(STRATEGIES)}）")
        if s.market != self.market:
            raise ValueError(f"{self.id}：策略 {self.strategy} 只适用于 {s.market} 市场")
        s.Params.model_validate(self.params)
        return self


class RiskConfig(BaseModel):
    """共用风控：所有基金一视同仁；多个上限同时生效时取最小值。"""

    model_config = ConfigDict(extra="forbid")

    exposure: dict[Literal["value", "trend"], dict[Regime, float]] = {
        "trend": {"attack": 1.0, "neutral": 0.6, "defend": 0.3},
        "value": {"attack": 1.0, "neutral": 0.85, "defend": 0.6},
    }
    neutral_new_entries: int = 1        # 中性市况：趋势基金每天只开最强的几笔；防守市况不开新仓
    breaker_size_factor: float = 0.5    # 第一档熔断：新开仓仓位乘以该系数
    breaker_cap: float = 0.5            # 第二档熔断：总仓位上限
    halt_exposure: float = 0.3          # 第三档熔断（基金暂停，需手动恢复）：总仓位上限
    recover: tuple[float, float] = (0.03, 0.05)   # 第一档在回撤收窄到 3% 内恢复，第二档在 5% 内恢复
    cross_fund_cap: float = 0.10        # 同一只股票（A+H 按发行人合并）在全部基金里合计不超过总资金的比例
    drift: float = 0.05                 # 权重偏离不到 5 个百分点不调
    replace_margin: float = 0.20        # 基金满仓时，新候选得分须比最弱持仓高出 20% 才替换
    fx_hkdcny: float = 0.92             # 没有汇率行情时合并计算用的港元兑人民币（仅作兜底）


class RegimeConfig(BaseModel):
    """市场状态：指数是否在长期均线上、宽度（日线股票池中站上 50 日线的比例）、20 日波动率在近一年的分位。"""

    model_config = ConfigDict(extra="forbid")

    index: dict[Market, str] = {"HK": "100.HSI", "A": "1.000300"}
    ma: int = 200
    breadth_ma: int = 50
    breadth_attack: float = 0.6
    breadth_defend: float = 0.4
    vol_window: int = 20
    vol_lookback: int = 250
    vol_high: float = 0.8               # 波动率分位高于该值算一个转弱信号
    vol_spike: float = 0.95             # 高于该值直接判防守
    min_breadth_n: int = 30             # 宽度样本少于该数时不用宽度


class UniverseConfig(BaseModel):
    """股票池：选股器的全市场表（港股通 + 全部 A 股）。第一阶段只用批量数据过滤，第二阶段才用日线。"""

    model_config = ConfigDict(extra="forbid")

    min_listing_days: int = 365                                   # 上市不满一年排除（A 股没有上市日期时按日线根数判断）
    min_mcap: dict[Market, float] = {"HK": 5e9, "A": 5e9}         # 第一阶段：总市值下限（上市地币种）
    min_turnover: dict[Market, float] = {"HK": 2e7, "A": 1e8}     # 第二阶段：20 日成交额中位数下限
    bars_universe: dict[Market, int] = {"HK": 400, "A": 1200}     # 第一阶段后按市值取前 N 只，每晚刷新日线
    participation: float = 0.1          # 流动性容量：成交额中位数 × 该参与率 × 5 个交易日（与选股器口径相同）


class BarsConfig(BaseModel):
    """晚间日线刷新：逐只请求、限速、出错退避；可中断，下次从最旧的继续。"""

    model_config = ConfigDict(extra="forbid")

    request_delay: float = Field(default=1.0, ge=0)   # 同一域名两次请求的最小间隔（秒）
    history: int = 320                  # 首次回填的日线根数（≥ 260 个交易日）
    incremental: int = 10               # 已有日线时每次取最近几根（与库里重叠的部分用来发现复权变化）
    max_errors: int = 8                 # 连续失败多少只后停止（下次续跑）
    backoff_max: float = 60.0           # 连续失败时的退避上限（秒）
    lot_days: int = 30                  # 港股每手股数缓存天数


class ExclusionConfig(BaseModel):
    """新建仓前的风险排除（所有基金共用）：维护的名单 + 选股器全市场公告扫描里的负面事件。"""

    model_config = ConfigDict(extra="forbid")

    codes: dict[str, str] = Field(default_factory=dict)          # 代码（00700 / HK:00700 / 600519）→ 原因
    name_keywords: dict[str, str] = Field(default_factory=dict)  # 名称包含 → 原因（如所属集团清盘）
    event_kinds: list[str] = ["default", "delist_risk", "audit", "investigation"]   # screen_events 的负面类别
    event_days: int = 365                                         # 回看天数


class VetoConfig(BaseModel):
    """新建仓前的模型否决：只能否决，不能增加或调整仓位；模型不可用时新建仓一律跳过。"""

    model_config = ConfigDict(extra="forbid")

    task: str = "screen"                # 用哪个任务的模型路由（与选股器快筛相同）
    max_tokens: int = 2000


class Costs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    HK: CostConfig = DEFAULT_COSTS["HK"]
    A: CostConfig = DEFAULT_COSTS["A"]


class FundsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    funds: list[FundDef] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    bars: BarsConfig = Field(default_factory=BarsConfig)
    costs: Costs = Field(default_factory=Costs)
    exclusions: ExclusionConfig = Field(default_factory=ExclusionConfig)
    veto: VetoConfig = Field(default_factory=VetoConfig)

    @field_validator("funds")
    @classmethod
    def _unique(cls, v: list[FundDef]) -> list[FundDef]:
        ids = [f.id for f in v]
        if dup := {i for i in ids if ids.count(i) > 1}:
            raise ValueError(f"基金 id 重复：{dup}")
        return v

    def fund(self, fund_id: str) -> FundDef | None:
        return next((f for f in self.funds if f.id == fund_id), None)

    def cost(self, market: str) -> CostConfig:
        return getattr(self.costs, market)


class FundsError(Exception):
    pass


def path(app) -> Path:
    return app.root / FILE


def load(app, file: Path | None = None) -> FundsFile:
    p = file or path(app)
    if not p.exists():
        raise FundsError(f"找不到 {p}：模拟基金的定义写在 config.yaml 同目录的 {FILE}")
    with open(p, encoding="utf-8") as f:
        try:
            return FundsFile.model_validate(yaml.safe_load(f) or {})
        except ValueError as e:
            raise FundsError(f"{p} 校验失败：{e}") from e
