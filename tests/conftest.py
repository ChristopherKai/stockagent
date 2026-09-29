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
    from stockagent.sources import ima

    monkeypatch.delenv(ima.ENV_CLIENT_ID, raising=False)  # 本机 .env 里的 ima 凭证不带进测试；需要的测试自行设置
    monkeypatch.delenv(ima.ENV_API_KEY, raising=False)
    monkeypatch.setattr(ima, "MIN_INTERVAL", 0)


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
