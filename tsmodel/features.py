"""시간·일정 특성. 과제 3~8 공용. 모두 그 시각에 미리 알 수 있는 정보만 쓴다(누수 없음).

일정 자료가 없어 config/prep.toml [schedule]의 가정 일정과 [calendar] 공휴일을 쓴다.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from tsdata.prep import PrepConfig

TIME_FEATURES = ["tod_sin", "tod_cos", "dow_sin", "dow_cos", "weekend"]
SCHEDULE_FEATURES = ["holiday", "workday", "in_class", "in_lunch", "min_to_class", "min_from_class_end"]


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def time_features(times: pd.Series | pd.DatetimeIndex, cfg: PrepConfig, schedule: bool = True) -> pd.DataFrame:
    """UTC 시각 → 특성 표. schedule=False면 시간대·요일만(일정 정보 제외 비교용)."""
    lt = pd.DatetimeIndex(pd.to_datetime(times)).tz_convert(cfg.tz)
    tod = (lt.hour * 60 + lt.minute).to_numpy()
    dow = lt.dayofweek.to_numpy()
    out = pd.DataFrame({
        "tod_sin": np.sin(2 * np.pi * tod / 1440), "tod_cos": np.cos(2 * np.pi * tod / 1440),
        "dow_sin": np.sin(2 * np.pi * dow / 7), "dow_cos": np.cos(2 * np.pi * dow / 7),
        "weekend": (dow >= 5).astype(float),
    })
    if not schedule:
        return out
    sc = cfg["schedule"]
    start, ls, le, end = (_minutes(sc[k]) for k in ("class_start", "lunch_start", "lunch_end", "class_end"))
    holiday = np.isin(lt.strftime("%Y-%m-%d"), cfg.raw.get("calendar", {}).get("holidays", []))
    workday = (dow < 5) & ~holiday
    in_class = workday & (((tod >= start) & (tod < ls)) | ((tod >= le) & (tod < end)))
    in_lunch = workday & (tod >= ls) & (tod < le)
    out["holiday"] = holiday.astype(float)
    out["workday"] = workday.astype(float)
    out["in_class"] = in_class.astype(float)
    out["in_lunch"] = in_lunch.astype(float)
    # 수업 시작까지 / 수업 종료 뒤 경과(분, 0~180으로 자르고 /180). 일하지 않는 날은 1(멀다)
    out["min_to_class"] = np.where(workday, np.clip(start - tod, 0, 180) / 180, 1.0)
    out["min_from_class_end"] = np.where(workday, np.clip(tod - end, 0, 180) / 180, 1.0)
    return out


def device():
    """학습·추론 장치. 기본 CPU: 설치된 PyTorch 2.14(cu130)는 Orin(CC 8.7)을 공식 지원하지 않는다(과제 7에서 GPU 따로 측정).
    TSINFER_DEVICE=cuda로 바꿀 수 있다."""
    import torch
    return torch.device(os.environ.get("TSINFER_DEVICE", "cpu"))
