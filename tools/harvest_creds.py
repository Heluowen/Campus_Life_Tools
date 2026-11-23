"""一站式凭据提取工具：从 Charles .chls 抓包文件或 cURL 命令自动解析电费 + 燃气全套凭据。

支持的提取项：
- 电费（建行校园e码通 B2CMainPlat_00 / TXCODE=YJF004）：
  CCB_USERID, CCB_SKEY, CCB_ROOMID, CCB_COOKIE, CCB_BRANCHID, CCB_REFERER, CCB_USER_AGENT
- 燃气（港华 vcc-cbs / vcc-oauth）：
  GAS_SUBSID, GAS_TOKEN, GAS_COOKIE, GAS_CLIENTID, GAS_REFRESH_TOKEN, GAS_TOKEN_ISSUED_AT

用法：
    python tools/harvest_creds.py <capture.chls> [--write]
    python tools/harvest_creds.py --curl <curl.txt> [--write]
    python tools/harvest_creds.py --curl-stdin [--write]
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOOLS_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOLS_DIR.parent
for _p in (str(TOOLS_DIR), str(BASE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _header_map_from_tx(reqh: dict) -> dict[str, str]:
    names = reqh.get("fieldNames") or []
    vals = reqh.get("fieldValues") or []
    out: dict[str, str] = {}
    for i in range(min(len(names), len(vals))):
        n = names[i].get("value") if isinstance(names[i], dict) else str(names[i])
        v = vals[i].get("value") if isinstance(vals[i], dict) else str(vals[i])
        key = str(n).strip().lower()
        val = str(v).strip()
        if not key:
            continue
        if key == "cookie" and key in out:
            out[key] = f"{out[key]}; {val}"
        else:
            out[key] = val
    return out


def _extract_from_request(
    url: str,
    headers: dict[str, str],
    req_body: str = "",
    resp_body: str = "",
) -> dict[str, str]:
    """从单条 HTTP 事务（URL / headers / 请求体 / 响应体）中提取可用凭据。"""
    low_headers = {k.lower(): v for k, v in headers.items()}
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query, keep_blank_values=False)
    form = parse_qs(req_body, keep_blank_values=False) if req_body else {}

    out: dict[str, str] = {}

    # 1. 建行校园e码通电费（B2CMainPlat_00 + TXCODE=YJF004）
    is_ccb = "xiaoyuan.ccb.com" in host or "b2cmainplat_00" in path.lower()
    if is_ccb:
        txcode = (form.get("TXCODE") or query.get("TXCODE") or [""])[0].upper()
        if txcode == "YJF004" or ("SKEY" in form and "USERID" in form and "roomid" in form):
            if form.get("USERID"):
                out["CCB_USERID"] = form["USERID"][0]
            if form.get("SKEY"):
                out["CCB_SKEY"] = form["SKEY"][0]
            if form.get("roomid"):
                out["CCB_ROOMID"] = form["roomid"][0]
            if form.get("BRANCHID"):
                out["CCB_BRANCHID"] = form["BRANCHID"][0]
            if low_headers.get("cookie"):
                out["CCB_COOKIE"] = low_headers["cookie"]
            if low_headers.get("referer"):
                out["CCB_REFERER"] = low_headers["referer"]
            if low_headers.get("user-agent"):
                out["CCB_USER_AGENT"] = low_headers["user-agent"]

    # 2. 港华燃气（weixin.towngasvcc.com / vcc-cbs / vcc-oauth）
    is_gas = "towngasvcc.com" in host or "/vcc-cbs/" in path or "/vcc-oauth/" in path
    if is_gas:
        if query.get("subsId"):
            out["GAS_SUBSID"] = query["subsId"][0]
        if query.get("clientid"):
            out["GAS_CLIENTID"] = query["clientid"][0]
        if form.get("refreshToken"):
            out["GAS_REFRESH_TOKEN"] = form["refreshToken"][0]
        auth = low_headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth.split(" ", 1)[1].strip()
            if tok:
                out["GAS_TOKEN"] = tok
        if low_headers.get("cookie"):
            out["GAS_COOKIE"] = low_headers["cookie"]

        # 若响应体是 accessToekn / refreshToken 返回的 JSON，优先取最新的 token 对
        if resp_body and ("access_token" in resp_body or "refresh_token" in resp_body):
            try:
                j = json.loads(resp_body)
            except ValueError:
                j = None
            if isinstance(j, dict):
                inner = j.get("data") if isinstance(j.get("data"), dict) else {}
                access = j.get("access_token") or inner.get("access_token")
                refresh = j.get("refresh_token") or inner.get("refresh_token")
                if access:
                    out["GAS_TOKEN"] = str(access)
                    out["GAS_TOKEN_ISSUED_AT"] = str(int(time.time()))
                if refresh:
                    out["GAS_REFRESH_TOKEN"] = str(refresh)

    return out


def parse_token_json(text: str) -> dict[str, str]:
    """解析港华 vcc-oauth accessToekn / refreshToken 的响应 JSON。

    支持顶层或 data 内层包含 access_token / refresh_token（以及可选 clientid / subsId）。
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return {}
    try:
        obj = json.loads(stripped)
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}
    inner = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    out: dict[str, str] = {}
    access = obj.get("access_token") or inner.get("access_token")
    refresh = obj.get("refresh_token") or inner.get("refresh_token")
    if access and str(access).strip():
        out["GAS_TOKEN"] = str(access).strip()
        out["GAS_TOKEN_ISSUED_AT"] = str(int(time.time()))
    if refresh and str(refresh).strip():
        out["GAS_REFRESH_TOKEN"] = str(refresh).strip()
    clientid = obj.get("clientid") or inner.get("clientid")
    if clientid and str(clientid).strip():
        out["GAS_CLIENTID"] = str(clientid).strip()
    subsid = obj.get("subsId") or inner.get("subsId")
    if subsid and str(subsid).strip():
        out["GAS_SUBSID"] = str(subsid).strip()
    return out


