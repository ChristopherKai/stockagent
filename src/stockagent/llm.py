"""统一的 LLM 调用层。

protocol=anthropic：官方 Anthropic SDK（Claude；base_url 可指向中转地址）
protocol=openai：   OpenAI 兼容接口（DeepSeek、通义千问、Kimi、OpenRouter、Ollama/vLLM 等）
protocol=mock：     离线模拟，用于测试和演练，不产生费用
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

from .config import LLMConfig, LLMProfile, TaskRoute
from .utils import iso, log, utcnow

ANTHROPIC_DEFAULT_URL = "https://api.anthropic.com"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _retryable_status(code: int) -> bool:
    """换一个模型可能成功的 HTTP 状态：余额不足、上游拒绝、超时、冲突、限流、服务端错误。"""
    return code in (402, 403, 408, 409, 429) or code >= 500


class LLMError(RuntimeError):
    """retryable=True 表示换一个模型可能成功（连接失败、5xx、限流、上游拒绝），会触发 fallback。"""

    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


@dataclass
class LLMResult:
    text: str
    data: dict | None
    model: str
    profile: str
    usage: dict


def json_instruction(schema: dict) -> str:
    return (
        "\n\n请只输出一个 JSON 对象（不要 Markdown 代码块，不要任何解释文字），必须符合以下 JSON Schema：\n"
        + json.dumps(schema, ensure_ascii=False)
    )


_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)

TOOL_PROTOCOL = """

## 工具
你可以调用下列工具（parameters 为 JSON Schema）：
{tools}

