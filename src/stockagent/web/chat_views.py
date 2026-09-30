"""报告研读对话的接口（报告页右侧栏用 fetch 调用，返回 JSON）。

提问只写库并把回答交给后台线程（app.state.chat_pool）；页面用 SSE（/chats/msg/{id}/events）实时接收回答过程：
思考、工具调用与结果、正文片段（纯文本，页面用 textContent 显示），结束时发送 final（服务端净化后的 HTML 与核对结果）。
回答来自 LLM（读过网页），最终渲染一律经 render.md_html 净化。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import chat
from ..utils import fmt_local
from .render import md_html
from .security import verify_csrf
from .views import company_or_404, holder

router = APIRouter(dependencies=[Depends(verify_csrf)])


def _web(data: dict) -> bool | None:
    v = data.get("web_search")
    return None if v is None else bool(v)


def _submit(request: Request, message_id: int, web: bool | None) -> None:
    chat.open_trace(message_id)  # 先建好事件流：页面立即连上时不会把「排队中」当成已中断
    request.app.state.chat_pool.submit(chat.answer, holder(request).app, message_id, web)


def message_json(m: dict) -> dict:
    out = {"id": m["id"], "role": m["role"], "status": m["status"], "progress": m["progress"], "quote": m["quote"],
           "at": fmt_local(m["created_at"])}
    if m["role"] == "assistant" and m["status"] == "done":
        out["html"] = str(md_html(m["content"]))
        out["checks"] = m["checks"]
        out["tools"] = len(m["tool_log"])
    else:
        out["text"] = m["content"]
    if m["role"] == "assistant":
        out["trace"] = m["trace"]
        out["model"] = m["model"]
    return out


def _chat_or_404(request: Request, chat_id: int) -> dict:
    c = holder(request).app.db.chat(chat_id)
    if c is None:
        raise HTTPException(404, "没有这个对话")
    return c


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "请求格式错误") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "请求格式错误")
    return data


@router.get("/c/{cid}/chats")
def chat_list(request: Request, cid: str, dossier: int | None = None, revision: int | None = None):
    app = holder(request).app
    company_or_404(app, cid)
    rows = [c for c in app.db.chats(cid) if (dossier is None or c["dossier_id"] == dossier)
            and (revision is None or c["revision_id"] == revision)]
    return {"chats": [{"id": c["id"], "title": c["title"], "dossier_id": c["dossier_id"], "revision_id": c["revision_id"],
                       "at": fmt_local(c["updated_at"])} for c in rows]}


@router.post("/c/{cid}/chats")
async def chat_start(request: Request, cid: str):
    app = holder(request).app
    c = company_or_404(app, cid)
    data = await _body(request)
    try:
        chat_id, mid = chat.start(app, c, int(data.get("dossier_id") or 0), str(data.get("question") or ""),
                                  str(data.get("quote") or "") or None, int(data.get("revision_id") or 0) or None)
    except (chat.ChatError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    _submit(request, mid, _web(data))
    return {"chat_id": chat_id, "message_id": mid}


@router.get("/chats/{chat_id}")
def chat_detail(request: Request, chat_id: int):
    c = _chat_or_404(request, chat_id)
    msgs = holder(request).app.db.chat_messages(chat_id)
    return {"id": c["id"], "title": c["title"], "dossier_id": c["dossier_id"], "messages": [message_json(m) for m in msgs]}


@router.post("/chats/{chat_id}/ask")
async def chat_ask(request: Request, chat_id: int):
    _chat_or_404(request, chat_id)
    data = await _body(request)
    try:
        mid = chat.ask(holder(request).app, chat_id, str(data.get("question") or ""), str(data.get("quote") or "") or None)
    except chat.ChatError as e:
        busy = "回答中" in str(e)
        return JSONResponse({"error": str(e)}, status_code=409 if busy else 400)
    _submit(request, mid, _web(data))
    return {"chat_id": chat_id, "message_id": mid}


@router.get("/chats/msg/{mid}")
def chat_message(request: Request, mid: int):
    m = holder(request).app.db.chat_message(mid)
    if m is None:
        raise HTTPException(404, "没有这条消息")
    return message_json(m)


def _sse(data: dict, event: str | None = None, eid: int | None = None) -> str:
    head = (f"id: {eid}\n" if eid is not None else "") + (f"event: {event}\n" if event else "")
    return head + f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/chats/msg/{mid}/events")
async def chat_events(request: Request, mid: int, after: int = 0):
    """回答过程的事件流（SSE）。after / Last-Event-ID：从第几个事件之后继续（断线重连）。
    回答不在本进程内进行（已结束，或服务重启过）时直接发送 final。"""
    db = holder(request).app.db
    if db.chat_message(mid) is None:
        raise HTTPException(404, "没有这条消息")
    last = request.headers.get("last-event-id")
    start = max(after, int(last) if last and last.isdigit() else 0)

    async def gen():
        i = start
        while True:
            trace = chat.get_trace(mid)
            if trace is None:
                break
            events = trace.events[i:]
            for ev in events:
                i += 1
                if ev["type"] != "done":
                    yield _sse(ev, eid=i)
            if trace.done_at is not None and i >= len(trace.events):
                break
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.1)
        m = db.chat_message(mid)
        if m is not None:
            yield _sse(message_json(m), event="final", eid=i)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/chats/{chat_id}/delete")
def chat_delete(request: Request, chat_id: int):
    _chat_or_404(request, chat_id)
    holder(request).app.db.delete_chat(chat_id)
    return {"ok": True}
