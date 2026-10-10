from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from stockagent.app import App

CONFIG = """
data_dir: data
llm:
  default_profile: mock
  profiles:
    mock: {protocol: mock, model: mock}
  tasks: {triage: mock, stock_brief: mock, market_brief: mock, profile: mock}
sources:
  google_news: false
  eastmoney_news: false
report:
  lookback_hours: 48
  min_materiality: 1
"""

WATCHLIST = """
companies:
  - id: acme
    name: 测试公司
    name_en: Acme
    aliases: [測試公司]
    listings:
      - {market: A, code: "300001"}
      - {market: HK, code: "1234"}
benchmarks:
  HK: [{secid: "100.HSI", name: 恒生指数}]
  A: [{secid: "1.000300", name: 沪深300}]
"""


@pytest.fixture(autouse=True)
def _no_external_search(monkeypatch):
    """测试不调用本机的 mcporter / Exa（避免联网和消耗额度）。"""
    from stockagent.sources import web

    monkeypatch.setattr(web, "MCPORTER", None)
    monkeypatch.setattr(web, "_cache", {})
    from stockagent.sources import emdata

    monkeypatch.setattr(emdata, "fcf_data", lambda *a, **k: None)  # 报表数据需要联网；需要的测试自行替换
    # 业绩 vs 一致预期（stock run 的一步）用到的东方财富接口同理
    monkeypatch.setattr(emdata, "financials", lambda *a, **k: [])
    monkeypatch.setattr(emdata, "research_reports", lambda *a, **k: [])
    monkeypatch.setattr(emdata, "a_consensus", lambda *a, **k: None)
    monkeypatch.setattr(emdata, "a_forecasts", lambda *a, **k: [])
    monkeypatch.setattr(emdata, "a_express", lambda *a, **k: [])
    monkeypatch.setattr(emdata, "valuation_history_a", lambda *a, **k: [])  # 程序参考价（anchor.py）的历史 PE
    from stockagent.sources import ccass, nasdaq, sec

    monkeypatch.setattr(ccass, "holdings", lambda *a, **k: None)  # 港交所 CCASS、SEC XBRL、Nasdaq 同理
    monkeypatch.setattr(sec, "company_facts", lambda *a, **k: {})
    monkeypatch.setattr(nasdaq, "_get", lambda *a, **k: {})
    from stockagent.sources import hkdi

    monkeypatch.setattr(hkdi, "lookup_sid", lambda *a, **k: 1)  # 港交所权益披露（DI）：持股变动一步用到
    monkeypatch.setattr(hkdi, "notices", lambda *a, **k: [])
    monkeypatch.setattr(sec, "schedule13", lambda *a, **k: None)
    from stockagent.sources import ima

    monkeypatch.delenv(ima.ENV_CLIENT_ID, raising=False)  # 本机 .env 里的 ima 凭证不带进测试；需要的测试自行设置
    monkeypatch.delenv(ima.ENV_API_KEY, raising=False)
    monkeypatch.setattr(ima, "MIN_INTERVAL", 0)
    from stockagent.sources import twitter

    monkeypatch.setattr(twitter, "BIN", None)  # 推特：不调用本机的 twitter-cli，也不读 ~/.agent-reach 里的 Cookie；需要的测试自行设置
    monkeypatch.setattr(twitter, "AGENT_REACH_CONFIG", Path("/nonexistent/agent-reach.yaml"))
    monkeypatch.delenv(twitter.ENV_AUTH, raising=False)
    monkeypatch.delenv(twitter.ENV_CT0, raising=False)
    monkeypatch.setattr(twitter, "MIN_INTERVAL", 0)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "config.yaml").write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    (tmp_path / "watchlist.yaml").write_text(textwrap.dedent(WATCHLIST), encoding="utf-8")
    return tmp_path


@pytest.fixture
def app(project: Path) -> App:
    a = App.load(project / "config.yaml")
    yield a
    a.db.close()
