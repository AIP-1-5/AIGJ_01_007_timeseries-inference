"""stdout 한 줄 로그: `시각 수준 이벤트 key=value …`. systemd가 journald로 보낸다. 비밀값을 넘기지 않는다."""

from __future__ import annotations

import sys

from .parser import now_iso


def _fmt(value) -> str:
    text = str(value)
    if text == "" or any(c in text for c in " \t\"="):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def log(level: str, event: str, **fields) -> None:
    parts = [now_iso(), level, event] + [f"{k}={_fmt(v)}" for k, v in fields.items()]
    print(" ".join(parts), file=sys.stdout, flush=True)
