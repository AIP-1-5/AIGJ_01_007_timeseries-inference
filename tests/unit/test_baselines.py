import numpy as np
import pandas as pd
import pytest

from tsdata.baselines import best_table, eval_mask, evaluate, predictions
from tsdata.prep import load_config

KST = "Asia/Seoul"
DAY = 288  # 5분 칸 하루


@pytest.fixture
def cfg():
    return load_config()


def test_predictions_by_hand(cfg):
    v = pd.Series(np.arange(DAY * 2, dtype=float))     # 값 = 칸 번호
    obs = pd.Series(True, index=v.index)
    p = predictions(v, obs, 15, cfg)
    t = DAY + 20
    assert p.loc[t, "persistence"] == t
    assert p.loc[t, "mean_1h"] == pytest.approx(np.mean(range(t - 11, t + 1)))   # 12칸
    assert p.loc[t, "yesterday"] == t + 3 - DAY                                  # t+15분−24시간
    assert p.loc[t, "target"] == t + 3


def test_mean_1h_needs_six_observed(cfg):
    v = pd.Series(np.full(30, 500.0))
    obs = pd.Series([False] * 30)
    obs[18:23] = True                                    # t=23 직전 12칸 중 관측 5칸
    p = predictions(v, obs, 5, cfg)
    assert pd.isna(p.loc[23, "mean_1h"])
    obs[23] = True                                       # 6칸
    assert predictions(v, obs, 5, cfg).loc[23, "mean_1h"] == 500.0


def test_eval_mask_requires_observed_target(cfg):
    n = DAY * 2
    v = pd.Series(np.full(n, 600.0))
    obs = pd.Series(True, index=v.index)
    t = DAY + 50
    obs[t + 12] = False                                  # 60분 뒤 정답이 관측 아님(채운 칸)
    period = pd.Series(False, index=v.index)
    period[DAY + 40:DAY + 60] = True
    assert not eval_mask(v, obs, period, 60, cfg)[t]
    assert eval_mask(v, obs, period, 5, cfg)[t]          # 5분 정답은 관측이라 포함
    assert not eval_mask(v, obs, period, 5, cfg)[DAY + 39]   # 기간 밖


def test_ten_minute_sensor_gets_points(cfg):
    """회의실처럼 관측이 한 칸 건너도, 정답이 관측인 칸에서 평가된다."""
    n = DAY * 2
    v = pd.Series(np.full(n, 600.0))
    obs = pd.Series([i % 2 == 0 for i in range(n)])
    period = pd.Series(False, index=v.index)
    period[DAY + 20:DAY + 40] = True
    assert eval_mask(v, obs, period, 5, cfg).sum() == 10


def frame(cfg, start_kst, values, observed=None):
    t0 = pd.Timestamp(start_kst, tz=KST).tz_convert("UTC")
    n = len(values)
    return pd.DataFrame({"time": pd.date_range(t0, periods=n, freq="5min"), "location": "실습실", "item": "co2",
                         "value": values, "observed": observed if observed is not None else [True] * n})


def test_evaluate_same_points_for_all_baselines(cfg):
    n = DAY * 4
    rng = np.random.default_rng(0)
    vals = 600 + np.cumsum(rng.normal(0, 5, n))
    s = frame(cfg, "2026-09-01 00:00", vals)
    perf, points = evaluate(s, cfg, periods=("test",))
    assert set(perf["baseline"]) == {"persistence", "mean_1h", "yesterday"}
    assert perf.groupby("horizon_min")["n"].nunique().eq(1).all()      # 거리마다 세 기준선이 같은 시점 수
    for h, g in points.groupby("horizon_min"):
        assert perf.query("horizon_min == @h")["n"].iloc[0] == len(g)
    # 손계산: 5분 현재값 유지 MAE
    t = points.query("horizon_min == 5")["time"]
    idx = s.set_index("time")["value"]
    by_hand = (idx.shift(-1).loc[t] - idx.loc[t]).abs().mean()
    got = perf.query("horizon_min == 5 and baseline == 'persistence'")["mae"].iloc[0]
    assert got == pytest.approx(by_hand)


def test_best_table_picks_smallest(cfg):
    perf = pd.DataFrame([
        {"period": "test", "location": "실습실", "item": "co2", "horizon_min": 60, "baseline": b, "mae": m, "n": 10}
        for b, m in [("persistence", 80.0), ("mean_1h", 95.0), ("yesterday", 120.0)]])
    assert best_table(perf)["baseline"].tolist() == ["persistence"]
