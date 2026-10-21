"""기준선 3종과 공통 평가 시점. research(002) R7.

입력은 tsdata.prep의 공간 시계열(5분 격자, value = 과거 값으로만 채운 대표값, observed = 실제 관측).
평가 시점은 공간·항목·예측 거리마다 하나이고, 세 기준선이 모두 같은 시점을 쓴다(AC-2).
이후 모델은 저장된 평가 시점(eval_points.parquet)을 불러 같은 시점에서 평가한다.
"""

from __future__ import annotations

import pandas as pd

from .prep import PrepConfig, period_of

BASELINES = ("persistence", "mean_1h", "yesterday")
LABELS = {"persistence": "현재값 유지", "mean_1h": "최근 1시간 평균", "yesterday": "어제 같은 시각"}


def predictions(values: pd.Series, observed: pd.Series, h_min: int, cfg: PrepConfig) -> pd.DataFrame:
    """한 공간·항목의 5분 격자 시계열에서, 기준 시각 t마다 t+h를 맞히는 세 기준선 값.

    모두 t 시점까지 알 수 있는 값만 쓴다(어제 같은 시각 = t+h−24h, h ≤ 24h라 t 이전).
    """
    step = cfg.step_min
    b = cfg["baseline"]
    win = b["mean_window_min"] // step
    k = h_min // step
    mean = values.rolling(win, min_periods=1).mean()
    enough = observed.astype(int).rolling(win, min_periods=1).sum() >= b["mean_min_observed"]
    return pd.DataFrame({
        "persistence": values,
        "mean_1h": mean.where(enough),
        "yesterday": values.shift((24 * 60 - h_min) // step),
        "target": values.shift(-k),
        "target_observed": observed.shift(-k, fill_value=False).astype(bool),
    })


def eval_mask(values: pd.Series, observed: pd.Series, period_mask: pd.Series, h_min: int,
              cfg: PrepConfig) -> pd.Series:
    """거리 h의 평가 시점: 기간 안, 정답 y(t+h)가 실제 관측, 세 기준선이 모두 계산됨.

    입력 y(t)는 관측이 아니어도 된다(서비스도 마지막 값 유지로 채운 값을 쓴다). 회의실(10분 간격)은
    관측 칸이 한 칸 건너라서, 입력·정답 모두 관측을 요구하면 평가 시점이 거의 없어진다.
    """
    p = predictions(values, observed, h_min, cfg)
    mask = period_mask & p["target_observed"] & p["target"].notna()
    for name in BASELINES:
        mask &= p[name].notna()
    return mask


def evaluate(series: pd.DataFrame, cfg: PrepConfig, periods=("val", "test")) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(성능표 [period, location, item, horizon_min, baseline, mae, n], 평가 시점 [period, location, item, horizon_min, time]).

    같은 기간·공간·항목·거리 안에서는 세 기준선이 같은 시점을 쓴다(AC-2). 이후 모델도 이 시점을 쓴다.
    """
    b = cfg["baseline"]
    rows, points = [], []
    for (loc, item), g in series.groupby(["location", "item"]):
        if loc not in b["spaces"] or item not in b["targets"]:
            continue
        g = g.sort_values("time").reset_index(drop=True)
        per = period_of(g["time"], cfg)
        for period in periods:
            for h in b["horizons_min"]:
                mask = eval_mask(g["value"], g["observed"], per == period, h, cfg)
                points.append(pd.DataFrame({"period": period, "location": loc, "item": item, "horizon_min": h,
                                            "time": g.loc[mask, "time"]}))
                p = predictions(g["value"], g["observed"], h, cfg)[mask]
                for name in BASELINES:
                    err = (p[name] - p["target"]).abs()
                    rows.append({"period": period, "location": loc, "item": item, "horizon_min": h,
                                 "baseline": name, "mae": float(err.mean()) if len(err) else float("nan"),
                                 "n": int(len(err))})
    return pd.DataFrame(rows), pd.concat(points, ignore_index=True)


def best_table(perf: pd.DataFrame) -> pd.DataFrame:
    """기간·공간·항목·거리마다 MAE가 가장 작은 기준선."""
    idx = perf.groupby(["period", "location", "item", "horizon_min"])["mae"].idxmin()
    return perf.loc[idx, ["period", "location", "item", "horizon_min", "baseline", "mae"]]
