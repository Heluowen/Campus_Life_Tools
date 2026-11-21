"""配置加载与校验模块。

所有敏感信息（USERID / SKEY / Cookie / Webhook）必须来自 .env 或环境变量，禁止硬编码。
接口协议见 campus_electricity_capture_spec.md：
- 网关：POST https://app.xiaoyuan.ccb.com/LHECISM/B2CMainPlat_00（表单编码）
- 业务路由：TXCODE=YJF004；凭据 USERID / SKEY 与房间号 roomid 来自 .env
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 读取当前目录的 .env；不存在时不影响通过环境变量注入（如 GitHub Actions）
load_dotenv()

PUSH_CHANNELS = ("wecom", "pushplus", "serverchan", "bark", "none")


class ConfigError(Exception):
    """配置缺失或非法。"""


def _raw(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _float(name: str, default: float) -> float:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是数字，当前值：{raw!r}") from exc


def _int(name: str, default: int) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，当前值：{raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _infer_channel(raw_channel: str, creds: dict) -> str:
    """PUSH_CHANNEL 未显式指定时，按已配置的凭据自动推断推送渠道。"""
    channel = raw_channel.strip().lower()
    if channel:
        return channel
    for candidate in ("wecom", "pushplus", "serverchan", "bark"):
        if creds[candidate]:
            return candidate
    return "none"


@dataclass(frozen=True)
class ApiSettings:
    """接口凭据与目标宿舍。"""

    userid: str
    skey: str
    room_id: str
    cookie: str = ""
    room_name: str = ""
    user_agent: str = ""
    gateway_url: str = ""

    @property
    def display_room(self) -> str:
        return self.room_name or "目标宿舍"


@dataclass(frozen=True)
class AlertSettings:
    warn_threshold: float
    critical_threshold: float
    warn_once_per_day: bool


@dataclass(frozen=True)
class PushSettings:
    channel: str
    wecom_webhook: str = ""
    pushplus_token: str = ""
    serverchan_key: str = ""
    bark_url: str = ""


@dataclass(frozen=True)
class GasSettings:
    """燃气（港华 vcc-cbs）配置；subsid 与 token 都存在时启用。"""

    enabled: bool
    subsid: str = ""
    token: str = ""
    cookie: str = ""
    warn_threshold: float = 15.0


@dataclass(frozen=True)
class RuntimeSettings:
    request_timeout: float
    max_retries: int
    alert_tz: str
    trust_env: bool


@dataclass(frozen=True)
class Settings:
    api: ApiSettings
    alert: AlertSettings
    push: PushSettings
    runtime: RuntimeSettings
    gas: GasSettings


def _validate_push(push: PushSettings) -> None:
    if push.channel == "wecom" and not push.wecom_webhook:
        raise ConfigError("PUSH_CHANNEL=wecom 需要填写 WECOM_WEBHOOK（企业微信群机器人地址）")
    if push.channel == "pushplus" and not push.pushplus_token:
        raise ConfigError("PUSH_CHANNEL=pushplus 需要填写 PUSHPLUS_TOKEN")
    if push.channel == "serverchan" and not push.serverchan_key:
        raise ConfigError("PUSH_CHANNEL=serverchan 需要填写 SERVERCHAN_KEY")
    if push.channel == "bark" and not push.bark_url:
        raise ConfigError("PUSH_CHANNEL=bark 需要填写 BARK_URL")


def load_settings() -> Settings:
    """从 .env / 环境变量加载并校验配置；配置非法时抛 ConfigError。

    每次调用都重新读取 .env（override=True），这样用户重新抓包更新凭据后，
    长驻进程（如网页面板、保活守护）无需重启即可生效。
    """
    load_dotenv(override=True)
    api = ApiSettings(
        userid=_raw("CCB_USERID"),
        skey=_raw("CCB_SKEY"),
        room_id=_raw("CCB_ROOMID"),
        cookie=_raw("CCB_COOKIE"),
        room_name=_raw("CCB_ROOM_NAME"),
        user_agent=_raw("CCB_USER_AGENT"),
        gateway_url=_raw("CCB_GATEWAY_URL"),
    )

    if not api.userid:
        raise ConfigError("缺少 CCB_USERID：请在 .env 中填写建行校园一卡通用户编号（抓包请求体 USERID）")
    if not api.skey:
        raise ConfigError("缺少 CCB_SKEY：请在 .env 中填写当前会话校验密钥（抓包请求体 SKEY）")
    if not api.room_id:
        raise ConfigError("缺少 CCB_ROOMID：请在 .env 中填写固化后的宿舍房间号（如 134-33--250-501）")

    alert = AlertSettings(
        warn_threshold=_float("WARN_THRESHOLD", 20.0),
        critical_threshold=_float("CRITICAL_THRESHOLD", 5.0),
        warn_once_per_day=_bool("WARN_ONCE_PER_DAY", True),
    )
    if alert.warn_threshold <= 0:
        raise ConfigError("WARN_THRESHOLD 必须大于 0")
    if alert.critical_threshold < 0:
        raise ConfigError("CRITICAL_THRESHOLD 不能为负数（0 表示禁用二级阈值）")
    if alert.critical_threshold > 0 and alert.critical_threshold >= alert.warn_threshold:
        raise ConfigError("CRITICAL_THRESHOLD 必须小于 WARN_THRESHOLD（设为 0 表示禁用二级阈值）")

    wecom_webhook = _raw("WECOM_WEBHOOK")
    pushplus_token = _raw("PUSHPLUS_TOKEN")
    serverchan_key = _raw("SERVERCHAN_KEY")
    bark_url = _raw("BARK_URL")
    creds = {
        "wecom": wecom_webhook,
        "pushplus": pushplus_token,
        "serverchan": serverchan_key,
        "bark": bark_url,
    }
    channel = _infer_channel(_raw("PUSH_CHANNEL"), creds)
    if channel not in PUSH_CHANNELS:
        raise ConfigError(f"PUSH_CHANNEL 只能是 {PUSH_CHANNELS}，当前值：{channel!r}")
    push = PushSettings(
        channel=channel,
        wecom_webhook=wecom_webhook,
        pushplus_token=pushplus_token,
        serverchan_key=serverchan_key,
        bark_url=bark_url,
    )
    _validate_push(push)

    runtime = RuntimeSettings(
        request_timeout=min(max(_float("REQUEST_TIMEOUT", 10.0), 1.0), 60.0),
        max_retries=min(max(_int("MAX_RETRIES", 2), 0), 2),
        alert_tz=_raw("ALERT_TZ") or "Asia/Shanghai",
        trust_env=_bool("TRUST_ENV", True),
    )

    gas_subsid = _raw("GAS_SUBSID")
    gas_token = _raw("GAS_TOKEN")
    gas = GasSettings(
        enabled=bool(gas_subsid and gas_token),
        subsid=gas_subsid,
        token=gas_token,
        cookie=_raw("GAS_COOKIE"),
        warn_threshold=_float("GAS_WARN_THRESHOLD", 15.0),
    )
    if gas.enabled and gas.warn_threshold <= 0:
        raise ConfigError("GAS_WARN_THRESHOLD 必须大于 0")

    return Settings(api=api, alert=alert, push=push, runtime=runtime, gas=gas)
