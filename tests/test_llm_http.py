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


class Recorder:
    def __init__(self):
        self.requests: list[dict] = []
        self.reply_text = '{"ok": true}'
        self.stop_reason = "end_turn"


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
