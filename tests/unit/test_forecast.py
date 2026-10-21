import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from tsdata.prep import load_config  # noqa: E402
from tsmodel.forecast import HORIZON, SEQ_LEN, ForecastGRU, build_inputs, space_frame, target_delta  # noqa: E402

KST = "Asia/Seoul"


@pytest.fixture
def cfg():
    return load_config()


def frame(cfg, n=60):
    t = pd.date_range(pd.Timestamp("2026-01-05 08:00", tz=KST).tz_convert("UTC"), periods=n, freq="5min")
    rows = []
    for i, ti in enumerate(t):
        for it, v in (("co2", 400 + i), ("temperature", 20 + i / 100), ("humidity", 50.0)):
            rows.append((ti, "실습실", it, float(v), True))
    s = pd.DataFrame(rows, columns=["time", "location", "item", "value", "observed"])
    norm = {"stats": {f"실습실|{it}": {"mean": 0.0, "std": 1.0} for it in ("co2", "temperature", "humidity")}}
    return space_frame(s, "실습실", norm), norm


def test_inputs_use_only_past_and_targets_are_future(cfg):
    fr, norm = frame(cfg)
    t = 30
    inp = build_inputs(fr, "실습실", ["실습실", "사무실", "회의실"], cfg, True, anchors=np.array([t]))
    assert inp["seq"].shape == (1, SEQ_LEN, 6)
    assert inp["seq"][0, -1, 0] == 400 + t and inp["seq"][0, 0, 0] == 400 + t - SEQ_LEN + 1   # 창 끝 = t
    assert inp["current"][0, 0] == 400 + t
    assert inp["future"][0, 0, 0] == 400 + t + 1 and inp["future"][0, -1, 0] == 400 + t + HORIZON
    y, m = target_delta(inp, np.array([1.0, 1.0], np.float32))
    assert y[0, :HORIZON].tolist() == list(range(1, HORIZON + 1)) and m.all()


def test_missing_inputs_filled_and_flagged(cfg):
    fr, _ = frame(cfg)
    fr.loc[fr.index[28], ["co2", "co2_n"]] = np.nan
    fr.loc[fr.index[28], "co2_obs"] = False
    inp = build_inputs(fr, "실습실", ["실습실"], cfg, True, anchors=np.array([30]))
    assert not np.isnan(inp["seq"]).any()
    assert inp["seq"][0, -3, 0] == 400 + 27 and inp["seq"][0, -3, 3] == 0   # 앞 값으로 채우고 관측=0


def test_model_shapes(cfg):
    m = ForecastGRU(6, 20)
    out = m(torch.zeros(4, SEQ_LEN, 6), torch.zeros(4, 20))
    assert out.shape == (4, HORIZON * 2)
