"""校园生活监控 - 系统托盘常驻助手（快捷查看余额 + 一键唤起 WebUI）。

特性：
- 悬停图标实时查看电费余额（含折算金额）、燃气余额（含表读数）与更新时间；
- 左键单击（或菜单首项）直接唤起本地 WebUI（若 Web 面板未运行则自动拉起）；
- 右键菜单直接展示最新数值、立即刷新电费/燃气、打开日志、仅退出托盘或停止全部后台服务；
- 根据状态自动切换托盘图标颜色（绿=充足 / 黄=偏低或数据偏旧 / 红=告急或凭据失效 / 灰=未知）；
- 余额新进入偏低/告急或认证失效时自动弹出系统托盘通知（带状态去重，不刷屏）；
- 采用 OS 内核单实例锁（tray.lock + tray.pid），重复运行自动退出；
- 优先使用 pystray + Pillow 渲染高分矢量感图标，缺省时自动提示或降级。

用法：
    pythonw tray.py --port 8877
    python  tray.py --port 8877 --poll 15
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import webbrowser

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import ConfigError, load_settings  # noqa: E402
import main as m  # noqa: E402

LOCK_FILE = BASE_DIR / "tray.lock"
PID_FILE = BASE_DIR / "tray.pid"
LOG_FILE = BASE_DIR / "keepalive.log"
_LOCK_FH = None

_STALE_SECONDS = 32 * 60
_TOOLTIP_MAX_LEN = 120  # Windows NOTIFYICONDATA.szTip 上限 128 字符（保留末尾 NUL）

_STATUS_COLORS = {
    "ok": ("#10b981", "#ecfdf5"),        # 翠绿
    "low": ("#f59e0b", "#fffbeb"),       # 琥珀黄
    "critical": ("#ef4444", "#fef2f2"),  # 警示红
    "unknown": ("#64748b", "#f1f5f9"),   # 冷静灰
}


def _acquire_lock() -> bool:
    """OS 内核级单实例文件锁：防止托盘图标重复出现，崩溃后自动释放。"""
    global _LOCK_FH
    try:
        fh = open(LOCK_FILE, "a+", encoding="utf-8")
    except OSError:
        return False

    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
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

                fh.seek(0)
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


def _is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


def _pythonw_executable() -> str:
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "python.exe":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return str(exe)


def _spawn_detached(args: list[str]) -> None:
    kwargs: dict = {"cwd": str(BASE_DIR), "close_fds": True}
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    subprocess.Popen(args, **kwargs)  # noqa: S603 - 仅启动项目自身受控脚本


def _ensure_web_running(port: int) -> bool:
    """确认本地 Web 面板正在监听；未启动则静默拉起并短暂等待就绪。"""
    if _is_port_open(port):
        return True
    server_py = BASE_DIR / "web" / "server.py"
    if not server_py.exists():
        return False
    try:
        _spawn_detached([_pythonw_executable(), str(server_py), "--port", str(port)])
    except OSError:
        return False
    for _ in range(15):
        if _is_port_open(port):
            return True
        time.sleep(0.2)
    return _is_port_open(port)


def _fmt_short_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso))
        return dt.strftime("%H:%M")
    except ValueError:
        return str(iso)[:16]


def _parse_ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return 0.0


def build_snapshot(state: dict | None = None, settings=None, *, now_ts: float | None = None) -> dict:
    """把 state.json 与当前配置整理为托盘图标、悬浮提示与菜单所需的轻量摘要。"""
    cur_ts = time.time() if now_ts is None else float(now_ts)
    st = m._load_state() if state is None else state
    cfg_err = ""
    if settings is None:
        try:
            settings = load_settings()
        except ConfigError as exc:
            cfg_err = str(exc)

    room = settings.api.display_room if settings else "校园生活监控"
    price = settings.api.price if settings else 0.0
    gas_enabled = bool(settings and settings.gas.enabled)

    elec_bal = st.get("last_balance")
    elec_level = str(st.get("last_level") or "unknown")
    elec_t = st.get("last_query_time")
    elec_auth_ts = _parse_ts(st.get("last_auth_error_at"))
    elec_ok_ts = _parse_ts(elec_t)
    elec_auth_bad = elec_auth_ts > elec_ok_ts and (cur_ts - elec_auth_ts) < 3600
    elec_stale = elec_ok_ts > 0 and (cur_ts - elec_ok_ts) > _STALE_SECONDS

    if isinstance(elec_bal, (int, float)):
        yuan_str = f" (≈¥{elec_bal * price:.1f})" if price > 0 else ""
        elec_line = f"⚡ 电费: {elec_bal:.2f} 度{yuan_str}"
        elec_short = f"电:{elec_bal:.1f}度"
    else:
        elec_line = "⚡ 电费: 暂无数据"
        elec_short = "电:--"

    if elec_auth_bad:
        elec_line += " [凭据失效]"
    elif elec_level == "critical":
        elec_line += " [告急]"
    elif elec_level == "low":
        elec_line += " [偏低]"
    elif elec_stale:
        elec_line += " [待更新]"

    gas_bal = st.get("gas_balance")
    gas_level = str(st.get("gas_level") or "unknown")
    gas_t = st.get("gas_last_query_time")
    gas_reading = st.get("gas_meter_reading") or ""
    gas_auth_ts = _parse_ts(st.get("gas_last_auth_error_at"))
    gas_ok_ts = _parse_ts(gas_t)
    gas_auth_bad = gas_auth_ts > gas_ok_ts and (cur_ts - gas_auth_ts) < 3600

    gas_line = ""
    gas_short = ""
    if gas_enabled or isinstance(gas_bal, (int, float)):
        if isinstance(gas_bal, (int, float)):
            rd = f" (表{gas_reading})" if gas_reading else ""
            gas_line = f"🔥 燃气: {gas_bal:.2f} 元{rd}"
            gas_short = f"气:{gas_bal:.1f}元"
        else:
            gas_line = "🔥 燃气: 暂无数据"
            gas_short = "气:--"
        if gas_auth_bad:
            gas_line += " [失效]"
        elif gas_level == "low":
            gas_line += " [偏低]"

    # 综合状态等级决定托盘图标颜色
    if cfg_err or elec_auth_bad or gas_auth_bad or elec_level == "critical":
        overall = "critical"
    elif elec_level == "low" or (gas_enabled and gas_level == "low") or elec_stale:
        overall = "low"
    elif isinstance(elec_bal, (int, float)):
        overall = "ok"
    else:
        overall = "unknown"

    updated_str = _fmt_short_time(elec_t or gas_t)
    tooltip_lines = [f"{room} · 更新 {updated_str}", elec_line]
    if gas_line:
        tooltip_lines.append(gas_line)
    if cfg_err:
        tooltip_lines.append("⚠ 配置异常")
    tooltip = "\n".join(tooltip_lines)
    if len(tooltip) > _TOOLTIP_MAX_LEN:
        compact = f"{room} ({updated_str})\n{elec_short}" + (f" | {gas_short}" if gas_short else "")
        tooltip = compact[:_TOOLTIP_MAX_LEN]

    return {
        "room": room,
        "overall": overall,
        "elec_line": elec_line,
        "gas_line": gas_line,
        "updated_str": updated_str,
        "tooltip": tooltip,
        "elec_auth_bad": elec_auth_bad,
        "gas_auth_bad": gas_auth_bad,
        "elec_level": elec_level,
        "gas_level": gas_level,
        "gas_enabled": gas_enabled,
        "config_error": cfg_err,
    }


def make_tray_image(status: str = "ok", size: int = 64):
    """用 Pillow 动态绘制清晰的圆形角标 + 闪电符号托盘图标（无需外部 .ico 文件）。"""
    from PIL import Image, ImageDraw

    bg_hex, fg_hex = _STATUS_COLORS.get(status, _STATUS_COLORS["unknown"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = max(2, size // 16)
    # 深色外描边提升浅色/深色任务栏对比度
    draw.ellipse((pad - 1, pad - 1, size - pad + 1, size - pad + 1), fill="#0f172a")
    draw.ellipse((pad + 1, pad + 1, size - pad - 1, size - pad - 1), fill=bg_hex)

    # 居中闪电多边形
    s = size
    bolt = [
        (int(s * 0.54), int(s * 0.16)),
        (int(s * 0.27), int(s * 0.54)),
        (int(s * 0.48), int(s * 0.54)),
        (int(s * 0.42), int(s * 0.84)),
        (int(s * 0.73), int(s * 0.45)),
        (int(s * 0.52), int(s * 0.45)),
    ]
    draw.polygon(bolt, fill=fg_hex)
    return img


def _stop_all_services(port: int) -> None:
    """调用同目录 stop.bat（隐藏窗口）或精确终止 Web 面板与保活进程。"""
    if os.name == "nt":
        stop_bat = BASE_DIR / "stop.bat"
        if stop_bat.exists():
            env = dict(os.environ)
            env["NO_PAUSE"] = "1"
            try:
                subprocess.run(
                    ["cmd", "/c", str(stop_bat)],
                    cwd=str(BASE_DIR),
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=10,
                    check=False,
                )
                return
            except Exception:  # noqa: BLE001
                pass
    # 非 Windows 或兜底：按 keepalive.pid 温和终止
    kp = BASE_DIR / "keepalive.pid"
    if kp.exists():
        try:
            pid = int(kp.read_text(encoding="utf-8").strip())
            os.kill(pid, 15)
        except (ValueError, OSError):
            pass


class CampusTrayApp:
    """系统托盘常驻控制器。"""

    def __init__(self, port: int = 8877, poll_seconds: int = 15) -> None:
        self.port = int(port)
        self.poll_seconds = max(5, int(poll_seconds))
        self.url = f"http://127.0.0.1:{self.port}"
        self._stop_event = threading.Event()
        self._refresh_lock = threading.Lock()
        self._snapshot = build_snapshot()
        self._last_alert_key = ""
        self._icon = None

    def _notify(self, title: str, message: str) -> None:
        if self._icon is None:
            return
        try:
            if getattr(self._icon, "HAS_NOTIFICATION", False):
                self._icon.notify(message, title)
        except Exception:  # noqa: BLE001
            pass

    def _sync_snapshot(self, *, notify_changes: bool = True) -> dict:
        snap = build_snapshot()
        prev_status = self._snapshot.get("overall")
        self._snapshot = snap
        if self._icon is not None:
            try:
                self._icon.title = snap["tooltip"]
                if snap["overall"] != prev_status:
                    self._icon.icon = make_tray_image(snap["overall"])
                self._icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

        if notify_changes:
            alert_key = ""
            alert_msg = ""
            if snap["elec_auth_bad"]:
                alert_key = "elec_auth"
                alert_msg = "电费会话已失效，点击托盘打开 WebUI 更新凭据"
            elif snap["gas_auth_bad"]:
                alert_key = "gas_auth"
                alert_msg = "燃气 Token 已失效，点击托盘打开 WebUI 更新凭据"
            elif snap["elec_level"] == "critical":
                alert_key = "elec_critical"
                alert_msg = f"{snap['elec_line']}，请尽快充电费！"
            elif snap["elec_level"] == "low":
                alert_key = "elec_low"
                alert_msg = f"{snap['elec_line']}，低于预警阈值"
            elif snap["gas_enabled"] and snap["gas_level"] == "low":
                alert_key = "gas_low"
                alert_msg = f"{snap['gas_line']}，低于燃气提醒阈值"

            if alert_key and alert_key != self._last_alert_key:
                self._last_alert_key = alert_key
                self._notify(f"校园生活监控 · {snap['room']}", alert_msg)
            elif not alert_key:
                self._last_alert_key = ""
        return snap

    def open_webui(self, _icon=None, _item=None) -> None:
        def _worker() -> None:
            _ensure_web_running(self.port)
            webbrowser.open(self.url)

        threading.Thread(target=_worker, daemon=True).start()

    def refresh_now(self, target: str = "all") -> None:
        """在后台线程触发实时刷新，不阻塞托盘 UI 消息循环。"""
        def _worker() -> None:
            if not self._refresh_lock.acquire(blocking=False):
                return
            try:
                try:
                    settings = load_settings()
                except ConfigError as exc:
                    self._notify("配置错误", str(exc))
                    return
                msgs: list[str] = []
                if target in ("all", "elec"):
                    _, e_err = m.fetch_and_update_elec(settings, dry_run=False, notify=False)
                    if e_err:
                        msgs.append(f"电费失败: {e_err.get('message', '')[:40]}")
                if target in ("all", "gas") and settings.gas.enabled:
                    _, g_err = m.fetch_and_update_gas(
                        settings, dry_run=False, notify=False, respect_interval=False
                    )
                    if g_err:
                        msgs.append(f"燃气提示: {g_err.get('message', '')[:40]}")
                snap = self._sync_snapshot(notify_changes=False)
                if msgs:
                    self._notify("刷新完成（含提示）", " | ".join(msgs))
                else:
                    summary = snap["elec_line"] + (f"\n{snap['gas_line']}" if snap["gas_line"] else "")
                    self._notify("刷新成功", summary)
            finally:
                self._refresh_lock.release()

        threading.Thread(target=_worker, daemon=True).start()

    def open_log_file(self, _icon=None, _item=None) -> None:
        try:
            if not LOG_FILE.exists():
                LOG_FILE.write_text("", encoding="utf-8")
            if os.name == "nt":
                os.startfile(str(LOG_FILE))  # noqa: S606 - 打开本机自身日志文件
            else:
                webbrowser.open(LOG_FILE.as_uri())
        except OSError:
            pass

    def quit_tray_only(self, icon=None, _item=None) -> None:
        self._stop_event.set()
        target = icon or self._icon
        if target is not None:
            target.stop()

    def quit_all(self, icon=None, _item=None) -> None:
        self._stop_event.set()
        _stop_all_services(self.port)
        target = icon or self._icon
        if target is not None:
            target.stop()

    def _poll_loop(self) -> None:
        # 启动后短暂延迟再做首次后台轮询，避免抢占首屏创建
        while not self._stop_event.wait(self.poll_seconds):
            self._sync_snapshot(notify_changes=True)

    def _build_menu(self):
        import pystray

        items = [
            pystray.MenuItem(
                lambda _item: f"🌐 打开 Web 面板（{self._snapshot['room']}）",
                self.open_webui,
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _item: self._snapshot["elec_line"], None, enabled=False),
            pystray.MenuItem(
                lambda _item: self._snapshot["gas_line"] or "🔥 燃气: 未配置",
                None,
                enabled=False,
                visible=lambda _item: bool(self._snapshot["gas_line"]),
            ),
            pystray.MenuItem(
                lambda _item: f"🕒 上次更新: {self._snapshot['updated_str']}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🔄 立即刷新全部", lambda _i, _m: self.refresh_now("all")),
            pystray.MenuItem("⚡ 仅刷新电费", lambda _i, _m: self.refresh_now("elec")),
            pystray.MenuItem(
                "🔥 仅刷新燃气",
                lambda _i, _m: self.refresh_now("gas"),
                visible=lambda _item: bool(self._snapshot["gas_enabled"]),
            ),
            pystray.MenuItem("📜 查看保活日志", self.open_log_file),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("隐藏托盘（保持后台运行）", self.quit_tray_only),
            pystray.MenuItem("⛔ 停止全部监控并退出", self.quit_all),
        ]
        return pystray.Menu(*items)

    def run(self) -> int:
        import pystray

        icon = pystray.Icon(
            "campus_life_monitor",
            icon=make_tray_image(self._snapshot["overall"]),
            title=self._snapshot["tooltip"],
            menu=self._build_menu(),
        )
        self._icon = icon

        def _setup(ic) -> None:
            ic.visible = True
            t = threading.Thread(target=self._poll_loop, daemon=True)
            t.start()

        try:
            icon.run(setup=_setup)
        finally:
            self._stop_event.set()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校园生活监控系统托盘图标")
    parser.add_argument("--port", type=int, default=8877, help="本地 Web 面板端口（默认 8877）")
    parser.add_argument("--poll", type=int, default=15, help="托盘状态轮询间隔秒数（默认 15）")
    args = parser.parse_args(argv)

    if not _acquire_lock():
        return 0

    try:
        app = CampusTrayApp(port=args.port, poll_seconds=args.poll)
        return app.run()
    except ImportError:
        try:
            print("[ERROR] 系统托盘需要 pystray 与 Pillow，请执行: pip install -r requirements.txt", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        _release_lock()


if __name__ == "__main__":
    sys.exit(main())
