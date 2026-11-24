"""轻量级 SQLite 历史长期归档、月度/自定义账单统计与 CSV 导出（纯标准库 sqlite3，零第三方依赖）。

核心能力：
1. 永久留存历史采样点与充值流水（不受 state.json 1200 条滑窗截断限制）；
2. 自动兼容冷启动迁移：首次打开或查询时自动把已有 state.json 的 history / gas_history / recharges 导入 SQLite（按 (source, ts) 幂等去重）；
3. 精准账单统计：支持任意起止日期区间（如本月、近 7 天、近 30 天、学期区间），逐段累加净消耗，完全免疫中途充值干扰；
4. 同比上周/上周期涨跌幅计算 + 宿舍室友 AA 均摊金额与群账单文案生成；
5. 一键导出带 UTF-8 BOM 的 Excel 友好 CSV（每日汇总 / 原始明细两种视图）。
"""
from __future__ import annotations

from contextlib import closing
import csv
import io
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "history.db"

_DB_LOCK = threading.Lock()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,           -- 'elec' | 'gas'
    ts TEXT NOT NULL,               -- ISO 8601 时间戳（精确到秒）
    day TEXT NOT NULL,              -- YYYY-MM-DD（便于按天/按月聚合）
    balance REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    meter_reading TEXT NOT NULL DEFAULT '',
    UNIQUE(source, ts)
);
CREATE INDEX IF NOT EXISTS idx_samples_source_day ON samples(source, day);
CREATE INDEX IF NOT EXISTS idx_samples_source_ts ON samples(source, ts);

CREATE TABLE IF NOT EXISTS recharges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,           -- 'elec' | 'gas'
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    before_bal REAL NOT NULL,
    after_bal REAL NOT NULL,
    delta REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    UNIQUE(source, ts)
);
CREATE INDEX IF NOT EXISTS idx_recharges_source_day ON recharges(source, day);
"""


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    target = db_path or DB_FILE
    conn = sqlite3.connect(str(target), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(_SCHEMA_SQL)
    return conn


def init_db(db_path: Path | None = None) -> Path:
    """初始化数据库表结构并返回路径。"""
    target = db_path or DB_FILE
    with _DB_LOCK:
        with closing(_connect(target)) as conn:
            conn.commit()
    return target


def _day_of(ts: str) -> str:
    return str(ts)[:10] if len(str(ts)) >= 10 else date.today().isoformat()


def _safe_csv_cell(val: object) -> str:
    """防止 CSV 公式注入（Excel 中以 = / + / - / @ 开头的字符串可能被当公式执行）。"""
    s = str(val)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        # 纯数字或带正负号的合法数值（如 -12.50）不需要转义，只转义非数值文本
        try:
            float(s)
        except ValueError:
            return "'" + s
    return s


def record_sample(
    source: str,
    ts: str,
    balance: float,
    *,
    unit: str = "",
    meter_reading: str = "",
    db_path: Path | None = None,
) -> None:
    """幂等写入单条历史采样点（同 source+ts 覆盖更新，避免重复）。"""
    day = _day_of(ts)
    with _DB_LOCK:
        with closing(_connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO samples (source, ts, day, balance, unit, meter_reading)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, ts) DO UPDATE SET
                    balance = excluded.balance,
                    unit = CASE WHEN excluded.unit != '' THEN excluded.unit ELSE samples.unit END,
                    meter_reading = CASE WHEN excluded.meter_reading != '' THEN excluded.meter_reading ELSE samples.meter_reading END
                """,
                (source, ts, day, round(float(balance), 2), unit, str(meter_reading or "")),
            )
            conn.commit()


