"""TTL 精确校准：抓包后探明会话空闲超时的准确值。

用法（在拿到新鲜 .env 凭据后立即运行）：
    python tools/ttl_probe.py --interval 120 --max-minutes 40

每 interval 秒发一次查询，直到返回「较长时间未操作」或达到 max-minutes；
打印最终存活时长与建议的保活间隔（推荐 ≈ TTL / 2）。

注意：校准时每 interval 秒一次请求，仅用于一次性测量，勿长期挂机。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ConfigError, load_settings
from query_elec import AuthExpiredError, QueryError, query_balance


def _now() -> datetime:
    return datetime.now()


def main() -> int:
    parser = argparse.ArgumentParser(description="会话空闲超时（TTL）校准")
    parser.add_argument("--interval", type=int, default=120, help="探测间隔秒数（默认 120）")
    parser.add_argument("--max-minutes", type=float, default=40, help="最长探测分钟数（默认 40）")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[ERROR] 配置错误：{exc}")
        return 2

    start = _now()
    deadline = start + timedelta(minutes=args.max_minutes)
    print(f"[TTL] 开始校准：间隔 {args.interval}s，最长 {args.max_minutes}min")
    print(f"[TTL] 若此前有过请求，请先等 1-2 分钟再运行以保证间隔准确")

    last_ok: datetime | None = None
    while True:
        now = _now()
        try:
            result = query_balance(
                settings.api,
                settings.runtime.request_timeout,
                settings.runtime.max_retries,
                settings.runtime.trust_env,
                Path(__file__).resolve().parent.parent / "cookies.json",
            )
            last_ok = now
            print(f"[{now:%H:%M:%S}] 存活 剩余={result.balance_text}")
        except AuthExpiredError as exc:
            since = (now - last_ok).total_seconds() if last_ok else None
            print(f"[{now:%H:%M:%S}] 会话已失效：{exc}")
            if since is not None:
                minutes = since / 60
                print(f"[TTL] 结论：空闲超时 TTL ≈ {minutes:.1f} 分钟（建议保活间隔 ≈ {minutes / 2:.0f} 秒~{minutes}分）")
            else:
                print("[TTL] 无存活记录（可能凭据本身已失效，请重新抓包）")
            return 0
        except QueryError as exc:
            print(f"[{now:%H:%M:%S}] 查询失败（不影响判断）：{exc}")

        if now >= deadline:
            print(f"[TTL] 达到最长探测时间仍未失效；TTL > {args.max_minutes} 分钟")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
