"""推送：飞书 / 企业微信 / Telegram 机器人。未配置的渠道自动跳过。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import requests

from .config import Settings, env
from .utils import log


def _truncate_bytes(s: str, limit: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[: limit - 20].decode("utf-8", errors="ignore") + "\n……（已截断）"


def channels(settings: Settings) -> list[str]:
    n = settings.notify
    out = []
    if env(n.feishu_webhook_env):
        out.append("飞书")
    if env(n.wecom_webhook_env):
        out.append("企业微信")
    if env(n.telegram_token_env) and n.telegram_chat_id:
        out.append("Telegram")
    return out


def missing(settings: Settings) -> list[str]:
    """配置了一半、因此不会推送的渠道（例如 Telegram 只有 token 没有 chat id）。"""
    n = settings.notify
    out = []
    if env(n.telegram_token_env) and not n.telegram_chat_id:
        out.append("Telegram 已设置 token，但还没有填写 chat id（设置 → 通用 → 推送）")
    if n.telegram_chat_id and not env(n.telegram_token_env):
        out.append(f"Telegram 已填写 chat id，但环境变量 {n.telegram_token_env} 里没有 token（设置 → 密钥）")
    if env(n.feishu_secret_env) and not env(n.feishu_webhook_env):
        out.append(f"飞书只设置了签名密钥，缺少 webhook（{n.feishu_webhook_env}）")
    return out


def push(settings: Settings, title: str, text: str) -> list[str]:
    """返回每个渠道的结果描述。"""
    n = settings.notify
    results = []

    if url := env(n.feishu_webhook_env):
        body: dict = {"msg_type": "text", "content": {"text": _truncate_bytes(f"{title}\n{text}", 29000)}}
        if secret := env(n.feishu_secret_env):
            ts = str(int(time.time()))
            sign = hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
            body.update(timestamp=ts, sign=base64.b64encode(sign).decode())
        results.append(_post("飞书", url, body, lambda d: d.get("code", d.get("StatusCode", -1)) == 0))

    if url := env(n.wecom_webhook_env):
        body = {"msgtype": "markdown", "markdown": {"content": _truncate_bytes(f"**{title}**\n{text}", 4000)}}
        results.append(_post("企业微信", url, body, lambda d: d.get("errcode", -1) == 0))

    if (token := env(n.telegram_token_env)) and n.telegram_chat_id:
        body = {"chat_id": n.telegram_chat_id, "text": f"{title}\n{text}"[:4000], "disable_web_page_preview": True}
        results.append(_post("Telegram", f"https://api.telegram.org/bot{token}/sendMessage", body,
                             lambda d: d.get("ok") is True))
    return results


def _post(name: str, url: str, body: dict, ok) -> str:
    try:
        r = requests.post(url, json=body, timeout=15)
        data = r.json() if r.content else {}
        if r.ok and ok(data):
            return f"已推送到{name}"
        log.warning("%s 推送失败：%s %s", name, r.status_code, str(data)[:200])
        return f"✗ {name}推送失败：{r.status_code} {str(data)[:120]}"
    except (requests.RequestException, ValueError) as e:
        return f"✗ {name}推送失败：{e}"
