import numpy as np
import pandas as pd
import pytest

from tsdata.diagnose import daily_reactions, missing_table, profile_events
from tsdata.prep import load_config

KST = "Asia/Seoul"


@pytest.fixture
def cfg():
    return load_config()


def day(date, fn, observed=None):
    t = pd.date_range(pd.Timestamp(date, tz=KST), periods=288, freq="5min")
    tod = t.hour * 60 + t.minute
    v = np.array([fn(m) for m in tod], dtype=float)
    obs = np.ones(288, bool) if observed is None else observed
    return pd.DataFrame({"time": t.tz_convert("UTC"), "location": "실습실", "item": "co2", "value": v,
                         "observed": obs})


def school(m):
    if m < 9 * 60 + 20:
        return 450
    if 12 * 60 <= m < 13 * 60:                     # 점심: 12:30 최저
        return 800 + abs(m - 750) * 10
    if m >= 18 * 60 + 30:
        return 450
    return 1200


def test_daily_reactions_by_hand(cfg):
    obs = np.ones(288, bool)
    obs[::2] = False                                # 관측 50% → 데이터 부족
    s = pd.concat([day("2026-01-05", school),          # 월: 정상
                   day("2026-01-06", lambda m: 450),   # 화: 상승 없음
                   day("2026-01-07", school, obs),     # 수: 데이터 부족
                   day("2026-01-10", school)])         # 토: 평일 아님
    d = daily_reactions(s, cfg).set_index("date")
    assert list(d.index) == ["2026-01-05", "2026-01-06", "2026-01-07"]
    mon = d.loc["2026-01-05"]
    assert (mon["start_delay"], mon["lunch_delay"], mon["end_delay"]) == (20, 30, 30)
    assert mon["status"] == "정상" and mon["period"] == "train"
    assert d.loc["2026-01-06", "status"] == "상승 없음"
    assert d.loc["2026-01-07", "status"] == "데이터 부족"


def test_daily_reactions_flags_days_unlike_usual(cfg):
    normal = [day(d, school) for d in ("2026-01-05", "2026-01-06", "2026-01-07")]
    late = day("2026-01-08", lambda m: 450 if m < 10 * 60 + 30 else school(m))
    d = daily_reactions(pd.concat(normal + [late]), cfg).set_index("date")
    assert d.attrs["ref_start"] == 20                       # 학습 기간 중앙값(늦은 날 포함 4일)
    assert d.loc["2026-01-08", "start_delay"] == 90
    assert d.loc["2026-01-08", "status"].startswith("어긋남: 시작 10:30")
    assert (d.loc[["2026-01-05", "2026-01-06", "2026-01-07"], "status"] == "정상").all()


def test_profile_events(cfg):
    tod = np.arange(0, 1440, 5)
    prof = pd.Series([school(m) for m in tod], index=tod, dtype=float)
    e = profile_events(prof, cfg)
    assert (e["onset"], e["lunch_min"], e["half_drop"]) == (560, 750, 1110)


def test_missing_table(cfg):
    t = pd.date_range(pd.Timestamp("2026-01-05", tz=KST), periods=4, freq="5min").tz_convert("UTC")
    s = pd.DataFrame({"time": t, "location": "실습실", "item": "co2", "value": [1.0, 1.0, None, 1.0],
                      "observed": [True, False, False, True]})
    removed = pd.DataFrame([{"location": "실습실", "item": "co2", "rule": "out_of_range", "n": 7},
                            {"location": "lab", "item": "co2", "rule": "other_space", "n": 2}])
    r = missing_table(s, removed, cfg).iloc[0]
    assert (r["observed"], r["usable"], r["observed_train"], r["out_of_range"]) == (0.5, 0.75, 0.5, 7)
