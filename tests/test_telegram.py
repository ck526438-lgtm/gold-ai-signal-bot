import json

import requests
from loguru import logger

from src.state import StateStore
from src.telegram_notifier import TelegramNotifier

TOKEN = "123456789:AAFakeSecretTokenValue_xyz"


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data if data is not None else {"ok": True, "result": []}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        r = self.responses.pop(0) if self.responses else FakeResp()
        if isinstance(r, Exception):
            raise r
        return r


def _notifier(cfg, session=None, dry_run=False, sleeps=None):
    cfg["monitoring"]["dry_run"] = dry_run
    sl = sleeps if sleeps is not None else []
    return TelegramNotifier(cfg, token=TOKEN, chat_id="42", session=session or FakeSession([]), sleep=sl.append)


def capture_logs():
    lines = []
    hid = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    return lines, hid


def test_dry_run_logs_instead_of_sending(cfg):
    sess = FakeSession([])
    n = _notifier(cfg, sess, dry_run=True)
    lines, hid = capture_logs()
    n.notify_entry("XAUUSD", "BUY", 2000.0, 1997.0, 2006.0, 0.1, "test")
    logger.remove(hid)
    assert sess.calls == [] and any("dry_run" in l for l in lines)


def test_token_never_logged_on_network_error(cfg):
    err = requests.ConnectionError(f"HTTPSConnectionPool: /bot{TOKEN}/sendMessage failed")
    sess = FakeSession([err, err, err])
    sleeps = []
    n = _notifier(cfg, sess, sleeps=sleeps)
    lines, hid = capture_logs()
    n.send("hello", 0)
    logger.remove(hid)
    assert len(sess.calls) == 3                   # 3 tentatives
    assert sleeps[-2:] == [1.0, 2.0]              # backoff exponentiel
    assert not any(TOKEN in l for l in lines)
    assert any("***" in l for l in lines)


def test_rate_limit_one_message_per_3s(cfg):
    sleeps = []
    n = _notifier(cfg, FakeSession([FakeResp(), FakeResp()]), sleeps=sleeps)
    n.send("a", 0)
    n.send("b", 0)
    assert sleeps and 2.5 < sleeps[-1] <= 3.0


def test_429_respects_retry_after(cfg):
    sleeps = []
    sess = FakeSession([FakeResp(429, {"ok": False, "parameters": {"retry_after": 7}}), FakeResp()])
    n = _notifier(cfg, sess, sleeps=sleeps)
    n._request("sendMessage", {"text": "x"})
    assert 7 in sleeps and len(sess.calls) == 2


def test_unauthorized_chat_ignored(cfg):
    n = _notifier(cfg)
    assert n.handle_command("/pause", "999") is None
    assert StateStore(cfg["monitoring"]["pause_file"]).read()["paused"] is False


def test_pause_resume(cfg):
    n = _notifier(cfg)
    st = StateStore(cfg["monitoring"]["pause_file"])
    n.handle_command("/pause", "42")
    assert st.read()["paused"] is True
    n.handle_command("/resume", "42")
    assert st.read()["paused"] is False


def test_stop_requires_confirmation(cfg):
    n = _notifier(cfg)
    st = StateStore(cfg["monitoring"]["pause_file"])
    assert "confirm" in n.handle_command("/stop confirm", "42").lower()  # sans /stop préalable
    assert st.read()["stop_requested"] is False
    reply = n.handle_command("/stop", "42")
    assert "/stop confirm" in reply and st.read()["stop_requested"] is False
    n.handle_command("/stop confirm", "42")
    s = st.read()
    assert s["stop_requested"] is True and s["close_positions_on_stop"] is False  # positions conservées


def test_stop_confirm_close(cfg):
    n = _notifier(cfg)
    n.handle_command("/stop", "42")
    n.handle_command("/stop confirm close", "42")
    assert StateStore(cfg["monitoring"]["pause_file"]).read()["close_positions_on_stop"] is True


def test_status_and_report(cfg, tmp_path):
    n = _notifier(cfg)
    n.status_provider = lambda: "STATUT OK"
    assert n.handle_command("/status", "42") == "STATUT OK"
    from src.execution import append_trade_csv
    import pandas as pd
    now = pd.Timestamp.now(tz="UTC").isoformat()
    append_trade_csv(cfg["logging"]["trades_csv"], {"ticket": 1, "pnl": 40.0, "close_time": now})
    append_trade_csv(cfg["logging"]["trades_csv"], {"ticket": 2, "pnl": -20.0, "close_time": now})
    rep = n.handle_command("/report", "42")
    assert "Trades : 2" in rep and "+20.00$" in rep


def test_poll_processes_commands(cfg):
    upd = {"ok": True, "result": [{"update_id": 5, "message": {"text": "/pause", "chat": {"id": 42}}}]}
    sess = FakeSession([FakeResp(200, upd), FakeResp()])
    n = _notifier(cfg, sess)
    assert n.poll_once() == 1
    assert n._offset == 6
    assert json.dumps(sess.calls[-1][1]).find("pause") >= 0
