"""WebUI 的安全措施（只监听本机，但浏览器里的其他网站仍可能向 127.0.0.1 发请求）：

- Host 白名单（TrustedHostMiddleware，在 server.py 装配）：防 DNS rebinding。
- 写操作（非 GET）：请求带 Origin（包括 null）时必须与 Host 一致；另加双提交 CSRF token（HttpOnly + SameSite=Strict
  的 cookie，与表单隐藏字段或 X-CSRF-Token 请求头比对）。否则跨站页面把模型的 base_url 改成它的地址，API key 就会随请求外泄。
- 请求体必须带 Content-Length 且不超过上限（Starlette 对上传文件的总大小不设限）。
- 响应头：CSP（只允许本站脚本）、禁止被嵌入 iframe、Referrer-Policy: same-origin
  （no-referrer 会让同源表单 POST 发出 Origin: null）。
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request

CSRF_COOKIE = "stock_csrf"
CSRF_FIELD = "csrf_token"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
SANDBOX_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]"]


def csrf_token(request: Request) -> str:
    """当前请求的 CSRF token：已有 cookie 就沿用，否则生成新的（由中间件写回 cookie）。"""
    token = getattr(request.state, "csrf", None)
    if token is None:
        token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        request.state.csrf = token
    return token


async def security_middleware(request: Request, call_next):
    token = csrf_token(request)
    response = await call_next(request)
    if request.cookies.get(CSRF_COOKIE) != token:
        response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="strict", path="/")
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Cache-Control", "no-store")
    return response


def max_body(request: Request) -> int:
    holder = request.app.state.holder
    return (holder.app.settings.web.upload_max_mb + 1) * 1024 * 1024


async def verify_csrf(request: Request) -> None:
    """router 级依赖：非 GET 请求校验 Origin、请求体大小与 CSRF token。
    表单由 Starlette 缓存在同一个 Request 上，端点里再读 request.form() 不会重复解析。"""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None and (origin == "null" or urlparse(origin).netloc != request.headers.get("host", "")):
        raise HTTPException(403, "跨站请求被拒绝")
    length = request.headers.get("content-length")
    if length is None:
        raise HTTPException(411, "请求缺少 Content-Length")
    if int(length) > max_body(request):
        raise HTTPException(413, "上传内容超过大小上限（config.yaml 的 web.upload_max_mb）")
    sent = request.headers.get("x-csrf-token")
    if not sent and request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        sent = (await request.form()).get(CSRF_FIELD)
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not sent or not hmac.compare_digest(cookie, str(sent)):
        raise HTTPException(403, "安全校验失败（CSRF token 不匹配），请刷新页面后重试")
