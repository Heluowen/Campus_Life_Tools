"""从 .chls 抓包里提取最近一次燃气业务请求的 Authorization token。

用途：当 token 交换接口（accessToekn / refreshToken）没被抓到时，页面实际发往
vcc-cbs 的业务请求（如 charge/preCheck）的 Authorization 头就是它的有效
access token。可直接写回 .env 的 GAS_TOKEN 恢复燃气查询。

用法：
    python tools/harvest_gas_token.py <file.chls> [--write]

    --write  提取成功后直接写入项目 .env 的 GAS_TOKEN
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import chls_dump as C
import extract as E


def hdr_map(reqh):
    names = reqh.get('fieldNames') or []
    vals = reqh.get('fieldValues') or []
    out = {}
    for i in range(min(len(names), len(vals))):
        n = names[i].get('value') if isinstance(names[i], dict) else str(names[i])
        v = vals[i].get('value') if isinstance(vals[i], dict) else str(vals[i])
        out[str(n)] = str(v)
    return out


def harvest(path):
    data = open(path, 'rb').read()
    objs = C.Reader(data).read_top()
    txs = []
    for o in objs:
        E.walk(o, txs)
    txs = [t for t in txs if t.cls == 'com.xk72.charles.model.Transaction']
    last = None
    for t in txs:
        f = t.fields
        host = E.plain_val(f.get('host')) or {}
        hostname = host.get('hostName') if isinstance(host, dict) else str(host)
        if 'towngasvcc' not in str(hostname) or 'vcc-cbs' not in hostname and 'weixin' not in hostname:
            continue
        reqh = E.plain_val(f.get('requestHeader')) or {}
        hdrs = hdr_map(reqh)
        auth = hdrs.get('Authorization') or hdrs.get('authorization')
        if auth and auth.startswith('Bearer '):
            last = auth.split(' ', 1)[1].strip()
    return last


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    tok = harvest(argv[1])
    if not tok:
        print('未找到 Authorization 头（抓包需包含解密后的业务请求）')
        return 1
    print('OK GAS_TOKEN =', tok)
    if '--write' in argv:
        from config import update_env
        update_env('GAS_TOKEN', tok)
        print('已写入 .env')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
