import json
import time
from dataclasses import replace

from collector.config import StorageCfg
from collector.store import Store
from collector.writer import Writer

UP = "application/1/device/{dev}/event/up"


def make_payloads(sample_lines, n):
    """fixture 첫 메시지를 fCnt만 바꿔 n건 만든다."""
    base = json.loads(sample_lines[0][1])
    out = []
    for i in range(n):
        m = dict(base, fCnt=1_000_000 + i)
        out.append((UP.format(dev="x"), json.dumps(m).encode()))
    return out


def kinds(store):
    return [k for (k,) in store.conn.execute("SELECT kind FROM collector_events ORDER BY id")]


def test_writer_saves_all(sample_lines, cfg, db_path):
    store = Store(db_path)
    w = Writer(store, cfg)
    w.start()
    for t, p in make_payloads(sample_lines, 1000):
        assert w.submit_message(t, p, "2026-10-21T05:10:00.000000Z")
    w.stop()
    assert store.conn.execute("SELECT count(DISTINCT f_cnt) FROM readings").fetchone()[0] == 1000
    assert w.stats.saved == 1000 and w.stats.processed == 1000
    assert kinds(store)[-1] == "stop"
    store.close()


def test_full_queue_drops_without_blocking(sample_lines, cfg, db_path):
    store = Store(db_path)
    small = replace(cfg, storage=StorageCfg(db_path=db_path, queue_max=5, heartbeat_s=60))
    w = Writer(store, small)  # 시작하지 않음 → 큐가 차기만 한다
    msgs = make_payloads(sample_lines, 20)
    t0 = time.monotonic()
    results = [w.submit_message(t, p, "2026-10-21T05:10:00.000000Z") for t, p in msgs]
    assert time.monotonic() - t0 < 0.5  # 수신 쪽이 막히지 않는다(FR-003)
    assert results.count(True) == 5 and results.count(False) == 15
    assert w.stats.queue_drop == 15
    w.start()
    w.stop()
    assert "queue_drop" in kinds(store)
    hb = store.conn.execute("SELECT detail FROM collector_events WHERE kind='stop'").fetchone()[0]
    assert json.loads(hb)["queue_drop"] == 15
    store.close()


def test_heartbeat_interval(cfg, db_path):
    store = Store(db_path)
    fast = replace(cfg, storage=StorageCfg(db_path=db_path, queue_max=100, heartbeat_s=0.3))
    w = Writer(store, fast)
    w.start()
    time.sleep(1.1)
    w.stop()
    n = kinds(store).count("heartbeat")
    assert 2 <= n <= 5
    detail = json.loads(store.conn.execute(
        "SELECT detail FROM collector_events WHERE kind='heartbeat' LIMIT 1").fetchone()[0])
    assert {"processed", "saved", "duplicate", "rejected", "unmapped", "queue_drop", "queue_size"} <= set(detail)
    store.close()


def test_stop_drains_queue(sample_lines, cfg, db_path):
    store = Store(db_path)
    w = Writer(store, cfg)
    for t, p in make_payloads(sample_lines, 50):
        w.submit_message(t, p, "2026-10-21T05:10:00.000000Z")
    w.start()
    w.stop()  # 큐에 남은 50건을 모두 쓰고 끝난다
    assert store.conn.execute("SELECT count(DISTINCT f_cnt) FROM readings").fetchone()[0] == 50
    assert kinds(store)[-1] == "stop"
    store.close()


def test_bad_message_does_not_kill_thread(sample_lines, cfg, db_path, monkeypatch):
    store = Store(db_path)
    w = Writer(store, cfg)
    calls = {"n": 0}
    real = store.save_uplink

    def flaky(msg, fcnt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(msg, fcnt)

    monkeypatch.setattr(store, "save_uplink", flaky)
    w.start()
    for t, p in make_payloads(sample_lines, 3):
        w.submit_message(t, p, "2026-10-21T05:10:00.000000Z")
    w.stop()
    assert w.stats.errors == 1 and w.stats.saved == 2
    assert "write_error" in kinds(store)
    store.close()


def test_join_and_events_go_through_writer(cfg, db_path):
    store = Store(db_path)
    w = Writer(store, cfg)
    w.start()
    w.submit_event("connect", detail={"rc": "Success"})
    w.submit_message("application/1/device/abcd/event/join", b'{"deviceInfo":{"devEui":"abcd"}}',
                     "2026-10-21T05:10:00.000000Z")
    w.submit_message("application/1/device/abcd/event/status", b"{}", "2026-10-21T05:10:00.000000Z")
    w.stop()
    k = kinds(store)
    assert k[0] == "connect" and "join" in k
    assert store.conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 0
    assert w.stats.ignored == 1
    store.close()