调用方法：输出一个或多个
<tool_call>
{{"name": "工具名", "arguments": {{参数}}}}
</tool_call>
然后停止输出，等待工具结果（会以 <tool_response> 返回）。信息足够后直接输出最终答案，不要再包含 <tool_call>。"""

FINISH_PROMPT = "工具调用次数已用完。请根据已经获得的信息直接写出最终答案；查不到的内容标注「未查到」。"
CONTINUE_PROMPT = "你的输出因长度上限被截断了。请从中断处直接继续写，不要重复已写内容，不要加开场白或总结。"


def strip_think(text: str) -> str:
    """去掉推理模型输出里的 <think>…</think>（有的服务只返回结束标签）。"""
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


_STREAM_TAGS = {"<think>": "thinking", "</think>": "text", "<tool_call>": "hidden", "</tool_call>": "text"}


class StreamSplitter:
    """把流式正文分成「思考」与「正文」事件（给界面实时展示用；最终答案仍按完整文本处理）。
    <think>…</think> 里的内容算思考；hide_tool_calls 时（文本工具协议）<tool_call>…</tool_call> 不展示。标签可能被拆在两个分片里。"""

    def __init__(self, emit: Callable[[dict], None], hide_tool_calls: bool = False):
        self.emit = emit
        self.hide = hide_tool_calls
        self.mode = "text"
        self.buf = ""

    def _out(self, text: str) -> None:
        if text and self.mode != "hidden":
            self.emit({"type": self.mode, "text": text})

    def feed(self, chunk: str) -> None:
        self.buf += chunk
        while self.buf:
            hits = [(i, t) for t in _STREAM_TAGS if (i := self.buf.find(t)) >= 0 and (self.hide or "tool_call" not in t)]
            if hits:
                i, tag = min(hits)
                self._out(self.buf[:i])
                self.mode = _STREAM_TAGS[tag]
                self.buf = self.buf[i + len(tag):]
                continue
            keep = max((k for t in _STREAM_TAGS for k in range(1, len(t)) if self.buf.endswith(t[:k])), default=0)  # 可能是半个标签
            self._out(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            return

    def flush(self) -> None:
        self._out(self.buf)
        self.buf = ""


def parse_tool_calls(text: str) -> list[tuple[str, dict]]:
    """解析正文中的 <tool_call>{...}</tool_call>（文本协议，或模型没走原生接口时）。"""
    calls = []
    for m in _TOOL_CALL_RE.finditer(text or ""):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("name"):
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append((str(obj["name"]), args if isinstance(args, dict) else {}))
    return calls


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象（兼容代码块包裹、前后多余文字、<think> 块）。"""
    s = strip_think(text)
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("输出中没有 JSON 对象") from None
        obj = json.loads(s[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层不是对象")
    return obj


class LLM:
    def __init__(self, cfg: LLMConfig, db=None):
        self.cfg = cfg
        self.db = db
        self._clients: dict[str, object] = {}
        self._lock = threading.Lock()
        self._down: dict[str, float] = {}  # profile → 暂停使用到何时（monotonic 秒）

    # ------------------------------------------------------------ 可用性

    def _profile_reason(self, name: str, prof: LLMProfile) -> str | None:
        if prof.protocol == "mock":
            return None
        if prof.api_key_env and not os.environ.get(prof.api_key_env):
            return f"环境变量 {prof.api_key_env} 未设置（profile: {name}，可写在 .env 中）"
        if prof.protocol == "anthropic" and not prof.api_key_env:
            if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                return f"profile {name} 未配置 api_key_env，且环境中没有 ANTHROPIC_API_KEY"
        return None

    def unavailable_reason(self, task: str) -> str | None:
        """任务的模型链（主模型 + fallback）中只要有一个配置齐全就返回 None。"""
        reasons = [self._profile_reason(n, self.cfg.profiles[n]) for n in self._chain(self.cfg.route(task)[0])]
        return None if any(r is None for r in reasons) else reasons[0]

    def describe(self, task: str) -> str:
        name, prof, route = self.cfg.route(task)
        s = f"{name} · {prof.protocol} · {prof.model}" + (f" · effort={route.effort or prof.effort}" if (route.effort or prof.effort) else "")
        chain = self._chain(name)
        if len(chain) > 1:
            s += "（备用 " + " → ".join(f"{n}:{self.cfg.profiles[n].model}" for n in chain[1:]) + "）"
        return s

    # ------------------------------------------------------------ fallback

    DOWN_SECONDS = 120

    def _chain(self, name: str) -> list[str]:
        chain: list[str] = []
        while name and name not in chain:
            chain.append(name)
            name = self.cfg.profiles[name].fallback
        return chain

    def _with_fallback(self, name: str, fn, use_fallback: bool = True):
        """fn(profile 名, profile) → 结果。主模型连接失败/5xx/限流/上游拒绝时依次改用 fallback，
        失败的模型 DOWN_SECONDS 秒内不再尝试。"""
        chain = self._chain(name) if use_fallback else [name]
        now = time.monotonic()
        # 暂停中的模型排到最后，作为其他模型都失败时的最后一试
        candidates = [n for n in chain if self._down.get(n, 0) <= now] + [n for n in chain if self._down.get(n, 0) > now]
        for i, pname in enumerate(candidates):
            try:
                return fn(pname, self.cfg.profiles[pname])
            except LLMError as e:
                if not e.retryable or i == len(candidates) - 1:
                    raise
                self._down[pname] = time.monotonic() + self.DOWN_SECONDS
                log.warning("[%s] 不可用，改用 %s：%s", pname, candidates[i + 1], str(e)[:160])
        raise AssertionError("unreachable")

    # ------------------------------------------------------------ 调用

    def complete(
        self,
        task: str,
        system: list[str],
        user: str,
        *,
        schema: dict | None = None,
        validate: Callable[[dict], dict] | None = None,
        max_tokens: int = 16000,
        profile: str | None = None,
    ) -> LLMResult:
        """system 按"稳定→较稳定"排列：最后一块会被标记为缓存断点。
        profile 可绕过任务路由直接指定（此时不走 fallback，便于测试某个模型）。"""
        name, _, route = self._resolve(task, profile)

        def attempt(pname: str, prof: LLMProfile) -> LLMResult:
            mt = min(route.max_tokens or max_tokens, prof.max_tokens)
            prompt = user
            for i in range(2):
                text, usage, model, truncated = self._call_logged(task, pname, prof, route, system, prompt, schema, mt)
                if truncated and schema is None:  # 文本：从中断处续写并拼接
                    text, usage = self._continue(task, pname, prof, route, system, prompt, text, usage, mt)
                elif truncated:  # JSON 无法续写：用该模型的最大输出上限重试一次
                    if mt < prof.max_tokens and i == 0:
                        log.warning("[%s] JSON 输出在 max_tokens=%d 处被截断，改用 %d 重试", pname, mt, prof.max_tokens)
                        mt = prof.max_tokens
                        continue
                    raise LLMError(f"[{pname}] JSON 输出达到 max_tokens={mt} 被截断，请调高该 profile 的 max_tokens")
                if schema is None:
                    return LLMResult(text=text, data=None, model=model, profile=pname, usage=usage)
                try:
                    data = extract_json(text)
                    if validate:
                        data = validate(data)
                    return LLMResult(text=text, data=data, model=model, profile=pname, usage=usage)
                except (ValueError, TypeError) as e:  # pydantic.ValidationError 是 ValueError 的子类
                    if i == 1:
                        raise LLMError(f"[{pname}] 模型输出无法解析：{str(e)[:300]}") from e
                    log.warning("[%s] 输出解析失败，重试一次：%s", pname, str(e)[:200])
                    prompt = user + f"\n\n（注意：上一次的输出无法解析：{str(e)[:300]}。请严格只输出一个合法 JSON 对象。）"
            raise LLMError(f"[{pname}] 模型输出无法解析")

        return self._with_fallback(name, attempt, use_fallback=profile is None)

    def _resolve(self, task: str, profile: str | None) -> tuple[str, LLMProfile, TaskRoute]:
        if profile is None:
            return self.cfg.route(task)
        if profile not in self.cfg.profiles:
            raise LLMError(f"没有名为 {profile} 的 profile")
        return profile, self.cfg.profiles[profile], TaskRoute(profile=profile)

    def profile_for(self, task: str) -> LLMProfile:
        return self.cfg.route(task)[1]

    def _logged(self, task: str, name: str, prof: LLMProfile, fn):
        """执行一次模型调用并记录用量；fn 返回 (结果, usage, model)。"""
        t0 = time.monotonic()
        rec = {"task": task, "profile": name, "model": prof.model, "started_at": iso(utcnow()), "ok": 0}
        try:
            result, usage, model = fn()
            rec.update(usage, model=model, ok=1)
            return result, usage, model
        except Exception as e:
            rec["error"] = str(e)[:500]
            raise
        finally:
            rec["duration_s"] = round(time.monotonic() - t0, 2)
            if self.db is not None:
                self.db.log_llm_call(rec)

    def _call_logged(self, task, name, prof, route, system, user, schema, max_tokens, history=None):
        """返回 (文本, usage, model, 是否因 max_tokens 截断)。history 为追加在首条 user 之后的消息（用于续写）。"""
        if prof.protocol == "anthropic":
            fn = lambda: self._anthropic(name, prof, route, system, user, schema, max_tokens, history)
        elif prof.protocol == "openai":
            fn = lambda: self._openai(name, prof, system, user, schema, max_tokens, history)
        else:
            fn = lambda: self._mock(system, user, schema)
        (text, truncated), usage, model = self._logged(task, name, prof, fn)
        return text, usage, model, truncated

    def _continue(self, task, name, prof, route, system, user, text, usage, max_tokens) -> tuple[str, dict]:
        """输出被 max_tokens 截断时，把已写部分作为 assistant 消息，请模型从中断处继续，最多 max_continuations 次。"""
        parts, total = [text], dict(usage)
        for _ in range(self.cfg.max_continuations):
            log.info("[%s] 输出达到 max_tokens=%d，续写第 %d 次", name, max_tokens, len(parts))
            history = [{"role": "assistant", "content": "".join(parts)}, {"role": "user", "content": CONTINUE_PROMPT}]
            more, u, _, truncated = self._call_logged(task, name, prof, route, system, user, None, max_tokens, history)
            parts.append(more)
            for k, v in u.items():
                total[k] = (total.get(k) or 0) + (v or 0)
            if not truncated:
                return "".join(parts), total
        log.warning("[%s] 续写 %d 次后仍未写完，保留已输出部分", name, self.cfg.max_continuations)
        return "".join(parts) + "\n\n（注意：内容过长，已续写多次仍未写完，以上为截断结果）", total

    # ------------------------------------------------------------ 带工具的 agent

    def run_agent(
        self,
        task: str,
        system: list[str],
        user: str,
        tools: list[dict],
        call_tool: Callable[[str, dict], str],
        *,
        max_steps: int = 12,
        max_tokens: int | None = None,
        profile: str | None = None,
        history: list[dict] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> LLMResult:
        """让模型在 max_steps 轮内调用工具，然后给出最终答案。
        tools: [{"name", "description", "parameters"(JSON Schema)}]；call_tool(name, args) 返回给模型看的文本。
        max_tokens：单次输出的需求；不给（None）表示调用方没有偏好，用该 profile 的 max_tokens；给了仍会被 profile 上限截断。
        history：多轮对话中之前的轮次 [{"role": "user"|"assistant", "content": 文本}]，放在本轮 user 之前（只含文本，不回放工具调用）。
        on_event：给出时改用流式请求，边生成边回调 {"type": "thinking"|"text", "text": 片段}（只用于展示；
        换用 fallback 模型前回调 {"type": "restart"}）。返回值与非流式相同。"""
        name, _, route = self._resolve(task, profile)
        prior = [{"role": m["role"], "content": m["content"]} for m in history or [] if m.get("content")]
        tries = 0

        def attempt(pname: str, prof: LLMProfile) -> LLMResult:
            nonlocal tries
            tries += 1
            if on_event and tries > 1:
                on_event({"type": "restart", "profile": pname})
            mt = min(route.max_tokens or max_tokens or prof.max_tokens, prof.max_tokens)
            if prof.protocol == "anthropic":
                return self._agent_anthropic(task, pname, prof, route, system, user, tools, call_tool, max_steps, mt, prior, on_event)
            if prof.protocol == "openai":
                return self._agent_openai(task, pname, prof, system, user, tools, call_tool, max_steps, mt, prior, on_event)
            return self._agent_mock(task, pname, prof, tools, call_tool, on_event)

        return self._with_fallback(name, attempt, use_fallback=profile is None)

    # 失败得这么快才值得重试（限流、瞬时 5xx）；耗了很久才失败（网关超时、读超时）重试只会再等一轮，直接换备用模型
    RETRY_FAST_S = 30.0

    def _openai_send(self, client, prof: LLMProfile, kwargs: dict, on_event):
        """发一次请求（带重试）。SDK 自带的重试固定对 5xx/超时再等满一轮（网关 2 分钟超时 ×4 = 8 分钟），
        所以客户端关掉它，由这里只对「很快就失败」的瞬时故障重试；504 与读超时不重试。"""
        import openai

        for attempt in range(prof.max_retries + 1):
            t0 = time.monotonic()
            try:
                if prof.stream or on_event is not None:
                    return self._openai_stream(client, prof, kwargs, on_event or (lambda ev: None))
                resp = client.chat.completions.create(**kwargs)
                u = resp.usage
                usage = {"input_tokens": getattr(u, "prompt_tokens", None), "output_tokens": getattr(u, "completion_tokens", None),
                         "cache_read": 0, "cache_write": 0}
                return resp, usage, resp.model or prof.model
            except (openai.RateLimitError, openai.APIStatusError, openai.APIConnectionError) as e:
                status = getattr(e, "status_code", None)
                transient = (isinstance(e, openai.RateLimitError) or status in (408, 409, 500, 502, 503)
                             or (isinstance(e, openai.APIConnectionError) and not isinstance(e, openai.APITimeoutError)))
                if not transient or attempt >= prof.max_retries or time.monotonic() - t0 > self.RETRY_FAST_S:
                    raise
                wait = min(0.5 * 2 ** attempt, 8.0)
                try:
                    wait = min(max(float(e.response.headers.get("retry-after", wait)), 0.0), 30.0)  # type: ignore[union-attr]
                except (AttributeError, ValueError):
                    pass
                log.info("[%s] 请求失败（%s），%.1f 秒后重试 %d/%d", prof.model, type(e).__name__, wait, attempt + 1, prof.max_retries)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _openai_create(self, name: str, prof: LLMProfile, kwargs: dict, on_event=None):
        """默认流式请求（prof.stream），分片拼成与非流式相同形状的结果（choices[0].message / finish_reason）：
        网关常对非流式请求设总时限（实测 2 分钟，长输出必超时），流式则只要持续有输出就不会断。on_event 给出时回调增量。"""
        import httpx
        import openai

        client = self._client(name, prof)
        try:
            resp, usage, model = self._openai_send(client, prof, kwargs, on_event)
            if not resp.choices:
                raise LLMError(f"[{name}] 返回为空")
            return resp, usage, model
        except openai.AuthenticationError as e:
            raise LLMError(f"[{name}] 认证失败，请检查 API key：{e.message}") from e
        except openai.NotFoundError as e:
            raise LLMError(f"[{name}] 模型或地址不存在（model={prof.model}, base_url={prof.base_url}）：{e.message}") from e
        except openai.BadRequestError as e:
            raise LLMError(f"[{name}] 请求被拒绝：{e.message}（若服务不支持工具调用，把 tool_mode 设为 text；不支持 JSON 模式则把 json_mode 设为 false；"
                           f"不支持流式则把 stream 设为 false）") from e
        except openai.RateLimitError as e:
            raise LLMError(f"[{name}] 触发限流：{e.message}", retryable=True) from e
        except openai.APIStatusError as e:
            raise LLMError(f"[{name}] API 错误 {e.status_code}：{e.message}", retryable=_retryable_status(e.status_code)) from e
        except openai.APIConnectionError as e:
            raise LLMError(f"[{name}] 网络连接失败：{e}", retryable=True) from e
        except (openai.APIError, httpx.HTTPError) as e:  # 流式输出中途断开
            raise LLMError(f"[{name}] 接口中断：{e}", retryable=True) from e

    @staticmethod
    def _openai_stream(client, prof: LLMProfile, kwargs: dict, on_event):
        stream = client.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
        split = StreamSplitter(on_event, hide_tool_calls=prof.tool_mode != "native")
        content: list[str] = []
        calls: dict[int, dict] = {}
        finish, model, u = None, prof.model, None
        for chunk in stream:
            model = getattr(chunk, "model", None) or model
            u = getattr(chunk, "usage", None) or u
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            d = choice.delta
            extra = getattr(d, "model_extra", None) or {}
            thinking = extra.get("reasoning_content") or extra.get("reasoning")  # DeepSeek、Qwen 等把思考放在单独字段
            if isinstance(thinking, str) and thinking:
                on_event({"type": "thinking", "text": thinking})
            if d.content:
                content.append(d.content)
                split.feed(d.content)
            for tc in d.tool_calls or []:
                slot = calls.setdefault(tc.index or 0, {"id": None, "name": "", "arguments": ""})
                slot["id"] = tc.id or slot["id"]
                if tc.function is not None:
                    if tc.function.name and tc.function.name != slot["name"]:
                        slot["name"] += tc.function.name
                    slot["arguments"] += tc.function.arguments or ""
            finish = choice.finish_reason or finish
        split.flush()
        tool_calls = [SimpleNamespace(id=c["id"] or f"call_{i}", function=SimpleNamespace(name=c["name"], arguments=c["arguments"] or "{}"))
                      for i, c in sorted(calls.items())]
        message = SimpleNamespace(content="".join(content), tool_calls=tool_calls or None)
        resp = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], model=model)
        usage = {"input_tokens": getattr(u, "prompt_tokens", None), "output_tokens": getattr(u, "completion_tokens", None),
                 "cache_read": 0, "cache_write": 0}
        return resp, usage, model

    def _agent_openai(self, task, name, prof: LLMProfile, system, user, tools, call_tool, max_steps, max_tokens,
                      history: list[dict] | None = None, on_event=None) -> LLMResult:
        native = prof.tool_mode == "native"
        sys_text = "\n\n".join(s for s in system if s)
        if not native:
            sys_text += TOOL_PROTOCOL.format(tools=json.dumps(tools, ensure_ascii=False))
        messages: list[dict] = [{"role": "system", "content": sys_text}, *(history or []), {"role": "user", "content": user}]
        oa_tools = [{"type": "function", "function": t} for t in tools]
        totals = {"input_tokens": 0, "output_tokens": 0}
        model = prof.model
        tool_rounds = conts = empties = 0
        acc, finishing = "", False
        while True:
            no_tools = tool_rounds >= max_steps
            if no_tools and not finishing:
                messages.append({"role": "user", "content": FINISH_PROMPT})
                finishing = True
            kwargs: dict = {"model": prof.model, "messages": messages, prof.max_tokens_param: max_tokens}
            if prof.temperature is not None:
                kwargs["temperature"] = prof.temperature
            if prof.extra_body:
                kwargs["extra_body"] = prof.extra_body
            if native:  # 不再允许调用工具时仍带上定义（历史里有工具调用），但 tool_choice 设为 none
                kwargs["tools"] = oa_tools
                kwargs["tool_choice"] = "none" if no_tools else "auto"
            resp, usage, model = self._logged(task, name, prof, lambda kw=kwargs: self._openai_create(name, prof, kw, on_event))
            for k in totals:
                totals[k] += usage.get(k) or 0
            choice = resp.choices[0]
            msg = choice.message
            content = strip_think(msg.content or "")
            tool_calls = list(msg.tool_calls or []) if native and not no_tools else []
            if tool_calls:
                tool_rounds += 1
                messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                    for tc in tool_calls]})
                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        result = call_tool(tc.function.name, args if isinstance(args, dict) else {})
                    except json.JSONDecodeError as e:
                        result = f"错误：参数不是合法 JSON（{e}）"
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue
            text_calls = [] if no_tools else parse_tool_calls(content)
            if text_calls:
                tool_rounds += 1
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": "\n".join(
                    f'<tool_response name="{n}">\n{call_tool(n, a)}\n</tool_response>' for n, a in text_calls)})
                continue
            final = _TOOL_CALL_RE.sub("", content).strip()
            if choice.finish_reason == "length":  # 最终答案被截断：从中断处续写
                if final and conts < self.cfg.max_continuations:
                    acc += final
                    conts += 1
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": CONTINUE_PROMPT})
                    continue
                if not (acc + final):
                    raise LLMError(f"[{name}] 输出达到 max_tokens={max_tokens} 被截断，请调高该 profile 的 max_tokens")
                final += "\n\n（注意：以上内容因输出长度上限被截断）"
            if acc + final:
                return LLMResult(text=acc + final, data=None, model=model, profile=name, usage=totals)
            empties += 1
            if finishing or empties > 2:
                raise LLMError(f"[{name}] 模型没有给出最终答案")
            messages.append({"role": "user", "content": "请继续：需要资料就调用工具，否则直接给出最终答案。"})

    def _agent_anthropic(self, task, name, prof: LLMProfile, route: TaskRoute, system, user, tools, call_tool,
                         max_steps, max_tokens, history: list[dict] | None = None, on_event=None) -> LLMResult:
        import anthropic
        import httpx

        client = self._client(name, prof)
        sys_blocks = [{"type": "text", "text": s} for s in system if s]
        if prof.prompt_cache and sys_blocks:
            sys_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        base: dict = {"model": prof.model, "max_tokens": min(max_tokens, 16000), "system": sys_blocks,
                      "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]}
        effort = route.effort or prof.effort
        if effort:
            base["output_config"] = {"effort": effort}
        if prof.thinking == "adaptive":
            base["thinking"] = {"type": "adaptive"}
        if prof.temperature is not None:
            base["temperature"] = prof.temperature
        messages: list[dict] = [*(history or []), {"role": "user", "content": user}]
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
        model = prof.model

        def create(kwargs):
            try:
                if on_event is not None:
                    msg = stream(kwargs)
                elif prof.refusal_fallback:
                    msg = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
                else:
                    msg = client.messages.create(**kwargs)
            except anthropic.APIStatusError as e:
                raise LLMError(f"[{name}] API 错误 {e.status_code}：{e.message}", retryable=_retryable_status(e.status_code)) from e
            except anthropic.APIConnectionError as e:
                raise LLMError(f"[{name}] 网络连接失败：{e}", retryable=True) from e
            except (anthropic.APIError, httpx.HTTPError) as e:  # 流式输出中途断开
                raise LLMError(f"[{name}] 接口中断：{e}", retryable=True) from e
            u = msg.usage
            usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                     "cache_read": getattr(u, "cache_read_input_tokens", None) or 0,
                     "cache_write": getattr(u, "cache_creation_input_tokens", None) or 0}
            return msg, usage, msg.model

        def stream(kwargs):
            ctx = (client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs) if prof.refusal_fallback
                   else client.messages.stream(**kwargs))
            with ctx as s:
                for ev in s:
                    if ev.type != "content_block_delta":
                        continue
                    if ev.delta.type == "text_delta":
                        on_event({"type": "text", "text": ev.delta.text})
                    elif ev.delta.type == "thinking_delta":
                        on_event({"type": "thinking", "text": ev.delta.thinking})
                return s.get_final_message()

        tool_rounds = conts = 0
        acc, finishing = "", False
        while True:
            no_tools = tool_rounds >= max_steps
            if no_tools and not finishing:
                messages.append({"role": "user", "content": FINISH_PROMPT})
                finishing = True
            kwargs = dict(base, messages=messages)
            if no_tools:
                kwargs["tool_choice"] = {"type": "none"}
            msg, usage, model = self._logged(task, name, prof, lambda kw=kwargs: create(kw))
            for k in totals:
                totals[k] += usage.get(k) or 0
            if msg.stop_reason == "refusal":
                raise LLMError(f"[{name}] 模型拒绝回答")
            messages.append({"role": "assistant", "content": msg.content})  # 原样回传（含 thinking 块）
            uses = [b for b in msg.content if b.type == "tool_use"]
            if not uses or no_tools:
                text = "".join(b.text for b in msg.content if b.type == "text").strip()
                if msg.stop_reason == "max_tokens":
                    if text and conts < self.cfg.max_continuations:
                        acc += text
                        conts += 1
                        messages.append({"role": "user", "content": CONTINUE_PROMPT})
                        continue
                    if not (acc + text):
                        raise LLMError(f"[{name}] 输出达到 max_tokens 被截断")
                    text += "\n\n（注意：以上内容因输出长度上限被截断）"
                return LLMResult(text=acc + text, data=None, model=model, profile=name, usage=totals)
            tool_rounds += 1
            results = []
            for b in uses:
                r = call_tool(b.name, b.input if isinstance(b.input, dict) else {})
                block = {"type": "tool_result", "tool_use_id": b.id, "content": r}
                if r.startswith("错误"):
                    block["is_error"] = True
                results.append(block)
            messages.append({"role": "user", "content": results})

    def _agent_mock(self, task, name, prof, tools, call_tool, on_event=None) -> LLMResult:
        """模拟：调用一次 calculate（如有）后给出固定答案，用于测试流程。"""
        text = "（模拟研究笔记，未调用真实模型）"
        if on_event:
            on_event({"type": "thinking", "text": "（模拟思考）"})
        if any(t["name"] == "calculate" for t in tools):
            call_tool("calculate", {"code": "1 + 1"})
        self._logged(task, name, prof, lambda: (None, {"input_tokens": 0, "output_tokens": 0}, "mock"))
        if on_event:
            for i in range(0, len(text), 6):
                on_event({"type": "text", "text": text[i:i + 6]})
        return LLMResult(text=text, data=None, model="mock", profile=name, usage={"input_tokens": 0, "output_tokens": 0})

    def _client(self, name: str, prof: LLMProfile):
        with self._lock:
            if name in self._clients:
                return self._clients[name]
            key = os.environ.get(prof.api_key_env) if prof.api_key_env else None
            if prof.api_key_env and not key:
                raise LLMError(f"环境变量 {prof.api_key_env} 未设置（profile: {name}）")
            if prof.protocol == "anthropic":
                import anthropic

                # base_url 总是显式传入，避免被环境里的 ANTHROPIC_BASE_URL 意外改写
                kwargs = {
                    "base_url": prof.base_url or ANTHROPIC_DEFAULT_URL,
                    "max_retries": prof.max_retries,
                    "timeout": prof.timeout,
                }
                if key:
                    kwargs["auth_token" if prof.auth == "bearer" else "api_key"] = key
                try:
                    client = anthropic.Anthropic(**kwargs)
                except Exception as e:
                    raise LLMError(f"[{name}] 初始化 Anthropic 客户端失败：{e}") from e
            elif prof.protocol == "openai":
                import openai

                client = openai.OpenAI(
                    api_key=key or "not-needed",  # 本地模型服务通常不校验 key
                    base_url=prof.base_url,
                    max_retries=0,  # 重试由 _openai_send 控制（SDK 的重试会对网关超时再等满一轮）
                    timeout=prof.timeout,
                )
            else:
                client = None
            self._clients[name] = client
            return client

    def _anthropic(self, name, prof: LLMProfile, route: TaskRoute, system, user, schema, max_tokens, history=None):
        import anthropic

        client = self._client(name, prof)
        sys_blocks = [{"type": "text", "text": s} for s in system if s]
        if prof.prompt_cache and sys_blocks:
            sys_blocks[-1]["cache_control"] = {"type": "ephemeral"}
        content = user if (schema is None or prof.structured_output) else user + json_instruction(schema)
        kwargs: dict = {
            "model": prof.model,
            "max_tokens": max_tokens,
            "system": sys_blocks,
            "messages": [{"role": "user", "content": content}, *(history or [])],
        }
        output_config: dict = {}
        effort = route.effort or prof.effort
        if effort:
            output_config["effort"] = effort
        if schema is not None and prof.structured_output:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config
        if prof.thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
        if prof.temperature is not None:
            kwargs["temperature"] = prof.temperature

        hint = "；若使用中转地址，可尝试把该 profile 的 structured_output / refusal_fallback 设为 false，或 thinking 设为 off"
        try:
            if prof.refusal_fallback:
                with client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs) as stream:
                    msg = stream.get_final_message()
            else:
                with client.messages.stream(**kwargs) as stream:
                    msg = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError(f"[{name}] 认证失败，请检查 API key：{e.message}") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"[{name}] 无权限或上游拒绝：{e.message}", retryable=True) from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"[{name}] 模型或地址不存在（model={prof.model}, base_url={prof.base_url or ANTHROPIC_DEFAULT_URL}）：{e.message}") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"[{name}] 请求被拒绝：{e.message}{hint}") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"[{name}] 触发限流（已自动重试 {prof.max_retries} 次）：{e.message}", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"[{name}] API 错误 {e.status_code}：{e.message}", retryable=_retryable_status(e.status_code)) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"[{name}] 网络连接失败：{e}", retryable=True) from e

        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise LLMError(f"[{name}] 模型拒绝回答（category={cat}）")
        text = "".join(b.text for b in msg.content if b.type == "text")
        u = msg.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", None) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", None) or 0,
        }
        return (text, msg.stop_reason == "max_tokens"), usage, msg.model

    def _openai(self, name, prof: LLMProfile, system, user, schema, max_tokens, history=None):
        content = user + (json_instruction(schema) if schema is not None else "")
        kwargs: dict = {
            "model": prof.model,
            "messages": [
                {"role": "system", "content": "\n\n".join(s for s in system if s)},
                {"role": "user", "content": content},
                *(history or []),
            ],
            prof.max_tokens_param: max_tokens,
        }
        if prof.temperature is not None:
            kwargs["temperature"] = prof.temperature
        if schema is not None and prof.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if prof.extra_body:
            kwargs["extra_body"] = prof.extra_body
        resp, usage, model = self._openai_create(name, prof, kwargs)
        choice = resp.choices[0]
        return (strip_think(choice.message.content or ""), choice.finish_reason == "length"), usage, model

    # ------------------------------------------------------------ 模拟

    def _mock(self, system, user, schema):
        usage = {"input_tokens": len(user) // 2, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
        if schema is None:
            first = user.strip().splitlines()[0][:40] if user.strip() else ""
            template = "\n".join(system)
            heads = re.findall(r"^## § .+$", template, re.M)
            if heads:  # 要求按章节输出（如深度报告）：按模板给出同样结构的占位文字
                title = "# 模拟报告：未调用真实模型\n\n" if re.search(r"^# \S", template, re.M) else ""
                body = "\n\n".join(f"{h}\n（模拟输出，未调用真实模型）" + "占位文字。" * 60 for h in heads)
                return (title + body, False), usage, "mock"
            return (f"（模拟输出，未调用真实模型）{first}", False), usage, "mock"
        ids = re.findall(r"id=([0-9a-f]{8,40})", user)
        return (json.dumps(_mock_value(schema, ids, ""), ensure_ascii=False), False), usage, "mock"


def _mock_value(schema: dict, ids: list[str], seed: str):
    t = schema.get("type")
    if "enum" in schema:
        opts = schema["enum"]
        return opts[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(opts)] if seed else opts[0]
    if t == "object":
        return {k: _mock_value(v, ids, seed + k) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        item = schema.get("items", {})
        if item.get("type") == "object" and "id" in item.get("properties", {}) and ids:
            out = []
            for i in ids:
                v = _mock_value(item, [], i)
                v["id"] = i
                out.append(v)
            return out
        return [_mock_value(item, [], seed + "0")]
    if t == "boolean":
        return True
    if t in ("integer", "number"):
        return 1
    return "（模拟）"
