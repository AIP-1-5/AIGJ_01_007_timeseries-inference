"""조회 API(표준 라이브러리 http.server). 최신 주기 결과를 메모리에서 읽어 JSON으로 돌려준다.

GET /health · /spaces · /spaces/<id> · /spaces/<id>/forecast?minutes=30 · /alerts
<id>는 영문 id(lab, office, meeting, outside) 또는 한글 이름.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from collector.log import log

from .core import ServiceConfig, ServiceState


def route(state: ServiceState, cfg: ServiceConfig, path: str) -> tuple[int, dict]:
    """경로 → (HTTP 상태, JSON). 테스트에서 서버 없이 부른다."""
    url = urlparse(path)
    parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
    snap = state.snapshot()
    if parts == ["health"]:
        t = snap["status"].get("time")
        age = None
        if t:
            age = int((datetime.now(timezone.utc) - datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds())
        ok = age is not None and age < 3 * cfg["service"]["interval_s"]
        return 200, {"status": "ok" if ok else "stale", "last_cycle": t, "age_s": age, "cycles": snap["cycles"],
                     "failures": snap["failures"], "models": snap["status"].get("models")}
    if parts == ["spaces"]:
        return 200, {"spaces": [{"id": sid, "name": p.get("space"), "status": p.get("status"),
                                 "data_time": p.get("data_time")} for sid, p in snap["spaces"].items()]}
    if parts == ["alerts"]:
        active = [dict(a, space=p.get("space")) for p in snap["spaces"].values() for a in p.get("alerts", [])]
        return 200, {"active": active, "recent_events": snap["events"][-50:]}
    if len(parts) in (2, 3) and parts[0] == "spaces":
        name = cfg.resolve_space(parts[1])
        if name is None:
            return 404, {"error": f"모르는 공간: {parts[1]}", "spaces": list(cfg.spaces.values())}
        p = snap["spaces"].get(cfg.space_id(name))
        if p is None:
            return 503, {"error": "아직 결과 없음(첫 주기 전)"}
        if len(parts) == 2:
            return 200, p
        if parts[2] != "forecast":
            return 404, {"error": "모르는 경로"}
        fc = p.get("forecast")
        if not fc:
            return 404, {"error": f"{name}은 예측 대상이 아니다"}
        try:
            minutes = int(parse_qs(url.query).get("minutes", ["60"])[0])
        except ValueError:
            minutes = -1
        if minutes not in fc["minutes"]:
            return 400, {"error": "minutes는 5~60 사이 5의 배수", "allowed": fc["minutes"]}
        i = fc["minutes"].index(minutes)
        return 200, {"space": name, "space_id": cfg.space_id(name), "minutes": minutes,
                     "data_time": p.get("data_time"), "co2": fc["co2"][i], "temperature": fc["temperature"][i],
                     "unit": {"co2": "ppm", "temperature": "°C"}, "model": fc["model"]}
    return 404, {"error": "모르는 경로", "paths": ["/health", "/spaces", "/spaces/<id>",
                                                 "/spaces/<id>/forecast?minutes=30", "/alerts"]}


def serve(state: ServiceState, cfg: ServiceConfig) -> ThreadingHTTPServer | None:
    """백그라운드 스레드로 API를 띄운다. 포트를 못 쓰면 None(서비스는 발행을 계속한다)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            code, body = route(state, cfg, self.path)
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # 요청마다 로그를 쓰지 않는다
            pass

    try:
        server = ThreadingHTTPServer((cfg["api"]["host"], cfg["api"]["port"]), Handler)
    except OSError as e:
        log("ERROR", "api_start_failed", port=cfg["api"]["port"], error=repr(e)[:200])
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="api").start()
    log("INFO", "api_started", port=server.server_address[1])
    return server
