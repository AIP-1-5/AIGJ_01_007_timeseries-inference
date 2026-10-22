"""MQTT 발행. retained라서 새로 접속한 구독자도 마지막 결과를 바로 받는다. 접속값은 .env(비밀값, 로그 금지).

브로커가 없거나 끊겨도 주기는 멈추지 않는다(paho 루프가 다시 붙고, 발행 실패는 로그만 남긴다).
"""

from __future__ import annotations

import getpass
import json
import socket

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from collector.config import ConfigError, mqtt_connection
from collector.log import log


class Publisher:
    def __init__(self, cfg: dict, env_file: str = ".env"):
        self.prefix = cfg["topic_prefix"].rstrip("/")
        self.qos = cfg.get("qos", 1)
        self.retain = cfg.get("retain", True)
        self.client = None
        try:
            conn = mqtt_connection(env_file)
        except ConfigError as e:
            log("WARN", "publish_disabled", error=str(e))
            return
        cid = f"{cfg.get('client_id_prefix', 'tsinfer-service')}-{getpass.getuser()}-{socket.gethostname()}"
        c = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=cid, clean_session=True,
                        protocol=mqtt.MQTTv311)
        if conn.username:
            c.username_pw_set(conn.username, conn.password or None)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.on_connect = lambda cl, ud, fl, rc, pr=None: log("INFO", "publish_connect", rc=str(rc))
        c.on_disconnect = lambda cl, ud, fl, rc, pr=None: log("WARN", "publish_disconnect", rc=str(rc))
        c.connect_async(conn.host, conn.port, keepalive=30)   # 주소는 로그에 쓰지 않는다
        c.loop_start()
        self.client = c

    def topic(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    def publish(self, suffix: str, payload: dict, retain: bool | None = None) -> bool:
        if self.client is None:
            return False
        try:
            info = self.client.publish(self.topic(suffix), json.dumps(payload, ensure_ascii=False), qos=self.qos,
                                       retain=self.retain if retain is None else retain)
            return info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:  # 발행 실패가 주기를 멈추면 안 된다
            log("WARN", "publish_failed", topic=suffix, error=repr(e)[:200])
            return False

    def publish_result(self, result: dict) -> int:
        """공간별 결과(retained) + status(retained) + 경보 이벤트(retained 아님). 성공한 발행 수."""
        ok = 0
        for sid, payload in result["spaces"].items():
            ok += self.publish(sid, payload)
        ok += self.publish("status", {"time": result["time"], "data_time": result["data_time"],
                                      "duration_ms": result["duration_ms"], "models": result["models"],
                                      "status": "ok"})
        for ev in result["events"]:
            ok += self.publish("alerts", ev, retain=False)
        return ok

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()
