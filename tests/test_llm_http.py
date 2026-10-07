"""用本地假服务器检查两种协议实际发出的请求和对响应的解析（不需要真实 key，不联网）。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stockagent.config import LLMConfig
from stockagent.llm import LLM, LLMError

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def _sse(text: str, stop_reason: str = "end_turn") -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def _chat_sse(model: str, text: str, finish: str = "stop", reasoning: str = "") -> bytes:
    def chunk(delta, finish_reason=None, usage=None, choices=True):
        d = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}] if choices else []}
        if usage:
            d["usage"] = usage
        return f"data: {json.dumps(d)}\n\n"

    parts = ([chunk({"role": "assistant", "reasoning_content": reasoning})] if reasoning else [])
    parts += [chunk({"role": "assistant", "content": text[:2]}), chunk({"content": text[2:]}), chunk({}, finish),
             chunk({}, usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}, choices=False), "data: [DONE]\n\n"]
    return "".join(parts).encode()


class Recorder:
    def __init__(self):
        self.requests: list[dict] = []
        self.statuses: list[tuple[int, dict]] = []  # 依次返回的错误状态（耗尽后正常响应）：(状态码, 额外响应头)
        self.reply_text = '{"ok": true}'
        self.stop_reason = "end_turn"
        self.finish_reason = "stop"
        self.replies: list[dict] = []  # 依次作为流式回复（text / finish / reasoning），耗尽后用 reply_text


@pytest.fixture
def server():
    rec = Recorder()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            rec.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            if self.path.startswith("/v1/messages"):
                payload = _sse(rec.reply_text, rec.stop_reason)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            elif rec.statuses:
                code, headers = rec.statuses.pop(0)
                payload = json.dumps({"error": {"message": f"boom {code}", "type": "x"}}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
            elif body.get("stream"):
                r = rec.replies.pop(0) if rec.replies else {}
                payload = _chat_sse(body["model"], r.get("text", rec.reply_text), r.get("finish", rec.finish_reason), r.get("reasoning", ""))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                payload = json.dumps({
                    "id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": rec.reply_text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    rec.url = f"http://127.0.0.1:{httpd.server_port}"
    yield rec
    httpd.shutdown()


def _llm(profile: dict) -> LLM:
    return LLM(LLMConfig.model_validate({"profiles": {"p": profile}}))


def test_anthropic_request_shape(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    llm = _llm({"protocol": "anthropic", "base_url": server.url, "model": "claude-opus-5", "api_key_env": "TEST_KEY",
                "effort": "high", "refusal_fallback": True, "max_retries": 0})
    res = llm.complete("triage", ["稳定指令", "公司背景"], "问题", schema=SCHEMA, max_tokens=1000)
    assert res.data == {"ok": True}
    assert res.usage["cache_read"] == 7 and res.usage["output_tokens"] == 5

    req = server.requests[-1]
    body, headers = req["body"], req["headers"]
    assert req["path"].startswith("/v1/messages")
    assert headers["x-api-key"] == "sk-test"
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert body["fallbacks"] == "default"
    assert body["stream"] is True
    assert body["model"] == "claude-opus-5" and body["max_tokens"] == 1000
    assert body["system"][0] == {"type": "text", "text": "稳定指令"}
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert body["thinking"] == {"type": "adaptive"}
    assert "temperature" not in body


def test_anthropic_proxy_friendly_settings(server, monkeypatch):
    """中转站兼容模式：不发 output_config / fallbacks / thinking，改用提示词约束 JSON，Bearer 认证。"""
    monkeypatch.setenv("TEST_KEY", "sk-relay")
    server.reply_text = '好的：```json\n{"ok": true}\n```'
    llm = _llm({"protocol": "anthropic", "base_url": server.url, "model": "claude-opus-5", "api_key_env": "TEST_KEY",
                "auth": "bearer", "structured_output": False, "refusal_fallback": False, "thinking": "off",
                "prompt_cache": False, "max_retries": 0})
    res = llm.complete("triage", ["s"], "问题", schema=SCHEMA)
    assert res.data == {"ok": True}
    req = server.requests[-1]
    assert req["headers"]["authorization"] == "Bearer sk-relay"
    body = req["body"]
    assert "output_config" not in body and "fallbacks" not in body and "thinking" not in body
    assert "cache_control" not in body["system"][0]
    assert "JSON Schema" in body["messages"][0]["content"]


def test_anthropic_truncation_continues_then_refusal(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    llm = _llm({"protocol": "anthropic", "base_url": server.url, "model": "claude-opus-5", "api_key_env": "TEST_KEY", "max_retries": 0})
    server.reply_text = "部分内容"
    server.stop_reason = "max_tokens"  # 一直被截断：续写 3 次后停止并注明
    res = llm.complete("t", ["s"], "u")
    assert len(server.requests) == 4 and res.text.startswith("部分内容" * 4) and "截断" in res.text
    cont = server.requests[1]["body"]["messages"]
    assert cont[1] == {"role": "assistant", "content": "部分内容"} and "从中断处直接继续" in cont[2]["content"]
    server.stop_reason = "refusal"
    with pytest.raises(LLMError, match="拒绝"):
        llm.complete("t", ["s"], "u")


def test_openai_compatible_request_shape(server, monkeypatch):
    monkeypatch.setenv("DS_KEY", "sk-ds")
    server.reply_text = '{"ok": true}'
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "deepseek-chat", "api_key_env": "DS_KEY",
                "temperature": 0.2, "max_tokens": 8000, "max_retries": 0})
    res = llm.complete("triage", ["指令A", "背景B"], "问题", schema=SCHEMA, max_tokens=16000)
    assert res.data == {"ok": True} and res.usage["input_tokens"] == 5
    req = server.requests[-1]
    body = req["body"]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer sk-ds"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 8000  # 调用方要 16000，被 profile 上限截断
    assert body["temperature"] == 0.2
    assert body["messages"][0] == {"role": "system", "content": "指令A\n\n背景B"}
    assert "JSON" in body["messages"][1]["content"]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}  # 默认流式：网关对非流式请求有总时限
    assert res.usage["output_tokens"] == 3


def test_openai_stream_false_sends_plain_request(server, monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY",
                "stream": False, "max_retries": 0})
    assert llm.complete("t", ["s"], "u", schema=SCHEMA).data == {"ok": True}
    assert "stream" not in server.requests[-1]["body"]


def test_openai_stream_length_finish_is_truncation(server, monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    server.finish_reason = "length"
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 0})
    res = llm.complete("t", ["s"], "u")
    assert len(server.requests) == 1 + llm.cfg.max_continuations and "截断" in res.text


def test_openai_gateway_timeout_is_not_retried(server, monkeypatch):
    """504（网关超时）重试只会再等满一轮：不在 SDK 里重试，立即作为可回退的错误抛出（2026-10-06 审稿白等 483 秒）。"""
    monkeypatch.setenv("DS_KEY", "k")
    server.statuses = [(504, {}), (504, {}), (504, {})]
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 3})
    with pytest.raises(LLMError, match="504") as ei:
        llm.complete("t", ["s"], "u")
    assert ei.value.retryable and len(server.requests) == 1


def test_openai_transient_errors_are_retried_quickly(server, monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    monkeypatch.setattr("stockagent.llm.time.sleep", lambda s: None)
    server.statuses = [(503, {}), (429, {"retry-after": "1"})]
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 3})
    assert llm.complete("t", ["s"], "u", schema=SCHEMA).data == {"ok": True}
    assert len(server.requests) == 3
    server.statuses = [(503, {})] * 5
    llm2 = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 1})
    with pytest.raises(LLMError, match="503"):
        llm2.complete("t", ["s"], "u")
    assert len(server.requests) == 5  # 3 + 首发 1 次 + 重试 1 次


def _agent_max_tokens(server, monkeypatch, config_extra: dict | None = None, profile_max: int = 20000, **kw) -> int:
    monkeypatch.setenv("DS_KEY", "k")
    prof = {"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY",
            "max_tokens": profile_max, "max_retries": 0}
    llm = LLM(LLMConfig.model_validate({"profiles": {"p": prof}, **(config_extra or {})}))
    tool = {"name": "calculate", "description": "", "parameters": {"type": "object", "properties": {}}}
    res = llm.run_agent("research", ["s"], "问题", [tool], lambda n, a: "", **kw)
    assert res.text == '{"ok": true}'
    return server.requests[-1]["body"]["max_tokens"]


def test_run_agent_without_max_tokens_uses_profile_limit(server, monkeypatch):
    """调用方没有偏好（不传 max_tokens）：请求体里的上限就是 profile 的 max_tokens，不是写死的默认值。"""
    assert _agent_max_tokens(server, monkeypatch, profile_max=20000) == 20000
    assert _agent_max_tokens(server, monkeypatch, profile_max=128000) == 128000


def test_run_agent_explicit_max_tokens_is_capped_by_profile(server, monkeypatch):
    assert _agent_max_tokens(server, monkeypatch, profile_max=20000, max_tokens=50000) == 20000  # 调用方要 50000，被截断
    assert _agent_max_tokens(server, monkeypatch, profile_max=20000, max_tokens=5000) == 5000


def test_run_agent_route_max_tokens_overrides_caller(server, monkeypatch):
    extra = {"tasks": {"research": {"profile": "p", "max_tokens": 3000}}}
    assert _agent_max_tokens(server, monkeypatch, extra, profile_max=20000) == 3000
    assert _agent_max_tokens(server, monkeypatch, extra, profile_max=20000, max_tokens=9000) == 3000


def test_invalid_json_is_retried_once(server, monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    server.reply_text = "抱歉，我无法输出 JSON"
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 0})
    with pytest.raises(LLMError, match="无法解析"):
        llm.complete("triage", ["s"], "u", schema=SCHEMA)
    assert len(server.requests) == 2
    assert "上一次的输出无法解析" in server.requests[-1]["body"]["messages"][1]["content"]


def test_openai_truncated_text_is_continued(server, monkeypatch):
    monkeypatch.setenv("DS_KEY", "k")
    replies = iter([("上半部分，", "length"), ("下半部分。", "stop")])
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 0})

    orig = llm._openai

    def fake(name, prof, system, user, schema, max_tokens, history=None):
        text, finish = next(replies)
        return (text, finish == "length"), {"input_tokens": 1, "output_tokens": 1}, "m"

    llm._openai = fake
    res = llm.complete("t", ["s"], "u")
    assert res.text == "上半部分，下半部分。" and res.usage["output_tokens"] == 2
    llm._openai = orig


def test_openai_empty_body_is_retried_and_logged(server, monkeypatch, caplog):
    """推理模型偶发只吐思考、正文为空（研报前半部分「只有 0 字」）：同请求重试一次，并把现场写进日志。"""
    monkeypatch.setenv("DS_KEY", "k")
    server.replies = [{"text": "", "reasoning": "先想一想报告怎么写"}, {"text": "# 报告正文"}]
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 0})
    with caplog.at_level("WARNING", logger="stockagent"):
        res = llm.complete("t", ["s"], "u")
    assert res.text == "# 报告正文" and len(server.requests) == 2
    assert "正文为空" in caplog.text and "思考 9 字" in caplog.text and "finish=stop" in caplog.text


def test_openai_empty_body_twice_is_returned_empty(server, monkeypatch):
    """重试一次后仍为空：不无限重试，空文本交给调用方（dossier 的完整性检查会再重写）。"""
    monkeypatch.setenv("DS_KEY", "k")
    server.replies = [{"text": "", "reasoning": "想"}, {"text": "", "reasoning": "想"}]
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 0})
    assert llm.complete("t", ["s"], "u").text == "" and len(server.requests) == 2


def test_openai_thinking_ate_the_budget_retries_with_full_budget(server, monkeypatch):
    """思考把 max_tokens 用完、正文一个字没写（finish=length）：不能拿空文本去「续写」，改用 profile 的最大输出上限重来。"""
    monkeypatch.setenv("DS_KEY", "k")
    server.replies = [{"text": "", "reasoning": "想" * 50, "finish": "length"}, {"text": "写好了"}]
    llm = _llm({"protocol": "openai", "base_url": server.url + "/v1", "model": "m", "api_key_env": "DS_KEY", "max_retries": 0,
                "max_tokens": 8000})
    res = llm.complete("t", ["s"], "u", max_tokens=1000)
    assert res.text == "写好了"
    assert [r["body"]["max_tokens"] for r in server.requests] == [1000, 8000]
