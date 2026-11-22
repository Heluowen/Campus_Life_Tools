"""主入口：单次查询 → 阈值判断 → 防骚扰 → 推送。

同时提供 run_once() 供 keep_alive.py 复用。

用法：
    python main.py             # 正常执行：查询、判断、推送、更新 state.json
    python main.py --dry-run   # 只查询与判断，不推送、不写状态（联调用）

退出码：
    0  查询成功（含无需告警的情况）
    1  运行时错误（如推送失败）
    2  配置错误
    3  登录态失效（凭据需要重新抓包更新）
    4  查询失败
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import ConfigError, Settings, load_settings, update_env
from notifier import NotifyError, send_notification
from query_elec import AuthExpiredError, QueryError, QueryResult, query_balance
from query_gas import GasAuthExpiredError, GasQueryError, GasResult, query_gas

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
COOKIE_STORE = BASE_DIR / "cookies.json"  # 会话 Cookie 轮换持久化（已 gitignore）
ENV_FILE = BASE_DIR / ".env"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_AUTH_EXPIRED = 3
EXIT_QUERY_FAILED = 4


def _on_gas_token_refreshed(access: str, refresh: str) -> None:
    """燃气 token 刷新后持久化到 .env（含签发时间，供主动续期判断）。"""
    update_env("GAS_TOKEN", access)
    update_env("GAS_REFRESH_TOKEN", refresh)
    update_env("GAS_TOKEN_ISSUED_AT", str(int(time.time())))

AUTH_ALERT_COOLDOWN = timedelta(hours=1)   # 登录失效提醒冷却（保活模式下防刷屏）
ERROR_ALERT_COOLDOWN = timedelta(hours=1)  # 查询失败提醒冷却
HISTORY_MAX = 500  # state.json 中保留的历史查询点数（Web 趋势图用）


def _now(settings: Settings) -> datetime:
    """按 ALERT_TZ 取当前时间；时区数据不可用时回退本地时间。"""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return datetime.now(ZoneInfo(settings.runtime.alert_tz))
    except (ZoneInfoNotFoundError, ValueError):
        print("[WARN] 时区数据不可用，改用本地时间", file=sys.stderr)
        return datetime.now()


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("[WARN] state.json 解析失败，按空状态处理", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def _cooldown_ok(state: dict, key: str, now: datetime, cooldown: timedelta) -> bool:
    """键 key 记录的上次提醒时间在 cooldown 之外才允许再次提醒。"""
    last = state.get(key, "")
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(str(last)) >= cooldown
    except ValueError:
        return True


def _determine_level(balance: float, settings: Settings) -> str:
    alert = settings.alert
    if alert.critical_threshold > 0 and balance < alert.critical_threshold:
        return "critical"
    if balance < alert.warn_threshold:
        return "low"
    return "ok"


def _should_notify_low(state: dict, today: str, settings: Settings) -> bool:
    """一级阈值告警：开启每日一次时，当日已提醒过 low/critical 则跳过。"""
    if not settings.alert.warn_once_per_day:
        return True
    return not (
        state.get("last_alert_date") == today
        and state.get("last_notified_level") in ("low", "critical")
    )


def _alert_message(level: str, result: QueryResult, settings: Settings, now: datetime):
    room = settings.api.display_room
    balance_text = result.balance_text
    if level == "critical":
        title = "宿舍电量即将耗尽（紧急）"
        threshold_line = f"紧急阈值：< {settings.alert.critical_threshold:g}"
    else:
        title = "宿舍电量余额不足"
        threshold_line = f"提醒阈值：< {settings.alert.warn_threshold:g}"
    content = f"宿舍：{room}\n剩余：{balance_text}"
    if settings.api.has_price:
        content += f"\n剩余价值：约 ¥{result.balance * settings.api.price:.2f}（单价 {settings.api.price:g} 元/度）"
    content += f"\n{threshold_line}\n查询时间：{_fmt_time(now)}"
    return title, content


def _push(settings: Settings, title: str, content: str, dry: bool) -> bool:
    if dry:
        print(f"[DRY-RUN] 跳过推送：「{title}」")
        return False
    try:
        ok = send_notification(settings.push, title, content)
    except NotifyError as exc:
        print(f"[WARN] 推送失败：{exc}", file=sys.stderr)
        return False
    if ok:
        print(f"[PUSH] 已推送：「{title}」")
    return ok


def _run_elec(settings: Settings, dry_run: bool = False) -> int:
    """电费查询流程：查询 → 阈值 → 推送。返回退出码。"""
    now = _now(settings)
    state = _load_state()

    try:
        result = query_balance(
            settings.api,
            settings.runtime.request_timeout,
            settings.runtime.max_retries,
            settings.runtime.trust_env,
            COOKIE_STORE,
        )
    except AuthExpiredError as exc:
        print(f"[AUTH-EXPIRED] {_fmt_time(now)} {exc}")
        title = "【电费监控】登录凭据已失效，请更新 Token"
        content = (
            f"宿舍：{settings.api.display_room}\n"
            f"原因：{exc}\n"
            f"处理：请重新抓包，更新 .env 中的 CCB_SKEY / CCB_COOKIE 后重试。\n"
            f"时间：{_fmt_time(now)}"
        )
        if dry_run:
            _push(settings, title, content, True)
            return EXIT_AUTH_EXPIRED
        state["last_auth_error_at"] = now.isoformat()
        if _cooldown_ok(state, "last_auth_alert_at", now, AUTH_ALERT_COOLDOWN):
            _push(settings, title, content, False)
            state["last_auth_alert_at"] = now.isoformat()
        else:
            print("[SKIP] 登录失效提醒冷却中（1 小时），不重复推送")
        _save_state(state)
        return EXIT_AUTH_EXPIRED
    except QueryError as exc:
        print(f"[QUERY-FAILED] {_fmt_time(now)} {exc}")
        title = "【电费监控】查询失败"
        content = f"宿舍：{settings.api.display_room}\n原因：{exc}\n时间：{_fmt_time(now)}"
        if dry_run:
            _push(settings, title, content, True)
            return EXIT_QUERY_FAILED
        state["last_error"] = {"time": now.isoformat(), "message": str(exc)}
        if _cooldown_ok(state, "last_error_alert_at", now, ERROR_ALERT_COOLDOWN):
            _push(settings, title, content, False)
            state["last_error_alert_at"] = now.isoformat()
        else:
            print("[SKIP] 查询失败提醒冷却中（1 小时），不重复推送")
        _save_state(state)
        return EXIT_QUERY_FAILED

    if result.room_id and result.room_id != settings.api.room_id:
        print(
            f"[WARN] 响应房间号 {result.room_id!r} 与配置 CCB_ROOMID {settings.api.room_id!r} 不一致，"
            "请确认 .env 中的目标宿舍",
            file=sys.stderr,
        )

    level = _determine_level(result.balance, settings)
    print(f"[QUERY] {_fmt_time(now)} 宿舍={settings.api.display_room} 剩余={result.balance_text} 状态={level}")

    today = now.date().isoformat()
    notify = False
    if level == "critical":
        notify = True
    elif level == "low":
        if _should_notify_low(state, today, settings):
            notify = True
        else:
            print(
                f"[SKIP] 今日已提醒过（{state.get('last_alert_date')}，级别 {state.get('last_notified_level')}），"
                "不重复推送"
            )

    push_ok = False
    if notify:
        title, content = _alert_message(level, result, settings, now)
        push_ok = _push(settings, title, content, dry_run)

    if not dry_run:
        state.pop("last_error", None)
        state["last_balance"] = round(result.balance, 2)
        state["last_query_time"] = now.isoformat()
        state["last_level"] = level
        # 历史记录（Web 面板趋势图数据），保留最近 500 条
        hist = state.get("history")
        if not isinstance(hist, list):
            hist = []
        hist.append({"t": now.isoformat(timespec="seconds"), "bal": round(result.balance, 2)})
        state["history"] = hist[-HISTORY_MAX:]
        if notify and (push_ok or settings.push.channel == "none"):
            state["last_alert_date"] = today
            state["last_notified_level"] = level
        _save_state(state)

    if notify and not push_ok and settings.push.channel != "none":
        return EXIT_ERROR
    return EXIT_OK


def _run_gas(settings: Settings, dry_run: bool = False) -> int:
    """燃气查询流程：查询 → 阈值 → 推送。返回退出码。"""
    gas = settings.gas
    now = _now(settings)
    state = _load_state()

    try:
        result = query_gas(
            gas.subsid,
            gas.token,
            settings.runtime.request_timeout,
            settings.runtime.max_retries,
            settings.runtime.trust_env,
            gas.cookie,
            refresh_token=gas.refresh_token,
            clientid=gas.clientid,
            token_issued_at=gas.token_issued_at,
            on_token_refreshed=_on_gas_token_refreshed,
        )
    except GasAuthExpiredError as exc:
        print(f"[GAS-AUTH-EXPIRED] {_fmt_time(now)} {exc}")
        title = "【燃气监控】Token 已失效，请更新 GAS_TOKEN"
        content = (
            f"户号：{gas.subsid[:12]}…\n"
            f"原因：{exc}\n"
            f"处理：重新打开燃气服务页，抓包取 Authorization: Bearer 更新 .env 的 GAS_TOKEN。\n"
            f"时间：{_fmt_time(now)}"
        )
        if dry_run:
            _push(settings, title, content, True)
            return EXIT_AUTH_EXPIRED
        state["gas_last_auth_error_at"] = now.isoformat()
        if _cooldown_ok(state, "gas_last_auth_alert_at", now, AUTH_ALERT_COOLDOWN):
            _push(settings, title, content, False)
            state["gas_last_auth_alert_at"] = now.isoformat()
        else:
            print("[SKIP] 燃气失效提醒冷却中（1 小时），不重复推送")
        _save_state(state)
        return EXIT_AUTH_EXPIRED
    except GasQueryError as exc:
        print(f"[GAS-QUERY-FAILED] {_fmt_time(now)} {exc}")
        title = "【燃气监控】查询失败"
        content = f"户号：{gas.subsid[:12]}…\n原因：{exc}\n时间：{_fmt_time(now)}"
        if dry_run:
            _push(settings, title, content, True)
            return EXIT_QUERY_FAILED
        state["gas_last_error"] = {"time": now.isoformat(), "message": str(exc)}
        if _cooldown_ok(state, "gas_last_error_alert_at", now, ERROR_ALERT_COOLDOWN):
            _push(settings, title, content, False)
            state["gas_last_error_alert_at"] = now.isoformat()
        else:
            print("[SKIP] 燃气查询失败提醒冷却中（1 小时），不重复推送")
        _save_state(state)
        return EXIT_QUERY_FAILED

    level = "low" if result.balance < gas.warn_threshold else "ok"
    reading = f"（表读数 {result.meter_reading}）" if result.meter_reading else ""
    print(f"[GAS] {_fmt_time(now)} 剩余={result.balance_text} 状态={level}{reading}")

    today = now.date().isoformat()
    notify = False
    if level == "low":
        if (
            settings.alert.warn_once_per_day
            and state.get("gas_last_alert_date") == today
            and state.get("gas_notified_level") in ("low", "critical")
        ):
            print("[SKIP] 燃气今日已提醒过，不重复推送")
        else:
            notify = True

    push_ok = False
    if notify:
        title = "燃气余额不足"
        content = (
            f"户号：{result.subs_code}\n"
            f"剩余：{result.balance_text}\n"
            f"提醒阈值：< {gas.warn_threshold:g} 元\n"
            f"查询时间：{_fmt_time(now)}"
        )
        push_ok = _push(settings, title, content, dry_run)

    if not dry_run:
        state.pop("gas_last_error", None)
        state["gas_balance"] = round(result.balance, 2)
        state["gas_last_query_time"] = now.isoformat()
        state["gas_level"] = level
        state["gas_meter_reading"] = result.meter_reading
        hist = state.get("gas_history")
        if not isinstance(hist, list):
            hist = []
        hist.append({"t": now.isoformat(timespec="seconds"), "bal": round(result.balance, 2)})
        state["gas_history"] = hist[-HISTORY_MAX:]
        if notify and (push_ok or settings.push.channel == "none"):
            state["gas_last_alert_date"] = today
            state["gas_notified_level"] = level
        _save_state(state)

    if notify and not push_ok and settings.push.channel != "none":
        return EXIT_ERROR
    return EXIT_OK


def _worst_code(*codes: int) -> int:
    """多数据源合并退出码：鉴权失效 > 查询失败 > 推送失败 > 成功。"""
    for c in (EXIT_AUTH_EXPIRED, EXIT_QUERY_FAILED, EXIT_ERROR):
        if c in codes:
            return c
    return EXIT_OK


def run_once(settings: Settings, dry_run: bool = False) -> int:
    """执行一轮完整监控：电费 + 燃气（若已配置）。返回合并退出码。"""
    codes = [_run_elec(settings, dry_run)]
    if settings.gas.enabled:
        codes.append(_run_gas(settings, dry_run))
    return _worst_code(*codes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校园宿舍电费自动查询与提醒")
    parser.add_argument("--dry-run", action="store_true", help="只查询与判断，不推送、不写状态")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[ERROR] 配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    return run_once(settings, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
