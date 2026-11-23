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
import keep_alive as ka  # noqa: E402
import main as m  # noqa: E402
import notifier  # noqa: E402
import query_elec as qe  # noqa: E402
import query_gas as qg  # noqa: E402
from harvest_creds import parse_curl  # noqa: E402
from web import server as web_server  # noqa: E402


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