def record_recharge(
    source: str,
    ts: str,
    before_bal: float,
    after_bal: float,
    delta: float,
    *,
    unit: str = "",
    db_path: Path | None = None,
) -> None:
    """幂等写入一条自动识别的充值记录。"""
    day = _day_of(ts)
    with _DB_LOCK:
        with closing(_connect(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO recharges (source, ts, day, before_bal, after_bal, delta, unit)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, ts) DO UPDATE SET
                    before_bal = excluded.before_bal,
                    after_bal = excluded.after_bal,
                    delta = excluded.delta,
                    unit = excluded.unit
                """,
                (
                    source,
                    ts,
                    day,
                    round(float(before_bal), 2),
                    round(float(after_bal), 2),
                    round(float(delta), 2),
                    unit,
                ),
            )
            conn.commit()


def sync_from_state(
    state: dict,
    *,
    elec_min_recharge: float = 5.0,
    gas_min_recharge: float = 10.0,
    db_path: Path | None = None,
) -> dict[str, int]:
    """把已有 state.json 中的 history / gas_history / 充值流水幂等导入 SQLite。

    返回导入统计 {"samples": N, "recharges": M}。可重复安全调用。
    """
    sample_rows: list[tuple[str, str, str, float, str, str]] = []
    recharge_rows: list[tuple[str, str, str, float, float, float, str]] = []

    def _collect_hist(src: str, items: Iterable, unit: str, min_delta: float) -> None:
        cleaned: list[tuple[str, float]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("t") or "").strip()
            try:
                bal = round(float(item.get("bal")), 2)
            except (TypeError, ValueError):
                continue
            if len(ts) < 10:
                continue
            cleaned.append((ts, bal))
            sample_rows.append((src, ts, _day_of(ts), bal, unit, ""))
        cleaned.sort(key=lambda x: x[0])
        for i in range(1, len(cleaned)):
            delta = round(cleaned[i][1] - cleaned[i - 1][1], 2)
            if delta >= min_delta:
                recharge_rows.append(
                    (src, cleaned[i][0], _day_of(cleaned[i][0]), cleaned[i - 1][1], cleaned[i][1], delta, unit)
                )

    def _collect_recharges(src: str, items: Iterable, default_unit: str) -> None:
        for ev in items:
            if not isinstance(ev, dict):
                continue
            ts = str(ev.get("t") or "").strip()
            if len(ts) < 10:
                continue
            try:
                before = round(float(ev.get("before")), 2)
                after = round(float(ev.get("after")), 2)
                delta = round(float(ev.get("delta")), 2)
            except (TypeError, ValueError):
                continue
            unit = str(ev.get("unit") or default_unit)
            recharge_rows.append((src, ts, _day_of(ts), before, after, delta, unit))

    if isinstance(state.get("history"), list):
        _collect_hist("elec", state["history"], "度", elec_min_recharge)
    if isinstance(state.get("gas_history"), list):
        _collect_hist("gas", state["gas_history"], "元", gas_min_recharge)
    if isinstance(state.get("elec_recharges"), list):
        _collect_recharges("elec", state["elec_recharges"], "度")
    if isinstance(state.get("gas_recharges"), list):
        _collect_recharges("gas", state["gas_recharges"], "元")

    with _DB_LOCK:
        with closing(_connect(db_path)) as conn:
            if sample_rows:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO samples (source, ts, day, balance, unit, meter_reading)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    sample_rows,
                )
            if recharge_rows:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO recharges (source, ts, day, before_bal, after_bal, delta, unit)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    recharge_rows,
                )
            conn.commit()
    return {"samples": len(sample_rows), "recharges": len(recharge_rows)}


def _calc_source_range(
    conn: sqlite3.Connection,
    source: str,
    start_day: str,
    end_day: str,
    min_recharge: float,
) -> dict:
    """计算单个数据源在 [start_day, end_day] 内的精确净消耗、每日明细与充值总额。

    跨天衔接：额外取 start_day 之前的最后一条基线点，确保 start_day 当天凌晨首段消耗不丢失。
    """
    baseline = conn.execute(
        "SELECT ts, day, balance FROM samples WHERE source = ? AND day < ? ORDER BY ts DESC LIMIT 1",
        (source, start_day),
    ).fetchone()
    rows = conn.execute(
        "SELECT ts, day, balance, meter_reading FROM samples WHERE source = ? AND day >= ? AND day <= ? ORDER BY ts ASC",
        (source, start_day, end_day),
    ).fetchall()

    daily: dict[str, dict] = {}
    prev_bal: float | None = float(baseline["balance"]) if baseline is not None else None
    total_used = 0.0
    total_recharged = 0.0
    detected_recharges: list[dict] = []

    for r in rows:
        d = str(r["day"])
        bal = float(r["balance"])
        entry = daily.get(d)
        if entry is None:
            entry = {
                "day": d,
                "start_bal": bal,
                "end_bal": bal,
                "used": 0.0,
                "recharged": 0.0,
                "samples": 0,
            }
            daily[d] = entry
        entry["end_bal"] = bal
        entry["samples"] += 1

        if prev_bal is not None:
            diff = round(prev_bal - bal, 4)
            if diff > 0:
                entry["used"] = round(entry["used"] + diff, 2)
                total_used = round(total_used + diff, 2)
            elif (-diff) >= min_recharge:
                inc = round(-diff, 2)
                entry["recharged"] = round(entry["recharged"] + inc, 2)
                total_recharged = round(total_recharged + inc, 2)
                detected_recharges.append(
                    {"t": str(r["ts"]), "day": d, "before": round(prev_bal, 2), "after": round(bal, 2), "delta": inc}
                )
        prev_bal = bal

    # 也融合 recharges 表里手工/实时记录的充值点（按时间戳去重）
    rec_rows = conn.execute(
        "SELECT ts, day, before_bal, after_bal, delta FROM recharges WHERE source = ? AND day >= ? AND day <= ? ORDER BY ts ASC",
        (source, start_day, end_day),
    ).fetchall()
    by_ts = {ev["t"]: ev for ev in detected_recharges}
    for rr in rec_rows:
        ts = str(rr["ts"])
        if ts not in by_ts:
            ev = {
                "t": ts,
                "day": str(rr["day"]),
                "before": round(float(rr["before_bal"]), 2),
                "after": round(float(rr["after_bal"]), 2),
                "delta": round(float(rr["delta"]), 2),
            }
            by_ts[ts] = ev
            total_recharged = round(total_recharged + ev["delta"], 2)
            if ev["day"] in daily:
                daily[ev["day"]]["recharged"] = round(daily[ev["day"]]["recharged"] + ev["delta"], 2)

    daily_list = [daily[k] for k in sorted(daily.keys())]
    return {
        "samples": len(rows),
        "days_with_data": len(daily_list),
        "start_bal": round(float(rows[0]["balance"]), 2) if rows else None,
        "end_bal": round(float(rows[-1]["balance"]), 2) if rows else None,
        "used": round(total_used, 2),
        "recharged": round(total_recharged, 2),
        "recharges": sorted(by_ts.values(), key=lambda x: x["t"]),
        "daily": daily_list,
    }


def _pct_change(current: float, previous: float) -> float | None:
    if previous <= 0.001:
        return None
    return round(((current - previous) / previous) * 100.0, 1)


def get_billing_report(
    *,
    start_day: str | None = None,
    end_day: str | None = None,
    elec_price: float = 0.0,
    roommates: int = 4,
    room_name: str = "宿舍",
    today: date | None = None,
    db_path: Path | None = None,
) -> dict:
    """生成 [start_day, end_day] 账单统计、上周/上周期同比及宿舍 AA 均摊结果。

    默认统计区间为本月 1 日至今天。
    """
    ref_today = today or date.today()
    s_date = date.fromisoformat(start_day) if start_day else ref_today.replace(day=1)
    e_date = date.fromisoformat(end_day) if end_day else ref_today
    if s_date > e_date:
        raise ValueError("开始日期不能晚于结束日期")

    people = max(1, min(int(roommates), 20))
    price = max(0.0, float(elec_price or 0.0))
    span_days = (e_date - s_date).days + 1

    # 上一等长周期（用于区间环比）
    prev_e = s_date - timedelta(days=1)
    prev_s = prev_e - timedelta(days=span_days - 1)

    # 固定近 7 天 vs 上 7 天（同比上周涨跌幅）
    w_curr_e = ref_today
    w_curr_s = ref_today - timedelta(days=6)
    w_prev_e = w_curr_s - timedelta(days=1)
    w_prev_s = w_prev_e - timedelta(days=6)

    with _DB_LOCK:
        with closing(_connect(db_path)) as conn:
            elec = _calc_source_range(conn, "elec", s_date.isoformat(), e_date.isoformat(), 5.0)
            gas = _calc_source_range(conn, "gas", s_date.isoformat(), e_date.isoformat(), 10.0)
            prev_elec = _calc_source_range(conn, "elec", prev_s.isoformat(), prev_e.isoformat(), 5.0)
            prev_gas = _calc_source_range(conn, "gas", prev_s.isoformat(), prev_e.isoformat(), 10.0)

            week_elec = _calc_source_range(conn, "elec", w_curr_s.isoformat(), w_curr_e.isoformat(), 5.0)
            week_prev_elec = _calc_source_range(conn, "elec", w_prev_s.isoformat(), w_prev_e.isoformat(), 5.0)
            week_gas = _calc_source_range(conn, "gas", w_curr_s.isoformat(), w_curr_e.isoformat(), 10.0)
            week_prev_gas = _calc_source_range(conn, "gas", w_prev_s.isoformat(), w_prev_e.isoformat(), 10.0)

            meta = conn.execute(
                "SELECT COUNT(*) AS cnt, MIN(day) AS first_day, MAX(day) AS last_day FROM samples"
            ).fetchone()
            # 防止异常巨大的起止日期跨度导致内存/算力滥用（上面仅查询 SQLite 实际有数据的点，无需额外循环）

    elec_cost = round(elec["used"] * price, 2) if price > 0 else 0.0
    gas_cost = round(gas["used"], 2)  # 燃气余额本身以「元」计价
    total_cost = round(elec_cost + gas_cost, 2)
    per_person = round(total_cost / people, 2)

    # 合并每日电费 + 燃气明细，便于前端表格和 CSV 导出
    elec_by_day = {d["day"]: d for d in elec["daily"]}
    gas_by_day = {d["day"]: d for d in gas["daily"]}
    all_days = sorted(set(elec_by_day.keys()) | set(gas_by_day.keys()))
    daily_merged: list[dict] = []
    for d in all_days:
        ed = elec_by_day.get(d)
        gd = gas_by_day.get(d)
        e_used = ed["used"] if ed else 0.0
        g_used = gd["used"] if gd else 0.0
        e_yuan = round(e_used * price, 2) if price > 0 else 0.0
        daily_merged.append(
            {
                "day": d,
                "elec_start": ed["start_bal"] if ed else None,
                "elec_end": ed["end_bal"] if ed else None,
                "elec_used": e_used,
                "elec_recharged": ed["recharged"] if ed else 0.0,
                "elec_cost": e_yuan,
                "gas_start": gd["start_bal"] if gd else None,
                "gas_end": gd["end_bal"] if gd else None,
                "gas_used": g_used,
                "gas_recharged": gd["recharged"] if gd else 0.0,
                "day_total_cost": round(e_yuan + g_used, 2),
            }
        )

    elec_line = (
        f"⚡ 电费消耗：{elec['used']:.2f} 度 × ¥{price:g} = ¥{elec_cost:.2f}"
        if price > 0
        else f"⚡ 电费消耗：{elec['used']:.2f} 度（未设单价，金额未计入）"
    )
    share_lines = [
        f"🧾 {room_name} 能源账单（{s_date.isoformat()} ~ {e_date.isoformat()}）",
        elec_line,
        f"🔥 燃气消耗：¥{gas_cost:.2f}",
        f"💰 合计费用：¥{total_cost:.2f}",
        f"👥 均摊人数：{people} 人 → 每人应付 ¥{per_person:.2f}",
    ]
    if elec["recharged"] > 0 or gas["recharged"] > 0:
        rc_parts = []
        if elec["recharged"] > 0:
            rc_parts.append(f"电费 +{elec['recharged']:.2f} 度")
        if gas["recharged"] > 0:
            rc_parts.append(f"燃气 +¥{gas['recharged']:.2f}")
        share_lines.append(f"💳 期间充值（已自动剔除干扰）：{'，'.join(rc_parts)}")

    return {
        "range": {"start": s_date.isoformat(), "end": e_date.isoformat(), "days": span_days},
        "archive": {
            "total_samples": int(meta["cnt"]) if meta else 0,
            "first_day": meta["first_day"] if meta else None,
            "last_day": meta["last_day"] if meta else None,
        },
        "elec": {
            **elec,
            "price": price,
            "cost_yuan": elec_cost,
            "prev_period_used": prev_elec["used"],
            "period_pct_change": _pct_change(elec["used"], prev_elec["used"]),
        },
        "gas": {
            **gas,
            "cost_yuan": gas_cost,
            "prev_period_used": prev_gas["used"],
            "period_pct_change": _pct_change(gas["used"], prev_gas["used"]),
        },
        "week_over_week": {
            "current_range": [w_curr_s.isoformat(), w_curr_e.isoformat()],
            "previous_range": [w_prev_s.isoformat(), w_prev_e.isoformat()],
            "elec_current": week_elec["used"],
            "elec_previous": week_prev_elec["used"],
            "elec_pct_change": _pct_change(week_elec["used"], week_prev_elec["used"]),
            "gas_current": week_gas["used"],
            "gas_previous": week_prev_gas["used"],
            "gas_pct_change": _pct_change(week_gas["used"], week_prev_gas["used"]),
        },
        "split": {
            "roommates": people,
            "elec_cost": elec_cost,
            "gas_cost": gas_cost,
            "total_cost": total_cost,
            "per_person": per_person,
            "share_text": "\n".join(share_lines),
        },
        "daily": daily_merged,
    }


def export_csv(
    *,
    start_day: str | None = None,
    end_day: str | None = None,
    mode: str = "daily",
    elec_price: float = 0.0,
    roommates: int = 4,
    room_name: str = "宿舍",
    today: date | None = None,
    db_path: Path | None = None,
) -> tuple[str, bytes]:
    """导出带 UTF-8 BOM 的 CSV 文件（Excel 双击不乱码）。

    mode:
      - "daily"：按日汇总 + 底部账单合计与室友 AA 均摊行（默认）
      - "raw"  ：起止日期内 SQLite 原始采样点明细
    返回 (建议文件名, UTF-8-SIG 字节流)。
    """
    report = get_billing_report(
        start_day=start_day,
        end_day=end_day,
        elec_price=elec_price,
        roommates=roommates,
        room_name=room_name,
        today=today,
        db_path=db_path,
    )
    s_day = report["range"]["start"]
    e_day = report["range"]["end"]

    buf = io.StringIO()
    writer = csv.writer(buf)

    if mode == "raw":
        writer.writerow(["数据源", "时间", "日期", "余额", "单位", "表读数"])
        with _DB_LOCK:
            with closing(_connect(db_path)) as conn:
                rows = conn.execute(
                    """
                    SELECT source, ts, day, balance, unit, meter_reading
                    FROM samples
                    WHERE day >= ? AND day <= ?
                    ORDER BY ts ASC
                    """,
                    (s_day, e_day),
                ).fetchall()
        for r in rows:
            src_cn = "电费" if r["source"] == "elec" else "燃气"
            writer.writerow(
                [
                    src_cn,
                    _safe_csv_cell(r["ts"]),
                    _safe_csv_cell(r["day"]),
                    f"{float(r['balance']):.2f}",
                    _safe_csv_cell(r["unit"]),
                    _safe_csv_cell(r["meter_reading"]),
                ]
            )
        fname = f"campus_raw_{s_day}_to_{e_day}.csv"
    else:
        writer.writerow(
            [
                "日期",
                "电费期初(度)",
                "电费期末(度)",
                "电费消耗(度)",
                "电费充值(度)",
                "电费折算(元)",
                "燃气期初(元)",
                "燃气期末(元)",
                "燃气消耗(元)",
                "燃气充值(元)",
                "当日合计(元)",
            ]
        )
        for d in report["daily"]:
            writer.writerow(
                [
                    d["day"],
                    "" if d["elec_start"] is None else f"{d['elec_start']:.2f}",
                    "" if d["elec_end"] is None else f"{d['elec_end']:.2f}",
                    f"{d['elec_used']:.2f}",
                    f"{d['elec_recharged']:.2f}",
                    f"{d['elec_cost']:.2f}",
                    "" if d["gas_start"] is None else f"{d['gas_start']:.2f}",
                    "" if d["gas_end"] is None else f"{d['gas_end']:.2f}",
                    f"{d['gas_used']:.2f}",
                    f"{d['gas_recharged']:.2f}",
                    f"{d['day_total_cost']:.2f}",
                ]
            )
        writer.writerow([])
        sp = report["split"]
        writer.writerow(
            [
                "区间合计",
                "",
                "",
                f"{report['elec']['used']:.2f}",
                f"{report['elec']['recharged']:.2f}",
                f"{sp['elec_cost']:.2f}",
                "",
                "",
                f"{report['gas']['used']:.2f}",
                f"{report['gas']['recharged']:.2f}",
                f"{sp['total_cost']:.2f}",
            ]
        )
        writer.writerow(
            [
                f"室友均摊({sp['roommates']}人)",
                f"电费单价={report['elec']['price']:g}元/度",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "每人应付(元)",
                f"{sp['per_person']:.2f}",
            ]
        )
        fname = f"campus_bill_{s_day}_to_{e_day}.csv"

    return fname, buf.getvalue().encode("utf-8-sig")
