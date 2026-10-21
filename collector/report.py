"""현황 조회(status)와 수집 현황 보고(report). 계약: contracts/cli.md. 계산은 coverage.py가 한다."""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from . import coverage as cv
from .config import Config


def _short(ts: str | None) -> str:
    """`2026-10-21T05:59:48.123456Z` → `2026-10-21T05:59:48Z`."""
    return ts[:19] + "Z" if ts else "-"


def _ago(ts: str | None, now: datetime) -> str:
    if not ts:
        return "기록 없음"
    s = int((now - cv.parse_iso(ts)).total_seconds())
    if s < 120:
        return f"{s}초 전"
    if s < 7200:
        return f"{s // 60}분 전"
    return f"{s // 3600}시간 전"


def _pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "-"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _table(header: list[str], rows: list[list[str]], right: set[int]) -> list[str]:
    """한글 폭(2칸)을 맞춘 고정폭 표. right: 오른쪽 정렬할 열 번호."""
    widths = [max(_width(r[i]) for r in [header] + rows) for i in range(len(header))]
    lines = []
    for r in [header] + rows:
        cells = []
        for i, cell in enumerate(r):
            pad = " " * (widths[i] - _width(cell))
            cells.append(pad + cell if i in right else cell + pad)
        lines.append("  ".join(cells).rstrip())
    return lines


