"""서비스가 systemd watchdog에 살아 있다는 신호를 보낸다(멈춤 → 자동 재시작)."""

import socket

from service.__main__ import sd_notify


def test_no_socket_outside_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("WATCHDOG=1") is False


def test_sends_to_notify_socket(tmp_path, monkeypatch):
    path = str(tmp_path / "notify")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    try:
        assert sd_notify("WATCHDOG=1")
        assert srv.recv(64) == b"WATCHDOG=1"
    finally:
        srv.close()


def test_abstract_socket(monkeypatch):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind("\0tsinfer-test-notify")
    srv.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", "@tsinfer-test-notify")
    try:
        assert sd_notify("READY=1")
        assert srv.recv(64) == b"READY=1"
    finally:
        srv.close()


def test_unreachable_socket_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "missing"))
    assert sd_notify("WATCHDOG=1") is False
