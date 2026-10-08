"""数据源熔断：按接口分开熔断、冷却 10 分钟、长驻进程里会恢复、多个源同时失败按断网处理（不写 kv）。"""

from __future__ import annotations

from datetime import timedelta

import pytest
import requests

from stockagent import app as appmod
from stockagent.utils import parse_iso, utcnow


def boom(*a, **k):
    raise requests.ConnectionError("Remote end closed connection")


def fetch_klines(*a, **k):  # 函数名决定熔断范围
    raise requests.ConnectionError("push2his refused")


def fetch_quote(*a, **k):
    return {"price": 1.0}


def test_kline_failure_does_not_block_quote_of_same_source(app):
    assert appmod._try(app, "eastmoney", fetch_klines, app.session, "1.600026") is None
    assert app.db.kv_get("down:eastmoney@kline")
    assert appmod._try(app, "eastmoney", fetch_quote, app.session, "1.600026") == {"price": 1.0}
    assert appmod._try(app, "eastmoney", fetch_klines, app.session, "1.600026") is None  # 冷却期内直接跳过，不再请求


def test_cooldown_is_ten_minutes_and_expires_in_memory(app):
    appmod._try(app, "eastmoney", boom, app.session)
    assert appmod._is_down(app, "eastmoney")
    at = utcnow() - timedelta(minutes=appmod.DOWN_MINUTES + 1)
    app.db.kv_set("down:eastmoney", at.isoformat())
    app.down._d["eastmoney"] = (at, appmod.DOWN_MINUTES, True)  # 长驻进程里的内存熔断也会过期
    assert not appmod._is_down(app, "eastmoney")
    assert "eastmoney" not in app.down


def test_kv_breaker_from_other_process_keeps_remaining_time(app):
    app.db.kv_set("down:tencent", (utcnow() - timedelta(minutes=8)).isoformat())
    assert appmod._is_down(app, "tencent")
    _, minutes, _ = app.down._d["tencent"]
    assert minutes < 3


def test_twitter_keeps_long_cooldown(app):
    assert appmod.down_minutes("twitter") == 60 and appmod.down_minutes("eastmoney@kline") == appmod.DOWN_MINUTES
    app.db.kv_set("down:twitter", (utcnow() - timedelta(minutes=30)).isoformat())
    assert appmod._is_down(app, "twitter")


def test_several_sources_failing_together_is_treated_as_network_outage(app):
    for src in ("eastmoney", "tencent", "sina"):
        appmod._try(app, src, boom, app.session)
    assert app.db.kv_get("down:sina")  # 3 个还不算断网
    appmod._try(app, "yahoo", boom, app.session)  # 第 4 个：判定为本机网络故障
    for src in ("eastmoney", "tencent", "sina", "yahoo"):
        assert app.db.kv_get(f"down:{src}") is None  # 全部从 kv 撤掉，不会跨进程封一小时
        assert src in app.down  # 本进程内短暂暂停
    app.down._d["yahoo"] = (utcnow() - timedelta(minutes=appmod.NETWORK_RETRY_MIN + 1), appmod.NETWORK_RETRY_MIN, True)
    assert "yahoo" not in app.down


def test_single_source_block_persists(app):
    appmod._try(app, "eastmoney", fetch_klines, app.session, "x")
    assert parse_iso(app.db.kv_get("down:eastmoney@kline"))


def test_status_reset_clears_scoped_and_memory(app):
    appmod._try(app, "eastmoney", fetch_klines, app.session, "x")
    from stockagent.web import views  # noqa: F401  (确认模块可导入)
    app.down.discard_source("eastmoney")
    assert "eastmoney@kline" not in app.down
