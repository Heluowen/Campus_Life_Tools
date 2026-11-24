"""会话保活守护进程（按数据源独立调度 + 自动日志轮转 + OS 内核单实例锁）。

背景：CCB B2C 会话空闲超时 < 30 分钟，每日 2-4 次定时查询之间会话必死；
港华燃气支持 90 分钟主动刷新续期，无需陪绑电费高频查询。

本进程采用「按数据源独立调度」：
- 电费：默认每 10 分钟保活一次；仅电费会话失效时对其单独退避 15 分钟，**绝不连累燃气**；
- 燃气：按 GAS_QUERY_INTERVAL（默认 30 分钟）查询，临近 90 分钟主动续期线自动提前放行；
  即使电费已过期，燃气仍按自身周期持续续期，避免续期链断裂；
- 凭据热感知：检测到 `.env` 文件修改时间变化时立刻唤醒并重置退避，粘贴新凭据后秒级恢复；
- 日志自动轮转：使用 RotatingFileHandler（单文件 1 MB，最多保留 3 份），避免无限膨胀。

用法：
    python keep_alive.py                  # 默认电费每 10 分钟、燃气按 GAS_QUERY_INTERVAL 独立调度
    python keep_alive.py --interval 900   # 电费每 15 分钟一次
    python keep_alive.py --max-hours 8    # 运行 8 小时后退出（测试用）
    python keep_alive.py --once           # 跑一轮（电费 + 燃气）后退出
"""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import ConfigError, ENV_FILE, Settings, load_settings
import main as m

BASE_DIR = Path(__file__).resolve().parent
LOCK_FILE = BASE_DIR / "keepalive.lock"
PID_FILE = BASE_DIR / "keepalive.pid"
LOG_FILE: Path | None = None
_LOG_HANDLER: RotatingFileHandler | None = None
_LOCK_FH = None

_WIN_LOCK_OFFSET = 0
_LOG_MAX_BYTES = 1024 * 1024  # 单日志文件上限 1 MB
_LOG_BACKUP_COUNT = 3         # 最多保留 3 个历史轮转备份
_AUTH_BACKOFF_SECONDS = 900   # 单源认证失效时的独立退避间隔（15 分钟）
_GAS_REFRESH_AGE = 5400.0     # 燃气主动续期阈值（90 分钟）
_WAKE_SLICE_SECONDS = 15.0    # 睡眠切片（秒）：及时响应 .env 热更新 / Ctrl+C / max-hours


def _get_console_hwnd() -> int:
    """返回当前进程附着的控制台窗口句柄（无窗口/非 Windows 返回 0）。"""
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _hide_console() -> None:
    """隐藏当前控制台窗口（计划任务运行时不弹黑框）。"""
    hwnd = _get_console_hwnd()
    if hwnd:
        try:
            import ctypes

            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:  # noqa: BLE001
            pass


def _close_console() -> None:
    """关闭当前控制台窗口（仅 --hide-console 模式使用，防止残留窗口）。"""
    hwnd = _get_console_hwnd()
    if hwnd:
        try:
            import ctypes

            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:  # noqa: BLE001
            pass


def _init_log_handler(log_path: Path | None) -> None:
    global LOG_FILE, _LOG_HANDLER
    if _LOG_HANDLER is not None:
        try:
            _LOG_HANDLER.close()
        except Exception:  # noqa: BLE001
            pass
        _LOG_HANDLER = None
    LOG_FILE = log_path
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                filename=str(log_path),
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            _LOG_HANDLER = handler
        except OSError:
            _LOG_HANDLER = None


def _log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001 - pythonw 下 sys.stdout 为 None，静默降级写文件
        pass
    if _LOG_HANDLER is not None:
        try:
            record = logging.LogRecord(
                name="keep_alive",
                level=logging.INFO,
                pathname=__file__,
                lineno=0,
                msg=line,
                args=(),
                exc_info=None,
            )
            _LOG_HANDLER.emit(record)
        except Exception:  # noqa: BLE001
            pass
    elif LOG_FILE is not None:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _acquire_lock() -> bool:
    """跨平台内核级单实例文件锁：进程崩溃或断电后由 OS 自动释放，避免残留死锁。"""
    global _LOCK_FH
    try:
        fh = open(LOCK_FILE, "a+", encoding="utf-8")
    except OSError:
        return False

    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False

    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
    except OSError:
        pass
    try:
        PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except OSError:
        pass
    _LOCK_FH = fh
    return True


