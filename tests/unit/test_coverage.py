from datetime import datetime, timedelta, timezone

import pytest

from collector.coverage import (collector_downtime, coverage_by_location_item, device_rates, gaps,
                                stale_devices)
from collector.store import Store

T0 = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
LAB = "실습실"
ROOM = "회의실"
DEV_A = "aaaa000000000001"   # 설정에 없는 장치: 간격은 모델(AM107=60s)에서
DEV_B = "bbbb000000000002"
DEV_ROOM = "24e124725d089152"  # 설정에서 interval_s=600


def at(minute, sec=10):
    return T0 + timedelta(minutes=minute, seconds=sec)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def conn(db_path):
    s = Store(db_path)
    yield s.conn
    s.close()


def add(conn, dev, f_cnt, minute, sec=10, item="co2", loc=LAB, model="AM107", epoch=0, recv_delay=1):
    t = at(minute, sec)
    conn.execute(
        """INSERT INTO readings (device_id, fcnt_epoch, f_cnt, item, value, location, model,
                                 measured_at, received_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)""",
        (dev, epoch, f_cnt, item, loc, model, iso(t), iso(t + timedelta(seconds=recv_delay))))
    conn.execute(
        """INSERT INTO devices (device_id, model, location, fcnt_epoch, last_f_cnt, last_measured_at,
                                last_received_at) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(device_id) DO UPDATE SET last_received_at=excluded.last_received_at""",
        (dev, model, loc, epoch, f_cnt, iso(t), iso(t + timedelta(seconds=recv_delay))))


def event(conn, kind, minute, sec=0):
    conn.execute("INSERT INTO collector_events (ts, kind) VALUES (?, ?)", (iso(at(minute, sec)), kind))


def alive(conn, start_min, end_min):
    """start_min부터 end_min 전까지 1분마다 heartbeat."""
    for m in range(start_min, end_min):
        event(conn, "heartbeat", m, 30)


def pair(rows, loc, item):
    return next(r for r in rows if (r.location, r.item) == (loc, item))


# (a) 1분 칸 커버율 ---------------------------------------------------------

def test_minute_coverage_any_sensor_fills_slot(conn, cfg):
    for m in range(0, 5):
        add(conn, DEV_A, 100 + m, m)
    for m in range(4, 8):           # 4분 칸은 둘 다 있음(한 칸으로 셈)
        add(conn, DEV_B, 200 + m, m)
    add(conn, DEV_A, 99, -1)        # 기간 앞: 제외
    add(conn, DEV_A, 110, 10, sec=0)  # 끝 경계(=end): 제외
    rows = coverage_by_location_item(conn, cfg, T0, T0 + timedelta(minutes=10))
    co2 = pair(rows, LAB, "co2")
    assert (co2.filled, co2.total, co2.count) == (8, 10, 9)
    assert co2.coverage == pytest.approx(0.8)
    assert co2.intervals == [60]


def test_minute_coverage_lists_silent_pairs_from_devices(conn, cfg):
    add(conn, DEV_ROOM, 1, -30, model="AM103", loc=ROOM)  # 기간 밖에만 있음
    rows = coverage_by_location_item(conn, cfg, T0, T0 + timedelta(minutes=10))
    co2 = pair(rows, ROOM, "co2")
    assert (co2.filled, co2.count, co2.coverage) == (0, 0, 0.0)
    assert co2.intervals == [600]
    assert {r.item for r in rows if r.location == ROOM} == {"co2", "temperature", "humidity", "battery"}


# (b) 센서 수신률 ------------------------------------------------------------

def test_device_rate_across_two_epochs(conn, cfg):
    for i, fc in enumerate([100, 101, 103]):
        add(conn, DEV_A, fc, i)
        add(conn, DEV_A, fc, i, item="temperature")  # 같은 메시지의 다른 항목: 한 번만 셈
    add(conn, DEV_A, 1, 5, epoch=1)
    add(conn, DEV_A, 2, 6, epoch=1)
    (r,) = device_rates(conn, cfg, T0, T0 + timedelta(minutes=10))
    assert (r.received, r.expected, r.skipped) == (5, 6, 1)
    assert r.rate == pytest.approx(5 / 6)
    assert (r.interval_s, r.interval_source) == (60, "config")


