"""消息推送模块：企业微信群机器人 / PushPlus / Server酱 / Bark。

推送失败统一抛 NotifyError；PUSH_CHANNEL=none 时跳过推送并返回 False。
"""
from __future__ import annotations

import httpx

from config import PushSettings

_CHANNEL_TIMEOUT = 10.0


class NotifyError(Exception):
    """推送失败。"""


def _post_json(url: str, payload: dict) -> dict:
    try:
        with httpx.Client(timeout=_CHANNEL_TIMEOUT) as client:
            resp = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise NotifyError(f"推送请求失败：{exc}") from exc
    if resp.status_code >= 400:
        raise NotifyError(f"推送返回 HTTP {resp.status_code}：{resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _send_wecom(push: PushSettings, title: str, content: str) -> bool:
    data = _post_json(push.wecom_webhook, {"msgtype": "text", "text": {"content": f"{title}\n{content}"}})
    if data.get("errcode", 0) != 0:
        raise NotifyError(f"企业微信返回错误：{data.get('errmsg', '未知错误')}")
    return True


def _send_pushplus(push: PushSettings, title: str, content: str) -> bool:
    data = _post_json(
        "https://www.pushplus.plus/send",
        {"token": push.pushplus_token, "title": title, "content": content, "template": "txt"},
    )
    if data.get("code") not in (200, "200"):
        raise NotifyError(f"PushPlus 返回错误：{data.get('msg', data)}")
    return True


def _send_serverchan(push: PushSettings, title: str, content: str) -> bool:
    url = f"https://sctapi.ftqq.com/{push.serverchan_key}.send"
    data = _post_json(url, {"title": title, "desp": content})
    if data and data.get("code", 0) != 0:
        raise NotifyError(f"Server酱返回错误：{data.get('message', data)}")
    return True


def _send_bark(push: PushSettings, title: str, content: str) -> bool:
    url = push.bark_url.rstrip("/")
    data = _post_json(url, {"title": title, "body": content, "group": "电费监控"})
    if data and data.get("code") != 200:
        raise NotifyError(f"Bark 返回错误：{data}")
    return True


_SENDERS = {
    "wecom": _send_wecom,
    "pushplus": _send_pushplus,
    "serverchan": _send_serverchan,
    "bark": _send_bark,
}


def send_notification(push: PushSettings, title: str, content: str) -> bool:
    """按配置渠道发送通知（支持逗号分隔多渠道并发/顺序发送）；全部失败时抛 NotifyError。"""
    if push.is_none:
        print("[INFO] PUSH_CHANNEL=none，跳过推送")
        return False

    any_ok = False
    errors: list[str] = []
    for ch in push.channels:
        if ch == "none":
            continue
        sender = _SENDERS.get(ch)
        if sender is None:
            errors.append(f"未支持的推送渠道：{ch}")
            continue
        try:
            if sender(push, title, content):
                any_ok = True
        except NotifyError as exc:
            errors.append(f"[{ch}] {exc}")

    if not any_ok and errors:
        raise NotifyError("；".join(errors))
    if errors:
        print(f"[WARN] 部分推送渠道失败：{'；'.join(errors)}")
    return any_ok
