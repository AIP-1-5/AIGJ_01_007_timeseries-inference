"""D-2: 키 이름이 바뀐 메시지를 설정만 바꿔 같은 항목으로 저장한다(quickstart 6)."""

import sqlite3
import subprocess
import sys

from tests.conftest import CONFIG, REPO

FIXTURE = REPO / "tests" / "fixtures" / "format_change.jsonl"
EXAMPLE = REPO / "config" / "examples" / "format-change.toml"


def replay(db, config):
    r = subprocess.run(
        [sys.executable, "-m", "collector", "replay", str(FIXTURE), "--db", db, "--config", str(config)],
        cwd=REPO, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def items(db):
    c = sqlite3.connect(db)
    rows = dict(c.execute("SELECT item, count(*) FROM readings GROUP BY item"))
    unmapped = {(m, k) for m, k in c.execute(
        "SELECT json_extract(detail, '$.model'), json_extract(detail, '$.key') "
        "FROM collector_events WHERE kind = 'unmapped_key'")}
    c.close()
    return rows, unmapped


def test_production_config_stores_raw_keys_and_warns(tmp_path):
    db = str(tmp_path / "before.db")
    out = replay(db, CONFIG)
    assert "처리 5, 저장 메시지 5" in out
    rows, unmapped = items(db)
    assert rows == {"temp": 5, "humidity": 3, "humi": 2}
    assert unmapped == {("EM300-TH", "temp"), ("EM300-TH-v2", "temp"), ("EM300-TH-v2", "humi")}


def test_example_config_maps_to_same_items(tmp_path):
    db = str(tmp_path / "after.db")
    out = replay(db, EXAMPLE)
    assert "처리 5, 저장 메시지 5" in out and "매핑 안 된 키 0" in out
    rows, unmapped = items(db)
    assert rows == {"temperature": 5, "humidity": 5}
    assert unmapped == set()
    c = sqlite3.connect(db)
    assert c.execute("SELECT DISTINCT model FROM readings WHERE device_id = 'ffff000000000001'").fetchall() == [
        ("EM300-TH-v2",)]
    c.close()
