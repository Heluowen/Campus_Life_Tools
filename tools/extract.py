"""Extract Charles transactions from .chls into structured summary.

Usage:
    python extract.py <file.chls>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chls_dump as C  # noqa: E402


def walk(v, acc, seen=None):
    if seen is None:
        seen = set()
    oid = id(v)
    if oid in seen:
        return
    seen.add(oid)
    if isinstance(v, C.JavaObject):
        acc.append(v)
        for x in v.fields.values():
            walk(x, acc, seen)
        for x in v.annotations:
            walk(x, acc, seen)
    elif isinstance(v, C.JavaArray):
        for x in v.values:
            walk(x, acc, seen)
    elif isinstance(v, list):
        for x in v:
            walk(x, acc, seen)


def plain_val(v, depth=0):
    if depth > 10:
        return "<deep>"
    if isinstance(v, C.BlockData):
        return v.text()
    if isinstance(v, C.JavaString):
        return v.value
    if isinstance(v, C.JavaObject):
        if v.cls == "java.util.ArrayList":
            return [plain_val(x, depth + 1) for x in v.annotations
                    if not isinstance(x, C.BlockData)]
        return {k: plain_val(x, depth + 1) for k, x in v.fields.items()}
    if isinstance(v, C.JavaArray):
        if len(v.desc_name) == 2 and v.desc_name[1] == "B":
            return bytes(v.values)
        return [plain_val(x, depth + 1) for x in v.values]
    if isinstance(v, list):
        return [plain_val(x, depth + 1) for x in v]
    return v


def body_text(body):
    if isinstance(body, C.JavaArray) and body.desc_name == "[B":
        data = bytes(body.values)
        if data[:2] == b"\x1f\x8b":
            import gzip
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary {len(data)}B>"
    return None


def main(argv):
    path = argv[1]
    data = open(path, "rb").read()
    objs = C.Reader(data).read_top()
    txs = []
    for o in objs:
        walk(o, txs)
    txs = [t for t in txs if t.cls == "com.xk72.charles.model.Transaction"]
    print(f"total transactions: {len(txs)}")
    for i, t in enumerate(txs):
        f = t.fields
        host = plain_val(f.get("host"))
        hostname = host.get("hostName") if isinstance(host, dict) else host
        reqh = plain_val(f.get("requestHeader")) or {}
        resh = plain_val(f.get("responseHeader")) or {}
        first = ""
        if isinstance(reqh, dict):
            fl = reqh.get("firstLine")
            first = plain_val(fl) if fl is not None else ""
        status = f.get("status")
        pathname = f.get("pathName")
        req_body = body_text(f.get("requestBody"))
        res_body = body_text(f.get("responseBody"))
        print("=" * 80)
        print(f"#{i} host={hostname} pathName={pathname!r} status={status}")
        print(f"  REQ: {first}")
        if req_body:
            print(f"  REQ-BODY({len(req_body)}): {req_body[:400]!r}")
        elif isinstance(f.get("requestBody"), C.JavaArray):
            print(f"  REQ-BODY: <binary/encrypted {len(f['requestBody'].values)}B>")
        print(f"  RESP: {plain_val(resh.get('firstLine')) if isinstance(resh, dict) else ''}")
        if res_body:
            print(f"  RESP-BODY({len(res_body)}): {res_body[:300]!r}")


if __name__ == "__main__":
    main(sys.argv)
