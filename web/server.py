#!/usr/bin/env python
"""本地电费与燃气面板：轻量 Web 控制中心（仅监听 127.0.0.1）。

用法：
    python web/server.py --port 8877
然后浏览器打开 http://127.0.0.1:8877

接口：
    GET  /api/status      → 当前状态 + 历史记录 + 充值事件 + 燃气续期倒计时（JSON）
    GET  /api/logs        → 读取 keepalive.log 最近 N 行实时运行日志
    GET  /api/settings    → 获取当前可调参数与推送渠道配置状态（不暴露敏感密钥）
    POST /api/settings    → 批量更新预警/紧急/异常耗电/燃气阈值、查询间隔、推送渠道到 .env
    POST /api/test_push   → 发送一条测试通知验证推送渠道
    POST /api/query       → 实时查询电费+燃气并更新状态（不触发推送）
    POST /api/query_elec  → 只刷新电费
    POST /api/query_gas   → 只刷新燃气
    POST /api/price       → 修改并持久化电费单价（ELEC_PRICE）
    POST /api/import_curl → 粘贴 cURL 或港华响应 JSON 快速热更新 .env 凭据
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
TOOLS_DIR = BASE / "tools"
LOG_FILE = BASE / "keepalive.log"
sys.path.insert(0, str(BASE))

import main as m  # noqa: E402
from config import ConfigError, PUSH_CHANNELS, load_settings, update_env, update_env_many  # noqa: E402
from notifier import NotifyError, send_notification  # noqa: E402

MAX_BODY_BYTES = 1024 * 256  # 256 KB，防意外大包
GAS_REFRESH_AGE_SECONDS = 5400.0  # 90 分钟主动续期线
GAS_TOKEN_TTL_SECONDS = 7200.0    # 120 分钟总有效期


def _merge_recharges(recorded: list | None, history: list, min_delta: float, unit: str) -> list[dict]:
    """合并 state 中已记录的充值流水与历史曲线回溯检测到的充值点（按时间去重）。"""
    by_time: dict[str, dict] = {}
    for ev in m.detect_recharges_from_history(history, min_delta, unit):
        by_time[ev["t"]] = ev
    if isinstance(recorded, list):
        for ev in recorded:
            if isinstance(ev, dict) and ev.get("t"):
                by_time[str(ev["t"])] = ev
    return sorted(by_time.values(), key=lambda x: str(x.get("t", "")))[-m.RECHARGE_HISTORY_MAX:]


def _gas_token_status(settings) -> dict:
    """计算燃气 Token 主动续期倒计时与总剩余时长，供面板直观展示。"""
    gas = settings.gas
    has_refresh = bool(gas.refresh_token and gas.clientid)
    issued = float(gas.token_issued_at or 0.0)
    if issued <= 0:
        return {
            "has_refresh": has_refresh,
            "issued_at": None,
            "next_refresh_in_sec": None,
            "expires_in_sec": None,
        }
    elapsed = max(0.0, time.time() - issued)
    return {
        "has_refresh": has_refresh,
        "issued_at": int(issued),
        "next_refresh_in_sec": int(GAS_REFRESH_AGE_SECONDS - elapsed),
        "expires_in_sec": int(GAS_TOKEN_TTL_SECONDS - elapsed),
    }


def _status_payload() -> dict:
    settings = load_settings()
    state = m._load_state()
    price = settings.api.price
    bal = state.get("last_balance")
    balance_yuan = round(bal * price, 2) if price > 0 and isinstance(bal, (int, float)) else None
    elec_hist = state.get("history") or []
    gas_hist = state.get("gas_history") or []
    elec_recharges = _merge_recharges(state.get("elec_recharges"), elec_hist, m.ELEC_RECHARGE_MIN_DELTA, "度")
    gas_recharges = _merge_recharges(state.get("gas_recharges"), gas_hist, m.GAS_RECHARGE_MIN_DELTA, "元")
    return {
        "balance": bal,
        "unit": "度",
        "level": state.get("last_level") or "unknown",
        "warn_threshold": settings.alert.warn_threshold,
        "critical_threshold": settings.alert.critical_threshold,
        "hourly_spike_threshold": settings.alert.hourly_spike_threshold,
        "last_query_time": state.get("last_query_time"),
        "last_auth_error_at": state.get("last_auth_error_at"),
        "room": settings.api.display_room,
        "price": price,
        "balance_yuan": balance_yuan,
        "history": elec_hist,
        "elec_recharges": elec_recharges,
        "gas_enabled": settings.gas.enabled,
        "gas_balance": state.get("gas_balance"),
        "gas_unit": "元",
        "gas_level": state.get("gas_level") or "unknown",
        "gas_warn_threshold": settings.gas.warn_threshold,
        "gas_last_query_time": state.get("gas_last_query_time"),
        "gas_subs_code": state.get("gas_subs_code") or "",
        "gas_meter_reading": state.get("gas_meter_reading"),
        "gas_last_auth_error_at": state.get("gas_last_auth_error_at"),
        "gas_maintenance": state.get("gas_maintenance"),
        "gas_maintenance_window": "每日 23:30–次日 00:30",
        "gas_history": gas_hist,
        "gas_recharges": gas_recharges,
        "gas_token_status": _gas_token_status(settings),
        "push_channels": list(settings.push.channels),
        "server_time": m._now(settings).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _settings_payload() -> dict:
    """返回可在网页控制中心安全查看和编辑的非敏感设置及渠道配置标记。"""
    settings = load_settings()
    return {
        "room_name": settings.api.room_name,
        "price": settings.api.price,
        "warn_threshold": settings.alert.warn_threshold,
        "critical_threshold": settings.alert.critical_threshold,
        "warn_once_per_day": settings.alert.warn_once_per_day,
        "hourly_spike_threshold": settings.alert.hourly_spike_threshold,
        "gas_warn_threshold": settings.gas.warn_threshold,
        "gas_query_interval": settings.gas.query_interval,
        "push_channel": settings.push.channel,
        "push_channels": list(settings.push.channels),
        "configured_channels": {
            "wecom": bool(settings.push.wecom_webhook),
            "pushplus": bool(settings.push.pushplus_token),
            "serverchan": bool(settings.push.serverchan_key),
            "bark": bool(settings.push.bark_url),
        },
    }


def _update_settings(data: dict) -> dict:
    """校验并原子写入网页控制中心提交的非敏感配置到 .env。"""
    current = load_settings()
    updates: dict[str, str] = {}

    warn = float(data["warn_threshold"]) if "warn_threshold" in data else current.alert.warn_threshold
    crit = float(data["critical_threshold"]) if "critical_threshold" in data else current.alert.critical_threshold
    spike = (
        float(data["hourly_spike_threshold"])
        if "hourly_spike_threshold" in data
        else current.alert.hourly_spike_threshold
    )
    gas_warn = float(data["gas_warn_threshold"]) if "gas_warn_threshold" in data else current.gas.warn_threshold
    gas_interval = (
        int(data["gas_query_interval"]) if "gas_query_interval" in data else current.gas.query_interval
    )

    if warn <= 0 or warn > 100000:
        raise ValueError("电费预警阈值必须在 0~100000 之间")
    if crit < 0 or (crit > 0 and crit >= warn):
        raise ValueError("电费告急阈值必须 ≥ 0 且小于预警阈值（0 表示禁用）")
    if spike < 0 or spike > 1000:
        raise ValueError("异常高耗电阈值范围应为 0~1000 度/小时（0 表示禁用）")
    if gas_warn <= 0 or gas_warn > 100000:
        raise ValueError("燃气预警阈值必须在 0~100000 之间")
    if gas_interval < 60 or gas_interval > 86400:
        raise ValueError("燃气查询间隔必须在 60~86400 秒之间")

    if "warn_threshold" in data:
        updates["WARN_THRESHOLD"] = f"{warn:g}"
    if "critical_threshold" in data:
        updates["CRITICAL_THRESHOLD"] = f"{crit:g}"
    if "hourly_spike_threshold" in data:
        updates["ELEC_SPIKE_THRESHOLD"] = f"{spike:g}"
    if "warn_once_per_day" in data:
        updates["WARN_ONCE_PER_DAY"] = "true" if bool(data["warn_once_per_day"]) else "false"
    if "gas_warn_threshold" in data:
        updates["GAS_WARN_THRESHOLD"] = f"{gas_warn:g}"
    if "gas_query_interval" in data:
        updates["GAS_QUERY_INTERVAL"] = str(gas_interval)
    if "room_name" in data:
        room_name = str(data["room_name"] or "").strip()
        if "\n" in room_name or "\r" in room_name:
            raise ValueError("宿舍显示名不能包含换行符")
        updates["CCB_ROOM_NAME"] = room_name
    if "push_channel" in data:
        raw_ch = str(data["push_channel"] or "").strip().lower()
        if raw_ch:
            parts = tuple(dict.fromkeys(p.strip() for p in raw_ch.split(",") if p.strip()))
            if "none" in parts and len(parts) > 1:
                raise ValueError("PUSH_CHANNEL 指定 none 时不能同时选择其他渠道")
            for p in parts:
                if p not in PUSH_CHANNELS:
                    raise ValueError(f"不支持的推送渠道：{p}")
                if p == "wecom" and not current.push.wecom_webhook:
                    raise ValueError("启用 wecom 需要先在 .env 配置 WECOM_WEBHOOK")
                if p == "pushplus" and not current.push.pushplus_token:
                    raise ValueError("启用 pushplus 需要先在 .env 配置 PUSHPLUS_TOKEN")
                if p == "serverchan" and not current.push.serverchan_key:
                    raise ValueError("启用 serverchan 需要先在 .env 配置 SERVERCHAN_KEY")
                if p == "bark" and not current.push.bark_url:
                    raise ValueError("启用 bark 需要先在 .env 配置 BARK_URL")
            updates["PUSH_CHANNEL"] = ",".join(parts)
        else:
            updates["PUSH_CHANNEL"] = ""

    if not updates:
        raise ValueError("未提供任何需要更新的设置项")

    update_env_many(updates)
    load_settings()  # 立即重载验证配置合法
    return {
        "ok": True,
        "updated_keys": sorted(updates.keys()),
        "settings": _settings_payload(),
        "message": "设置已保存到 .env（无需重启即刻生效）",
    }


def _send_test_push() -> dict:
    """向当前启用的推送渠道发送一条测试消息。"""
    settings = load_settings()
    if settings.push.is_none:
        raise ValueError("当前推送渠道为 none（未配置或已关闭推送）")
    now = m._now(settings)
    title = "【校园生活监控】测试推送成功"
    content = (
        f"宿舍：{settings.api.display_room}\n"
        f"当前启用渠道：{', '.join(settings.push.channels)}\n"
        f"发送时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    ok = send_notification(settings.push, title, content)
    return {
        "ok": bool(ok),
        "channels": list(settings.push.channels),
        "message": f"已向 {', '.join(settings.push.channels)} 发送测试推送",
    }


def _read_logs(tail: int = 80) -> dict:
    """读取 keepalive.log 最近 N 行（安全限制上限 300 行）。"""
    n = max(1, min(int(tail), 300))
    if not LOG_FILE.exists():
        return {"exists": False, "path": LOG_FILE.name, "lines": [], "total_lines": 0}
    try:
        all_lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"exists": True, "path": LOG_FILE.name, "lines": [f"[读取失败] {exc}"], "total_lines": 0}
    return {
        "exists": True,
        "path": LOG_FILE.name,
        "lines": all_lines[-n:],
        "total_lines": len(all_lines),
    }


def _query_once() -> dict:
    """手动刷新：实时查询电费+燃气并更新状态（不触发推送）。电费失败不影响燃气刷新。"""
    settings = load_settings()
    _, elec_error = m.fetch_and_update_elec(settings, dry_run=False, notify=False)
    gas_error = None
    if settings.gas.enabled:
        _, gas_error = m.fetch_and_update_gas(settings, dry_run=False, notify=False, respect_interval=False)
    payload = _status_payload()
    if elec_error:
        payload["elec_error"] = elec_error
    if gas_error:
        payload["gas_error"] = gas_error
    return payload


def _query_elec_only() -> dict:
    """只刷新电费（独立按钮用），不触碰燃气。"""
    settings = load_settings()
    _, elec_error = m.fetch_and_update_elec(settings, dry_run=False, notify=False)
    payload = _status_payload()
    if elec_error:
        payload["elec_error"] = elec_error
    return payload


def _query_gas_only() -> dict:
    """只刷新燃气（独立按钮用）。"""
    settings = load_settings()
    _, gas_error = m.fetch_and_update_gas(settings, dry_run=False, notify=False, respect_interval=False)
    payload = _status_payload()
    if gas_error:
        payload["gas_error"] = gas_error
    return payload


def _import_curl(raw_text: str) -> dict:
    """从粘贴的 cURL 命令或港华 OAuth 响应 JSON 提取电费/燃气凭据并热更新 .env。"""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    from harvest_creds import parse_input  # noqa: PLC0415

    extracted = parse_input(raw_text)
    if not extracted:
        raise ValueError("未能识别出电费（YJF004 cURL）或燃气（towngasvcc cURL / access_token 响应 JSON）凭据")

    update_env_many(extracted)
    if any(k in extracted for k in ("CCB_SKEY", "CCB_COOKIE")):
        try:
            if m.COOKIE_STORE.exists():
                m.COOKIE_STORE.unlink()
        except OSError:
            pass
    return {
        "ok": True,
        "updated_keys": sorted(extracted.keys()),
        "message": f"已更新 {len(extracted)} 项凭据：{', '.join(sorted(extracted.keys()))}",
    }


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length < 0 or length > MAX_BODY_BYTES:
        raise ValueError(f"请求体过大（上限 {MAX_BODY_BYTES} 字节）")
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def _send_json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_static(handler: BaseHTTPRequestHandler, rel: str) -> None:
    root = STATIC_DIR.resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return _send_json(handler, 404, {"error": "not found"})
    if not target.is_file():
        return _send_json(handler, 404, {"error": "not found"})
    ctype = {
        "html": "text/html; charset=utf-8",
        "css": "text/css; charset=utf-8",
        "js": "application/javascript; charset=utf-8",
        "svg": "image/svg+xml",
        "png": "image/png",
    }.get(target.suffix.lstrip("."), "application/octet-stream")
    data = target.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return _send_static(self, "index.html")
        if path == "/api/status":
            try:
                return _send_json(self, 200, _status_payload())
            except ConfigError as exc:
                return _send_json(self, 500, {"error": "config", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/settings":
            try:
                return _send_json(self, 200, _settings_payload())
            except ConfigError as exc:
                return _send_json(self, 500, {"error": "config", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/logs":
            try:
                qs = parse_qs(parsed.query)
                tail = int((qs.get("tail") or ["80"])[0])
                return _send_json(self, 200, _read_logs(tail))
            except (ValueError, TypeError) as exc:
                return _send_json(self, 400, {"error": "bad-request", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        return _send_json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/query":
            try:
                return _send_json(self, 200, _query_once())
            except ConfigError as exc:
                return _send_json(self, 500, {"error": "config", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/query_gas":
            try:
                return _send_json(self, 200, _query_gas_only())
            except ConfigError as exc:
                return _send_json(self, 500, {"error": "config", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/query_elec":
            try:
                return _send_json(self, 200, _query_elec_only())
            except ConfigError as exc:
                return _send_json(self, 500, {"error": "config", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/price":
            try:
                data = _read_json_body(self)
                price = float(data.get("price"))
                if not (0 <= price <= 1000):
                    raise ValueError("单价范围应为 0~1000 元/度")
                update_env("ELEC_PRICE", f"{price:g}")
                load_settings()  # 立即重载，确认可解析
                return _send_json(self, 200, {"ok": True, "price": price, "message": "单价已保存到 .env"})
            except (ValueError, TypeError) as exc:
                return _send_json(self, 400, {"error": "bad-request", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/settings":
            try:
                data = _read_json_body(self)
                return _send_json(self, 200, _update_settings(data))
            except (ValueError, TypeError, ConfigError) as exc:
                return _send_json(self, 400, {"error": "bad-request", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/test_push":
            try:
                return _send_json(self, 200, _send_test_push())
            except (ValueError, ConfigError) as exc:
                return _send_json(self, 400, {"error": "bad-request", "message": str(exc)})
            except NotifyError as exc:
                return _send_json(self, 502, {"error": "notify-failed", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/import_curl":
            try:
                data = _read_json_body(self)
                curl_text = str(data.get("curl") or "").strip()
                if not curl_text:
                    raise ValueError("请粘贴 cURL 命令或响应 JSON 内容")
                return _send_json(self, 200, _import_curl(curl_text))
            except (ValueError, TypeError) as exc:
                return _send_json(self, 400, {"error": "bad-request", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        return _send_json(self, 404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        # pythonw 无控制台时 sys.stdout 为 None，print 会抛异常导致请求失败
        try:
            print(f"[web] {self.address_string()} {fmt % args}", flush=True)
        except Exception:  # noqa: BLE001
            pass


class _PanelServer(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 会让两个进程同时绑同一端口（双实例互相抢请求），
    # 这里禁用它，保证端口独占；同名进程重复启动会直接报错退出。
    allow_reuse_address = False


def _log(message: str) -> None:
    """pythonw 无控制台时 sys.stdout 为 None，print 会抛异常，这里静默降级。"""
    try:
        print(message, flush=True)
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地电费与燃气面板 Web 服务")
    parser.add_argument("--port", type=int, default=8877, help="监听端口（默认 8877）")
    args = parser.parse_args(argv)
    try:
        httpd = _PanelServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        _log(f"[ERROR] 端口 {args.port} 被占用（可能已有面板在运行）：{exc}")
        return 1
    _log(f"校园生活监控面板已启动：http://127.0.0.1:{args.port}")
    _log("页面每 30 秒自动同步；「立即刷新」会实时查询网关。Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
