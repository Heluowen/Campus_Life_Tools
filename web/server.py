#!/usr/bin/env python
"""本地电费与燃气面板：轻量 Web 服务（仅监听 127.0.0.1）。

用法：
    python web/server.py --port 8877
然后浏览器打开 http://127.0.0.1:8877

接口：
    GET  /api/status      → 当前状态 + 历史记录（JSON）
    POST /api/query       → 实时查询电费+燃气并更新状态（不触发推送）
    POST /api/query_elec  → 只刷新电费
    POST /api/query_gas   → 只刷新燃气
    POST /api/price       → 修改并持久化电费单价（ELEC_PRICE）
    POST /api/import_curl → 粘贴 cURL 快速更新 .env 凭据（电费 / 燃气）
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
TOOLS_DIR = BASE / "tools"
sys.path.insert(0, str(BASE))

import main as m  # noqa: E402
from config import ConfigError, load_settings, update_env, update_env_many  # noqa: E402

MAX_BODY_BYTES = 1024 * 256  # 256 KB，防意外大包


def _status_payload() -> dict:
    settings = load_settings()
    state = m._load_state()
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
        "gas_subs_code": state.get("gas_subs_code") or "",
        "gas_meter_reading": state.get("gas_meter_reading"),
        "gas_last_auth_error_at": state.get("gas_last_auth_error_at"),
        "gas_maintenance": state.get("gas_maintenance"),
        "gas_maintenance_window": "每日 23:30–次日 00:30",
        "gas_history": state.get("gas_history") or [],
        "server_time": m._now(settings).strftime("%Y-%m-%d %H:%M:%S"),
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
        if path == "/api/import_curl":
            try:
                data = _read_json_body(self)
                curl_text = str(data.get("curl") or "").strip()
                if not curl_text:
                    raise ValueError("请粘贴 cURL 命令内容")
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
