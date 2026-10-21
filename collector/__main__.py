"""CLI 진입점. 계약: contracts/cli.md. 종료 코드 0 정상, 2 설정 오류."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import socket
import sys
import threading

from .config import ConfigError, load_config, mqtt_connection
from .log import log
from .parser import now_iso
from .store import SchemaError, Store

UP_TEMPLATE = "application/replay/device/{dev}/event/up"


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config/collector.toml", help="설정 파일 (기본 config/collector.toml)")
    common.add_argument("--env-file", default=".env", help="비밀값 파일 (환경변수가 우선)")

    p = argparse.ArgumentParser(prog="collector", description="과제 1 센서 데이터 수집기")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="상시 수집 (systemd가 실행)")
    run.add_argument("--db", help="설정의 storage.db_path 덮어쓰기")

    rep = sub.add_parser("replay", parents=[common], help="jsonl 메시지를 수집 경로에 넣기 (브로커 없이)")
    rep.add_argument("path")
    rep.add_argument("--db", help="설정의 storage.db_path 덮어쓰기")
    rep.add_argument("--topic-template", default=UP_TEMPLATE,
                     help="업링크 JSON만 있는 줄에 쓸 토픽 ({dev} = devEui)")

    sub.add_parser("check-config", parents=[common], help="설정 파일만 검사")
    return p


def _replay_lines(path: str, template: str):
    """줄마다 (topic, payload bytes). `{"topic","payload"}` 또는 업링크 JSON 하나."""
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                yield template.format(dev="unknown"), raw
                continue
            if isinstance(obj, dict) and "topic" in obj and "payload" in obj:
                payload = obj["payload"]
                if not isinstance(payload, str):
                    payload = json.dumps(payload, ensure_ascii=False)
                yield str(obj["topic"]), payload.encode("utf-8")
            else:
                dev = obj.get("deviceInfo", {}).get("devEui", "unknown") if isinstance(obj, dict) else "unknown"
                yield template.format(dev=dev), raw


def cmd_replay(args) -> int:
    from .writer import Pipeline

    cfg = load_config(args.config)
    store = Store(args.db or cfg.storage.db_path)
    try:
        pipe = Pipeline(store, cfg)
        for topic, payload in _replay_lines(args.path, args.topic_template):
            pipe.handle(topic, payload, now_iso())
        s = pipe.stats
        print(f"처리 {s.processed}, 저장 메시지 {s.saved}, 중복 무시 {s.duplicate}, "
              f"버림 {s.rejected}, 매핑 안 된 키 {s.unmapped}")
    finally:
        store.close()
    return 0


def cmd_check_config(args) -> int:
    cfg = load_config(args.config)
    keys = sum(len(m.keys) for m in cfg.models.values())
    print(f"설정 정상: {cfg.path} (모델 {len(cfg.models)}개, 키 매핑 {keys}개, 장치 덮어쓰기 {len(cfg.devices)}개, "
          f"unmapped={cfg.unmapped_policy}, join_topic={'켬' if cfg.mqtt.join_topic else '끔'})")
    return 0


def cmd_run(args) -> int:
    from .mqtt_client import MqttCollector, client_id
    from .writer import Writer

    cfg = load_config(args.config)
    conn = mqtt_connection(args.env_file)
    db = args.db or cfg.storage.db_path
    store = Store(db)
    stop = threading.Event()

    def on_signal(signum, frame):
        log("INFO", "signal", sig=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    start_detail = {"pid": os.getpid(), "host": socket.gethostname(), "user": getpass.getuser(),
                    "db": db, "config": cfg.path, "client_id": client_id(cfg)}
    store.log_event("start", detail=start_detail)
    log("INFO", "start", **start_detail)

    writer = Writer(store, cfg)
    writer.start()
    mq = MqttCollector(cfg, conn, writer)
    mq.start()

    code = 0
    while not stop.wait(1.0):
        if not writer.is_alive():  # 쓰기 스레드가 죽으면 프로세스를 끝내 systemd가 재시작하게 한다
            log("ERROR", "writer_dead")
            code = 1
            break
    mq.stop()
    writer.stop()
    store.close()
    log("INFO", "exit", code=code)
    return code


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    handlers = {"run": cmd_run, "replay": cmd_replay, "check-config": cmd_check_config}
    try:
        return handlers[args.command](args)
    except (ConfigError, SchemaError) as e:
        print(f"설정 오류: {e}", file=sys.stderr, flush=True)
        log("ERROR", "config_error", error=str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