# (c) 간격 기준 커버율 -------------------------------------------------------

def test_interval_coverage_uses_configured_600s(conn, cfg):
    for i, m in enumerate([0, 10, 20, 40, 50]):
        add(conn, DEV_ROOM, 10 + i, m, model="AM103", loc=ROOM)
    (r,) = device_rates(conn, cfg, T0, T0 + timedelta(minutes=60))
    assert (r.interval_s, r.interval_source) == (600, "config")
    assert (r.interval_filled, r.interval_total) == (5, 6)
    assert r.interval_coverage == pytest.approx(5 / 6)


def test_interval_observed_median_when_not_configured(conn, cfg):
    # fCnt 1건당 120초. 건너뜀(12→14)이 있어도 건당 간격으로 나눠 120초
    for fc, m in [(10, 0), (11, 2), (12, 4), (14, 8)]:
        add(conn, DEV_A, fc, m, model="UNKNOWN")
    (r,) = device_rates(conn, cfg, T0, T0 + timedelta(minutes=10))
    assert (r.interval_s, r.interval_source) == (120, "observed")


# (d) 결측 구간 묶기 ---------------------------------------------------------

def test_gaps_grouped_and_short_ones_unclassified(conn, cfg):
    alive(conn, -1, 21)
    for m in [0, 1, 2, 4] + list(range(10, 20)):
        add(conn, DEV_A, 100 + m, m)
    found = gaps(conn, cfg, T0, T0 + timedelta(minutes=20))
    lab = [g for g in found if (g.location, g.item) == (LAB, "co2")]
    assert [(g.start, g.minutes) for g in lab] == [(iso(at(3, 0)), 1), (iso(at(5, 0)), 5)]
    assert lab[0].cause is None                 # 3분 미만: 원인 판정 안 함(건수만)
    assert lab[1].end == iso(at(10, 0))
    assert lab[1].cause == "sensor_loss"        # 4분 칸 f_cnt=104 → 10분 칸 110


# (e) 원인 분류 --------------------------------------------------------------

def make_gap(conn, before_fc, after_fc, after_epoch=0):
    """0~2분과 8~9분에 데이터, 3~7분(5분) 빈 구간."""
    for m in range(0, 3):
        add(conn, DEV_A, before_fc - 2 + m, m)
    add(conn, DEV_A, after_fc, 8, epoch=after_epoch)
    add(conn, DEV_A, after_fc + 1, 9, epoch=after_epoch)


def lab_gap(conn, cfg, minutes=10, **kw):
    found = [g for g in gaps(conn, cfg, T0, T0 + timedelta(minutes=minutes), **kw)
             if (g.location, g.item) == (LAB, "co2") and g.cause is not None]
    assert len(found) == 1
    return found[0]


def test_cause_collector_down_from_heartbeat_gap(conn, cfg):
    make_gap(conn, 102, 108)
    alive(conn, -1, 2)          # 마지막 heartbeat 01:30
    alive(conn, 8, 11)          # 다음 08:30 → 420초 공백
    g = lab_gap(conn, cfg)
    assert g.cause == "collector_down"
    assert "heartbeat" in g.evidence


def test_cause_disconnected(conn, cfg):
    make_gap(conn, 102, 108)
    alive(conn, -1, 11)
    event(conn, "disconnect", 3, 5)
    event(conn, "connect", 7, 50)
    g = lab_gap(conn, cfg)
    assert g.cause == "disconnected"


def test_cause_collector_loss_from_queue_drop(conn, cfg):
    make_gap(conn, 102, 108)
    alive(conn, -1, 11)
    event(conn, "queue_drop", 5)
    assert lab_gap(conn, cfg).cause == "collector_loss"


def test_cause_sensor_loss_from_fcnt_skip(conn, cfg):
    make_gap(conn, 102, 108)
    alive(conn, -1, 11)
    g = lab_gap(conn, cfg)
    assert g.cause == "sensor_loss"
    assert "102→108" in g.evidence and "5건" in g.evidence