def parse_input(text: str) -> dict[str, str]:
    """统一解析粘贴内容：自动识别港华 OAuth 响应 JSON 或 cURL 命令。"""
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        got = parse_token_json(stripped)
        if got:
            return got
    return parse_curl(stripped)


def parse_curl(curl_text: str) -> dict[str, str]:
    """解析常见浏览器/Charles 导出的 cURL（bash / PowerShell / cmd 续行格式）。"""
    # 处理反斜杠、脱字符(^)和反引号(`)换行续接
    normalized = re.sub(r"(\\|\^|`)\r?\n", " ", curl_text.strip())
    # 兼容 Windows cmd 双引号中 "" 转义
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        tokens = shlex.split(normalized, posix=False)

    url = ""
    headers: dict[str, str] = {}
    body_parts: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("curl", "curl.exe"):
            i += 1
            continue
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            hdr = tokens[i + 1]
            if ":" in hdr:
                k, v = hdr.split(":", 1)
                k_low = k.strip().lower()
                v_str = v.strip()
                if k_low == "cookie" and k_low in headers:
                    headers[k_low] = f"{headers[k_low]}; {v_str}"
                else:
                    headers[k_low] = v_str
            i += 2
            continue
        if tok in ("-b", "--cookie") and i + 1 < len(tokens):
            val = tokens[i + 1].strip()
            headers["cookie"] = f"{headers['cookie']}; {val}" if "cookie" in headers else val
            i += 2
            continue
        if tok in ("-A", "--user-agent") and i + 1 < len(tokens):
            headers["user-agent"] = tokens[i + 1].strip()
            i += 2
            continue
        if tok in ("-e", "--referer") and i + 1 < len(tokens):
            headers["referer"] = tokens[i + 1].strip()
            i += 2
            continue
        if tok in ("-d", "--data", "--data-raw", "--data-urlencode", "--data-binary", "--data-ascii") and i + 1 < len(tokens):
            body_parts.append(tokens[i + 1])
            i += 2
            continue
        if tok in ("--url",) and i + 1 < len(tokens):
            url = tokens[i + 1].strip()
            i += 2
            continue
        if tok.startswith(("http://", "https://")) and not url:
            url = tok.strip()
        i += 1

    if not url:
        return {}
    return _extract_from_request(url, headers, "&".join(body_parts))


def _fallback_scan_chls(raw: bytes) -> dict[str, str]:
    """当 chls_dump 解析某些 JDK 11+ Java 对象标签（如 StackTraceElement）失败时的兜底二进制扫描。"""
    out: dict[str, str] = {}

    # 1. 扫描港华燃气（towngasvcc）
    if b"towngasvcc" in raw:
        bearers = re.findall(rb"Bearer ([0-9a-fA-F]{20,64})", raw)
        if bearers:
            out["GAS_TOKEN"] = bearers[-1].decode("ascii")
        subs = re.findall(rb"subsId=([0-9a-zA-Z]{8,64})", raw)
        if subs:
            out["GAS_SUBSID"] = subs[-1].decode("ascii")
        cids = re.findall(rb"clientid=([0-9a-zA-Z_\-]{6,64})", raw)
        if cids:
            out["GAS_CLIENTID"] = cids[-1].decode("ascii")
        tickets = re.findall(rb"__h5_oauth_ticket__=([^;\x00-\x1f\r\n]+)", raw)
        if tickets:
            out["GAS_COOKIE"] = f"__h5_oauth_ticket__={tickets[-1].decode('latin1')}"
        for m_json in re.finditer(rb"\{[^{}]{0,400}\"refresh_token\"[^{}]{0,400}\}", raw):
            try:
                j = json.loads(m_json.group(0).decode("utf-8", errors="ignore"))
            except ValueError:
                continue
            if isinstance(j, dict):
                if j.get("access_token"):
                    out["GAS_TOKEN"] = str(j["access_token"])
                    out["GAS_TOKEN_ISSUED_AT"] = str(int(time.time()))
                if j.get("refresh_token"):
                    out["GAS_REFRESH_TOKEN"] = str(j["refresh_token"])

    # 2. 扫描建行电费（TXCODE=YJF004）
    for m_form in re.finditer(rb"[^\x00-\x1f\"'<>]{0,200}TXCODE=YJF004[^\x00-\x1f\"'<>]{0,300}", raw):
        body = m_form.group(0).decode("latin1")
        form = parse_qs(body, keep_blank_values=False)
        if form.get("USERID") and form.get("SKEY") and form.get("roomid"):
            out["CCB_USERID"] = form["USERID"][0]
            out["CCB_SKEY"] = form["SKEY"][0]
            out["CCB_ROOMID"] = form["roomid"][0]
            if form.get("BRANCHID"):
                out["CCB_BRANCHID"] = form["BRANCHID"][0]

    return out


