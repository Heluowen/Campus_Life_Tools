"""会话保活守护进程（用户已知情选择的高频折中方案）。

背景：CCB B2C 会话空闲超时 < 30 分钟，每日 2-4 次定时查询之间会话必死。
本进程按固定间隔（默认 10 分钟）持续发送一次查询，维持会话活跃；
余额低于阈值时按与 main.py 相同的规则推送提醒（含日常防骚扰与 1 小时冷却）。

⚠️ 风险提示（用户已知情选择）：
- 每天约 96~144 次请求，可能触发接口风控；请勿再调低间隔（下限 60s）；
- 电脑休眠 / 断网 / 重启后会话仍会失效，需重新抓包更新 .env；
- 服务端轮换的 Cookie 会自动持久化到 cookies.json，无需手工维护。

用法：
    python keep_alive.py                  # 每 10 分钟一次，无限循环
    python keep_alive.py --interval 900   # 每 15 分钟一次
    python keep_alive.py --max-hours 8    # 运行 8 小时后退出（测试用）

Windows 计划任务（登录自启）见 README「保活模式」章节。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import ConfigError, load_settings
import main as m

BASE_DIR = Path(__file__).resolve().parent
LOCK_FILE = BASE_DIR / "keepalive.lock"
LOG_FILE: Path | None = None


def _log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if LOG_FILE is not None:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _acquire_lock() -> bool:
    """单实例锁：已有存活的 keep_alive 进程则返回 False。"""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ValueError, OSError):
            pass
    try:
        LOCK_FILE.write_text(str(os.getpid()))
    except OSError:
        pass
    return True


def _release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


def main() -> int:
    global LOG_FILE
    parser = argparse.ArgumentParser(description="电费监控会话保活守护进程")
    parser.add_argument("--interval", type=int, default=600, help="保活间隔秒数（默认 600）")
    parser.add_argument("--max-hours", type=float, default=0, help="运行多少小时后退出（0=无限，测试用小数）")
    parser.add_argument("--once", action="store_true", help="只执行一次后退出（测试/手动巡检用）")
    parser.add_argument("--log-file", default="", help="把日志追加写入该文件（计划任务无窗口运行时使用）")
    args = parser.parse_args()
    if args.log_file:
        LOG_FILE = Path(args.log_file)

    if args.interval < 60:
        _log("间隔过短（<60s），已按 60s 执行")
        args.interval = 60

    if not _acquire_lock():
        _log("已有 keep_alive 进程在运行（keepalive.lock），本进程退出")
        return 0

    deadline = datetime.now() + timedelta(hours=args.max_hours) if args.max_hours > 0 else None
    _log(f"保活启动：间隔 {args.interval}s" + (f"，运行至 {deadline:%Y-%m-%d %H:%M}" if deadline else "，无限运行"))
    try:
        while True:
            try:
                settings = load_settings()
                code = m.run_once(settings, dry_run=False)
            except ConfigError as exc:
                _log(f"配置错误：{exc}")
                code = m.EXIT_CONFIG

            if code == m.EXIT_OK:
                _log("查询成功")
            elif code == m.EXIT_AUTH_EXPIRED:
                _log("会话已失效（需重新抓包更新 .env）")
            elif code == m.EXIT_QUERY_FAILED:
                _log("查询失败（将按间隔重试）")
            else:
                _log(f"本次结束 code={code}")

            try:
                LOCK_FILE.write_text(str(os.getpid()))
            except OSError:
                pass

            if args.once:
                _log("--once 已执行一次，退出")
                break
            if deadline is not None and datetime.now() >= deadline:
                _log("达到运行时限，退出")
                break
            sleep_seconds = args.interval
            if code in (m.EXIT_AUTH_EXPIRED, m.EXIT_CONFIG):
                # 死会话/坏配置期间退避，降低无意义请求频率；更新 .env 后自动恢复
                sleep_seconds = max(args.interval, 900)
                _log(f"退避 {sleep_seconds}s 后重试（更新 .env 后无需重启亦可生效）")
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，退出")
    finally:
        _release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
