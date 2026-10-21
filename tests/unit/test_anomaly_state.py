import numpy as np
import pandas as pd
import pytest

pytest.importorskip("torch")

from tsdata.prep import load_config  # noqa: E402
from tsmodel import anomaly as an  # noqa: E402
from tsmodel import state as stt  # noqa: E402

KST = "Asia/Seoul"


@pytest.fixture
def cfg():
    return load_config()


def frame(n=288 * 10, co2=600.0):
    t = pd.date_range(pd.Timestamp("2026-09-07", tz=KST).tz_convert("UTC"), periods=n, freq="5min")
    rng = np.random.default_rng(0)
    f = pd.DataFrame({"co2": co2 + rng.normal(0, 5, n), "temperature": 22 + rng.normal(0, 0.05, n),
                      "humidity": 50.0}, index=t)
    for it in ("co2", "temperature", "humidity"):
        f[f"{it}_obs"] = True
    return f


def test_inject_marks_events_and_changes_values(cfg):
    f = frame()
    mask = np.ones(len(f), bool)
    inj, events = an.inject(f, mask, cfg, np.random.default_rng(1), per_type=1)
    assert sorted(e["type"] for e in events) == sorted(an.FAULTS)
    for e in events:
        seg = slice(e["start"], e["end"])
        if e["type"] == "stuck":
            assert inj[e["item"]].iloc[seg].nunique() == 1
        elif e["type"] == "spike":
            assert (inj[e["item"]].iloc[seg] - f[e["item"]].iloc[seg]).abs().iloc[0] >= 8
        elif e["type"] in ("drift", "hvac_stop"):
            assert (inj[e["item"]].iloc[seg] >= f[e["item"]].iloc[seg] - 1e-9).all()


def test_event_evaluation_by_hand():
    flags = np.zeros(1000, bool)
    flags[105] = True            # 고장 1 탐지(지연 5칸 = 25분)
    flags[500:503] = True        # 고장과 무관 → 헛경보 1건(묶음)
    flags[506] = True            # 30분(6칸) 안이라 같은 경보로 묶임
    events = [{"start": 100, "end": 130}, {"start": 300, "end": 310}]
    r = an.evaluate_events(flags, events, np.ones(1000, bool))
    assert [e["detected"] for e in r["events"]] == [True, False]
    assert r["events"][0]["delay_min"] == 25
    assert r["false_alarms"] == 1


def test_vent_label_excludes_already_exceeded(cfg):
    f = frame(n=60)
    f["co2"] = np.r_[np.full(20, 900.0), np.linspace(900, 1100, 20), np.full(20, 1100.0)]
    norm = {"stats": {f"실습실|{it}": {"mean": 0.0, "std": 1.0} for it in ("co2", "temperature", "humidity")}}
    d = stt.vent_dataset(f, "실습실", norm, cfg)
    first_over = int(np.argmax(f["co2"].to_numpy() >= 1000))
    assert d["label"].iloc[first_over - 1] == 1          # 30분 안에 넘음
    assert not d["usable"].iloc[first_over:].any()       # 이미 넘은 시점은 제외


def test_metrics_macro_f1():
    m = stt.metrics(np.array([1, 1, 0, 0]), np.array([1, 0, 0, 0]))
    assert (m["recall"], m["precision"], m["false_occupied_rate"]) == (0.5, 1.0, 0.0)
    assert m["macro_f1"] == pytest.approx((2 / 3 + 0.8) / 2)
