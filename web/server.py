#!/usr/bin/env python
"""本地电费面板：轻量 Web 服务（仅监听 127.0.0.1）。

用法：
    python web/server.py --port 8877
然后浏览器打开 http://127.0.0.1:8877

接口：
    GET  /api/status   → 当前状态 + 历史记录（JSON）
    POST /api/query    → 实时查询一次并更新状态（不触发推送）
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(BASE))

import main as m  # noqa: E402
from config import ConfigError, load_settings  # noqa: E402
from query_elec import AuthExpiredError, QueryError, query_balance  # noqa: E402
from query_gas import GasAuthExpiredError, GasQueryError, query_gas  # noqa: E402


def _load_state() -> dict:
    try:
        data = json.loads(m.STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _status_payload() -> dict:
    settings = load_settings()
    state = _load_state()
    return {
        "balance": state.get("last_balance"),
        "unit": "度",
        "level": state.get("last_level") or "unknown",
        "warn_threshold": settings.alert.warn_threshold,
        "critical_threshold": settings.alert.critical_threshold,
        "last_query_time": state.get("last_query_time"),
        "last_auth_error_at": state.get("last_auth_error_at"),
        "room": settings.api.display_room,
        "history": state.get("history") or [],
        "gas_enabled": settings.gas.enabled,
        "gas_balance": state.get("gas_balance"),
        "gas_unit": "元",
        "gas_level": state.get("gas_level") or "unknown",
        "gas_warn_threshold": settings.gas.warn_threshold,
        "gas_last_query_time": state.get("gas_last_query_time"),
        "gas_meter_reading": state.get("gas_meter_reading"),
        "gas_last_auth_error_at": state.get("gas_last_auth_error_at"),
        "gas_history": state.get("gas_history") or [],
        "server_time": m._now(settings).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _query_once() -> dict:
    """手动刷新：实时查询一次并更新状态（不触发推送，推送由保活任务负责）。"""
    settings = load_settings()
    now = m._now(settings)
    result = query_balance(
        settings.api,
        settings.runtime.request_timeout,
        settings.runtime.max_retries,
        settings.runtime.trust_env,
        m.COOKIE_STORE,
    )
    state = _load_state()
    state.pop("last_error", None)
    state["last_balance"] = round(result.balance, 2)
    state["last_query_time"] = now.isoformat()
    state["last_level"] = m._determine_level(result.balance, settings)
    hist = state.get("history")
    if not isinstance(hist, list):
        hist = []
    hist.append({"t": now.isoformat(timespec="seconds"), "bal": round(result.balance, 2)})
    state["history"] = hist[-m.HISTORY_MAX:]

    gas_error = None
    if settings.gas.enabled:
        try:
            gas = query_gas(
                settings.gas.subsid,
                settings.gas.token,
                settings.runtime.request_timeout,
                settings.runtime.max_retries,
                settings.runtime.trust_env,
                settings.gas.cookie,
            )
            state.pop("gas_last_error", None)
            state["gas_balance"] = round(gas.balance, 2)
            state["gas_last_query_time"] = now.isoformat()
            state["gas_level"] = "low" if gas.balance < settings.gas.warn_threshold else "ok"
            state["gas_meter_reading"] = gas.meter_reading
            gh = state.get("gas_history")
            if not isinstance(gh, list):
                gh = []
            gh.append({"t": now.isoformat(timespec="seconds"), "bal": round(gas.balance, 2)})
            state["gas_history"] = gh[-m.HISTORY_MAX:]
        except GasAuthExpiredError as exc:
            state["gas_last_auth_error_at"] = now.isoformat()
            gas_error = {"error": "gas-auth", "message": str(exc)}
        except GasQueryError as exc:
            state["gas_last_error"] = {"time": now.isoformat(), "message": str(exc)}
            gas_error = {"error": "gas-query", "message": str(exc)}

    m._save_state(state)
    payload = _status_payload()
    if gas_error:
        payload["gas_error"] = gas_error
    return payload


def _send_json(handler, code: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_static(handler, rel: str) -> None:
    root = STATIC_DIR.resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
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
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return _send_static(self, "index.html")
        if path == "/api/status":
            try:
                return _send_json(self, 200, _status_payload())
            except ConfigError as exc:
                return _send_json(self, 500, {"error": "config", "message": str(exc)})
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
            except AuthExpiredError as exc:
                return _send_json(self, 502, {"error": "auth", "message": str(exc)})
            except QueryError as exc:
                return _send_json(self, 502, {"error": "query", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        return _send_json(self, 404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地电费面板 Web 服务")
    parser.add_argument("--port", type=int, default=8877, help="监听端口（默认 8877）")
    args = parser.parse_args(argv)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"电费面板已启动：http://127.0.0.1:{args.port}")
    print("页面每 30 秒自动同步；「立即刷新」会实时查询网关。Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
