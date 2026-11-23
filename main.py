"""主入口：单次查询 → 阈值判断 → 防骚扰 → 推送。

同时提供公共查询与状态更新原语（fetch_and_update_elec / fetch_and_update_gas / run_once）
供 keep_alive.py 与 web/server.py 统一复用，避免逻辑重复与并发写盘竞争。

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
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import ConfigError, Settings, load_settings, update_env_many
from notifier import NotifyError, send_notification
from query_elec import AuthExpiredError, QueryError, QueryResult, query_balance
from query_gas import GasAuthExpiredError, GasMaintenanceError, GasQueryError, GasResult, query_gas

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
COOKIE_STORE = BASE_DIR / "cookies.json"  # 会话 Cookie 轮换持久化（已 gitignore）
ENV_FILE = BASE_DIR / ".env"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_AUTH_EXPIRED = 3
EXIT_QUERY_FAILED = 4

AUTH_ALERT_COOLDOWN = timedelta(hours=1)   # 登录失效提醒冷却（保活模式下防刷屏）
ERROR_ALERT_COOLDOWN = timedelta(hours=1)  # 查询失败提醒冷却
HISTORY_MAX = 1200  # 至少覆盖完整 7 天高频采集（7*144=1008），配合同小时同值合并可保存更久

_STATE_LOCK = threading.Lock()


def _on_gas_token_refreshed(access: str, refresh: str) -> None:
    """燃气 token 刷新后原子批量持久化到 .env（含签发时间，供主动续期判断）。"""
    update_env_many(
        {
            "GAS_TOKEN": access,
            "GAS_REFRESH_TOKEN": refresh,
            "GAS_TOKEN_ISSUED_AT": str(int(time.time())),
        }
    )


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
    """进程/线程安全的原子写盘，防止 Web 多线程与保活守护并发覆盖同一临时文件。"""
    tmp = STATE_FILE.with_name(f"{STATE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp.write_text(content, encoding="utf-8")
        # Windows 下偶发短暂文件占用时重试替换
        for attempt in range(3):
            try:
                tmp.replace(STATE_FILE)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _append_history(state: dict, key: str, now: datetime, balance: float) -> None:
    """追加历史点并智能去重：同一小时内连续相同余额只保留首尾关键点，延长 7 天+ 保留跨度。"""
    hist = state.get(key)
    if not isinstance(hist, list):
        hist = []
    bal_val = round(balance, 2)
    ts = now.isoformat(timespec="seconds")

    if len(hist) >= 2:
        prev1 = hist[-1]
        prev2 = hist[-2]
        if (
            isinstance(prev1, dict)
            and isinstance(prev2, dict)
            and prev1.get("bal") == bal_val
            and prev2.get("bal") == bal_val
            and str(prev1.get("t", ""))[:13] == ts[:13]
            and str(prev2.get("t", ""))[:13] == ts[:13]
        ):
            hist[-1] = {"t": ts, "bal": bal_val}
            state[key] = hist[-HISTORY_MAX:]
            return

    hist.append({"t": ts, "bal": bal_val})
    state[key] = hist[-HISTORY_MAX:]


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


def fetch_and_update_elec(
    settings: Settings,
    *,
    dry_run: bool = False,
    notify: bool = True,
) -> tuple[int, dict | None]:
    """查询电费并按需更新 state.json / 触发推送。

    返回 (exit_code, error_dict_or_none)。供 CLI、保活守护、Web 面板共享。
    """
    now = _now(settings)

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
        err = {"error": "auth", "message": str(exc)}
        title = "【电费监控】登录凭据已失效，请更新 Token"
        content = (
            f"宿舍：{settings.api.display_room}\n"
            f"原因：{exc}\n"
            f"处理：请重新抓包，更新 .env 中的 CCB_SKEY / CCB_COOKIE 后重试。\n"
            f"时间：{_fmt_time(now)}"
        )
        if dry_run:
            if notify:
                _push(settings, title, content, True)
            return EXIT_AUTH_EXPIRED, err
        with _STATE_LOCK:
            state = _load_state()
            state["last_auth_error_at"] = now.isoformat()
            if notify:
                if _cooldown_ok(state, "last_auth_alert_at", now, AUTH_ALERT_COOLDOWN):
                    _push(settings, title, content, False)
                    state["last_auth_alert_at"] = now.isoformat()
                else:
                    print("[SKIP] 登录失效提醒冷却中（1 小时），不重复推送")
            _save_state(state)
        return EXIT_AUTH_EXPIRED, err
    except QueryError as exc:
        print(f"[QUERY-FAILED] {_fmt_time(now)} {exc}")
        err = {"error": "query", "message": str(exc)}
        title = "【电费监控】查询失败"
        content = f"宿舍：{settings.api.display_room}\n原因：{exc}\n时间：{_fmt_time(now)}"
        if dry_run:
            if notify:
                _push(settings, title, content, True)
            return EXIT_QUERY_FAILED, err
        with _STATE_LOCK:
            state = _load_state()
            state["last_error"] = {"time": now.isoformat(), "message": str(exc)}
            if notify:
                if _cooldown_ok(state, "last_error_alert_at", now, ERROR_ALERT_COOLDOWN):
                    _push(settings, title, content, False)
                    state["last_error_alert_at"] = now.isoformat()
                else:
                    print("[SKIP] 查询失败提醒冷却中（1 小时），不重复推送")
            _save_state(state)
        return EXIT_QUERY_FAILED, err

    if result.room_id and result.room_id != settings.api.room_id:
        print(
            f"[WARN] 响应房间号 {result.room_id!r} 与配置 CCB_ROOMID {settings.api.room_id!r} 不一致，"
            "请确认 .env 中的目标宿舍",
            file=sys.stderr,
        )

    level = _determine_level(result.balance, settings)
    print(f"[QUERY] {_fmt_time(now)} 宿舍={settings.api.display_room} 剩余={result.balance_text} 状态={level}")

    today = now.date().isoformat()
    push_ok = False
    should_notify = False

    with _STATE_LOCK:
        state = _load_state()
        if notify:
            if level == "critical":
                should_notify = True
            elif level == "low":
                if _should_notify_low(state, today, settings):
                    should_notify = True
                else:
                    print(
                        f"[SKIP] 今日已提醒过（{state.get('last_alert_date')}，级别 {state.get('last_notified_level')}），"
                        "不重复推送"
                    )

        if should_notify:
            title, content = _alert_message(level, result, settings, now)
            push_ok = _push(settings, title, content, dry_run)

        if not dry_run:
            state.pop("last_error", None)
            state.pop("last_auth_error_at", None)
            state["last_balance"] = round(result.balance, 2)
            state["last_query_time"] = now.isoformat()
            state["last_level"] = level
            _append_history(state, "history", now, result.balance)
            if should_notify and (push_ok or settings.push.is_none):
                state["last_alert_date"] = today
                state["last_notified_level"] = level
            _save_state(state)

    if should_notify and not push_ok and not settings.push.is_none:
        return EXIT_ERROR, {"error": "notify", "message": "告警推送失败"}
    return EXIT_OK, None


def _gas_interval_due(state: dict, now: datetime, interval_sec: int) -> bool:
    """保活轮询场景：判断燃气距离上次成功查询是否已达到最小查询间隔。"""
    last = state.get("gas_last_query_time")
    if not last:
        return True
    try:
        elapsed = (now - datetime.fromisoformat(str(last))).total_seconds()
    except ValueError:
        return True
    return elapsed >= interval_sec


def fetch_and_update_gas(
    settings: Settings,
    *,
    dry_run: bool = False,
    notify: bool = True,
    respect_interval: bool = False,
) -> tuple[int, dict | None]:
    """查询燃气并按需更新 state.json / 触发推送。

    respect_interval=True 时（保活守护模式），若距上次查询未满 GAS_QUERY_INTERVAL 且
    token 尚未接近主动续期阈值，则跳过本次查询以降低接口频率。
    """
    gas = settings.gas
    if not gas.enabled:
        return EXIT_OK, {"error": "gas-disabled", "message": "未配置燃气（需要在 .env 填 GAS_SUBSID 与 GAS_TOKEN）"}

    now = _now(settings)
    if respect_interval:
        state_snap = _load_state()
        token_near_expiry = gas.token_issued_at > 0 and (time.time() - gas.token_issued_at) >= 5400.0
        if not token_near_expiry and not _gas_interval_due(state_snap, now, gas.query_interval):
            return EXIT_OK, None

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
        err = {"error": "gas-auth", "message": str(exc)}
        title = "【燃气监控】Token 已失效，请更新 GAS_TOKEN"
        content = (
            f"户号：{gas.subsid[:12]}…\n"
            f"原因：{exc}\n"
            f"处理：重新打开燃气服务页，抓包取 Authorization: Bearer 更新 .env 的 GAS_TOKEN。\n"
            f"时间：{_fmt_time(now)}"
        )
        if dry_run:
            if notify:
                _push(settings, title, content, True)
            return EXIT_AUTH_EXPIRED, err
        with _STATE_LOCK:
            state = _load_state()
            state["gas_last_auth_error_at"] = now.isoformat()
            if notify:
                if _cooldown_ok(state, "gas_last_auth_alert_at", now, AUTH_ALERT_COOLDOWN):
                    _push(settings, title, content, False)
                    state["gas_last_auth_alert_at"] = now.isoformat()
                else:
                    print("[SKIP] 燃气失效提醒冷却中（1 小时），不重复推送")
            _save_state(state)
        return EXIT_AUTH_EXPIRED, err
    except GasMaintenanceError as exc:
        # 每日固定维护窗口（如 23:30-次日 00:30）：只记录状态不发故障推送，避免半夜误报骚扰
        print(f"[GAS-MAINTENANCE] {_fmt_time(now)} {exc}")
        err = {"error": "gas-maintenance", "message": str(exc)}
        if not dry_run:
            with _STATE_LOCK:
                state = _load_state()
                state["gas_maintenance"] = {"time": now.isoformat(), "message": str(exc)}
                _save_state(state)
        return EXIT_OK, err
    except GasQueryError as exc:
        print(f"[GAS-QUERY-FAILED] {_fmt_time(now)} {exc}")
        err = {"error": "gas-query", "message": str(exc)}
        title = "【燃气监控】查询失败"
        content = f"户号：{gas.subsid[:12]}…\n原因：{exc}\n时间：{_fmt_time(now)}"
        if dry_run:
            if notify:
                _push(settings, title, content, True)
            return EXIT_QUERY_FAILED, err
        with _STATE_LOCK:
            state = _load_state()
            state["gas_last_error"] = {"time": now.isoformat(), "message": str(exc)}
            if notify:
                if _cooldown_ok(state, "gas_last_error_alert_at", now, ERROR_ALERT_COOLDOWN):
                    _push(settings, title, content, False)
                    state["gas_last_error_alert_at"] = now.isoformat()
                else:
                    print("[SKIP] 燃气查询失败提醒冷却中（1 小时），不重复推送")
            _save_state(state)
        return EXIT_QUERY_FAILED, err

    level = "low" if result.balance < gas.warn_threshold else "ok"
    reading = f"（表读数 {result.meter_reading}）" if result.meter_reading else ""
    print(f"[GAS] {_fmt_time(now)} 剩余={result.balance_text} 状态={level}{reading}")

    today = now.date().isoformat()
    push_ok = False
    should_notify = False

    with _STATE_LOCK:
        state = _load_state()
        if notify and level == "low":
            if (
                settings.alert.warn_once_per_day
                and state.get("gas_last_alert_date") == today
                and state.get("gas_notified_level") in ("low", "critical")
            ):
                print("[SKIP] 燃气今日已提醒过，不重复推送")
            else:
                should_notify = True

        if should_notify:
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
            state.pop("gas_last_auth_error_at", None)
            state.pop("gas_maintenance", None)
            state["gas_balance"] = round(result.balance, 2)
            state["gas_last_query_time"] = now.isoformat()
            state["gas_level"] = level
            state["gas_subs_code"] = result.subs_code
            state["gas_meter_reading"] = result.meter_reading
            _append_history(state, "gas_history", now, result.balance)
            if should_notify and (push_ok or settings.push.is_none):
                state["gas_last_alert_date"] = today
                state["gas_notified_level"] = level
            _save_state(state)

    if should_notify and not push_ok and not settings.push.is_none:
        return EXIT_ERROR, {"error": "notify", "message": "燃气告警推送失败"}
    return EXIT_OK, None


def _run_elec(settings: Settings, dry_run: bool = False) -> int:
    code, _ = fetch_and_update_elec(settings, dry_run=dry_run, notify=True)
    return code


def _run_gas(settings: Settings, dry_run: bool = False, respect_interval: bool = False) -> int:
    code, _ = fetch_and_update_gas(
        settings, dry_run=dry_run, notify=True, respect_interval=respect_interval
    )
    return code


def _worst_code(*codes: int) -> int:
    """多数据源合并退出码：鉴权失效 > 查询失败 > 推送失败 > 成功。"""
    for c in (EXIT_AUTH_EXPIRED, EXIT_QUERY_FAILED, EXIT_ERROR):
        if c in codes:
            return c
    return EXIT_OK


def run_once(settings: Settings, dry_run: bool = False, respect_gas_interval: bool = False) -> int:
    """执行一轮完整监控：电费 + 燃气（若已配置）。返回合并退出码。"""
    codes = [_run_elec(settings, dry_run)]
    if settings.gas.enabled:
        codes.append(_run_gas(settings, dry_run, respect_interval=respect_gas_interval))
    return _worst_code(*codes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校园宿舍电费与燃气自动查询与提醒")
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
