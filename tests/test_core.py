from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from stockagent import docs, technical
from stockagent.config import LLMConfig, Listing
from stockagent.llm import LLM, LLMError, extract_json
from stockagent.triage import TRIAGE_SCHEMA, TriageItem, _bigrams, _validate, same_event
from stockagent.utils import CST, normalize_title


# ---------------------------------------------------------------- 配置

def test_listing_normalization():
    assert Listing(market="HK", code="2577").code == "02577"
    assert Listing(market="HK", code="2577").display == "02577.HK"
    assert Listing(market="A", code="688337").exchange == "SH"
    assert Listing(market="A", code="300308").exchange == "SZ"
    assert Listing(market="A", code="920001").exchange == "BJ"
    assert Listing(market="US", code="nvda", exchange="nasdaq").code == "NVDA"
    with pytest.raises(ValueError):
        Listing(market="A", code="12345")


def test_watchlist_lookup(app):
    wl = app.watchlist
    for key in ("acme", "测试公司", "測試公司", "Acme", "300001", "01234", "1234", "01234.HK", "300001.SZ"):
        assert wl.get(key) is not None and wl.get(key).id == "acme", key
    assert wl.get("不存在") is None


def test_llm_config_validates_routes():
    with pytest.raises(ValueError):
        LLMConfig.model_validate({"profiles": {"a": {"model": "x"}}, "tasks": {"triage": "missing"}})
    cfg = LLMConfig.model_validate({"profiles": {"a": {"model": "x"}}, "tasks": {"triage": {"profile": "a", "effort": "low"}}})
    name, prof, route = cfg.route("triage")
    assert name == "a" and route.effort == "low"
    assert cfg.route("unknown")[0] == "a"  # 未配置的任务走 default_profile


# ---------------------------------------------------------------- LLM

@pytest.mark.parametrize("text", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '好的，结果如下：\n{"a": 1}\n以上。',
])
def test_extract_json(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json("没有 JSON")


def test_mock_llm_returns_valid_triage(app):
    user = "[1] id=abcdef123456 | 公告\n[2] id=0123456789ab | 新闻"
    res = app.llm.complete("triage", ["sys"], user, schema=TRIAGE_SCHEMA, validate=_validate)
    assert [i["id"] for i in res.data["items"]] == ["abcdef123456", "0123456789ab"]
    assert all(1 <= i["materiality"] <= 5 for i in res.data["items"])


def test_llm_missing_key_is_reported(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    cfg = LLMConfig.model_validate({"profiles": {"c": {"protocol": "anthropic", "model": "claude-opus-5", "api_key_env": "NO_SUCH_KEY"}}})
    llm = LLM(cfg)
    assert "NO_SUCH_KEY" in llm.unavailable_reason("triage")
    with pytest.raises(LLMError):
        llm.complete("triage", ["s"], "u")


def test_triage_item_validators():
    item = TriageItem(id="x", relevant=True, event_type="bogus", direction="up", materiality=9,
                      novelty="?", horizon="?", summary_zh="s")
    assert (item.event_type, item.direction, item.materiality, item.novelty, item.horizon) == ("other", "unclear", 5, "new", "days")


def test_same_event_is_conservative():
    def check(a, b):
        a, b = normalize_title(a), normalize_title(b)
        return same_event(a, b, _bigrams(a), _bigrams(b))
    assert check("中际旭创：完成50亿元股份回购", "中际旭创：完成50亿元股份回购！")
    assert not check("中际旭创：股东拟减持不超过1%股份", "中际旭创：股东拟增持不超过1%股份")
    assert not check("9月21日363股获融资买入超亿元", "9月23日309股获融资买入超亿元")


# ---------------------------------------------------------------- 文档

@pytest.mark.parametrize("name,doc_type,period", [
    ("2025年報.pdf", "annual_report", "2025"),
    ("二零二六年中期報告.pdf", "interim_report", "2026H1"),
    ("普源精电2026年半年度报告.pdf", "interim_report", "2026H1"),
    ("2025年年度报告.pdf", "annual_report", "2025"),
    ("2026年第三季度报告.pdf", "quarterly_report", "2026Q3"),
    ("中金-深度研报-2026.pdf", "research", "2026"),
    ("调研纪要0928.docx", "transcript", None),
    ("random.pdf", "other", None),
])
def test_infer_doc_type_and_period(name, doc_type, period):
    assert docs.infer_doc_type(name) == doc_type
    assert docs.infer_period(name, doc_type) == period


def test_inbox_import_documents_and_csv(app):
    inbox = app.data_dir / "inbox"
    (inbox / "测试公司").mkdir(parents=True)
    (inbox / "测试公司" / "2025年报.md").write_text("# 年报\n" + "主营业务收入增长。" * 200, encoding="utf-8")
    (inbox / "acme").mkdir()
    (inbox / "acme" / "prices.csv").write_text("指标,期间,数值,单位\n产品价格,2026-09,45000,元/吨\n", encoding="utf-8")
    (inbox / "industry.csv").write_text("metric,period,value,unit,company\nR32价格,2026-09,\"45,000\",元/吨,\n", encoding="utf-8")
    (inbox / "unknown").mkdir()

    msgs = docs.import_inbox(app.db, app.data_dir, app.watchlist)
    assert any("导入：测试公司 · 年报 2025" in m for m in msgs), msgs
    assert any("不在自选股中" in m for m in msgs)
    d = app.db.documents("acme")[0]
    assert d["doc_type"] == "annual_report" and d["period"] == "2025" and d["chars"] > 1000
    assert app.db.manual_data("acme")[0]["value"] == 45000
    assert app.db.manual_data("")[0]["metric"] == "R32价格"
    # 导入后文件归档，重复导入按哈希去重
    assert not (inbox / "测试公司" / "2025年报.md").exists()
    archived = next((inbox / "_imported").rglob("2025年报.md"))
    doc, new = docs.store_document(app.db, app.data_dir, "acme", archived, doc_type="annual_report",
                                   period="2025", title="x", source="manual")
    assert not new


def test_pdf_text_extraction(tmp_path):
    import pymupdf

    pdf = tmp_path / "t.pdf"
    doc = pymupdf.open()
    for i in range(2):
        doc.new_page().insert_text((72, 72), f"Page {i + 1} revenue grew")
    doc.save(pdf)
    text, pages = docs.extract_text(pdf)
    assert pages == 2 and "[第2页]" in text and "revenue" in text


# ---------------------------------------------------------------- 技术面

def _bars(closes, start="2026-01-01"):
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": closes, "close": closes, "high": [c * 1.01 for c in closes],
                         "low": [c * 0.99 for c in closes], "volume": [1000.0] * len(closes)})


