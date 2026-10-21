from dataclasses import replace
from types import SimpleNamespace

from collector.config import MqttConnection
from collector.mqtt_client import MqttCollector, client_id


class FakeClient:
    def __init__(self):
        self.calls = []
        self.subscribed = []

    def username_pw_set(self, u, p):
        self.calls.append(("auth", u))

    def reconnect_delay_set(self, min_delay, max_delay):
        self.calls.append(("delay", min_delay, max_delay))

    def subscribe(self, topics):
        self.subscribed.append(list(topics))
        return 0, 1

    def connect_async(self, host, port, keepalive):
        self.calls.append(("connect_async", host, port, keepalive))

    def loop_start(self):
        self.calls.append(("loop_start",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def loop_stop(self):
        self.calls.append(("loop_stop",))


class FakeWriter:
    def __init__(self):
        self.events = []
        self.messages = []

    def submit_event(self, kind, device_id=None, detail=None, ts=None):
        self.events.append((kind, detail))
        return True

    def submit_message(self, topic, payload, received_at):
        self.messages.append((topic, payload, received_at))
        return True


RC_OK = SimpleNamespace(is_failure=False, __str__=lambda self: "Success")
RC_BAD = SimpleNamespace(is_failure=True)
FLAGS = SimpleNamespace(session_present=False)
CONN = MqttConnection("broker", 1883, "user", "secret")


def make(cfg):
    fc, fw = FakeClient(), FakeWriter()
    return MqttCollector(cfg, CONN, fw, client=fc), fc, fw


def test_setup_calls(cfg):
    mq, fc, _ = make(cfg)
    assert ("delay", 1, 10) in fc.calls
    assert ("auth", "user") in fc.calls
    mq.start()
    assert ("connect_async", "broker", 1883, 30) in fc.calls  # 첫 연결 실패도 루프가 재시도
    assert fc.calls[-1] == ("loop_start",)


def test_resubscribe_on_every_connect(cfg):
    mq, fc, fw = make(cfg)
    for _ in range(3):  # 처음 + 재연결 2회
        mq._on_connect(fc, None, FLAGS, RC_OK, None)
    assert len(fc.subscribed) == 3
    assert fc.subscribed[0] == [("application/+/device/+/event/up", 1),
                                ("application/+/device/+/event/join", 1)]
    kinds = [k for k, _ in fw.events]
    assert kinds == ["connect", "subscribe"] * 3


def test_join_topic_off(cfg):
    off = replace(cfg, mqtt=replace(cfg.mqtt, join_topic=""))
    mq, fc, _ = make(off)
    mq._on_connect(fc, None, FLAGS, RC_OK, None)
    assert fc.subscribed[0] == [("application/+/device/+/event/up", 1)]


def test_refused_connect_does_not_subscribe(cfg):
    mq, fc, fw = make(cfg)
    mq._on_connect(fc, None, FLAGS, RC_BAD, None)
    assert fc.subscribed == []
    assert fw.events[0][0] == "connect_refused"


def test_disconnect_event(cfg):
    mq, fc, fw = make(cfg)
    mq._on_disconnect(fc, None, None, "Unspecified error", None)
    assert fw.events == [("disconnect", {"rc": "Unspecified error"})]


def test_connect_fail_does_not_raise(cfg):
    mq, fc, _ = make(cfg)
    mq._on_connect_fail(fc, None)  # 예외 없이 로그만


def test_message_goes_to_queue(cfg):
    mq, fc, fw = make(cfg)
    mq._on_message(fc, None, SimpleNamespace(topic="a/event/up", payload=b"{}"))
    assert fw.messages[0][:2] == ("a/event/up", b"{}")
    assert fw.messages[0][2].endswith("Z")


def test_client_id_has_user_and_host(cfg):
    cid = client_id(cfg)
    assert cid.startswith("tsinfer-collector-") and cid.count("-") >= 3


def test_secret_not_logged(cfg, capsys):
    mq, fc, _ = make(cfg)
    mq._on_connect(fc, None, FLAGS, RC_OK, None)
    mq._on_connect_fail(fc, None)
    out = capsys.readouterr().out
    assert "secret" not in out and "user" not in out.split("connect")[1][:40]


def test_broker_address_not_logged(cfg, capsys):
    mq, fc, _ = make(cfg)
    mq._on_connect(fc, None, FLAGS, RC_OK, None)
    mq._on_connect_fail(fc, None)
    assert "broker" not in capsys.readouterr().out