def test_cause_sensor_loss_from_rejoin(conn, cfg):
    make_gap(conn, 102, 1, after_epoch=1)
    alive(conn, -1, 11)
    g = lab_gap(conn, cfg)
    assert g.cause == "sensor_loss"
    assert "재가입" in g.evidence


def test_cause_no_transmission_when_fcnt_continuous(conn, cfg):
    make_gap(conn, 102, 103)
    alive(conn, -1, 11)
    assert lab_gap(conn, cfg).cause == "no_transmission"


def test_cause_unknown_without_data_before(conn, cfg):
    alive(conn, -1, 11)
    for m in range(5, 10):
        add(conn, DEV_A, 100 + m, m)
    assert lab_gap(conn, cfg).cause == "unknown"


def test_gaps_stop_at_now(conn, cfg):
    alive(conn, -1, 11)
    for m in range(0, 5):
        add(conn, DEV_A, 100 + m, m)
    found = gaps(conn, cfg, T0, T0 + timedelta(minutes=60), now=at(5, 0))
    assert not [g for g in found if (g.location, g.item) == (LAB, "co2")]


# 수집기 꺼짐 계산 -----------------------------------------------------------

def test_downtime_restart_without_stop_and_stop_start(conn, cfg):
    alive(conn, 0, 3)                 # 마지막 02:30
    event(conn, "start", 2, 50)       # 20초 만에 start: stop 없이 재시작 → 꺼짐
    alive(conn, 3, 5)                 # 04:30
    event(conn, "disconnect", 4, 39)  # 정상 종료 과정의 disconnect: 연결 끊김으로 세지 않음
    event(conn, "stop", 4, 40)
    event(conn, "start", 4, 55)       # stop → start: 꺼짐
    alive(conn, 5, 10)
    downs = collector_downtime(conn, T0, T0 + timedelta(minutes=10), now=at(9, 40))
    assert all(d.kind == "down" for d in downs)
    assert [(d.start, d.end) for d in downs] == [
        (iso(at(2, 30)), iso(at(2, 50))), (iso(at(4, 40)), iso(at(4, 55)))]


def test_downtime_tail_until_now(conn, cfg):
    alive(conn, 0, 3)
    downs = collector_downtime(conn, T0, T0 + timedelta(minutes=10), now=at(8, 0))
    assert [(d.kind, d.start, d.end) for d in downs] == [("down", iso(at(2, 30)), iso(at(8, 0)))]


# (f) 끊김 경고 --------------------------------------------------------------

def test_stale_warning_uses_twice_interval_or_5min(conn, cfg):
    now = at(60, 0)
    add(conn, DEV_ROOM, 1, 35, sec=0, model="AM103", loc=ROOM, recv_delay=0)   # 25분 전, 600s → 경고
    add(conn, "24e124128c140101", 1, 45, sec=0, model="AM107", recv_delay=0)  # 15분 전, 600s → 없음
    add(conn, DEV_A, 1, 54, sec=0, model="UNKNOWN", recv_delay=0)             # 6분 전, 모름 → 경고
    add(conn, DEV_B, 1, 56, sec=0, model="UNKNOWN", recv_delay=0)             # 4분 전, 모름 → 없음
    warned = {w.device_id: w for w in stale_devices(conn, cfg, now)}
    assert set(warned) == {DEV_ROOM, DEV_A}
    assert (warned[DEV_ROOM].age_s, warned[DEV_ROOM].limit_s) == (1500, 1200)
    assert warned[DEV_A].limit_s == 300


def test_cause_collector_down_when_skipped_messages_fall_in_earlier_downtime(conn, cfg):
    # 수집기가 기간 앞(-20~-5분)에 꺼져 있었고, 그동안 보낸 메시지가 fCnt 건너뜀으로 남는다
    alive(conn, -30, -20)
    event(conn, "stop", -20, 40)
    event(conn, "start", -5)
    alive(conn, -5, 11)
    add(conn, DEV_A, 100, -25)
    for m in range(5, 10):         # 기간 첫 데이터는 5분. 0~4분 빈 구간, 그 앞 행은 -25분
        add(conn, DEV_A, 115 + m, m)
    g = lab_gap(conn, cfg)
    assert g.cause == "collector_down"
    assert "100→120" in g.evidence and "stop" in g.evidence
