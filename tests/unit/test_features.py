import pandas as pd
import pytest

from tsdata.prep import load_config
from tsmodel.features import SCHEDULE_FEATURES, TIME_FEATURES, time_features


@pytest.fixture
def cfg():
    return load_config()


def utc(*kst):
    return pd.Series(pd.to_datetime(list(kst)).tz_localize("Asia/Seoul").tz_convert("UTC"))


def test_schedule_flags(cfg):
    f = time_features(utc("2026-10-20 10:00", "2026-10-20 12:30", "2026-10-20 19:00",
                          "2026-10-09 10:00", "2026-10-18 10:00"), cfg)
    assert f["in_class"].tolist() == [1, 0, 0, 0, 0]       # 화 수업 / 점심 / 퇴실 후 / 한글날 / 일요일
    assert f["in_lunch"].tolist() == [0, 1, 0, 0, 0]
    assert f["holiday"].tolist() == [0, 0, 0, 1, 0]
    assert f["workday"].tolist() == [1, 1, 1, 0, 0]
    assert f["min_from_class_end"].iloc[2] == pytest.approx(60 / 180)


def test_without_schedule_has_only_time(cfg):
    f = time_features(utc("2026-10-20 10:00"), cfg, schedule=False)
    assert list(f.columns) == TIME_FEATURES
    assert set(SCHEDULE_FEATURES).isdisjoint(f.columns)