def _release_lock() -> None:
    global _LOCK_FH
    fh = _LOCK_FH
    _LOCK_FH = None
    if fh is not None:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(_WIN_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
    for path in (PID_FILE, LOCK_FILE):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def _env_mtime() -> float:
    try:
        return ENV_FILE.stat().st_mtime
    except OSError:
        return 0.0


def _compute_next_delay(
    *,
    code: int,
    base_interval: float,
    auth_backoff: float = _AUTH_BACKOFF_SECONDS,
) -> float:
    """计算单个数据源下一次调度的等待秒数（单源退避互不影响）。"""
    if code in (m.EXIT_AUTH_EXPIRED, m.EXIT_CONFIG):
        return max(base_interval, auth_backoff)
    return max(base_interval, 60.0)


def _gas_next_delay(settings: Settings, code: int) -> float:
    """计算燃气下一次调度秒数：兼顾 GAS_QUERY_INTERVAL 与 90 分钟主动续期截止线。"""
    if code in (m.EXIT_AUTH_EXPIRED, m.EXIT_CONFIG):
        return max(float(settings.gas.query_interval), float(_AUTH_BACKOFF_SECONDS))
    base = max(float(settings.gas.query_interval), 60.0)
    # 若存在 token_issued_at，确保不会错过 90 分钟主动续期窗口
    if settings.gas.token_issued_at > 0 and settings.gas.refresh_token:
        until_refresh = (settings.gas.token_issued_at + _GAS_REFRESH_AGE) - time.time()
        if until_refresh > 0:
            return min(base, max(until_refresh + 5.0, 60.0))
        return 60.0
    return base


class SourceScheduler:
    """电费与燃气独立调度器：单源失效退避不会阻塞另一数据源的保活与续期。"""

    def __init__(self, elec_interval: int) -> None:
        self.elec_interval = max(int(elec_interval), 60)
        self.next_elec_at: float = 0.0
        self.next_gas_at: float = 0.0
        self._last_env_mtime: float = _env_mtime()

    def check_env_reload(self, now_mono: float) -> bool:
        """若检测到 .env 被外部更新（如面板粘贴新凭据），立即唤醒退避中的数据源。"""
        mt = _env_mtime()
        if mt and mt != self._last_env_mtime:
            self._last_env_mtime = mt
            self.next_elec_at = min(self.next_elec_at, now_mono)
            self.next_gas_at = min(self.next_gas_at, now_mono)
            return True
        return False

    def step(self, now_mono: float, *, force_both: bool = False) -> tuple[int, list[str]]:
        """执行当前到期的所有数据源，更新各自的下次执行时间，返回 (合并退出码, 日志详情列表)。"""
        try:
            settings = load_settings()
        except ConfigError as exc:
            self.next_elec_at = now_mono + max(self.elec_interval, _AUTH_BACKOFF_SECONDS)
            self.next_gas_at = now_mono + _AUTH_BACKOFF_SECONDS
            return m.EXIT_CONFIG, [f"配置错误：{exc}"]

        codes: list[int] = []
        details: list[str] = []

        if force_both or now_mono >= self.next_elec_at:
            elec_code, elec_err = m.fetch_and_update_elec(settings, dry_run=False, notify=True)
            codes.append(elec_code)
            delay = _compute_next_delay(code=elec_code, base_interval=self.elec_interval)
            self.next_elec_at = now_mono + delay
            if elec_err:
                details.append(f"电费({elec_err.get('error')}): {elec_err.get('message')}")
            else:
                details.append("电费: OK")

        if settings.gas.enabled and (force_both or now_mono >= self.next_gas_at):
            gas_code, gas_err = m.fetch_and_update_gas(
                settings,
                dry_run=False,
                notify=True,
                respect_interval=not force_both,
            )
            codes.append(gas_code)
            # 主动续期可能刚写回新的 GAS_TOKEN_ISSUED_AT，重新读取以便精确对齐下次续期点
            try:
                latest_settings = load_settings()
                self._last_env_mtime = _env_mtime()
            except ConfigError:
                latest_settings = settings
            self.next_gas_at = now_mono + _gas_next_delay(latest_settings, gas_code)
            if gas_err:
                details.append(f"燃气({gas_err.get('error')}): {gas_err.get('message')}")
            else:
                details.append("燃气: OK")
        elif not settings.gas.enabled:
            self.next_gas_at = now_mono + max(self.elec_interval, 600)

        return (m._worst_code(*codes) if codes else m.EXIT_OK), details

    def next_wakeup_seconds(self, now_mono: float) -> float:
        target = min(self.next_elec_at, self.next_gas_at)
        return max(1.0, target - now_mono)


def main() -> int:
    parser = argparse.ArgumentParser(description="校园生活监控会话保活守护进程（按源独立调度）")
    parser.add_argument("--interval", type=int, default=600, help="电费保活间隔秒数（默认 600，下限 60）")
    parser.add_argument("--max-hours", type=float, default=0, help="运行多少小时后退出（0=无限，测试用小数）")
    parser.add_argument("--once", action="store_true", help="只执行一次后退出（测试/手动巡检用）")
    parser.add_argument("--log-file", default="", help="把日志写入该文件（自动按 1MB 轮转，保留 3 份）")
    parser.add_argument(
        "--hide-console",
        action="store_true",
        help="静默模式：隐藏并自动关闭当前控制台窗口（计划任务/计划程序调用时使用）",
    )
    args = parser.parse_args()
    if args.log_file:
        _init_log_handler(Path(args.log_file))
    if args.hide_console:
        _hide_console()

    if args.interval < 60:
        _log("间隔过短（<60s），已按 60s 执行")
        args.interval = 60

    if not _acquire_lock():
        _log("已有 keep_alive 进程在运行（keepalive.lock），本进程退出")
        return 0

    deadline = datetime.now() + timedelta(hours=args.max_hours) if args.max_hours > 0 else None
    _log(
        f"保活启动（独立调度）：电费间隔 {args.interval}s"
        + (f"，运行至 {deadline:%Y-%m-%d %H:%M}" if deadline else "，无限运行")
    )
    scheduler = SourceScheduler(args.interval)
    try:
        while True:
            now_mono = time.monotonic()
            code, details = scheduler.step(now_mono, force_both=args.once)
            detail_text = f" | {' ; '.join(details)}" if details else ""
            if code == m.EXIT_OK:
                _log(f"巡检完成{detail_text}")
            elif code == m.EXIT_AUTH_EXPIRED:
                _log(f"存在会话失效（该源独立退避，其他源不受影响）{detail_text}")
            elif code == m.EXIT_QUERY_FAILED:
                _log(f"查询失败（将按间隔重试）{detail_text}")
            elif code == m.EXIT_CONFIG:
                _log(f"配置错误{detail_text}")
            else:
                _log(f"本次结束 code={code}{detail_text}")

            if args.once:
                _log("--once 已执行一次，退出")
                break
            if deadline is not None and datetime.now() >= deadline:
                _log("达到运行时限，退出")
                break

            # 分片睡眠：每 15 秒检查一次 .env 热更新或运行时限，确保更新凭据后快速唤醒
            while True:
                if deadline is not None and datetime.now() >= deadline:
                    break
                now_mono = time.monotonic()
                if scheduler.check_env_reload(now_mono):
                    _log("检测到 .env 已更新，提前唤醒巡检")
                    break
                remain = scheduler.next_wakeup_seconds(now_mono)
                if remain <= 0.5:
                    break
                time.sleep(min(remain, _WAKE_SLICE_SECONDS))
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，退出")
    finally:
        _release_lock()
        _init_log_handler(None)
        if args.hide_console:
            _close_console()  # 计划任务场景：窗口用完自动关掉，不留黑框
    return 0


if __name__ == "__main__":
    sys.exit(main())
