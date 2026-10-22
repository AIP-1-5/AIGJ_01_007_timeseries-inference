from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pandas as pd
import pytest

from collector.config import load_config as load_collector_config
from collector.store import Store
from service.api import route
from service.core import (AlertManager, Service, ServiceState, build_series, load_service_config,
                          minutes_to_limit, persistence_forecast, read_recent, rule_alerts)
from tsdata.prep import clean, load_config as load_prep_config, to_space_series

NOW = datetime(2026, 10, 21, 8, 23, 5, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load_service_config()


@pytest.fixture
def prep_cfg():
    return load_prep_config()


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fill_db(path, minutes=90, end=NOW, heartbeat=True):
    s = Store(path)
    rows = []
    for i in range(minutes):
        t = end - timedelta(minutes=minutes - i, seconds=-10)
        for dev, loc, model, items in (("aaaa", "실습실", "AM107", {"co2": 600 + i, "temperature": 22.0, "humidity": 50.0}),
                                       ("bbbb", "실습실", "AM107", {"co2": 700 + i, "temperature": 22.4, "humidity": 51.0})):
            for item, v in items.items():
                rows.append((dev, 0, i, item, float(v), loc, model, iso(t), iso(t)))
    s.conn.executemany(
        "INSERT INTO readings (device_id, fcnt_epoch, f_cnt, item, value, location, model, measured_at, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    s.conn.execute("INSERT INTO devices (device_id, model, location, fcnt_epoch, last_f_cnt) VALUES "
                   "('aaaa', 'AM107', '실습실', 0, 0), ('bbbb', 'AM107', '실습실', 0, 0)")
    if heartbeat:
        s.conn.execute("INSERT INTO collector_events (ts, kind) VALUES (?, 'heartbeat')", (iso(end - timedelta(seconds=30)),))
    s.close()


def test_service_preprocessing_equals_training(db_path, prep_cfg):
    fill_db(db_path)
    start, end = NOW - timedelta(hours=2), NOW.replace(minute=20, second=0)
    raw = read_recent(db_path, start, NOW)
    got = build_series(raw, prep_cfg, (start, end))
    # 학습 쪽: 같은 행을 스냅샷 형태(문자열 값)로 만들어 tsdata.prep을 직접 부른다
    direct = raw.copy()
    direct["raw_value"] = direct["raw_value"].astype(float).astype(str)
    cleaned, _ = clean(direct, prep_cfg)
    want = to_space_series(cleaned, prep_cfg, (pd.Timestamp("2026-10-21 06:20", tz="UTC"), pd.Timestamp(end)))
    pd.testing.assert_frame_equal(got.reset_index(drop=True), want.reset_index(drop=True))
    lab = got[(got["location"] == "실습실") & (got["item"] == "co2")].dropna(subset=["value"])
    assert lab["time"].max() == pd.Timestamp("2026-10-21 08:15", tz="UTC")   # 부분 칸(08:20~) 제외


def test_persistence_forecast_shape():
    f = persistence_forecast({"co2": 812.34, "temperature": 22.456})
    assert f["model"] == "fallback" and f["minutes"] == list(range(5, 65, 5))
    assert f["co2"] == [812.3] * 12 and f["temperature"] == [22.46] * 12


def test_minutes_to_limit():
    fc = [900, 950, 990, 1001, 1050] + [1100] * 7
    assert minutes_to_limit(880, fc, 1000) == {"limit": 1000, "exceeded_now": False, "minutes_to_exceed": 20}
    assert minutes_to_limit(1010, fc, 1000)["exceeded_now"] is True
    assert minutes_to_limit(500, [600] * 12, 1000)["minutes_to_exceed"] is None


def test_alert_dedup_new_repeat_resolved():
    am = AlertManager(30)
    key = ("실습실", "data_gap")
    t = NOW
    assert [e["status"] for e in am.update({key: {}}, t)] == ["new"]
    assert am.update({key: {}}, t + timedelta(minutes=1)) == []            # 매분 다시 보내지 않음
    assert [e["status"] for e in am.update({key: {}}, t + timedelta(minutes=30))] == ["repeat"]
    assert am.update({key: {}}, t + timedelta(minutes=31)) == []
    ev = am.update({}, t + timedelta(minutes=40))
    assert [(e["status"], e["type"]) for e in ev] == [("resolved", "data_gap")]
    assert am.active == {}


def test_data_gap_and_collector_down(cfg, prep_cfg):
    ccfg = load_collector_config(cfg.repo_path(cfg["paths"]["collector_config"]))
    status = {"heartbeat": iso(NOW - timedelta(seconds=30)),
              "last_measured": {"실습실": iso(NOW - timedelta(minutes=6)), "사무실": iso(NOW - timedelta(minutes=1)),
                                "회의실": iso(NOW - timedelta(minutes=15))},
              "devices": [("24e124725d089152", "AM103", "회의실")]}
    got = rule_alerts(pd.DataFrame(columns=["time", "raw_value", "device_id", "location", "item"]),
                      status, NOW, cfg, prep_cfg, ccfg)
    assert ("실습실", "data_gap") in got and got[("실습실", "data_gap")]["limit_s"] == 300
    assert ("사무실", "data_gap") not in got
    assert ("회의실", "data_gap") not in got          # 10분 센서: 기준 20분
    assert ("사무실 밖", "data_gap") in got            # 기록 없음
    assert ("*", "collector_down") not in got
    status["heartbeat"] = iso(NOW - timedelta(minutes=10))
    assert ("*", "collector_down") in rule_alerts(pd.DataFrame(columns=["time", "raw_value", "device_id",
                                                                          "location", "item"]),
                                                    status, NOW, cfg, prep_cfg, ccfg)


def test_cycle_on_synthetic_db(db_path, cfg):
    fill_db(db_path)
    svc = Service(cfg)
    svc.db_path = db_path
    svc.normalizer = lambda: None                      # 모델·정규화 없음 → fallback
    r = svc.cycle(NOW)
    lab = r["spaces"]["lab"]
    assert lab["status"] == "ok" and lab["forecast"]["model"] == "fallback"
    assert lab["data_time"] == "2026-10-21T08:15:00Z"
    assert lab["current"]["co2"] == pytest.approx(733.5, abs=1)     # 두 센서 08:15~08:20 평균의 중앙값
    assert r["spaces"]["office"]["status"] == "degraded"            # 사무실 기록 없음 → 끊김
    assert ("office", "data_gap") in {(cfg.space_id(e["space"]), e["type"]) for e in r["events"]}


def test_api_routes(cfg):
    st = ServiceState()
    st.spaces = {"lab": {"space": "실습실", "space_id": "lab", "status": "ok", "data_time": "2026-10-21T08:15:00Z",
                         "forecast": persistence_forecast({"co2": 800.0, "temperature": 22.0}), "alerts": []}}
    st.status = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "models": {}}
    code, body = route(st, cfg, "/health")
    assert code == 200 and body["status"] == "ok"
    code, body = route(st, cfg, "/spaces/lab/forecast?minutes=30")
    assert code == 200 and body["co2"] == 800.0 and body["minutes"] == 30
    code, body = route(st, cfg, f"/spaces/{quote('실습실')}")
    assert code == 200 and body["space_id"] == "lab"
    assert route(st, cfg, "/spaces/lab/forecast?minutes=7")[0] == 400
    assert route(st, cfg, "/spaces/nowhere")[0] == 404
    assert route(st, cfg, "/spaces/office")[0] == 503


def test_model_hot_reload(tmp_path, cfg):
    torch = pytest.importorskip("torch")
    from service.core import ModelStore
    from tsmodel.state import MLP

    cfg.raw["models"] = dict(cfg.raw["models"], dir=str(tmp_path))
    store = ModelStore(cfg)
    assert store.get("vent") is None                                    # 파일 없음 → fallback
    path = tmp_path / cfg["models"]["vent"]
    torch.save({"state_dict": MLP(5).state_dict(), "meta": {"n_in": 5, "threshold": 0.5, "features": list("abcde"),
                                                             "spaces": ["실습실"]}}, path)
    model, meta = store.get("vent")
    assert meta["n_in"] == 5
    assert store.get("vent")[0] is model                                # 그대로면 다시 읽지 않음
    torch.save({"state_dict": MLP(7).state_dict(), "meta": {"n_in": 7, "threshold": 0.4, "features": list("abcdefg"),
                                                             "spaces": ["실습실"]}}, path)
    import os
    os.utime(path, (1, 2_000_000_000))
    assert store.get("vent")[1]["n_in"] == 7                            # 수정 시각이 바뀌면 다시 읽음
