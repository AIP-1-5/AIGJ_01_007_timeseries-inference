"""과제 8 서비스 실행.

    python -m service run  [--config config/service.toml]   # 상시 실행(systemd)
    python -m service once [--no-publish]                    # 한 주기만 계산해 JSON 출력(시험용)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

from collector.log import log

from .core import DEFAULT_CONFIG, Service, load_service_config


def _append_cycle_log(path: str, result: dict) -> None:
    """12시간 가동 요약용 한 줄 기록(주기 시각, 처리 시간, 공간별 상태, 경보 이벤트)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = {"time": result["time"], "duration_ms": result["duration_ms"], "models": result["models"],
            "spaces": {sid: {"status": p.get("status"), "alerts": [a["type"] for a in p.get("alerts", [])]}
                       for sid, p in result["spaces"].items()},
            "events": result["events"]}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def sd_notify(msg: str) -> bool:
    """systemd에 상태를 알린다(READY=1, WATCHDOG=1). systemd 밖(NOTIFY_SOCKET 없음)에서는 아무것도 안 한다.
    주기마다 WATCHDOG=1을 보내고, 주기가 멈춰 WatchdogSec 동안 안 오면 systemd가 죽이고 다시 띄운다.
    2026-10-22 09:07 서비스가 죽지 않고 멈춰 Restart=always가 동작하지 않았다(decisions R11)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]                # 추상 네임스페이스 소켓
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
        return True
    except OSError:
        return False


def cmd_once(args) -> int:
    svc = Service(load_service_config(args.config))
    result = svc.cycle()
    if not args.no_publish:
        from .publish import Publisher
        pub = Publisher(svc.cfg["mqtt"], args.env_file)
        time.sleep(2)
        print(f"발행 {pub.publish_result(result)}건", file=sys.stderr)
        time.sleep(1)
        pub.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args) -> int:
    from .api import serve
    from .publish import Publisher

    cfg = load_service_config(args.config)
    svc = Service(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda s, f: stop.set())
    signal.signal(signal.SIGINT, lambda s, f: stop.set())
    server = serve(svc.state, cfg)
    pub = Publisher(cfg["mqtt"], args.env_file)
    interval, offset = cfg["service"]["interval_s"], cfg["service"]["offset_s"]
    cycle_log = os.path.expanduser(cfg["service"]["cycle_log"])
    log("INFO", "service_start", pid=os.getpid(), interval_s=interval)
    sd_notify("READY=1")
    while not stop.is_set():
        now = time.time()
        nxt = (now // interval + 1) * interval + offset     # 벽시계 매분 offset초에 맞춘다
        if stop.wait(nxt - now):
            break
        try:
            result = svc.cycle(datetime.now(timezone.utc))
            sent = pub.publish_result(result)
            _append_cycle_log(cycle_log, result)
            alerts = sum(len(p.get("alerts", [])) for p in result["spaces"].values())
            log("INFO", "cycle", duration_ms=result["duration_ms"], published=sent, active_alerts=alerts,
                events=len(result["events"]), models=",".join(k for k, v in result["models"].items() if v == "loaded"))
        except Exception as e:  # 한 주기 실패가 다음 주기를 막지 않는다
            svc.mark_failure(repr(e))
            log("ERROR", "cycle_failed", error=repr(e)[:300])
            traceback.print_exc(limit=3)
        sd_notify("WATCHDOG=1")   # 실패한 주기도 "멈추지 않았다"는 신호다. 멈춤만 잡는다
    pub.close()
    if server:
        server.shutdown()
    log("INFO", "service_stop")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="service", description="과제 8 상시 추론 서비스")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "once"):
        s = sub.add_parser(name)
        s.add_argument("--config", default=str(DEFAULT_CONFIG))
        s.add_argument("--env-file", default=".env")
        if name == "once":
            s.add_argument("--no-publish", action="store_true", help="MQTT로 보내지 않고 출력만")
    args = p.parse_args(argv)
    return {"run": cmd_run, "once": cmd_once}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