def status_data(conn: sqlite3.Connection, cfg: Config, hours: float, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    end = cv.floor_minute(now)
    start = end - timedelta(hours=hours)
    rates = cv.device_rates(conn, cfg, start, end)
    observed = {r.device_id: r.interval_s for r in rates if r.interval_source == "observed"}
    downtime = cv.collector_downtime(conn, start, end, now=now)
    return {
        "start": cv.to_iso(start), "end": cv.to_iso(end), "hours": hours, "now": cv.to_iso(now),
        "collector": {k: cv.last_event_ts(conn, k) for k in ("heartbeat", "start", "connect", "disconnect")},
        "downtime": [asdict(d) for d in downtime],
        "locations": [asdict(c) for c in cv.coverage_by_location_item(conn, cfg, start, end)],
        "devices": [asdict(r) for r in rates],
        "stale": [asdict(s) for s in cv.stale_devices(conn, cfg, now, observed)],
    }


def format_status(d: dict) -> str:
    now = cv.parse_iso(d["now"])
    col = d["collector"]
    lines = [f"기간: {d['start'][:16]}Z ~ {d['end'][:16]}Z ({d['hours']:g}h, measured_at 기준)"]
    link = f"마지막 연결 {_short(col['connect'])}"
    if col["disconnect"] and (not col["connect"] or col["disconnect"] > col["connect"]):
        link += f", 끊김 {_short(col['disconnect'])} 이후 재연결 없음"
    lines.append(f"수집기: 마지막 heartbeat {_short(col['heartbeat'])} ({_ago(col['heartbeat'], now)}), {link}")
    downs = [x for x in d["downtime"] if x["kind"] == "down"]
    discs = [x for x in d["downtime"] if x["kind"] == "disconnected"]
    if downs or discs:
        secs = sum((cv.parse_iso(x["end"]) - cv.parse_iso(x["start"])).total_seconds() for x in downs)
        lines.append(f"기간 안 수집기 꺼짐 {len(downs)}회(합계 {secs / 60:.1f}분), 연결 끊김 {len(discs)}회")
    lines.append("")

    rows = []
    for c in d["locations"]:
        note = ""
        if c["intervals"] and min(c["intervals"]) > 60:
            note = f"(간격 {min(c['intervals'])}s 이상 센서만 있음)"
        rows.append([c["location"], c["item"], str(c["count"]), _short(c["last_received_at"]),
                     _pct(c["coverage"]), note])
    lines += _table(["공간", "항목", "건수", "마지막 수신", "1분 커버율", ""], rows, {2, 4})
    lines.append("")

    rows = []
    for r in d["devices"]:
        iv = f"{r['interval_s']}s" if r["interval_s"] else "-"
        if r["interval_source"] == "observed":
            iv += "(관측)"
        rows.append([r["device_id"], r["model"], r["location"], f"{r['received']}/{r['expected']}",
                     str(r["skipped"]), _pct(r["rate"]), iv, _pct(r["interval_coverage"]),
                     _short(r["last_received_at"])])
    lines += _table(["센서", "모델", "공간", "받음/기대", "건너뜀", "수신률", "간격", "간격커버율", "마지막 수신"],
                    rows, {3, 4, 5, 6, 7})

    if d["stale"]:
        lines.append("")
        for s in d["stale"]:
            lines.append(f"경고: {s['device_id']} ({s['model']}, {s['location']}) 마지막 수신 "
                         f"{s['age_s'] // 60}분 {s['age_s'] % 60}초 전 (기준 {s['limit_s']}s 초과)")
    return "\n".join(lines)


# --- 수집 현황 보고(D-1) -------------------------------------------------------

KST = timezone(timedelta(hours=9))
EVENT_KINDS = ("start", "stop", "connect", "disconnect", "connect_refused", "queue_drop", "write_error",
               "parse_error", "unmapped_key", "join", "epoch_bump")
INTERVAL_NOTE = ("간격 {iv}s 센서만 있어 1분 칸 커버율은 구조적으로 낮다. 센서 전송 간격 기준 커버율로 판정한다"
                 "(spec Assumptions, decisions Q2b)")


def _kst(ts: str) -> str:
    return cv.parse_iso(ts).astimezone(KST).strftime("%m-%d %H:%M")


def _judge(c: dict, cfg: Config, rates: dict) -> tuple[str, float]:
    """판정 대상 항목의 AC-3 판정과 판정에 쓴 커버율.

    긴 간격 센서(60초 초과)만 있는 공간·항목은 센서 간격 기준 커버율로 판정한다. 1분 칸 판정과 같은 뜻이
    되도록 "그 공간 센서 중 하나라도" 채우면 되는 것으로 보고, 센서별 간격 커버율 중 가장 큰 값을 쓴다.
    """
    target = cfg.report.coverage_target
    if c["intervals"] and min(c["intervals"]) > 60:
        covs = [rates[d]["interval_coverage"] for d in c["devices"]
                if d in rates and rates[d]["interval_coverage"] is not None]
        cov = max(covs) if covs else 0.0
        return ("통과(간격 기준)" if cov >= target else "미통과(간격 기준)"), cov
    return ("통과" if c["coverage"] >= target else "미통과"), c["coverage"]


def report_data(conn: sqlite3.Connection, cfg: Config, start: datetime, end: datetime,
                now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start, requested_end = cv.floor_minute(start), cv.floor_minute(end)
    end = min(requested_end, cv.floor_minute(now))  # 아직 오지 않은 시간은 빈 칸으로 세지 않는다
    downtime = cv.collector_downtime(conn, start, end, now=now)
    s, e = cv.to_iso(start), cv.to_iso(end)
    events = dict(conn.execute(
        f"""SELECT kind, count(*) FROM collector_events WHERE ts >= ? AND ts < ?
            AND kind IN ({','.join('?' * len(EVENT_KINDS))}) GROUP BY kind""", (s, e, *EVENT_KINDS)))
    rejected = conn.execute("SELECT count(*) FROM rejected WHERE received_at >= ? AND received_at < ?",
                            (s, e)).fetchone()[0]
    starts = conn.execute(
        "SELECT detail FROM collector_events WHERE kind = 'start' AND ts < ? ORDER BY ts DESC LIMIT 1",
        (e,)).fetchone()
    return {
        "start": s, "end": e, "requested_end": cv.to_iso(requested_end), "now": cv.to_iso(now),
        "locations": [asdict(c) for c in cv.coverage_by_location_item(conn, cfg, start, end)],
        "devices": [asdict(r) for r in cv.device_rates(conn, cfg, start, end)],
        "gaps": [asdict(g) for g in cv.gaps(conn, cfg, start, end, now=now, downtime=downtime)],
        "downtime": [asdict(d) for d in downtime],
        "events": {k: events.get(k, 0) for k in EVENT_KINDS},
        "rejected": rejected,
        "last_start": starts[0] if starts else None,
    }


def format_report(d: dict, cfg: Config, meta: dict) -> str:
    """meta: 측정 조건(host, power_mode, service, commit, config_path, config_sha, db, data)."""
    judged = cfg.report.judged_items
    target = cfg.report.coverage_target
    start, end = cv.parse_iso(d["start"]), cv.parse_iso(d["end"])
    hours = (end - start).total_seconds() / 3600
    rates = {r["device_id"]: r for r in d["devices"]}
    out = [f"# 수집 현황 보고 (D-1): {d['start'][:16]}Z ~ {d['end'][:16]}Z", ""]

    out += ["## 1. 측정 조건", "", "| 항목 | 값 |", "|---|---|",
            f"| 기간 | {d['start'][:16]}Z ~ {d['end'][:16]}Z ({hours:.1f}시간), KST {_kst(d['start'])} ~ {_kst(d['end'])}"
            + (f". **요청한 끝 {d['requested_end'][:16]}Z보다 짧음(만든 시각까지만 집계)**"
               if d["requested_end"] != d["end"] else "") + " |",
            f"| 데이터 | {meta['data']} |",
            f"| 수집 보드 | {meta['host']} (전원 모드 {meta['power_mode']}) |",
            f"| 수집 서비스 | {meta['service']} |",
            "| 시각 기준 | `measured_at = min(rxInfo[].nsTime)` (research R1) |",
            f"| 코드 커밋 | `{meta['commit']}` |",
            f"| 설정 | `{meta['config_path']}` (sha256 `{meta['config_sha']}`) |",
            f"| 만든 시각 | {d['now'][:19]}Z |", ""]

    # 2. 공간·항목별 커버율
    out += ["## 2. 공간·항목별 1분 칸 커버율 (AC-3)", "",
            f"1분 칸마다 그 공간의 센서 하나라도 값이 있으면 채워진 칸이다. 판정 항목은 {', '.join(judged)}, "
            f"목표 {target:.0%}. 판정 항목이 없는 공간(예: 사무실 밖의 co2)은 해당 없음.", "",
            "| 공간 | 항목 | 건수 | 채워진 칸/전체 | 1분 커버율 | 센서 간격 | 판정 |", "|---|---|---|---|---|---|---|"]
    tally = {"통과": 0, "미통과": 0}
    by_interval: dict[str, list[str]] = {}
    for c in (c for c in d["locations"] if c["item"] in judged):
        verdict, _ = _judge(c, cfg, rates)
        tally["통과" if verdict.startswith("통과") else "미통과"] += 1
        iv = ", ".join(f"{i}s" for i in c["intervals"]) or "-"
        mark = f"**{verdict}**" if verdict.startswith("미통과") else verdict
        out.append(f"| {c['location']} | {c['item']} | {c['count']} | {c['filled']}/{c['total']} | "
                   f"{_pct(c['coverage'])} | {iv} | {mark} |")
        if verdict.endswith("(간격 기준)"):
            by_interval.setdefault(c["location"], []).append(c["item"])
    out += ["", f"판정 대상 {sum(tally.values())}개: 통과 {tally['통과']}, 미통과 {tally['미통과']}."]
    for loc, items in by_interval.items():
        c = next(x for x in d["locations"] if x["location"] == loc and x["item"] == items[0])
        parts = [f"{dev[-4:]} 간격 기준 커버율 {_pct(rates[dev]['interval_coverage'])}, 수신률 {_pct(rates[dev]['rate'])}"
                 for dev in c["devices"] if dev in rates]
        out += ["", f"- {loc} {', '.join(items)}: {INTERVAL_NOTE.format(iv=min(c['intervals']))}. {'; '.join(parts)}"]
    others = [c for c in d["locations"] if c["item"] not in judged]
    if others:
        out += ["", "판정 외 항목(참고): " + ", ".join(
            f"{c['location']} {c['item']} {_pct(c['coverage'])}" for c in others)]
    out.append("")

    # 3. 센서별 수신률
    out += ["## 3. 센서별 수신률과 간격 기준 커버율", "",
            "수신률 = 받은 메시지 ÷ fCnt 범위로 본 기대 메시지(epoch별 max − min + 1의 합). "
            "간격 기준 커버율 = 센서 간격 칸 중 값이 있는 칸의 비율(간격 뒤 '(관측)'은 설정이 없어 관측 중앙값을 쓴 것).", "",
            "| 센서 | 모델 | 공간 | 받음/기대 | 건너뜀 | 수신률 | 간격 | 간격 커버율 | 마지막 수신 |",
            "|---|---|---|---|---|---|---|---|---|"]
    for r in d["devices"]:
        iv = f"{r['interval_s']}s" if r["interval_s"] else "-"
        if r["interval_source"] == "observed":
            iv += "(관측)"
        out.append(f"| `{r['device_id']}` | {r['model']} | {r['location']} | {r['received']}/{r['expected']} | "
                   f"{r['skipped']} | {_pct(r['rate'])} | {iv} | {_pct(r['interval_coverage'])} | "
                   f"{_short(r['last_received_at'])} |")
    out.append("")

    # 4. 결측 구간: 같은 공간·시각·원인은 항목을 묶어 한 줄로
    classified = [g for g in d["gaps"] if g["item"] in judged and g["cause"]]
    # 전송 간격(fCnt가 이어짐)은 손실이 아니다. 10분 센서는 10분마다 생기므로 표에서 빼고 건수만 적는다
    listed = [g for g in classified if g["cause"] != "no_transmission"]
    no_tx = [g for g in classified if g["cause"] == "no_transmission"]
    merged: dict[tuple, list[str]] = {}
    for g in listed:
        merged.setdefault((g["location"], g["start"], g["end"], g["minutes"], g["cause"], g["evidence"]),
                          []).append(g["item"])
    out += [f"## 4. 결측 구간 ({cfg.report.gap_list_min_minutes}분 이상, 판정 항목)", ""]
    if merged:
        out += ["| 공간 | 항목 | 시작 (KST) | 끝 (KST) | 길이(분) | 추정 원인 | 근거 |", "|---|---|---|---|---|---|---|"]
        for (loc, gs, ge, minutes, cause, ev), items in sorted(merged.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            out.append(f"| {loc} | {', '.join(sorted(items))} | {_kst(gs)} | {_kst(ge)} | {minutes} | "
                       f"{cv.CAUSES[cause]} | {ev} |")
        by_cause: dict[str, list[int]] = {}
        for g in listed:
            by_cause.setdefault(g["cause"], []).append(g["minutes"])
        out += ["", "원인별(공간·항목 단위): " + ", ".join(
            f"{cv.CAUSES[k]} {len(v)}건·{sum(v)}분" for k, v in sorted(by_cause.items()))]
    else:
        out.append("없음.")
    if no_tx:
        per_loc: dict[str, int] = {}
        for g in no_tx:
            per_loc[g["location"]] = per_loc.get(g["location"], 0) + 1
        out += ["", "전송 간격(센서가 그 사이 보내지 않음, fCnt 이어짐 — 손실 아님)이라 표에서 뺀 구간(공간·항목 단위): "
                + ", ".join(f"{loc} {n}건" for loc, n in sorted(per_loc.items()))]
    short: dict[tuple[str, str], int] = {}
    for g in d["gaps"]:
        if g["item"] in judged and not g["cause"]:
            short[(g["location"], g["item"])] = short.get((g["location"], g["item"]), 0) + 1
    out += ["", f"{cfg.report.gap_list_min_minutes}분 미만 빈 구간(건수만): " + (", ".join(
        f"{loc} {item} {n}건" for (loc, item), n in sorted(short.items())) or "없음"), ""]

    # 5. 수집기 이벤트
    ev = d["events"]
    out += ["## 5. 수집기 이벤트 요약", "",
            "| start | stop | connect | disconnect | 연결 거부 | 큐 버림 | 쓰기 오류 | 파싱 오류 | 매핑 안 된 키 | join | epoch 증가 | 버린 메시지 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
            "| " + " | ".join(str(ev[k]) for k in EVENT_KINDS) + f" | {d['rejected']} |", ""]
    if d["downtime"]:
        out += ["꺼짐·끊김 구간:", ""]
        for x in d["downtime"]:
            secs = (cv.parse_iso(x["end"]) - cv.parse_iso(x["start"])).total_seconds()
            kind = "수집기 꺼짐" if x["kind"] == "down" else "연결 끊김"
            out.append(f"- {kind} {_kst(x['start'])} ~ {_kst(x['end'])} ({secs:.0f}초): {x['evidence']}")
    else:
        out.append("꺼짐·끊김 구간 없음.")
    out.append("")

    # 6. 해석: 사람이 쓴다(원칙 V)
    out += ["## 6. 해석", "",
            "> 자동으로 채우지 않는다. 숫자를 보고 사람이 쓴다.", "",
            "- **95% 미만 공간·항목과 이유**: ",
            "- **결측 구간별 원인 확인**: ",
            "- **긴 간격 센서(간격 기준 판정) 판단**: ",
            "- **그래서 무엇을 해야 하나**: ",
            "- **생각해 볼 점 1: 측정 시각을 무엇으로 정했나** (research R1): ",
            "- **생각해 볼 점 2: 수집과 학습이 같은 저장소를 쓰면** (research R5): ", ""]
    return "\n".join(out)
