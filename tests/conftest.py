import json
from pathlib import Path

import pytest

from collector.config import load_config

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "collector.toml"
FIXTURE = REPO / "tests" / "fixtures" / "uplink_sample.jsonl"
UP_TOPIC = "application/app/device/{dev}/event/up"


@pytest.fixture
def cfg():
    return load_config(CONFIG)


@pytest.fixture
def sample_lines():
    """fixture 30건을 (topic, payload bytes) 목록으로 돌려준다."""
    out = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        m = json.loads(line)
        out.append((UP_TOPIC.format(dev=m["deviceInfo"]["devEui"]), line.encode("utf-8")))
    return out


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "sub" / "t.db")
