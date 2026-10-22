"""MQTT 수신. paho-mqtt 2.x, 자동 재연결, on_connect마다 재구독. research R3.

콜백은 큐에 넣기만 한다(디스크를 기다리지 않음). 이벤트도 Writer 큐를 거쳐 기록한다.
"""

from __future__ import annotations

import getpass
import socket

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .config import Config, MqttConnection
from .log import log
from .parser import now_iso
from .writer import Writer


def client_id(cfg: Config) -> str:
    # 팀원 수집기와 겹치면 브로커가 서로 끊어 내므로 사용자·호스트를 넣는다
    return f"{cfg.mqtt.client_id_prefix}-{getpass.getuser()}-{socket.gethostname()}"


class MqttCollector:
    def __init__(self, cfg: Config, conn: MqttConnection, writer: Writer, client=None):
        self.cfg = cfg
        self.conn = conn
        self.writer = writer
        self.client = client or mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id(cfg),
            clean_session=cfg.mqtt.clean_session,
            protocol=mqtt.MQTTv311,
        )
        if conn.username:
            self.client.username_pw_set(conn.username, conn.password or None)
        self.client.reconnect_delay_set(min_delay=cfg.mqtt.reconnect_min_s, max_delay=cfg.mqtt.reconnect_max_s)
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def topics(self) -> list[tuple[str, int]]:
        """측정값은 uplink_topic만. join_topic은 epoch 판정용(비어 있으면 구독 안 함)."""
        out = [(self.cfg.mqtt.uplink_topic, self.cfg.mqtt.qos)]
        if self.cfg.mqtt.join_topic:
            out.append((self.cfg.mqtt.join_topic, self.cfg.mqtt.qos))
        return out

    # --- 콜백 (paho 네트워크 스레드) -----------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        rc = str(reason_code)
        if getattr(reason_code, "is_failure", False):
            log("WARN", "connect_refused", rc=rc)
            self.writer.submit_event("connect_refused", detail={"rc": rc})
            return
        session = bool(getattr(flags, "session_present", False))
        log("INFO", "connect", port=self.conn.port, session_present=session)  # 주소는 비밀값이라 남기지 않는다
        self.writer.submit_event("connect", detail={"rc": rc, "session_present": session})
        topics = self.topics()
        result, mid = client.subscribe(topics)
        names = [t for t, _ in topics]
        log("INFO", "subscribe", topics=",".join(names), result=result)
        self.writer.submit_event("subscribe", detail={"topics": names, "result": int(result)})

    def _on_connect_fail(self, client, userdata):
        # TCP 연결 실패. paho 루프가 reconnect_delay 간격으로 계속 다시 시도한다
        log("WARN", "connect_fail", port=self.conn.port)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        rc = str(reason_code)
        log("WARN", "disconnect", rc=rc)
        self.writer.submit_event("disconnect", detail={"rc": rc})

    def _on_message(self, client, userdata, msg):
        if not self.writer.submit_message(msg.topic, msg.payload, now_iso()):
            pass  # 큐가 가득 참: Writer가 queue_drop으로 세고 기록한다

    # --- 시작·정지 ---------------------------------------------------------

    def start(self) -> None:
        """connect_async + loop_start: 첫 연결이 실패해도 예외 없이 계속 다시 시도한다."""
        self.client.connect_async(self.conn.host, self.conn.port, keepalive=self.cfg.mqtt.keepalive_s)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.disconnect()
        finally:
            self.client.loop_stop()
