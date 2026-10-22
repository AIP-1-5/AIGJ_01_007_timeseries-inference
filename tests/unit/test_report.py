from datetime import timedelta

from collector.report import format_report, report_data
from tests.unit.test_coverage import DEV_A, DEV_ROOM, LAB, ROOM, T0, add, alive, at, conn  # noqa: F401

META = {"host": "test", "power_mode": "MAXN_SUPER", "service": "`svc`", "commit": "abc1234",
        "config_path": "config/collector.toml", "config_sha": "0" * 12, "data": "합성"}


def build(conn):
    alive(conn, -1, 61)
    for m in range(0, 60):
        if m not in range(20, 25):                 # 실습실 co2 5분 빈 구간 → 55/60
            add(conn, DEV_A, 100 + m, m)
            add(conn, DEV_A, 100 + m, m, item="temperature")
            add(conn, DEV_A, 100 + m, m, item="humidity")
    for i, m in enumerate(range(0, 60, 10)):
        for item in ("co2", "temperature", "humidity"):
            add(conn, DEV_ROOM, 10 + i, m, item=item, loc=ROOM, model="AM103")


def test_report_verdicts_gaps_and_interval_judgment(conn, cfg):
    build(conn)
    d = report_data(conn, cfg, T0, T0 + timedelta(hours=1), now=at(90))
    text = format_report(d, cfg, META)
    assert f"| {LAB} | co2 | 55 | 55/60 | 91.7% | 60s | **미통과** |" in text
    assert f"| {ROOM} | co2 | 6 | 6/60 | 10.0% | 600s | 통과(간격 기준) |" in text
    assert text.count("간격 600s 센서만 있어") == 1   # 공간 단위로 한 줄
    assert "9152 간격 기준 커버율 100.0%, 수신률 100.0%" in text
    assert "판정 대상 6개: 통과 3, 미통과 3." in text
    # 실습실 3항목의 같은 빈 구간은 한 줄로 묶인다. fCnt 119→125 → 센서·무선 손실
    assert f"| {LAB} | co2, humidity, temperature | 10-21 09:20 | 10-21 09:25 | 5 | 센서·무선 쪽 손실 |" in text
    assert "## 6. 해석" in text and "`abc1234`" in text and "(1.0시간)" in text


def test_report_does_not_count_future_minutes(conn, cfg):
    build(conn)
    d = report_data(conn, cfg, T0, T0 + timedelta(hours=24), now=at(20, 30))
    assert d["end"] == "2026-10-21T00:20:00.000000Z"
    text = format_report(d, cfg, META)
    assert f"| {LAB} | co2 | 20 | 20/20 | 100.0% | 60s | 통과 |" in text
    assert "요청한 끝 2026-10-22T00:00Z보다 짧음" in text


def test_long_interval_space_fails_when_interval_coverage_low(conn, cfg):
    alive(conn, -1, 61)
    for i, m in enumerate([0, 10]):                 # 1시간 중 2칸만 → 간격 기준 33%
        add(conn, DEV_ROOM, 10 + i, m, loc=ROOM, model="AM103")
    text = format_report(report_data(conn, cfg, T0, T0 + timedelta(hours=1), now=at(90)), cfg, META)
    assert f"| {ROOM} | co2 | 2 | 2/60 | 3.3% | 600s | **미통과(간격 기준)** |" in text
