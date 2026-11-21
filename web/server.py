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
from config import ConfigError, load_settings, update_env  # noqa: E402
from query_elec import AuthExpiredError, QueryError, query_balance  # noqa: E402
from query_gas import GasAuthExpiredError, GasQueryError, query_gas  # noqa: E402


def _load_state() -> dict:
    try:
        data = json.loads(m.STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set_env_value(key: str, value: str) -> None:
    """更新 .env 中的某个键（存在则替换，不存在则追加）。"""
    env_path = m.ENV_FILE
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    lines = text.splitlines()
    replaced = False
    out = []
    for line in lines:
        if line.startswith(key + "="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def _status_payload() -> dict:
    settings = load_settings()
    state = _load_state()
    price = settings.api.price
    bal = state.get("last_balance")
    balance_yuan = round(bal * price, 2) if price > 0 and isinstance(bal, (int, float)) else None
    return {
        "balance": bal,
        "unit": "度",
        "level": state.get("last_level") or "unknown",
        "warn_threshold": settings.alert.warn_threshold,
        "critical_threshold": settings.alert.critical_threshold,
        "last_query_time": state.get("last_query_time"),
        "last_auth_error_at": state.get("last_auth_error_at"),
        "room": settings.api.display_room,
        "price": price,
        "balance_yuan": balance_yuan,
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


def _apply_gas(settings, state: dict, now) -> dict | None:
    """查询一次燃气并写入 state；返回 gas_error（成功时为 None）。"""
    try:
        gas = query_gas(
            settings.gas.subsid,
            settings.gas.token,
            settings.runtime.request_timeout,
            settings.runtime.max_retries,
            settings.runtime.trust_env,
            settings.gas.cookie,
            refresh_token=settings.gas.refresh_token,
            clientid=settings.gas.clientid,
            on_token_refreshed=lambda tok: update_env("GAS_TOKEN", tok),
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
        return None
    except GasAuthExpiredError as exc:
        state["gas_last_auth_error_at"] = now.isoformat()
        return {"error": "gas-auth", "message": str(exc)}
    except GasQueryError as exc:
        state["gas_last_error"] = {"time": now.isoformat(), "message": str(exc)}
        return {"error": "gas-query", "message": str(exc)}


def _query_once() -> dict:
    """手动刷新：实时查询电费+燃气并更新状态（不触发推送）。电费失败不影响燃气刷新。"""
    settings = load_settings()
    now = m._now(settings)
    state = _load_state()

    elec_error = None
    try:
        result = query_balance(
            settings.api,
            settings.runtime.request_timeout,
            settings.runtime.max_retries,
            settings.runtime.trust_env,
            m.COOKIE_STORE,
        )
        state.pop("last_error", None)
        state["last_balance"] = round(result.balance, 2)
        state["last_query_time"] = now.isoformat()
        state["last_level"] = m._determine_level(result.balance, settings)
        hist = state.get("history")
        if not isinstance(hist, list):
            hist = []
        hist.append({"t": now.isoformat(timespec="seconds"), "bal": round(result.balance, 2)})
        state["history"] = hist[-m.HISTORY_MAX:]
    except AuthExpiredError as exc:
        state["last_auth_error_at"] = now.isoformat()
        elec_error = {"error": "auth", "message": str(exc)}
    except QueryError as exc:
        state["last_error"] = {"time": now.isoformat(), "message": str(exc)}
        elec_error = {"error": "query", "message": str(exc)}

    gas_error = None
    if settings.gas.enabled:
        gas_error = _apply_gas(settings, state, now)

    m._save_state(state)
    payload = _status_payload()
    if elec_error:
        payload["elec_error"] = elec_error
    if gas_error:
        payload["gas_error"] = gas_error
    return payload


def _query_gas_only() -> dict:
    """只刷新燃气（独立按钮用）。"""
    settings = load_settings()
    now = m._now(settings)
    state = _load_state()
    if settings.gas.enabled:
        gas_error = _apply_gas(settings, state, now)
    else:
        gas_error = {"error": "gas-disabled", "message": "未配置燃气（需要在 .env 填 GAS_SUBSID 与 GAS_TOKEN）"}
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
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/query_gas":
            try:
                return _send_json(self, 200, _query_gas_only())
            except ConfigError as exc:
                return _send_json(self, 500, {"error": "config", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        if path == "/api/price":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(raw.decode("utf-8"))
                price = float(data.get("price"))
                if not (0 <= price <= 1000):
                    raise ValueError("单价范围应为 0~1000 元/度")
                _set_env_value("ELEC_PRICE", f"{price:g}")
                load_settings()  # 立即重载，确认可解析
                return _send_json(self, 200, {"ok": True, "price": price, "message": "单价已保存到 .env"})
            except (ValueError, TypeError) as exc:
                return _send_json(self, 400, {"error": "bad-request", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return _send_json(self, 500, {"error": "internal", "message": str(exc)})
        return _send_json(self, 404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)


class _PanelServer(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 会让两个进程同时绑同一端口（双实例互相抢请求），
    # 这里禁用它，保证端口独占；同名进程重复启动会直接报错退出。
    allow_reuse_address = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地电费面板 Web 服务")
    parser.add_argument("--port", type=int, default=8877, help="监听端口（默认 8877）")
    args = parser.parse_args(argv)
    try:
        httpd = _PanelServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"[ERROR] 端口 {args.port} 被占用（可能已有面板在运行）：{exc}")
        return 1
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