def test_snapshot_uptrend():
    s = technical.snapshot(_bars([100 + i for i in range(80)]))
    assert s["trend"] == "上升趋势"
    assert s["ret_5d"] == pytest.approx(179 / 174 - 1)
    assert s["rsi14"] > 90


def test_snapshot_short_history():
    s = technical.snapshot(_bars([10, 11, 12]))
    assert s["ret_1d"] == pytest.approx(12 / 11 - 1) and s["ret_20d"] is None and s["trend"] == "数据不足"


def test_reaction_respects_market_close():
    bars = _bars([10, 10, 11, 12], start="2026-09-21")  # 21, 22, 23, 24
    pre = datetime(2026, 9, 22, 10, 0, tzinfo=CST)      # 盘中发布：基准是 21 日收盘
    post = datetime(2026, 9, 22, 17, 0, tzinfo=CST)     # 收盘后发布：基准是 22 日收盘
    assert technical.reaction(bars, pre, "A")["ret"] == pytest.approx(12 / 10 - 1)
    assert technical.reaction(bars, post, "A")["days"] == 2
    latest = datetime(2026, 9, 24, 18, 0, tzinfo=CST)
    assert technical.reaction(bars, latest, "A")["ret"] is None  # 还没有交易日


def test_ah_premium_with_lagged_fx():
    a = _bars([90, 90, 90], start="2026-09-21")
    h = _bars([100, 100, 100], start="2026-09-21")
    fx = pd.DataFrame({"date": ["2026-09-18", "2026-09-22"], "close": [0.9, 0.9]})  # 汇率缺 21、23 日
    r = technical.ah_premium(a, h, fx)
    assert r["current"] == pytest.approx(0.0) and r["samples"] == 3


def test_utc_storage_roundtrip():
    from stockagent.utils import fmt_local, iso

    s = iso(datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc))
    assert s.endswith("+00:00") and fmt_local(s) == "09-28 09:00"
    with pytest.raises(ValueError):
        iso(datetime(2026, 9, 28))


def test_expected_report_periods():
    from datetime import date

    from stockagent.brief import expected_periods

    assert expected_periods(date(2026, 9, 28)) == ("2025", "2026H1")
    assert expected_periods(date(2026, 4, 10)) == ("2024", "2025H1")
    assert timedelta(days=1)  # 保持导入被使用
