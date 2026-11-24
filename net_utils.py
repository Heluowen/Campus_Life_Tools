"""公共 HTTP 弹性请求辅助：解决代理/VPN 切换引发的 SSL EOF 与连接闪断。

策略：
1. 优先按配置 trust_env 发送请求，并在网络异常 / 超时 / 5xx 时按 max_retries 重试；
2. 当检测到明显属于代理/TLS 链路故障（ConnectError / ProxyError / SSL EOF / 连接被重置）时，
   若尚未尝试相反的 trust_env 模式，自动回退用 `not trust_env` 探测一次：
   - 例如原本 TRUST_ENV=1 但系统代理挂了 → 自动切直连（trust_env=False）自救；
   - 例如原本 TRUST_ENV=0 但当前环境必须经代理出网 → 自动切系统代理（trust_env=True）自救。
3. 可通过 `.env` 的 `AUTO_PROXY_FALLBACK=0` 关闭该行为（默认开启）。
"""
from __future__ import annotations

import os
import time
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")

_RETRY_DELAY = 2.0


def _auto_fallback_enabled() -> bool:
    raw = (os.getenv("AUTO_PROXY_FALLBACK") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def is_transport_glitch(exc: BaseException) -> bool:
    """判断是否属于代理/TLS/连接层闪断（值得尝试切换 trust_env 回退）。"""
    if isinstance(exc, (httpx.ConnectError, httpx.ProxyError, httpx.RemoteProtocolError, httpx.ReadError)):
        return True
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "10054",
            "connection reset",
            "proxy",
            "ssl",
        )
    )


def build_client(
    *,
    timeout: float,
    trust_env: bool,
    follow_redirects: bool = True,
    cookies: dict | None = None,
) -> httpx.Client:
    """构造带独立连接关闭头与保守连接池的 httpx.Client，降低复用陈旧 TLS 连接触发 EOF 的概率。"""
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=4)
    kwargs: dict = {
        "timeout": timeout,
        "follow_redirects": follow_redirects,
        "trust_env": trust_env,
        "limits": limits,
    }
    if cookies is not None:
        kwargs["cookies"] = cookies
    return httpx.Client(**kwargs)


def run_with_fallback(
    fn: Callable[[bool], T],
    *,
    trust_env: bool,
    allow_fallback: bool | None = None,
) -> T:
    """执行一次带 trust_env 自动回退的请求调用。

    fn(use_trust_env) 需在发生网络层错误时抛出带原始 __cause__ 的异常或 httpx.HTTPError。
    """
    enabled = _auto_fallback_enabled() if allow_fallback is None else allow_fallback
    try:
        return fn(trust_env)
    except Exception as first_exc:
        if not enabled:
            raise
        root = first_exc.__cause__ or first_exc
        if not (is_transport_glitch(first_exc) or is_transport_glitch(root)):
            raise
        try:
            return fn(not trust_env)
        except Exception:
            # 回退路径也失败时，保留首次错误上下文抛出（更贴合用户配置）
            raise first_exc from None


def sleep_retry(delay: float = _RETRY_DELAY) -> None:
    time.sleep(delay)
