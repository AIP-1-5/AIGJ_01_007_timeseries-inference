"""커버율·수신률·결측 구간과 원인. research R8, contracts/cli.md, decisions.md(원인 분류).

모든 함수는 읽기 전용 연결(store.open_readonly)로 부른다. 수집기의 쓰기를 막지 않는다(WAL).
시각 인자는 UTC datetime, 기간은 [start, end). DB의 시각 문자열은 고정 형식이라 문자열 비교로 범위를 자른다.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config

DOWN_GAP_S = 90         # heartbeat 공백이 이보다 길면 수집기가 꺼져 있었다고 본다 (R8)
STALE_DEFAULT_S = 300   # 간격을 모르는 센서의 끊김 경고 기준
LOSS_SLACK_S = 60       # queue_drop은 몇 초 뒤에 기록되므로 구간 끝을 이만큼 늘려 본다

# 수집기가 살아 있었다는 증거가 되는 이벤트
ALIVE_KINDS = ("start", "stop", "heartbeat", "connect", "connect_refused", "disconnect", "subscribe")
LOSS_KINDS = ("queue_drop", "write_error")

CAUSES = {
    "collector_down": "수집기 중단",
    "disconnected": "브로커 연결 끊김",
    "collector_loss": "수집기 쪽 손실",
    "sensor_loss": "센서·무선 쪽 손실",
    "no_transmission": "전송 간격(센서가 보내지 않음)",
    "unknown": "원인 미상",
}


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _hms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%SZ")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def configured_interval(cfg: Config, device_id: str, model: str | None) -> int | None:
    """장치 덮어쓰기 > 모델 기본값. 둘 다 없으면 None."""
    dev = cfg.devices.get(device_id)
    if dev and dev.interval_s:
        return dev.interval_s
    m = cfg.models.get(model or "")
    return m.interval_s if m else None


def _known_devices(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """devices 테이블: device_id → (model, location)."""
    return {d: (m or "", loc or "") for d, m, loc in
            conn.execute("SELECT device_id, model, location FROM devices")}


# --- 1분 칸 커버율 ------------------------------------------------------------

@dataclass
class LocItemCoverage:
    location: str
    item: str
    count: int                    # 기간 안 행 수
    last_received_at: str | None  # 기간 안 마지막 수신(수집기 시계)
    filled: int                   # 값이 있는 1분 칸
    total: int                    # 전체 1분 칸
    coverage: float
    intervals: list[int]          # 이 공간·항목 센서들의 전송 간격(초, 아는 것만)
    devices: list[str]            # 이 공간·항목을 재는 센서


def coverage_by_location_item(conn: sqlite3.Connection, cfg: Config,
                              start: datetime, end: datetime) -> list[LocItemCoverage]:
    """공간·항목마다 1분 칸 커버율. 칸 안에 센서 하나라도 값이 있으면 채워진 칸(FR-017).

    기간 안에 한 건도 없는 공간·항목도 devices 테이블과 설정의 모델 키로 찾아 0%로 보여 준다.
    """
    start, end = floor_minute(start), floor_minute(end)
    total = max(0, int((end - start).total_seconds() // 60))
    devices = _known_devices(conn)

    found: dict[tuple[str, str], tuple[int, str | None, int]] = {}
    pair_devices: dict[tuple[str, str], set[str]] = {}
    for loc, item, n, last, filled, devs in conn.execute(
            """SELECT location, item, count(*), max(received_at),
                      count(DISTINCT substr(measured_at, 1, 16)), group_concat(DISTINCT device_id)
               FROM readings WHERE measured_at >= ? AND measured_at < ?
               GROUP BY location, item""", (to_iso(start), to_iso(end))):
        found[(loc, item)] = (n, last, filled)
        pair_devices[(loc, item)] = set(devs.split(","))

    for dev, (model, loc) in devices.items():
        m = cfg.models.get(model)
        for item in (m.keys.values() if m else ()):
            pair_devices.setdefault((loc, item), set()).add(dev)

    rows = []
    for (loc, item) in sorted(pair_devices):
        n, last, filled = found.get((loc, item), (0, None, 0))
        intervals = sorted({i for d in pair_devices[(loc, item)]
                            if (i := configured_interval(cfg, d, devices.get(d, ("", ""))[0]))})
        rows.append(LocItemCoverage(loc, item, n, last, filled, total,
                                    filled / total if total else 0.0, intervals,
                                    sorted(pair_devices[(loc, item)])))
    return rows


# --- 센서별 수신률 -------------------------------------------------------------

@dataclass
class DeviceRate:
    device_id: str
    model: str
    location: str
    received: int                  # 기간 안 고유 (epoch, f_cnt) 수
    expected: int                  # epoch별 (max − min + 1)의 합
    skipped: int
    rate: float | None             # 받은 수 ÷ 기대 수. 기간 안에 한 건도 없으면 None
    interval_s: int | None
    interval_source: str | None    # "config" | "observed" | None
    interval_filled: int
    interval_total: int
    interval_coverage: float | None
    last_received_at: str | None


def device_rates(conn: sqlite3.Connection, cfg: Config,
                 start: datetime, end: datetime) -> list[DeviceRate]:
    """센서마다 수신률과 전송 간격 기준 커버율(R8). 간격은 설정 우선, 없으면 관측 중앙값."""
    msgs: dict[str, list[tuple[int, int, datetime]]] = {}
    meta: dict[str, list] = {}
    for dev, epoch, fc, measured, recv, model, loc in conn.execute(
            """SELECT device_id, fcnt_epoch, f_cnt, min(measured_at), max(received_at), max(model), max(location)
               FROM readings WHERE measured_at >= ? AND measured_at < ?
               GROUP BY device_id, fcnt_epoch, f_cnt
               ORDER BY device_id, min(measured_at)""", (to_iso(start), to_iso(end))):
        msgs.setdefault(dev, []).append((epoch, fc, parse_iso(measured)))
        m = meta.setdefault(dev, [model, loc, recv])
        m[2] = max(m[2], recv)
    for dev, (model, loc) in _known_devices(conn).items():
        meta.setdefault(dev, [model, loc, None])

    span = (end - start).total_seconds()
    out = []
    for dev in sorted(meta):
        model, loc, last = meta[dev]
        rows = msgs.get(dev, [])
        by_epoch: dict[int, list[int]] = {}
        for epoch, fc, _ in rows:
            by_epoch.setdefault(epoch, []).append(fc)
        expected = sum(max(f) - min(f) + 1 for f in by_epoch.values())
        received = len(rows)

        interval, source = configured_interval(cfg, dev, model), "config"
        if interval is None:
            interval, source = observed_interval(rows), "observed"
        if interval is None:
            source = None

        filled = total = 0
        if interval:
            total = math.ceil(span / interval)
            filled = len({int((t - start).total_seconds() // interval) for _, _, t in rows})
        out.append(DeviceRate(
            dev, model, loc, received, expected, expected - received,
            received / expected if expected else None,
            interval, source, filled, total, filled / total if total else None, last))
    return out


def observed_interval(rows: list[tuple[int, int, datetime]]) -> int | None:
    """연속한 두 메시지의 시간차 ÷ fCnt 차의 중앙값(초). 건너뜀이 있어도 1건당 간격이 나온다."""
    per_msg = [(t2 - t1).total_seconds() / (f2 - f1)
               for (e1, f1, t1), (e2, f2, t2) in zip(rows, rows[1:])
               if e1 == e2 and f2 > f1]
    return round(statistics.median(per_msg)) if per_msg else None


# --- 수집기 꺼짐·끊김 ---------------------------------------------------------

@dataclass
class Downtime:
    start: str
    end: str
    kind: str        # "down" | "disconnected"
    evidence: str


def collector_downtime(conn: sqlite3.Connection, start: datetime, end: datetime,
                       now: datetime | None = None) -> list[Downtime]:
    """수집기 이벤트로 꺼져 있던 시간과 브로커 연결이 끊겨 있던 시간을 구한다.

    꺼짐: 이벤트 사이 공백 > 90초, stop 뒤 다음 기록까지, stop 기록 없이 start가 나온 경우(강제 종료) 그 직전 공백.
    """
    limit = min(end, _now(now))
    s, e = to_iso(start), to_iso(limit)
    marks = ",".join("?" * len(ALIVE_KINDS))
    prev = conn.execute(f"SELECT max(ts) FROM collector_events WHERE kind IN ({marks}) AND ts < ?",
                        (*ALIVE_KINDS, s)).fetchone()[0]
    prev_kind = conn.execute("SELECT kind FROM collector_events WHERE ts = ? ORDER BY id DESC LIMIT 1",
                             (prev,)).fetchone()[0] if prev else None
    rows = conn.execute(f"""SELECT ts, kind FROM collector_events
                            WHERE kind IN ({marks}) AND ts >= ? AND ts < ? ORDER BY ts, id""",
                        (*ALIVE_KINDS, s, e)).fetchall()

    def clip(a: datetime, b: datetime) -> tuple[datetime, datetime] | None:
        a, b = max(a, start), min(b, limit)
        return (a, b) if b > a else None

    out: list[Downtime] = []
    cur_t, cur_kind = (parse_iso(prev), prev_kind) if prev else (start, None)

    # 기간 시작 때 이미 끊겨 있었는지
    last_link = conn.execute(
        """SELECT kind, ts FROM collector_events WHERE kind IN ('connect', 'disconnect', 'start') AND ts < ?
           ORDER BY ts DESC, id DESC LIMIT 1""", (s,)).fetchone()
    disc_from = parse_iso(last_link[1]) if last_link and last_link[0] == "disconnect" else None

    for t, kind in [(parse_iso(ts), k) for ts, k in rows] + [(limit, None)]:
        gap = (t - cur_t).total_seconds()
        reason = None
        if cur_kind == "stop":
            reason = f"stop {_hms(cur_t)} → 다음 기록 {_hms(t)}"
        elif kind == "start" and cur_kind is not None:
            reason = f"stop 기록 없이 재시작(마지막 기록 {_hms(cur_t)}, start {_hms(t)})"
        elif gap > DOWN_GAP_S:
            reason = f"heartbeat 공백 {_hms(cur_t)}~{_hms(t)} ({gap:.0f}초)"
        if reason and (span := clip(cur_t, t)):
            out.append(Downtime(to_iso(span[0]), to_iso(span[1]), "down", reason))

        if kind == "disconnect" and disc_from is None:
            disc_from = t
        elif kind == "stop":
            disc_from = None  # 정상 종료 때의 disconnect는 브로커 문제가 아니다(꺼짐 구간이 덮는다)
        elif kind in ("connect", "start", None) and disc_from is not None:
            if span := clip(disc_from, t):
                end_txt = {"connect": "connect", "start": "재시작", None: "현재까지"}[kind]
                out.append(Downtime(to_iso(span[0]), to_iso(span[1]), "disconnected",
                                    f"disconnect {_hms(disc_from)} → {end_txt} {_hms(t)}"))
            disc_from = None
        cur_t, cur_kind = t, kind

    return sorted(out, key=lambda d: d.start)


# --- 결측 구간과 원인 ---------------------------------------------------------

@dataclass
class Gap:
    location: str
    item: str
    start: str
    end: str
    minutes: int
    cause: str | None      # CAUSES의 키. 짧은 구간(gap_list_min_minutes 미만)은 판정하지 않아 None
    evidence: str


def gaps(conn: sqlite3.Connection, cfg: Config, start: datetime, end: datetime,
         now: datetime | None = None, downtime: list[Downtime] | None = None) -> list[Gap]:
    """공간·항목별로 연속한 빈 1분 칸을 구간으로 묶고, 긴 구간만 원인을 판정한다.

    판정 순서: 수집기 꺼짐/연결 끊김 → 수집기 쪽 손실(queue_drop·write_error) → fCnt 건너뜀·재가입(센서·무선)
    → fCnt가 이어짐(그 사이 센서가 보내지 않음) → 앞뒤 데이터가 없어 원인 미상.
    """
    start = floor_minute(start)
    limit = floor_minute(min(end, _now(now)))
    if downtime is None:
        downtime = collector_downtime(conn, start, limit, now=now)
    pairs = [(c.location, c.item) for c in coverage_by_location_item(conn, cfg, start, limit)]

    filled: dict[tuple[str, str], set[str]] = {}
    for loc, item, minute in conn.execute(
            """SELECT location, item, substr(measured_at, 1, 16) FROM readings
               WHERE measured_at >= ? AND measured_at < ? GROUP BY 1, 2, 3""",
            (to_iso(start), to_iso(limit))):
        filled.setdefault((loc, item), set()).add(minute)

    total = int((limit - start).total_seconds() // 60)
    out = []
    for loc, item in pairs:
        have = filled.get((loc, item), set())
        run_start = None
        for i in range(total + 1):
            t = start + timedelta(minutes=i)
            empty = i < total and t.strftime("%Y-%m-%dT%H:%M") not in have
            if empty and run_start is None:
                run_start = t
            elif not empty and run_start is not None:
                minutes = int((t - run_start).total_seconds() // 60)
                cause, evidence = None, ""
                if minutes >= cfg.report.gap_list_min_minutes:
                    cause, evidence = _classify(conn, loc, item, run_start, t, downtime)
                out.append(Gap(loc, item, to_iso(run_start), to_iso(t), minutes, cause, evidence))
                run_start = None
    return out


def _classify(conn: sqlite3.Connection, loc: str, item: str, gs: datetime, ge: datetime,
              downtime: list[Downtime]) -> tuple[str, str]:
    a, b = to_iso(gs), to_iso(ge)
    for d in downtime:
        if d.start < b and d.end > a:
            return ("collector_down" if d.kind == "down" else "disconnected"), d.evidence

    marks = ",".join("?" * len(LOSS_KINDS))
    losses = conn.execute(
        f"SELECT kind, count(*) FROM collector_events WHERE kind IN ({marks}) AND ts >= ? AND ts < ? GROUP BY kind",
        (*LOSS_KINDS, a, to_iso(ge + timedelta(seconds=LOSS_SLACK_S)))).fetchall()
    if losses:
        return "collector_loss", ", ".join(f"{k} {n}건" for k, n in losses)

    devs = [r[0] for r in conn.execute(
        "SELECT DISTINCT device_id FROM readings WHERE location = ? AND item = ? AND measured_at < ?",
        (loc, item, b))]
    skips, continuous = [], []
    for dev in devs:
        before = conn.execute(
            """SELECT fcnt_epoch, f_cnt, measured_at FROM readings WHERE location = ? AND item = ? AND device_id = ?
               AND measured_at < ? ORDER BY measured_at DESC LIMIT 1""", (loc, item, dev, a)).fetchone()
        after = conn.execute(
            """SELECT fcnt_epoch, f_cnt, measured_at FROM readings WHERE location = ? AND item = ? AND device_id = ?
               AND measured_at >= ? ORDER BY measured_at LIMIT 1""", (loc, item, dev, b)).fetchone()
        if not before or not after:
            continue
        tail = dev[-4:]
        if before[0] != after[0] or after[1] - before[1] > 1:
            # 건너뛴 메시지가 빈 구간 밖(기간 앞 등)에서 수집기가 꺼진 동안 사라졌을 수 있다
            downs = collector_downtime(conn, parse_iso(before[2]), parse_iso(after[2]))
            if downs:
                return ("collector_down" if downs[0].kind == "down" else "disconnected",
                        f"{tail} fCnt {before[1]}→{after[1]} 사이 {downs[0].evidence}")
        if before[0] != after[0]:
            skips.append(f"{tail} 재가입(epoch {before[0]}→{after[0]})")
        elif after[1] - before[1] > 1:
            skips.append(f"{tail} fCnt {before[1]}→{after[1]} ({after[1] - before[1] - 1}건 건너뜀)")
        else:
            continuous.append(f"{tail} fCnt {before[1]}→{after[1]}")
    if skips:
        return "sensor_loss", "; ".join(skips)
    if continuous:
        return "no_transmission", "; ".join(continuous)
    return "unknown", "구간 앞뒤에 같은 센서의 데이터가 없음"


# --- 끊김 경고와 수집기 상태 --------------------------------------------------

@dataclass
class Stale:
    device_id: str
    model: str
    location: str
    last_received_at: str
    age_s: int
    limit_s: int


def stale_devices(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None,
                  observed: dict[str, int] | None = None) -> list[Stale]:
    """마지막 수신이 센서 간격의 2배(간격을 모르면 5분)보다 오래된 센서."""
    now = _now(now)
    observed = observed or {}
    out = []
    for dev, model, loc, last in conn.execute(
            "SELECT device_id, model, location, last_received_at FROM devices ORDER BY device_id"):
        if not last:
            continue
        interval = configured_interval(cfg, dev, model) or observed.get(dev)
        limit = 2 * interval if interval else STALE_DEFAULT_S
        age = int((now - parse_iso(last)).total_seconds())
        if age > limit:
            out.append(Stale(dev, model or "", loc or "", last, age, limit))
    return out


def last_event_ts(conn: sqlite3.Connection, kind: str) -> str | None:
    return conn.execute("SELECT max(ts) FROM collector_events WHERE kind = ?", (kind,)).fetchone()[0]
