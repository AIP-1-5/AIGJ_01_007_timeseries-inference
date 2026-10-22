import sqlite3
from dataclasses import replace

import pytest

from collector.parser import parse_uplink
from collector.store import SchemaError, Store, open_readonly

RECV = "2026-10-21T05:10:00.000000Z"


@pytest.fixture
def store(db_path):
    s = Store(db_path)
    yield s
    s.close()


@pytest.fixture
def msg(sample_lines, cfg):
    t, p = sample_lines[0]
    return parse_uplink(t, p, RECV, cfg)


def count(store, table="readings"):
    return store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_pragmas_and_schema(store):
    c = store.conn
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"readings", "devices", "collector_events", "rejected", "schema_meta"} <= tables
    assert c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "1"
    idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_readings_loc_item_time", "idx_readings_time", "idx_events_kind_ts"} <= idx


def test_schema_version_mismatch(db_path):
    Store(db_path).close()
    c = sqlite3.connect(db_path)
    c.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
    c.commit()
    c.close()
    with pytest.raises(SchemaError):
        Store(db_path)


def test_save_one_message(store, msg, cfg):
    r = store.save_uplink(msg, cfg.fcnt)
    assert (r.epoch, r.inserted, r.duplicate, r.bumped) == (0, 4, False, False)
    assert count(store) == 4
    row = store.conn.execute(
        "SELECT device_id, fcnt_epoch, f_cnt, location, point, spot, model, measured_at, gw_time, "
        "received_at, gateway_ids FROM readings WHERE item='co2'").fetchone()
    assert row == ("24e124126d152862", 0, 104884, "실습실", "후방 오른쪽", "후방 우측", "EM500-CO2",
                   "2026-10-21T05:06:15.008186Z", "2026-10-21T05:06:14.992000Z", RECV, "24e124fffef79304")
    dev = store.conn.execute("SELECT fcnt_epoch, last_f_cnt, last_received_at FROM devices").fetchone()
    assert dev == (0, 104884, RECV)


def test_resave_is_ignored(store, msg, cfg):
    store.save_uplink(msg, cfg.fcnt)
    later = replace(msg, measured_at="2026-10-21T05:06:15.999999Z", received_at="2026-10-21T06:00:00.000000Z")
    r = store.save_uplink(later, cfg.fcnt)
    assert r.duplicate and r.inserted == 0
    assert count(store) == 4
    assert {r[0] for r in store.conn.execute("SELECT measured_at FROM readings")} == {msg.measured_at}


def test_failure_rolls_back_whole_message(store, msg, cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_write_device", boom)
    with pytest.raises(RuntimeError):
        store.save_uplink(msg, cfg.fcnt)
    assert count(store) == 0
    assert count(store, "devices") == 0
    assert not store.conn.in_transaction
    monkeypatch.undo()
    assert store.save_uplink(msg, cfg.fcnt).inserted == 4  # 다음 메시지는 정상 저장


def test_epoch_bump_and_event(store, msg, cfg):
    store.save_uplink(msg, cfg.fcnt)
    reset = replace(msg, f_cnt=2, measured_at="2026-10-21T07:00:00.000000Z")
    r = store.save_uplink(reset, cfg.fcnt)
    assert (r.epoch, r.bumped, r.inserted) == (1, True, 4)
    kinds = [k for (k,) in store.conn.execute("SELECT kind FROM collector_events")]
    assert kinds == ["epoch_bump"]


def test_join_bumps_epoch(store, msg, cfg):
    store.save_uplink(msg, cfg.fcnt)
    assert store.save_join(msg.device_id, RECV) == 1
    r = store.save_uplink(replace(msg, f_cnt=0, measured_at="2026-10-21T07:00:00.000000Z"), cfg.fcnt)
    assert (r.epoch, r.bumped) == (1, False)
    kinds = [k for (k,) in store.conn.execute("SELECT kind FROM collector_events ORDER BY id")]
    assert kinds == ["join"]  # join이 이미 올렸으므로 fCnt=0 업링크로 한 번 더 올리지 않는다


def test_epoch_survives_restart(db_path, msg, cfg):
    s = Store(db_path)
    s.save_uplink(msg, cfg.fcnt)
    s.save_uplink(replace(msg, f_cnt=2, measured_at="2026-10-21T07:00:00.000000Z"), cfg.fcnt)
    s.close()
    s = Store(db_path)
    r = s.save_uplink(replace(msg, f_cnt=3, measured_at="2026-10-21T07:01:00.000000Z"), cfg.fcnt)
    assert r.epoch == 1  # FR-011
    s.close()


def test_events_and_rejected(store):
    store.log_event("start", detail={"pid": 1}, ts=RECV)
    store.save_rejected(RECV, "t/event/up", "not_json", b"x" * 5000)
    assert store.conn.execute("SELECT kind, detail FROM collector_events").fetchone() == ("start", '{"pid": 1}')
    reason, n = store.conn.execute("SELECT reason, length(payload) FROM rejected").fetchone()
    assert (reason, n) == ("not_json", 4096)


def test_readonly_reader_does_not_block_writer(store, msg, cfg, db_path):
    store.save_uplink(msg, cfg.fcnt)
    ro = open_readonly(db_path)
    ro.execute("BEGIN")
    assert ro.execute("SELECT count(*) FROM readings").fetchone()[0] == 4  # 읽기 트랜잭션 열린 채로
    r = store.save_uplink(replace(msg, f_cnt=msg.f_cnt + 1, measured_at="2026-10-21T05:07:15.000000Z"), cfg.fcnt)
    assert r.inserted == 4  # FR-021: 쓰기 성공
    ro.rollback()
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO schema_meta VALUES ('x', 'y')")
    ro.close()
