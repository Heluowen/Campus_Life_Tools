"""燃气余额查询（港华燃气 vcc-cbs 接口，微信服务号「广州东永港华燃气」）。

协议要点（来自 water_2.chls 抓包）：
- 接口：GET https://weixin.towngasvcc.com/nv1/vcc-cbs/charge/preCheck?subsId=<subsId>
  （可选 timestamp/sign 参数；实测 sign 不校验，可省略）
- 鉴权：请求头 Authorization: Bearer <token>（微信 OAuth 发放，token 有效期未知）
- 响应：datas.savingSum 为气费余额（元，字符串），datas.readingRptList[0].currReading 为表读数
- 失效判断：HTTP 401/403、resultCode != 0 且提示 token/登录/请重新授权
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

GAS_HOST = "https://weixin.towngasvcc.com"
GAS_OAUTH = "https://weixin.towngasvcc.com/vcc-oauth"

_RETRY_DELAY = 2.0


class GasAuthExpiredError(Exception):
    """燃气 token 失效：HTTP 401/403 或业务提示需要重新授权/登录。"""


class GasQueryError(Exception):
    """燃气查询失败：网络异常、非预期状态码或响应解析失败。"""


@dataclass
class GasResult:
    balance: float  # 元
    subs_code: str  # 户号（脱敏展示用）
    meter_reading: str  # 表读数
    payload: dict  # 原始响应

    @property
    def balance_text(self) -> str:
        return f"{self.balance:.2f} 元"


def _parse_gas(resp: httpx.Response) -> GasResult:
    if resp.status_code in (401, 403):
        raise GasAuthExpiredError(f"HTTP {resp.status_code}（燃气 token 失效）")
    text = resp.text or ""
    try:
        payload = resp.json()
    except ValueError:
        snippet = text[:200].replace("\n", " ")
        if "登录" in text or "oauth" in text.lower():
            raise GasAuthExpiredError("响应提示需要重新登录/授权（GAS_TOKEN 可能已过期）")
        raise GasQueryError(f"响应不是合法 JSON（HTTP {resp.status_code}，内容片段：{snippet!r}）")

    result_code = str(payload.get("resultCode", "")).strip()
    if result_code not in ("0", "00"):
        msg = str(
            payload.get("errorMsg")
            or payload.get("errorTitle")
            or payload.get("resultMsg")
            or result_code
        )
        lowered = (result_code + " " + msg).lower()
        if any(k in lowered for k in ("token", "过期", "登录", "授权", "oauth", "401", "403")):
            raise GasAuthExpiredError(f"resultCode={result_code!r}，msg={msg!r}（GAS_TOKEN 已失效，需重新抓包）")
        raise GasQueryError(f"接口返回失败：resultCode={result_code!r}，msg={msg!r}")

    data = payload.get("datas")
    if not isinstance(data, dict):
        raise GasQueryError("响应缺少 datas 对象，无法解析气费余额")
    saving = data.get("savingSum")
    if saving is None or str(saving).strip() == "":
        raise GasQueryError("响应缺少 savingSum（气费余额）字段")
    try:
        balance = float(str(saving).replace(",", "").strip())
    except ValueError as exc:
        raise GasQueryError(f"savingSum 不是数字：{saving!r}") from exc

    reading = ""
    rr = data.get("readingRptList")
    if isinstance(rr, list) and rr and isinstance(rr[0], dict):
        reading = str(rr[0].get("currReading") or "")
    return GasResult(
        balance=balance,
        subs_code=str(data.get("subsCode") or ""),
        meter_reading=reading,
        payload=payload,
    )


def _sign_params(params: dict) -> str:
    """vcc-oauth / vcc-cbs 请求签名：md5(按 key 排序拼接 "key+value"(排除 sign/空值) + "hbasesoft.com-prod").upper()"""
    import hashlib

    parts = []
    for k in sorted(params.keys()):
        v = params[k]
        if k == "sign" or v is None or v == "":
            continue
        parts.append(f"{k}{v}")
    return hashlib.md5(("".join(parts) + "hbasesoft.com-prod").encode()).hexdigest().upper()


def _refresh_tokens(
    clientid: str, refresh_token: str, timeout: float, trust_env: bool
) -> tuple[str, str] | None:
    """用 vcc-oauth refreshToken 接口换取新的 (access_token, refresh_token)。

    实测正确姿势（2026-11-22 验证）：
    POST /vcc-oauth/oauth/authorize2/refreshToken?clientid=<ch>&timestamp=<ms>&sign=<md5>
    表单参数：refreshToken=<refresh_token>
    响应：{"access_token":..., "refresh_token":..., "expires_in":7200}（会轮换 refresh_token！）
    """
    import time

    if not (clientid and refresh_token):
        return None
    url = f"{GAS_OAUTH}/oauth/authorize2/refreshToken"
    ts = str(int(time.time() * 1000))
    params = {"clientid": clientid, "timestamp": ts}
    params["sign"] = _sign_params(params)
    try:
        with httpx.Client(timeout=timeout, trust_env=trust_env, follow_redirects=False) as c:
            r = c.post(url, params=params, data={"refreshToken": refresh_token})
        data = r.json()
    except Exception:  # noqa: BLE001 - 刷新尽力而为，失败交给上层提示重新抓包
        return None
    if not isinstance(data, dict):
        return None
    access = data.get("access_token") or (data.get("data") or {}).get("access_token")
    refresh = data.get("refresh_token") or (data.get("data") or {}).get("refresh_token")
    if not access:
        return None
    return str(access), str(refresh or refresh_token)


def query_gas(
    subsid: str,
    token: str,
    timeout: float,
    max_retries: int = 2,
    trust_env: bool = True,
    cookie: str = "",
    refresh_token: str = "",
    clientid: str = "",
    on_token_refreshed=None,
) -> GasResult:
    """查询燃气余额（元）。

    token 过期（20001）时：若配置了 refresh_token + clientid，先调 vcc-oauth
    refreshToken 换新 token 对（回调 on_token_refreshed(access, refresh) 持久化
    到 .env），再重试一次；刷新失败则抛 GasAuthExpiredError 提示重新抓包。
    """
    url = f"{GAS_HOST}/nv1/vcc-cbs/charge/preCheck?subsId={subsid}"
    attempts = 1 + max(0, min(int(max_retries), 2))

    def _call(tok: str) -> GasResult:
        headers = {"Authorization": f"Bearer {tok}", "Accept": "*/*"}
        if cookie:
            headers["Cookie"] = cookie
        last_error: GasQueryError | None = None
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=trust_env) as client:
            for attempt in range(attempts):
                try:
                    resp = client.get(url, headers=headers)
                except httpx.TimeoutException:
                    last_error = GasQueryError("燃气查询超时")
                except httpx.HTTPError as exc:
                    last_error = GasQueryError(f"燃气查询网络失败：{exc}")
                else:
                    return _parse_gas(resp)
                if attempt < attempts - 1:
                    time.sleep(_RETRY_DELAY)
        raise last_error or GasQueryError("燃气查询失败（未知原因）")

    try:
        return _call(token)
    except GasAuthExpiredError:
        pair = _refresh_tokens(clientid, refresh_token, timeout, trust_env)
        if not pair:
            raise
        new_access, new_refresh = pair
        if on_token_refreshed is not None:
            try:
                on_token_refreshed(new_access, new_refresh)
            except Exception:  # noqa: BLE001
                pass
        return _call(new_access)
