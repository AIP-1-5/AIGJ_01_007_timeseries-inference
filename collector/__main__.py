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

    st = sub.add_parser("status", parents=[common], help="현황 조회 (읽기 전용)")
    st.add_argument("--db", help="설정의 storage.db_path 덮어쓰기")
    st.add_argument("--hours", type=float, default=24, help="최근 몇 시간 (기본 24)")
    st.add_argument("--json", action="store_true", help="JSON으로 출력")

    rp = sub.add_parser("report", parents=[common], help="수집 현황 보고(D-1) Markdown 만들기 (읽기 전용)")
    rp.add_argument("--db", help="설정의 storage.db_path 덮어쓰기")
    rp.add_argument("--start", required=True, help="시작 (UTC ISO, 예: 2026-10-21T07:04Z)")
    rp.add_argument("--end", help="끝 (기본: 시작 + 24시간)")
    rp.add_argument("--out", help="저장할 파일 (없으면 화면에 출력)")
    rp.add_argument("--data", default="실데이터 (교육장 MQTT 브로커)", help="측정 조건의 데이터 설명")
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


def cmd_status(args) -> int:
    from .report import format_status, status_data
    from .store import open_readonly

    cfg = load_config(args.config)
    db = args.db or cfg.storage.db_path
    if not os.path.exists(db):
        print(f"DB가 없다: {db}", file=sys.stderr)
        return 1
    conn = open_readonly(db)
    try:
        data = status_data(conn, cfg, args.hours)
    finally:
        conn.close()
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else format_status(data))
    return 0


def _report_meta(cfg, data_desc: str, last_start: str | None) -> dict:
    """보고서 측정 조건. 명령이 실패해도 보고서는 만든다(값에 '확인 못 함')."""
    import hashlib
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=repo).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    commit = run(["git", "rev-parse", "--short", "HEAD"]) or "확인 못 함"
    if run(["git", "status", "--porcelain", "--untracked-files=no"]):
        commit += " + 커밋 안 된 변경"
    power = next((line.split(":", 1)[1].strip() for line in run(["nvpmodel", "-q"]).splitlines()
                  if line.startswith("NV Power Mode")), "확인 못 함")
    service = "확인 못 함"
    if last_start:
        info = json.loads(last_start)
        service = (f"`tsinfer-collector-{info.get('user', '?')}` (마지막 start: host={info.get('host')}, "
                   f"user={info.get('user')}, client_id={info.get('client_id')})")
    with open(cfg.path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()[:12]
    return {"host": socket.gethostname(), "power_mode": power, "service": service, "commit": commit,
            "config_path": os.path.relpath(cfg.path, repo), "config_sha": sha, "data": data_desc}


def cmd_report(args) -> int:
    from datetime import timedelta

    from .coverage import parse_iso
    from .report import format_report, report_data
    from .store import open_readonly

    cfg = load_config(args.config)
    db = args.db or cfg.storage.db_path
    if not os.path.exists(db):
        print(f"DB가 없다: {db}", file=sys.stderr)
        return 1
    try:
        start = parse_iso(args.start)
        end = parse_iso(args.end) if args.end else start + timedelta(hours=24)
    except ValueError as e:
        print(f"시각 형식 오류: {e}", file=sys.stderr)
        return 2
    conn = open_readonly(db)
    try:
        data = report_data(conn, cfg, start, end)
    finally:
        conn.close()
    text = format_report(data, cfg, _report_meta(cfg, args.data, data["last_start"]))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"보고서: {args.out}")
    else:
        print(text)
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
    handlers = {"run": cmd_run, "replay": cmd_replay, "check-config": cmd_check_config,
                "status": cmd_status, "report": cmd_report}
    try:
        return handlers[args.command](args)
    except (ConfigError, SchemaError) as e:
        print(f"설정 오류: {e}", file=sys.stderr, flush=True)
        log("ERROR", "config_error", error=str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
