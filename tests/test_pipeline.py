"""不联网的端到端：写入行情与信息条目 → 模拟 LLM 分级 → 生成简报 → 重要事件提醒。"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from stockagent import brief, profile, triage
from stockagent.app import _item, add_manual_item
from stockagent.brief import _colorize
from stockagent.utils import utcnow


def _seed_bars(app, secid, start_price):
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=90).strftime("%Y-%m-%d")
    bars = [{"date": d, "open": start_price + i, "close": start_price + i, "high": start_price + i + 1,
             "low": start_price + i - 1, "volume": 1000.0 + i, "amount": None} for i, d in enumerate(dates)]
    app.db.upsert_bars(secid, bars)


def test_end_to_end_with_mock_llm(app):
    company = app.watchlist.get("acme")
    for secid, p in (("0.300001", 50), ("116.01234", 40), ("1.000300", 4000), ("100.HSI", 24000)):
        _seed_bars(app, secid, p)
    now = utcnow()
    for i in range(3):
        x = {"source_id": f"n{i}", "title": f"测试公司发布第{i}号公告：签订重大合同", "published_at": now - timedelta(hours=i + 1),
             "url": f"https://example.com/{i}", "category": "公告"}
        assert app.db.add_item(_item(company, company.listings[1], "announcement", "hkex", x, "公告"))
    assert not app.db.add_item(_item(company, company.listings[1], "announcement", "hkex",
                                     {"source_id": "n0", "title": "重复", "published_at": now}, "公告"))
    add_manual_item(app, company, "渠道调研：订单饱满", "来自经销商")

    msgs = triage.run(app, [company])
    assert msgs == ["测试公司 分级 4 条"]
    assert not app.db.untriaged("acme")

    md_path, html_path, summary, msgs = brief.build(app, [company])
    text = md_path.read_text(encoding="utf-8")
    assert "## 今日要点" in text and "### 测试公司" in text and "AH 溢价" not in text  # 没有汇率数据
    assert "缺 " in text and "年报" in text  # 数据缺口提示
    assert html_path.exists() and (app.data_dir / "reports" / "latest.html").exists()
    assert "完整报告" in summary

    out = brief.alerts(app, do_push=False, window_hours=48)
    assert out and ("没有新的重要事件" in out[0] or "测试公司" in out[0])
    assert not app.db.pending_alerts(app.settings.notify.alert_min_materiality)


def test_profile_generation_with_mock(app):
    company = app.watchlist.get("acme")
    assert "没有可用资料" in profile.generate(app, company)
    src = app.data_dir / "2025年报.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("管理层讨论与分析\n" + "收入增长。" * 5000, encoding="utf-8")
    from stockagent.docs import store_document

    store_document(app.db, app.data_dir, "acme", src, doc_type="annual_report", period="2025", title="2025年报", source="manual")
    assert "画像已生成" in profile.generate(app, company)
    assert profile.needs_profile(app, company) is None
    assert "已是最新" in profile.generate(app, company)


def test_colorize():
    html = "<td>+1.2%</td><td>-3.4%</td><td>ATR 5.1%</td>"
    red = _colorize(html, red_up=True)
    assert '<span class="up">+1.2%</span>' in red and '<span class="down">-3.4%</span>' in red and "ATR 5.1%" in red
    assert '<span class="down">+1.2%</span>' in _colorize(html, red_up=False)
