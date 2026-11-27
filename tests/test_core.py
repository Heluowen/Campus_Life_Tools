"""自动化单元测试：覆盖配置解析、多渠道推送、历史记录去重、并发安全、凭据提取与 Web API。"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for p in (str(ROOT), str(TOOLS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config as cfg  # noqa: E402
import history_db  # noqa: E402
import keep_alive as ka  # noqa: E402
import main as m  # noqa: E402
import notifier  # noqa: E402
import query_elec as qe  # noqa: E402
import query_gas as qg  # noqa: E402
from harvest_creds import parse_curl  # noqa: E402
from web import server as web_server  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_local_data_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """强制隔离所有单元测试的本地数据文件（history.db / state.json / cookies.json），绝不污染真实数据！"""
    monkeypatch.setattr(history_db, "DB_FILE", tmp_path / "test_history.db")
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "test_state.json")
    monkeypatch.setattr(m, "COOKIE_STORE", tmp_path / "test_cookies.json")


def _make_settings(tmp_path: Path, **env_overrides: str) -> cfg.Settings:
    env_file = tmp_path / ".env"
    base = {
        "CCB_USERID": "u1001",
        "CCB_SKEY": "skey_abc",
        "CCB_ROOMID": "100-1--2-301",
        "CCB_ROOM_NAME": "测试宿舍",
        "CCB_BRANCHID": "999000000",
        "CCB_REFERER": "https://example.test/ref.html",
        "ELEC_PRICE": "0.65",
        "WARN_THRESHOLD": "20",
        "CRITICAL_THRESHOLD": "5",
        "PUSH_CHANNEL": "none",
        "GAS_SUBSID": "sub123",
        "GAS_TOKEN": "tok_init",
        "GAS_WARN_THRESHOLD": "15",
        "GAS_QUERY_INTERVAL": "1800",
    }
    base.update(env_overrides)
    env_file.write_text("\n".join(f"{k}={v}" for k, v in base.items()) + "\n", encoding="utf-8")
    # 清理可能残留的环境变量，确保从临时 .env 干净加载
    with mock.patch.dict("os.environ", {}, clear=True):
        return cfg.load_settings(env_path=env_file)


def test_config_branch_referer_and_multi_channel(tmp_path: Path) -> None:
    s = _make_settings(
        tmp_path,
        PUSH_CHANNEL="wecom,bark",
        WECOM_WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
        BARK_URL="https://api.day.app/testkey",
    )
    assert s.api.branch_id == "999000000"
    assert s.api.referer == "https://example.test/ref.html"
    assert s.push.channels == ("wecom", "bark")
    assert not s.push.is_none

    form = qe._build_form(s.api)
    headers = qe._build_headers(s.api)
    assert form["BRANCHID"] == "999000000"
    assert headers["Referer"] == "https://example.test/ref.html"


def test_config_auto_infer_multiple_channels(tmp_path: Path) -> None:
    s = _make_settings(
        tmp_path,
        PUSH_CHANNEL="",
        WECOM_WEBHOOK="https://example.test/wecom",
        PUSHPLUS_TOKEN="pp_tok",
    )
    assert s.push.channels == ("wecom", "pushplus")


def test_update_env_many_atomic(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nCCB_SKEY=old\nELEC_PRICE=0.5\n", encoding="utf-8")
    cfg.update_env_many({"CCB_SKEY": "new_skey", "GAS_TOKEN": "tok_new"}, env_path=env_file)
    text = env_file.read_text(encoding="utf-8")
    assert "# comment" in text
    assert "CCB_SKEY=new_skey" in text
    assert "ELEC_PRICE=0.5" in text
    assert "GAS_TOKEN=tok_new" in text


def test_notifier_multi_channel_partial_failure() -> None:
    push = cfg.PushSettings(
        channel="wecom,bark",
        channels=("wecom", "bark"),
        wecom_webhook="https://example.test/wecom",
        bark_url="https://example.test/bark",
    )
    with mock.patch.dict(
        notifier._SENDERS,
        {
            "wecom": mock.MagicMock(side_effect=notifier.NotifyError("wecom down")),
            "bark": mock.MagicMock(return_value=True),
        },
    ):
        assert notifier.send_notification(push, "title", "body") is True

    with mock.patch.dict(
        notifier._SENDERS,
        {
            "wecom": mock.MagicMock(side_effect=notifier.NotifyError("wecom down")),
            "bark": mock.MagicMock(side_effect=notifier.NotifyError("bark down")),
        },
    ):
        with pytest.raises(notifier.NotifyError):
            notifier.send_notification(push, "title", "body")


def test_history_dedup_same_hour_and_preserve_changes() -> None:
    state: dict = {}
    t0 = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    m._append_history(state, "history", t0, 100.0)
    m._append_history(state, "history", t0 + timedelta(minutes=10), 100.0)
    m._append_history(state, "history", t0 + timedelta(minutes=20), 100.0)
    m._append_history(state, "history", t0 + timedelta(minutes=30), 100.0)
    # 同一小时内连续 4 个 100.0 应只保留首尾 2 个关键点
    assert len(state["history"]) == 2
    assert state["history"][0]["t"].endswith("10:00:00+08:00")
    assert state["history"][1]["t"].endswith("10:30:00+08:00")

    # 电量变化应立即新增点
    m._append_history(state, "history", t0 + timedelta(minutes=40), 99.5)
    assert len(state["history"]) == 3
    # 跨小时应新增点
    m._append_history(state, "history", t0 + timedelta(hours=1, minutes=5), 99.5)
    assert len(state["history"]) == 4


def test_gas_interval_throttling_and_force_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    s = _make_settings(tmp_path, GAS_QUERY_INTERVAL="1800")

    fake_res = qg.GasResult(balance=88.5, subs_code="1800001", meter_reading="1500", payload={})
    mock_query = mock.MagicMock(return_value=fake_res)
    monkeypatch.setattr(m, "query_gas", mock_query)

    # 第一次查询应执行
    code, err = m.fetch_and_update_gas(s, notify=False, respect_interval=True)
    assert code == m.EXIT_OK and err is None
    assert mock_query.call_count == 1
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["gas_subs_code"] == "1800001"

    # 间隔未满 1800s，保活模式下应跳过
    code2, err2 = m.fetch_and_update_gas(s, notify=False, respect_interval=True)
    assert code2 == m.EXIT_OK and err2 is None
    assert mock_query.call_count == 1

    # 手动刷新（respect_interval=False）应立即执行
    code3, err3 = m.fetch_and_update_gas(s, notify=False, respect_interval=False)
    assert code3 == m.EXIT_OK and err3 is None
    assert mock_query.call_count == 2


def test_parse_curl_elec_and_gas() -> None:
    elec_curl = """curl 'https://app.xiaoyuan.ccb.com/LHECISM/B2CMainPlat_00' \\
      -H 'Cookie: CCBIBS1=abc123; IN001=xyz' \\
      -H 'Referer: https://app.xiaoyuan.ccb.com/EMTSTATIC/test.html' \\
      -H 'User-Agent: MicroMessenger/7.0' \\
      --data-raw 'BRANCHID=555000000&TXCODE=YJF004&SKEY=MY_SKEY&USERID=MY_UID&roomid=1-2--3-401&PayType=elec'
    """
    got_elec = parse_curl(elec_curl)
    assert got_elec["CCB_USERID"] == "MY_UID"
    assert got_elec["CCB_SKEY"] == "MY_SKEY"
    assert got_elec["CCB_ROOMID"] == "1-2--3-401"
    assert got_elec["CCB_COOKIE"] == "CCBIBS1=abc123; IN001=xyz"
    assert got_elec["CCB_BRANCHID"] == "555000000"

    gas_curl = (
        "curl 'https://weixin.towngasvcc.com/nv1/vcc-cbs/charge/preCheck?subsId=SUB999&clientid=CH01' "
        "-H 'Authorization: Bearer deadbeef12345678' -b '__h5_oauth_ticket__=tk1'"
    )
    got_gas = parse_curl(gas_curl)
    assert got_gas["GAS_SUBSID"] == "SUB999"
    assert got_gas["GAS_CLIENTID"] == "CH01"
    assert got_gas["GAS_TOKEN"] == "deadbeef12345678"
    assert got_gas["GAS_COOKIE"] == "__h5_oauth_ticket__=tk1"


def test_web_import_curl_clears_cookie_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CCB_SKEY=old\n", encoding="utf-8")
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text('{"CCBIBS1": "stale"}', encoding="utf-8")

    monkeypatch.setattr(cfg, "ENV_FILE", env_file)
    monkeypatch.setattr(m, "COOKIE_STORE", cookie_file)

    curl = (
        "curl 'https://app.xiaoyuan.ccb.com/LHECISM/B2CMainPlat_00' "
        "-H 'Cookie: CCBIBS1=fresh_cookie' "
        "--data 'TXCODE=YJF004&SKEY=fresh_skey&USERID=u1&roomid=r1'"
    )
    resp = web_server._import_curl(curl)
    assert resp["ok"] is True
    assert "CCB_SKEY" in resp["updated_keys"]
    assert not cookie_file.exists()
    assert "CCB_SKEY=fresh_skey" in env_file.read_text(encoding="utf-8")


def test_concurrent_save_state_no_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            for j in range(15):
                with m._STATE_LOCK:
                    st = m._load_state()
                    st[f"k_{idx}"] = j
                    m._save_state(st)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    final_state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    for i in range(6):
        assert final_state[f"k_{i}"] == 14


def test_keepalive_file_lock_mutual_exclusion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ka, "LOCK_FILE", tmp_path / "keepalive.lock")
    monkeypatch.setattr(ka, "PID_FILE", tmp_path / "keepalive.pid")
    assert ka._acquire_lock() is True
    try:
        # PID 单独写入无锁的 keepalive.pid，确保 Windows 下 stop.bat / Get-Content 随时可读
        pid_text = (tmp_path / "keepalive.pid").read_text(encoding="utf-8").strip()
        assert pid_text == str(ka.os.getpid())
        # 对 keepalive.lock 再次加锁必须失败（模拟第二个并发实例）
        if ka.os.name == "nt":
            import msvcrt

            with open(tmp_path / "keepalive.lock", "a+", encoding="utf-8") as fh2:
                fh2.seek(ka._WIN_LOCK_OFFSET)
                with pytest.raises(OSError):
                    msvcrt.locking(fh2.fileno(), msvcrt.LK_NBLCK, 1)
    finally:
        ka._release_lock()
    assert not (tmp_path / "keepalive.lock").exists()
    assert not (tmp_path / "keepalive.pid").exists()


def test_elec_auth_expired_uppercase_errorcode_and_errormsg() -> None:
    import httpx

    payload = {
        "status": "1",
        "msg": "",
        "TXCODE": "YJF004",
        "ERRORCODE": "0130Z1108912",
        "ERRORMSG": "您已较长时间未进行操作，为保护您的资金安全，系统已自动退出。请关闭浏览器所有网页，重新登录。",
    }
    resp = httpx.Response(200, json=payload)
    with pytest.raises(qe.AuthExpiredError, match="0130Z1108912"):
        qe._parse_response(resp)

    payload_hist = {
        "status": "1",
        "msg": "",
        "ERRORCODE": "0130Z1108006",
        "ERRORMSG": "该页面为历史页面，请关闭浏览器重新登录",
    }
    with pytest.raises(qe.AuthExpiredError, match="0130Z1108006"):
        qe._parse_response(httpx.Response(200, json=payload_hist))


def test_gas_maintenance_window_parsing_and_no_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "state.json")
    s = _make_settings(tmp_path, PUSH_CHANNEL="bark", BARK_URL="https://api.day.app/test")

    maint_payload = {
        "resultCode": "0",
        "datas": {
            "errorCode": "2000E1",
            "errorTitle": "暂停办理业务",
            "errorMsg": "每日23:30至次日00:30暂停办理此业务，请稍候再来",
            "savingSum": None,
        },
    }
    resp = httpx.Response(200, json=maint_payload)
    with pytest.raises(qg.GasMaintenanceError, match="23:30"):
        qg._parse_gas(resp)

    mock_push = mock.MagicMock(return_value=True)
    monkeypatch.setattr(m, "send_notification", mock_push)
    monkeypatch.setattr(
        m,
        "query_gas",
        mock.MagicMock(side_effect=qg.GasMaintenanceError("每日23:30至次日00:30暂停办理此业务，请稍候再来")),
    )

    code, err = m.fetch_and_update_gas(s, notify=True, respect_interval=False)
    assert code == m.EXIT_OK
    assert err == {"error": "gas-maintenance", "message": "每日23:30至次日00:30暂停办理此业务，请稍候再来"}
    mock_push.assert_not_called()
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "gas_maintenance" in saved
    assert "23:30" in saved["gas_maintenance"]["message"]


def test_successful_query_clears_stale_auth_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st_file = tmp_path / "state.json"
    st_file.write_text(
        json.dumps(
            {
                "last_auth_error_at": "2026-11-23T12:00:00+08:00",
                "gas_last_auth_error_at": "2026-11-23T12:00:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "STATE_FILE", st_file)
    s = _make_settings(tmp_path)

    monkeypatch.setattr(
        m,
        "query_balance",
        mock.MagicMock(return_value=qe.QueryResult(balance=100.0, unit="度", room_id="100-1--2-301", payload={})),
    )
    monkeypatch.setattr(
        m,
        "query_gas",
        mock.MagicMock(return_value=qg.GasResult(balance=50.0, subs_code="1800001", meter_reading="100", payload={})),
    )

    assert m.fetch_and_update_elec(s, notify=False) == (m.EXIT_OK, None)
    assert m.fetch_and_update_gas(s, notify=False, respect_interval=False) == (m.EXIT_OK, None)
    saved = json.loads(st_file.read_text(encoding="utf-8"))
    assert "last_auth_error_at" not in saved
    assert "gas_last_auth_error_at" not in saved


def test_web_import_gas_token_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GAS_TOKEN=old_tok\nGAS_REFRESH_TOKEN=old_ref\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "ENV_FILE", env_file)

    raw_json = '{"access_token": "acc_12345678", "refresh_token": "ref_87654321", "expires_in": 7200}'
    resp = web_server._import_curl(raw_json)
    assert resp["ok"] is True
    assert set(resp["updated_keys"]) == {"GAS_TOKEN", "GAS_REFRESH_TOKEN", "GAS_TOKEN_ISSUED_AT"}

    text = env_file.read_text(encoding="utf-8")
    assert "GAS_TOKEN=acc_12345678" in text
    assert "GAS_REFRESH_TOKEN=ref_87654321" in text
    assert "GAS_TOKEN_ISSUED_AT=" in text

    # 也支持嵌套在 data 对象中的 JSON 格式
    nested_json = '{"code": 0, "data": {"access_token": "acc_nested", "refresh_token": "ref_nested"}}'
    resp2 = web_server._import_curl(nested_json)
    assert resp2["ok"] is True
    text2 = env_file.read_text(encoding="utf-8")
    assert "GAS_TOKEN=acc_nested" in text2
    assert "GAS_REFRESH_TOKEN=ref_nested" in text2


def test_net_utils_proxy_fallback_on_ssl_eof() -> None:
    import httpx
    import net_utils

    seen: list[bool] = []

    def flaky(use_trust_env: bool) -> str:
        seen.append(use_trust_env)
        if not use_trust_env:
            err = qe.QueryError("网络请求失败：[SSL: UNEXPECTED_EOF_WHILE_READING]")
            err.__cause__ = httpx.ConnectError("EOF occurred in violation of protocol")
            raise err
        return "recovered"

    assert net_utils.run_with_fallback(flaky, trust_env=False, allow_fallback=True) == "recovered"
    assert seen == [False, True]

    # 业务认证错误（非网络传输闪断）不得触发 trust_env 切换重试
    seen_auth: list[bool] = []

    def auth_fail(use_trust_env: bool) -> str:
        seen_auth.append(use_trust_env)
        raise qe.AuthExpiredError("expired")

    with pytest.raises(qe.AuthExpiredError):
        net_utils.run_with_fallback(auth_fail, trust_env=False, allow_fallback=True)
    assert seen_auth == [False]


def test_source_scheduler_independent_backoff_and_env_wakeup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    s = _make_settings(
        tmp_path,
        GAS_QUERY_INTERVAL="1800",
        GAS_REFRESH_TOKEN="ref_valid",
        GAS_TOKEN_ISSUED_AT=str(int(time.time() - 5100)),  # 距 90 分钟主动续期只剩 5 分钟
    )
    monkeypatch.setattr(ka, "ENV_FILE", env_file)
    monkeypatch.setattr(ka, "load_settings", lambda: s)

    # 模拟电费会话失效，而燃气正常
    monkeypatch.setattr(
        m,
        "fetch_and_update_elec",
        mock.MagicMock(return_value=(m.EXIT_AUTH_EXPIRED, {"error": "auth", "message": "expired"})),
    )
    monkeypatch.setattr(
        m,
        "fetch_and_update_gas",
        mock.MagicMock(return_value=(m.EXIT_OK, None)),
    )

    sched = ka.SourceScheduler(elec_interval=600)
    t0 = 1000.0
    code, details = sched.step(t0)
    assert code == m.EXIT_AUTH_EXPIRED
    assert any("电费(auth)" in d for d in details)
    assert any("燃气: OK" in d for d in details)
    # 电费独立退避 >= 900s，但燃气必须在临近 90 分钟续期点（约 305s）被唤醒，不受电费退避牵连！
    assert sched.next_elec_at - t0 >= 900.0
    assert 200.0 <= (sched.next_gas_at - t0) <= 360.0
    assert sched.next_wakeup_seconds(t0) == pytest.approx(sched.next_gas_at - t0, abs=1.0)

    # 模拟 .env 文件被热更新，调度器应立即将两个源的下次执行时间提前到当前时刻
    new_mtime = env_file.stat().st_mtime + 10.0
    import os as _os

    _os.utime(env_file, (new_mtime, new_mtime))
    assert sched.check_env_reload(t0 + 20.0) is True
    assert sched.next_elec_at <= t0 + 20.0
    assert sched.next_gas_at <= t0 + 20.0
    # 关键回归测试：到达预定唤醒时间点时，next_wakeup_seconds 必须返回 0.0，绝不能卡在 1.0 导致死循环永不唤醒！
    assert sched.next_wakeup_seconds(t0 + 20.0) == 0.0
    assert sched.next_wakeup_seconds(t0 + 5000.0) == 0.0


def test_keepalive_rotating_log_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = tmp_path / "keepalive.log"
    monkeypatch.setattr(ka, "_LOG_MAX_BYTES", 200)
    monkeypatch.setattr(ka, "_LOG_BACKUP_COUNT", 2)
    ka._init_log_handler(log_path)
    try:
        for i in range(25):
            ka._log(f"rotation-check-line-{i:02d}-" + ("x" * 20))
    finally:
        ka._init_log_handler(None)

    assert log_path.exists()
    assert (tmp_path / "keepalive.log.1").exists()
    assert not (tmp_path / "keepalive.log.3").exists()


def test_recharge_detection_and_window_consumption_immune_to_topup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tz = timezone(timedelta(hours=8))
    t0 = datetime(2026, 11, 24, 10, 0, 0, tzinfo=tz)
    # 模拟：100 -> 97（耗 3）-> 297（充 +200）-> 294.5（耗 2.5）；净消耗应为 5.5，而不是负数！
    hist = [
        {"t": t0.isoformat(timespec="seconds"), "bal": 100.0},
        {"t": (t0 + timedelta(minutes=20)).isoformat(timespec="seconds"), "bal": 97.0},
        {"t": (t0 + timedelta(minutes=30)).isoformat(timespec="seconds"), "bal": 297.0},
        {"t": (t0 + timedelta(minutes=50)).isoformat(timespec="seconds"), "bal": 294.5},
    ]
    used = m.calc_window_consumption(hist, t0 + timedelta(minutes=55), timedelta(hours=1))
    assert used == 5.5

    recharges = m.detect_recharges_from_history(hist, min_delta=5.0, unit="度")
    assert len(recharges) == 1
    assert recharges[0]["delta"] == 200.0
    assert recharges[0]["before"] == 97.0
    assert recharges[0]["after"] == 297.0


def test_hourly_spike_alert_and_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st_file = tmp_path / "state.json"
    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 11, 24, 15, 0, 0, tzinfo=tz)
    # 40 分钟前电量 106.0，本次降到 100.0 -> 1 小时消耗 6.0 度，超过阈值 5.0
    st_file.write_text(
        json.dumps(
            {
                "last_balance": 106.0,
                "history": [{"t": (now - timedelta(minutes=40)).isoformat(timespec="seconds"), "bal": 106.0}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "STATE_FILE", st_file)
    monkeypatch.setattr(m, "_now", lambda _s: now)
    s = _make_settings(
        tmp_path,
        ELEC_SPIKE_THRESHOLD="5.0",
        PUSH_CHANNEL="bark",
        BARK_URL="https://api.day.app/test",
    )

    mock_push = mock.MagicMock(return_value=True)
    monkeypatch.setattr(m, "send_notification", mock_push)
    monkeypatch.setattr(
        m,
        "query_balance",
        mock.MagicMock(return_value=qe.QueryResult(balance=100.0, unit="度", room_id="100-1--2-301", payload={})),
    )

    assert m.fetch_and_update_elec(s, notify=True) == (m.EXIT_OK, None)
    assert mock_push.call_count == 1
    assert "异常高耗电" in mock_push.call_args.args[1]

    # 10 分钟后再掉 1 度仍在 2 小时冷却期内，不得重复刷屏推送
    monkeypatch.setattr(m, "_now", lambda _s: now + timedelta(minutes=10))
    monkeypatch.setattr(
        m,
        "query_balance",
        mock.MagicMock(return_value=qe.QueryResult(balance=99.0, unit="度", room_id="100-1--2-301", payload={})),
    )
    assert m.fetch_and_update_elec(s, notify=True) == (m.EXIT_OK, None)
    assert mock_push.call_count == 1


def test_web_control_center_settings_logs_and_test_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    log_file = tmp_path / "keepalive.log"
    log_file.write_text("[2026-11-24 22:00:00] line1\n[2026-11-24 22:10:00] line2\n", encoding="utf-8")

    s = _make_settings(
        tmp_path,
        PUSH_CHANNEL="bark",
        BARK_URL="https://api.day.app/testkey",
        GAS_REFRESH_TOKEN="ref_ok",
        GAS_CLIENTID="ch_ok",
        GAS_TOKEN_ISSUED_AT=str(int(time.time() - 600)),
    )
    monkeypatch.setattr(cfg, "ENV_FILE", env_file)
    monkeypatch.setattr(web_server, "LOG_FILE", log_file)

    with mock.patch.dict("os.environ", {}, clear=True):
        cur = web_server._settings_payload()
        assert cur["configured_channels"]["bark"] is True
        assert cur["configured_channels"]["wecom"] is False

        res = web_server._update_settings(
            {
                "warn_threshold": 30,
                "critical_threshold": 8,
                "hourly_spike_threshold": 4.5,
                "gas_warn_threshold": 20,
                "gas_query_interval": 1200,
                "push_channel": "bark",
            }
        )
        assert res["ok"] is True
        assert res["settings"]["warn_threshold"] == 30.0
        assert res["settings"]["hourly_spike_threshold"] == 4.5
        assert res["settings"]["gas_query_interval"] == 1200

        # 未配置 wecom 凭据时尝试启用应被拦截
        with pytest.raises(ValueError, match="WECOM_WEBHOOK"):
            web_server._update_settings({"push_channel": "wecom"})

        # 测试推送调用 notifier
        with mock.patch.object(web_server, "send_notification", return_value=True) as mp:
            tp = web_server._send_test_push()
            assert tp["ok"] is True
            assert mp.call_count == 1

        # 读取实时日志与燃气续期倒计时
        logs = web_server._read_logs(tail=1)
        assert logs["total_lines"] == 2
        assert logs["lines"] == ["[2026-11-24 22:10:00] line2"]

        ts = web_server._gas_token_status(cfg.load_settings(env_path=env_file))
        assert ts["has_refresh"] is True
        assert 4700 <= ts["next_refresh_in_sec"] <= 4810


def test_sqlite_archive_sync_billing_wow_and_csv_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date
    import history_db

    db_file = tmp_path / "history.db"
    monkeypatch.setattr(history_db, "DB_FILE", db_file)

    # 构造跨越两周且中途含充值的 state.json 样本：
    # 上周（11-12 ~ 11-18）：电费消耗 20 度，燃气消耗 10 元
    # 本周（11-19 ~ 11-25）：电费先耗 10 度，再充 +100 度，再耗 20 度（净消耗 30 度，同比上周 +50%）；燃气消耗 20 元（同比 +100%）
    fake_state = {
        "history": [
            {"t": "2026-11-12T08:00:00+08:00", "bal": 500.0},
            {"t": "2026-11-15T20:00:00+08:00", "bal": 490.0},
            {"t": "2026-11-18T22:00:00+08:00", "bal": 480.0},
            {"t": "2026-11-20T10:00:00+08:00", "bal": 470.0},
            {"t": "2026-11-20T12:00:00+08:00", "bal": 570.0},  # 充值 +100 度
            {"t": "2026-11-24T18:00:00+08:00", "bal": 550.0},
        ],
        "gas_history": [
            {"t": "2026-11-12T08:00:00+08:00", "bal": 100.0},
            {"t": "2026-11-18T22:00:00+08:00", "bal": 90.0},
            {"t": "2026-11-24T18:00:00+08:00", "bal": 70.0},
        ],
    }

    # 幂等导入两次，总数不翻倍
    history_db.sync_from_state(fake_state, db_path=db_file)
    history_db.sync_from_state(fake_state, db_path=db_file)

    report = history_db.get_billing_report(
        start_day="2026-11-01",
        end_day="2026-11-25",
        elec_price=0.8,
        roommates=4,
        room_name="测试宿舍",
        today=date(2026, 11, 25),
        db_path=db_file,
    )
    assert report["archive"]["total_samples"] == 9
    assert report["elec"]["used"] == 50.0
    assert report["elec"]["recharged"] == 100.0
    assert report["elec"]["cost_yuan"] == 40.0
    assert report["gas"]["used"] == 30.0
    assert report["split"]["total_cost"] == 70.0
    assert report["split"]["per_person"] == 17.5
    assert "每人应付 ¥17.50" in report["split"]["share_text"]

    wow = report["week_over_week"]
    assert wow["elec_previous"] == 20.0
    assert wow["elec_current"] == 30.0
    assert wow["elec_pct_change"] == 50.0
    assert wow["gas_previous"] == 10.0
    assert wow["gas_current"] == 20.0
    assert wow["gas_pct_change"] == 100.0

    # 验证每日汇总 CSV 与原始明细 CSV（带 UTF-8 BOM）
    fname_daily, csv_daily = history_db.export_csv(
        start_day="2026-11-01",
        end_day="2026-11-25",
        mode="daily",
        elec_price=0.8,
        roommates=4,
        today=date(2026, 11, 25),
        db_path=db_file,
    )
    assert fname_daily == "campus_bill_2026-11-01_to_2026-11-25.csv"
    assert csv_daily.startswith(b"\xef\xbb\xbf")
    text_daily = csv_daily.decode("utf-8-sig")
    assert "区间合计" in text_daily
    assert "室友均摊(4人)" in text_daily
    assert "17.50" in text_daily

    fname_raw, csv_raw = history_db.export_csv(
        start_day="2026-11-01",
        end_day="2026-11-25",
        mode="raw",
        today=date(2026, 11, 25),
        db_path=db_file,
    )
    assert fname_raw == "campus_raw_2026-11-01_to_2026-11-25.csv"
    assert "电费,2026-11-20T12:00:00+08:00" in csv_raw.decode("utf-8-sig")


def test_main_fetch_records_to_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import history_db

    db_file = tmp_path / "history.db"
    st_file = tmp_path / "state.json"
    monkeypatch.setattr(history_db, "DB_FILE", db_file)
    monkeypatch.setattr(m, "STATE_FILE", st_file)
    s = _make_settings(tmp_path)

    monkeypatch.setattr(
        m,
        "query_balance",
        mock.MagicMock(return_value=qe.QueryResult(balance=120.5, unit="度", room_id="100-1--2-301", payload={})),
    )
    monkeypatch.setattr(
        m,
        "query_gas",
        mock.MagicMock(return_value=qg.GasResult(balance=66.0, subs_code="1800001", meter_reading="1500", payload={})),
    )

    assert m.fetch_and_update_elec(s, notify=False) == (m.EXIT_OK, None)
    assert m.fetch_and_update_gas(s, notify=False, respect_interval=False) == (m.EXIT_OK, None)

    rep = history_db.get_billing_report(elec_price=0.65, roommates=4, db_path=db_file)
    assert rep["archive"]["total_samples"] == 2


def test_security_hardening_env_injection_csv_formula_csrf_and_nan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    # 1. .env 写入禁止换行符/空字节注入与非法键名
    env_file = tmp_path / ".env"
    env_file.write_text("CCB_ROOM_NAME=ok\n", encoding="utf-8")
    with pytest.raises(ValueError, match="换行"):
        cfg.update_env_many({"CCB_ROOM_NAME": "room\nPUSH_CHANNEL=evil"}, env_path=env_file)
    with pytest.raises(ValueError, match="非法"):
        cfg.update_env_many({"BAD KEY": "123"}, env_path=env_file)

    # 2. CSV 导出防止公式注入（=HYPERLINK 等以 =/@/+/- 开头的非数字文本自动加前导单引号）
    assert history_db._safe_csv_cell("=CMD|' /C calc'!A0").startswith("'")
    assert history_db._safe_csv_cell("@SUM(A1:A2)").startswith("'")
    assert history_db._safe_csv_cell("-12.50") == "-12.50"

    # 3. Web 服务拒绝跨站 Origin（防 CSRF）与 NaN/Inf 数值注入
    s = _make_settings(tmp_path)
    monkeypatch.setattr(cfg, "ENV_FILE", env_file)
    httpd = web_server._PanelServer(("127.0.0.1", 0), web_server.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with httpx.Client(trust_env=False, timeout=5) as c:
            # 恶意外部网页跨站 POST 必须返回 403
            r_csrf = c.post(
                f"http://127.0.0.1:{port}/api/price",
                headers={"Origin": "https://evil.example.com"},
                json={"price": 0.5},
            )
            assert r_csrf.status_code == 403
            assert r_csrf.headers.get("X-Content-Type-Options") == "nosniff"
            assert r_csrf.headers.get("X-Frame-Options") == "DENY"

            # NaN / Inf 必须返回 400，不得污染 .env
            r_nan = c.post(
                f"http://127.0.0.1:{port}/api/price",
                headers={"Origin": f"http://127.0.0.1:{port}"},
                content=b'{"price": NaN}',
            )
            assert r_nan.status_code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_tray_snapshot_icon_lock_and_tooltip_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tray

    s = _make_settings(tmp_path, CCB_ROOM_NAME="极长测试宿舍展示名-测试截断", ELEC_PRICE="0.69")
    now_dt = datetime(2026, 11, 27, 23, 40, 0, tzinfo=timezone(timedelta(hours=8)))
    now_ts = now_dt.timestamp()

    # 1. 正常健康状态：绿标 + 折算金额 + 燃气读数 + tooltip 不超过 Windows 128 字符上限
    st_ok = {
        "last_balance": 120.5,
        "last_level": "ok",
        "last_query_time": (now_dt - timedelta(minutes=5)).isoformat(),
        "gas_balance": 75.65,
        "gas_level": "ok",
        "gas_meter_reading": "1474",
        "gas_last_query_time": (now_dt - timedelta(minutes=10)).isoformat(),
    }
    snap_ok = tray.build_snapshot(st_ok, s, now_ts=now_ts)
    assert snap_ok["overall"] == "ok"
    assert "120.50 度" in snap_ok["elec_line"]
    assert "75.65 元" in snap_ok["gas_line"]
    assert len(snap_ok["tooltip"]) <= tray._TOOLTIP_MAX_LEN

    # 2. 数据超过 32 分钟未更新：变黄（low）并标 [待更新]
    st_stale = dict(st_ok, last_query_time=(now_dt - timedelta(minutes=45)).isoformat())
    snap_stale = tray.build_snapshot(st_stale, s, now_ts=now_ts)
    assert snap_stale["overall"] == "low"
    assert "待更新" in snap_stale["elec_line"]

    # 3. 认证失效且比最近成功时间更新：变红（critical）并标 [凭据失效]
    st_auth = dict(st_ok, last_auth_error_at=(now_dt - timedelta(minutes=1)).isoformat())
    snap_auth = tray.build_snapshot(st_auth, s, now_ts=now_ts)
    assert snap_auth["overall"] == "critical"
    assert snap_auth["elec_auth_bad"] is True
    assert "凭据失效" in snap_auth["elec_line"]

    # 4. 动态绘制四种状态图标均成功且尺寸正确
    for st_name in ("ok", "low", "critical", "unknown"):
        img = tray.make_tray_image(st_name, size=64)
        assert img.size == (64, 64)
        assert img.mode == "RGBA"

    # 5. 托盘单实例内核锁与 PID 文件验证
    monkeypatch.setattr(tray, "LOCK_FILE", tmp_path / "tray.lock")
    monkeypatch.setattr(tray, "PID_FILE", tmp_path / "tray.pid")
    assert tray._acquire_lock() is True
    try:
        assert (tmp_path / "tray.pid").read_text(encoding="utf-8").strip() == str(tray.os.getpid())
        if tray.os.name == "nt":
            import msvcrt

            with open(tmp_path / "tray.lock", "a+", encoding="utf-8") as fh2:
                fh2.seek(0)
                with pytest.raises(OSError):
                    msvcrt.locking(fh2.fileno(), msvcrt.LK_NBLCK, 1)
    finally:
        tray._release_lock()
    assert not (tmp_path / "tray.lock").exists()
    assert not (tmp_path / "tray.pid").exists()
