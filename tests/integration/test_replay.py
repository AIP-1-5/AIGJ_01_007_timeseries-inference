import json
import sqlite3
import subprocess
import sys

from tests.conftest import CONFIG, FIXTURE, REPO


def run_replay(path, db, *extra):
    return subprocess.run(
        [sys.executable, "-m", "collector", "replay", str(path), "--db", db, "--config", str(CONFIG), *extra],
        cwd=REPO, capture_output=True, text=True, timeout=60)


def test_replay_fixture_twice(db_path):
    r = run_replay(FIXTURE, db_path)
    assert r.returncode == 0, r.stderr
    assert "처리 30, 저장 메시지 28, 중복 무시 2, 버림 0, 매핑 안 된 키 0" in r.stdout

    c = sqlite3.connect(db_path)
    rows = c.execute("SELECT count(*) FROM readings").fetchone()[0]
    assert c.execute("SELECT count(DISTINCT device_id || ':' || f_cnt) FROM readings").fetchone()[0] == 28
    c.close()

    r = run_replay(FIXTURE, db_path)
    assert "처리 30, 저장 메시지 0, 중복 무시 30" in r.stdout
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT count(*) FROM readings").fetchone()[0] == rows
    assert c.execute(
        "SELECT count(*) FROM (SELECT 1 FROM readings GROUP BY device_id, fcnt_epoch, f_cnt, item HAVING count(*) > 1)"
    ).fetchone()[0] == 0

    # 중복 쌍은 처음 들어온 줄의 measured_at을 유지한다
    lines = [json.loads(l) for l in FIXTURE.read_text(encoding="utf-8").splitlines()]
    for dev, fcnt in (("24e124126d152862", 104884), ("24e124136d151836", 212969)):
        first = next(m for m in lines if m["deviceInfo"]["devEui"] == dev and m["fCnt"] == fcnt)
        ns = min(r["nsTime"] for r in first["rxInfo"])
        got = {x for (x,) in c.execute(
            "SELECT measured_at FROM readings WHERE device_id=? AND f_cnt=?", (dev, fcnt))}
        assert len(got) == 1
        assert got.pop()[:26] == ns[:26]  # 마이크로초까지 같음
    c.close()


def test_replay_topic_payload_lines_and_rejects(tmp_path, db_path):
    good = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
    lines = [
        json.dumps({"topic": "application/1/device/x/event/up", "payload": good}),
        json.dumps({"topic": "application/1/device/x/event/status", "payload": good}),  # 무시
        json.dumps({"topic": "application/1/device/x/event/up", "payload": "{broken"}),  # 버림
        "not json at all",                                                              # 버림
    ]
    p = tmp_path / "mixed.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = run_replay(p, db_path)
    assert r.returncode == 0, r.stderr
    assert "처리 4, 저장 메시지 1, 중복 무시 0, 버림 2, 매핑 안 된 키 0" in r.stdout
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT count(*) FROM rejected").fetchone()[0] == 2
    assert c.execute("SELECT count(*) FROM collector_events WHERE kind='parse_error'").fetchone()[0] == 2


def test_replay_bad_config_exit_2(tmp_path, db_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[mqtt]\npassword = 'x'\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "collector", "replay", str(FIXTURE), "--db", db_path,
                        "--config", str(bad)], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 2
    assert "password" in r.stderr
