"""SQLite 저장소. 메시지 1건 = 트랜잭션 1개. data-model.md, research R5.

쓰기는 수집기의 쓰기 스레드 하나만 한다. 다른 프로세스는 open_readonly()로 읽는다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from .config import FcntCfg
from .epoch import DeviceState, decide, on_join
from .parser import UplinkMessage, now_iso

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    device_id   TEXT    NOT NULL,
    fcnt_epoch  INTEGER NOT NULL,
    f_cnt       INTEGER NOT NULL,
    item        TEXT    NOT NULL,
    value       REAL    NOT NULL,
    location    TEXT    NOT NULL DEFAULT '',
    point       TEXT    NOT NULL DEFAULT '',
    spot        TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    measured_at TEXT    NOT NULL,
    gw_time     TEXT,
    received_at TEXT    NOT NULL,
    gateway_ids TEXT,
    PRIMARY KEY (device_id, fcnt_epoch, f_cnt, item)
);
CREATE INDEX IF NOT EXISTS idx_readings_loc_item_time ON readings (location, item, measured_at);
CREATE INDEX IF NOT EXISTS idx_readings_time ON readings (measured_at);

CREATE TABLE IF NOT EXISTS devices (
    device_id        TEXT PRIMARY KEY,
    model            TEXT,
    location         TEXT,
    point            TEXT,
    spot             TEXT,
    fcnt_epoch       INTEGER NOT NULL,
    last_f_cnt       INTEGER NOT NULL,
    last_measured_at TEXT,
    last_received_at TEXT
);

CREATE TABLE IF NOT EXISTS collector_events (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    device_id TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON collector_events (kind, ts);

CREATE TABLE IF NOT EXISTS rejected (
    id          INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    topic       TEXT,
    reason      TEXT NOT NULL,
    payload     TEXT
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

PAYLOAD_MAX = 4096


class SchemaError(Exception):
    """DB 스키마 버전이 코드와 다르다. 조용히 바꾸지 않고 멈춘다."""


@dataclass(frozen=True)
class SaveResult:
    epoch: int
    inserted: int      # 새로 들어간 행 수
    duplicate: bool    # 항목이 있었는데 한 행도 안 들어감(이미 있음)
    bumped: bool       # 이 메시지로 epoch이 올라감


def open_readonly(path: str) -> sqlite3.Connection:
    """읽기 전용 연결. WAL이라 수집기의 쓰기를 막지 않는다."""
    return sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, timeout=5)


class Store:
    def __init__(self, path: str):
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        # isolation_level=None: 트랜잭션을 BEGIN/COMMIT으로 직접 연다.
        # check_same_thread=False: run 시작 때 메인 스레드에서 열고, 이후에는 쓰기 스레드만 쓴다.
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
        if exists:
            row = self.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                found = row[0] if row else None
                raise SchemaError(f"DB 스키마 버전 {found!r} ≠ 코드 {SCHEMA_VERSION!r}: {self.path}")
        self.conn.executescript("BEGIN;" + SCHEMA + "COMMIT;")
        self.conn.execute("INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

    def close(self) -> None:
        self.conn.close()

    # --- 트랜잭션 도우미 -------------------------------------------------

    def _tx(self, fn):
        """BEGIN IMMEDIATE ~ COMMIT. 예외가 나면 ROLLBACK하고 다시 던진다(FR-008)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn()
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return result

    def _read_device(self, device_id: str) -> DeviceState | None:
        row = self.conn.execute(
            "SELECT fcnt_epoch, last_f_cnt, last_measured_at FROM devices WHERE device_id=?",
            (device_id,)).fetchone()
        return DeviceState(*row) if row else None

    def _write_device(self, msg: UplinkMessage, state: DeviceState) -> None:
        self.conn.execute(
            """INSERT INTO devices (device_id, model, location, point, spot, fcnt_epoch, last_f_cnt,
                                    last_measured_at, last_received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id) DO UPDATE SET
                   model=excluded.model, location=excluded.location, point=excluded.point,
                   spot=excluded.spot, fcnt_epoch=excluded.fcnt_epoch, last_f_cnt=excluded.last_f_cnt,
                   last_measured_at=excluded.last_measured_at,
                   last_received_at=max(coalesce(devices.last_received_at, ''), excluded.last_received_at)""",
            (msg.device_id, msg.model, msg.location, msg.point, msg.spot, state.fcnt_epoch,
             state.last_f_cnt, state.last_measured_at, msg.received_at))

    def _insert_event(self, kind: str, device_id: str | None, detail, ts: str | None) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        self.conn.execute("INSERT INTO collector_events (ts, kind, device_id, detail) VALUES (?, ?, ?, ?)",
                          (ts or now_iso(), kind, device_id, detail))

    # --- 공개 API ---------------------------------------------------------

    def save_uplink(self, msg: UplinkMessage, fcnt: FcntCfg) -> SaveResult:
        def work():
            state = self._read_device(msg.device_id)
            epoch, new_state, bumped = decide(state, msg.f_cnt, msg.measured_at,
                                              fcnt.reset_max, fcnt.min_drop)
            before = self.conn.total_changes
            self.conn.executemany(
                """INSERT OR IGNORE INTO readings (device_id, fcnt_epoch, f_cnt, item, value, location,
                       point, spot, model, measured_at, gw_time, received_at, gateway_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(msg.device_id, epoch, msg.f_cnt, item, value, msg.location, msg.point, msg.spot,
                  msg.model, msg.measured_at, msg.gw_time, msg.received_at, msg.gateway_ids)
                 for item, value in msg.items])
            inserted = self.conn.total_changes - before
            self._write_device(msg, new_state)
            if bumped:
                self._insert_event("epoch_bump", msg.device_id, {
                    "from": state.fcnt_epoch, "to": epoch, "last_f_cnt": state.last_f_cnt,
                    "f_cnt": msg.f_cnt, "reason": "fcnt_reset"}, msg.received_at)
            return SaveResult(epoch, inserted, bool(msg.items) and inserted == 0, bumped)

        return self._tx(work)

    def save_join(self, device_id: str, ts: str | None = None) -> int:
        """join 이벤트: epoch을 올리고 이벤트를 남긴다. 측정값은 저장하지 않는다. 새 epoch을 돌려준다."""
        def work():
            state = self._read_device(device_id)
            new = on_join(state)
            self.conn.execute(
                """INSERT INTO devices (device_id, fcnt_epoch, last_f_cnt, last_measured_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                       fcnt_epoch=excluded.fcnt_epoch, last_f_cnt=excluded.last_f_cnt""",
                (device_id, new.fcnt_epoch, new.last_f_cnt, new.last_measured_at))
            self._insert_event("join", device_id, {"epoch": new.fcnt_epoch}, ts)
            return new.fcnt_epoch

        return self._tx(work)

    def log_event(self, kind: str, device_id: str | None = None, detail=None, ts: str | None = None) -> None:
        self._tx(lambda: self._insert_event(kind, device_id, detail, ts))

    def save_rejected(self, received_at: str, topic: str, reason: str, payload: bytes) -> None:
        text = payload[:PAYLOAD_MAX].decode("utf-8", errors="replace")[:PAYLOAD_MAX]
        self._tx(lambda: self.conn.execute(
            "INSERT INTO rejected (received_at, topic, reason, payload) VALUES (?, ?, ?, ?)",
            (received_at, topic, reason, text)))
