import pandas as pd
import pytest

from tsdata.prep import (analysis_span, clean, fill_short_gaps, fit_normalizer, load_config, period_of,
                         to_space_series)

KST = "Asia/Seoul"


@pytest.fixture
def cfg():
    return load_config()


def raw_rows(rows):
    """rows: (time KST 문자열, raw_value, device_id, location, item)."""
    df = pd.DataFrame(rows, columns=["time", "raw_value", "device_id", "location", "item"])
    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize(KST).dt.tz_convert("UTC")
    return df


def test_clean_rules_and_counts(cfg):
    raw = raw_rows([
        ("2025-09-01 09:00", "650", "a", "실습실", "co2"),
        ("2025-09-01 09:01", "65535", "a", "실습실", "co2"),      # 범위 밖
        ("2025-09-01 09:02", "0", "a", "실습실", "co2"),          # 범위 밖
        ("2025-09-01 09:03", "abc", "a", "실습실", "co2"),        # 숫자 아님
        ("2025-09-01 09:04", "-0.1", "b", "사무실 밖", "temperature"),  # 오류 값
        ("2025-09-01 09:04", "127.5", "b", "사무실 밖", "humidity"),    # 범위 밖
        ("2025-09-01 09:05", "23.1", "b", "사무실 밖", "temperature"),
        ("2025-09-01 09:06", "600", "", "실습실", "co2"),         # 센서 ID 없음
        ("2025-09-01 09:07", "900", "c", "lab", "co2"),           # 대상 밖 공간
    ])
    df, removed = clean(raw, cfg)
    assert sorted(df["value"].tolist()) == [23.1, 650.0]
    got = {(r.location, r.item, r.rule): r.n for r in removed.itertuples()}
    assert got == {("실습실", "co2", "out_of_range"): 2, ("실습실", "co2", "non_numeric"): 1,
                   ("사무실 밖", "temperature", "error_value"): 1, ("사무실 밖", "humidity", "out_of_range"): 1,
                   ("실습실", "co2", "no_device"): 1, ("lab", "co2", "other_space"): 1}


def test_space_series_sensor_mean_fill_then_median(cfg):
    raw = raw_rows([
        ("2025-09-01 09:00", "600", "a", "실습실", "co2"),
        ("2025-09-01 09:02", "700", "a", "실습실", "co2"),   # a의 5분 평균 650
        ("2025-09-01 09:01", "800", "b", "실습실", "co2"),
        ("2025-09-01 09:03", "2000", "c", "실습실", "co2"),  # 중앙값이라 c에 끌려가지 않음
        ("2025-09-01 09:06", "500", "a", "실습실", "co2"),   # b, c는 마지막 값 유지
    ])
    df, _ = clean(raw, cfg)
    start = pd.Timestamp("2025-09-01 09:00", tz=KST).tz_convert("UTC")
    s = to_space_series(df, cfg, (start, start + pd.Timedelta(minutes=30)))
    assert s["value"].tolist()[:3] == [800.0, 800.0, 800.0]   # 1칸: (500, 800, 2000) → 800
    assert s["n_observed"].tolist()[:3] == [3, 1, 0]
    assert s["observed"].tolist() == [True, True, False, False, False, False]
    assert s["n_sensors"].tolist() == [3, 3, 3, 3, 1, 0]     # b·c는 0칸 + 3칸까지, a는 1칸 + 3칸까지
    assert pd.isna(s["value"].iloc[5])
    assert (s["time"].diff().dropna() == pd.Timedelta(minutes=5)).all()


def test_fill_uses_only_past_values():
    v = pd.Series([1.0, None, None, 4.0, None, None, None, None, 9.0, None])
    out = fill_short_gaps(v, 3)
    assert out[:8].tolist()[:7] == [1.0, 1.0, 1.0, 4.0, 4.0, 4.0, 4.0]   # 다음 값(4, 9)을 끌어오지 않음
    assert pd.isna(out[7])                # 4칸째부터는 비움
    assert out[9] == 9.0


def test_fill_is_causal():
    """앞부분 값은 뒤에 무엇이 오든 같다(서비스에서 실시간으로 계산한 값과 같음)."""
    a = pd.Series([1.0, None, None, 2.0])
    b = pd.Series([1.0, None, None, 100.0])
    assert fill_short_gaps(a, 3)[:3].tolist() == fill_short_gaps(b, 3)[:3].tolist()


def test_period_boundaries_kst_inclusive(cfg):
    t = pd.Series(pd.to_datetime([
        "2025-08-31 23:59", "2025-09-01 00:00", "2026-06-30 23:55", "2026-07-01 00:00",
        "2026-08-31 23:55", "2026-09-01 00:00", "2026-10-20 23:55", "2026-10-21 00:00",
    ]).tz_localize(KST).tz_convert("UTC"))
    assert period_of(t, cfg).tolist() == ["", "train", "train", "val", "val", "test", "test", ""]


def test_analysis_span(cfg):
    a, b = analysis_span(cfg)
    assert a == pd.Timestamp("2025-08-31 15:00", tz="UTC") and b == pd.Timestamp("2026-10-20 15:00", tz="UTC")


def series_frame(cfg, values_by_period):
    """기간별 값 목록으로 공간 시계열을 만든다. 보간 칸(observed=False)도 섞는다."""
    rows = []
    starts = {"train": "2026-01-05 09:00", "val": "2026-07-06 09:00", "test": "2026-09-07 09:00"}
    for period, vals in values_by_period.items():
        t0 = pd.Timestamp(starts[period], tz=KST).tz_convert("UTC")
        for i, (v, obs) in enumerate(vals):
            rows.append((t0 + pd.Timedelta(minutes=5 * i), "실습실", "co2", v, obs))
    return pd.DataFrame(rows, columns=["time", "location", "item", "value", "observed"])


def test_normalizer_uses_train_observed_only(cfg):
    base = {"train": [(400.0, True), (600.0, True), (5000.0, False)],   # 보간 칸 5000은 제외
            "val": [(800.0, True)], "test": [(900.0, True)]}
    a = fit_normalizer(series_frame(cfg, base), cfg)["stats"]["실습실|co2"]
    assert (a["mean"], a["std"], a["n"]) == (500.0, 100.0, 2)
    changed = dict(base, val=[(99999.0, True)], test=[(-5.0, True), (1e6, True)])
    b = fit_normalizer(series_frame(cfg, changed), cfg)["stats"]["실습실|co2"]
    assert a == b                                                      # AC-1
