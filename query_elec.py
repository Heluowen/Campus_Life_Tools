"""核心电量查询与响应解析模块（基于 TXCODE=YJF004 真实抓包协议）。

协议要点（详见 campus_electricity_capture_spec.md）：
- 网关固定：POST https://app.xiaoyuan.ccb.com/LHECISM/B2CMainPlat_00
- 表单编码 application/x-www-form-urlencoded，无加密、无动态签名，可直接重放
- 表单参数：TXCODE=YJF004 固定；USERID / SKEY / roomid 来自 .env；PayType=elec
- 响应 JSON：status=="0" 成功；data.bal 为剩余电量字符串；data.meterflag 为计量单位
- 会话：每次响应 Set-Cookie 会更新 CCBIBS1 / IN001，必须复用同一个 httpx.Client

若接口迁移或字段变化，优先修改本文件顶部的网关/交易码/UA/Referer 常量。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import ApiSettings

GATEWAY_URL = "https://app.xiaoyuan.ccb.com/LHECISM/B2CMainPlat_00"
TXCODE_BALANCE = "YJF004"
PAY_TYPE = "elec"
# 平台公共表单字段（真实抓包 test_6.chls 中每条请求都携带）
BRANCHID = "555000000"
SERVLET_NAME = "B2CMainPlat_00"
CCB_IBS_VERSION = "V6"
PT_STYLE = "10"
ORIGIN = "https://app.xiaoyuan.ccb.com"
REFERER = "https://app.xiaoyuan.ccb.com/EMTSTATIC/DZK2026111301/index2026111301.html"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
    "NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF"
)

_RETRY_DELAY = 2.0  # 重试间隔（秒），避免连续请求触发接口风控
_AUTH_MARKERS = ("登录", "失效", "过期", "未登录", "session", "token", "logout")


class AuthExpiredError(Exception):
    """登录态失效：HTTP 401/403、返回登录页 HTML、或业务提示登录失效。"""


class QueryError(Exception):
    """查询失败：网络异常、非预期状态码或响应解析失败。"""


@dataclass
class QueryResult:
    balance: float
    unit: str  # 来自响应 data.meterflag，如 "度"
    room_id: str  # 响应 data.roomid（用于校对配置）
    payload: dict  # 原始响应，仅用于本地排错，不进入推送内容

    @property
    def balance_text(self) -> str:
        return f"{self.balance:.2f} {self.unit}".strip()


def _gateway_url(api: ApiSettings) -> str:
    """优先使用配置/环境变量中的网关覆盖地址（仅用于接口迁移或本地联调）。"""
    configured = (api.gateway_url or os.getenv("CCB_GATEWAY_URL") or "").strip()
    return configured or GATEWAY_URL


def _build_headers(api: ApiSettings) -> dict:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "User-Agent": api.user_agent or DEFAULT_USER_AGENT,
    }
    return headers


def _parse_cookie_str(cookie_str: str) -> dict:
    """把 "a=1; b=2" 形式的 Cookie 字符串解析成字典。"""
    out = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        if sep and name.strip():
            out[name.strip()] = value
    return out


def _load_cookie_store(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_cookie_store(path: Path | None, cookies) -> None:
    if not path:
        return
    # httpx Cookies 可能包含同名不同域的条目，dict() 会抛 CookieConflict，改为遍历 CookieJar
    pairs = {}
    try:
        for c in cookies.jar:
            pairs[c.name] = c.value
    except AttributeError:
        return
    try:
        path.write_text(json.dumps(pairs, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass


def _build_form(api: ApiSettings) -> dict:
    # 平台公共字段 + YJF004 业务字段（与真实抓包完全一致，seq 见 test_6.chls）
    return {
        "BRANCHID": BRANCHID,
        "SERVLET_NAME": SERVLET_NAME,
        "CCB_IBSVersion": CCB_IBS_VERSION,
        "PT_STYLE": PT_STYLE,
        "TXCODE": TXCODE_BALANCE,
        "SKEY": api.skey,
        "USERID": api.userid,
        "aid": "",
        "roomid": api.room_id,
        "PayType": PAY_TYPE,
    }


def _looks_like_login_page(text: str) -> bool:
    lowered = text.lower()
    if "登录" not in text and "login" not in lowered:
        return False
    return any(tag in lowered for tag in ("<html", "<form", "<script"))


def _clean_html_text(html: str) -> str:
    """粗略提取 HTML 页面的可见文字，用于把服务器提示透传到报错里。"""
    import re

    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _html_error_detail(text: str) -> str:
    detail = _clean_html_text(text)
    return f"（服务器提示：{detail[:120]}）" if detail else ""


def _msg_looks_auth(msg: str) -> bool:
    lowered = msg.lower()
    return any(marker in msg or marker in lowered for marker in _AUTH_MARKERS)


def _parse_response(resp: httpx.Response) -> QueryResult:
    if resp.status_code in (401, 403):
        raise AuthExpiredError(f"HTTP {resp.status_code}（登录态失效）")
    text = resp.text or ""
    try:
        payload = resp.json()
    except ValueError:
        if _looks_like_login_page(text):
            raise AuthExpiredError(f"接口返回登录页 HTML（Cookie/SKEY 可能已失效）{_html_error_detail(text)}")
        snippet = text[:200].replace("\n", " ")
        raise QueryError(f"响应不是合法 JSON（HTTP {resp.status_code}，内容片段：{snippet!r}）")

    if not isinstance(payload, dict):
        raise QueryError("响应 JSON 顶层不是对象，无法解析余额")

    status = str(payload.get("status", "")).strip()
    msg = str(payload.get("msg", "")).strip()
    if status != "0":
        if _msg_looks_auth(msg):
            raise AuthExpiredError(f"status={status!r}，msg={msg!r}（疑似登录失效或会话过期）")
        raise QueryError(f"接口返回失败：status={status!r}，msg={msg!r}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise QueryError("响应缺少 data 对象，无法解析余额")

    bal_raw = data.get("bal")
    if bal_raw is None or str(bal_raw).strip() == "":
        raise QueryError("data.bal 为空，请检查响应结构是否与抓包一致")
    try:
        balance = float(str(bal_raw).replace(",", "").strip())
    except ValueError as exc:
        raise QueryError(f"data.bal 不是数字：{bal_raw!r}") from exc

    return QueryResult(
        balance=balance,
        unit=str(data.get("meterflag") or "").strip(),
        room_id=str(data.get("roomid") or "").strip(),
        payload=payload,
    )


def query_balance(
    api: ApiSettings,
    timeout: float,
    max_retries: int,
    trust_env: bool = True,
    cookie_store_path: Path | None = None,
) -> QueryResult:
    """发送 YJF004 查询并解析余额。

    - 网络异常 / 超时 / 5xx：按 max_retries 重试（最多 2 次）；
    - 401/403、登录页 HTML、业务提示登录失效：抛 AuthExpiredError；
    - 其余解析失败：抛 QueryError，不重试。

    Cookie 策略：
    1. 优先使用轮换持久化文件（cookies.json）中较新的会话 Cookie 覆盖 .env 旧值；
    2. 若本次请求被判定登录失效，而持久化文件与 .env 的 Cookie 不同（说明用户
       重新抓包更新了 .env），自动回退用 .env 的新 Cookie 重试一次，成功后重建
       持久化文件。这样「重新抓包」和「轮换文件」可以共存。
    """
    gateway = _gateway_url(api)
    headers = _build_headers(api)
    form = _build_form(api)
    attempts = 1 + max(0, min(int(max_retries), 2))
    last_error: QueryError | None = None

    env_cookies = _parse_cookie_str(api.cookie)
    store = _load_cookie_store(cookie_store_path)
    merged_cookies = dict(env_cookies)
    if store:
        merged_cookies.update(store)

    def _do_query(query_cookies: dict) -> QueryResult:
        inner_error: QueryError | None = None
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=trust_env, cookies=query_cookies) as client:
            for attempt in range(attempts):
                try:
                    resp = client.post(gateway, headers=headers, data=form)
                except httpx.TimeoutException:
                    inner_error = QueryError(f"请求超时（>{timeout:g} 秒）")
                except httpx.HTTPError as exc:
                    inner_error = QueryError(f"网络请求失败：{exc}")
                else:
                    _save_cookie_store(cookie_store_path, client.cookies)
                    if resp.status_code >= 500:
                        inner_error = QueryError(f"服务端错误 HTTP {resp.status_code}")
                    else:
                        return _parse_response(resp)
                if attempt < attempts - 1:
                    time.sleep(_RETRY_DELAY)
        raise inner_error or QueryError("查询失败（未知原因）")

    try:
        return _do_query(merged_cookies)
    except AuthExpiredError:
        if store and env_cookies != merged_cookies:
            # 持久化 Cookie 失效但 .env 有新值（用户重新抓包过）：用 .env 重试
            return _do_query(dict(env_cookies))
        raise