def harvest_chls(path: str | Path) -> dict[str, str]:
    """解析 Charles .chls 文件，按时间顺序覆盖提取最新一组电费与燃气凭据。"""
    import chls_dump as C  # noqa: PLC0415
    import extract as E  # noqa: PLC0415

    data = Path(path).read_bytes()
    try:
        objs = C.Reader(data).read_top()
    except Exception:  # noqa: BLE001 - 部分 Charles 抓包含 JDK 11+ 异常对象，自动回退原始流扫描
        return _fallback_scan_chls(data)

    txs = []
    for o in objs:
        E.walk(o, txs)
    txs = [t for t in txs if t.cls == "com.xk72.charles.model.Transaction"]

    merged: dict[str, str] = {}
    for t in txs:
        f = t.fields
        host_obj = E.plain_val(f.get("host")) or {}
        hostname = host_obj.get("hostName") if isinstance(host_obj, dict) else str(host_obj)
        reqh = E.plain_val(f.get("requestHeader")) or {}
        if not isinstance(reqh, dict):
            continue
        first_line = E.plain_val(reqh.get("firstLine")) or ""
        headers = _header_map_from_tx(reqh)
        req_target = ""
        if isinstance(first_line, str) and first_line.strip():
            parts = first_line.split()
            req_target = parts[1] if len(parts) >= 2 else ""
        # HTTP/2 事务在 Charles 中没有传统 HTTP/1.1 firstLine，请求路径在 :path 伪头中
        if not req_target:
            req_target = headers.get(":path") or str(E.plain_val(f.get("pathName")) or "")
        if req_target.startswith(("http://", "https://")):
            url = req_target
        else:
            scheme = headers.get(":scheme") or E.plain_val(f.get("protocol")) or "https"
            authority = headers.get(":authority") or hostname
            url = f"{scheme}://{authority}{req_target}"
        req_body = E.body_text(f.get("requestBody")) or ""
        resp_body = E.body_text(f.get("responseBody")) or ""
        extracted = _extract_from_request(url, headers, req_body, resp_body)
        if extracted:
            merged.update(extracted)
    return merged


def _mask(key: str, val: str) -> str:
    if any(s in key for s in ("SKEY", "COOKIE", "TOKEN")):
        if len(val) <= 10:
            return "***"
        return f"{val[:4]}...{val[-4:]} (len={len(val)})"
    return val


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 .chls 或 cURL 自动提取电费与燃气凭据")
    parser.add_argument("chls_file", nargs="?", help="Charles .chls 文件路径")
    parser.add_argument("--curl", default="", help="包含 cURL 命令的文本文件路径")
    parser.add_argument("--curl-stdin", action="store_true", help="从标准输入读取 cURL 命令")
    parser.add_argument("--write", action="store_true", help="直接写回项目 .env（并清理旧 cookies.json）")
    args = parser.parse_args(argv)

    if args.curl_stdin:
        extracted = parse_input(sys.stdin.read())
    elif args.curl:
        extracted = parse_input(Path(args.curl).read_text(encoding="utf-8"))
    elif args.chls_file:
        extracted = harvest_chls(args.chls_file)
    else:
        parser.print_help()
        return 2

    if not extracted:
        print("[WARN] 未识别到电费（YJF004）或燃气（towngasvcc）凭据，请确认已开启 HTTPS 解密。")
        return 1

    print(f"成功提取 {len(extracted)} 项凭据：")
    for k in sorted(extracted.keys()):
        print(f"  {k} = {_mask(k, extracted[k])}")

    if args.write:
        from config import update_env_many  # noqa: PLC0415

        update_env_many(extracted)
        cookie_store = BASE_DIR / "cookies.json"
        if any(k in extracted for k in ("CCB_SKEY", "CCB_COOKIE")) and cookie_store.exists():
            try:
                cookie_store.unlink()
                print("已清理旧 cookies.json（确保立即使用新会话 Cookie）")
            except OSError:
                pass
        print("已原子写入 .env。")
    else:
        print("提示：添加 --write 参数即可自动写入 .env。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
