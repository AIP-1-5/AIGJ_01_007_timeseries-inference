"""과제 5: 상태 추정 — 사용 여부(실습실)와 환기 선제 경보. specs/005-state.

- 사용 여부 정답 = 가정 일정(평일 수업 시간, 공휴일 제외). 일정 자료가 실습실 것뿐이라 실습실만.
  입력에 일정·시각을 넣지 않는다(정답을 만든 정보 → 누수, 원칙 II).
- 환기 경보 정답 = 지금 CO₂ < 1000이고 30분 안에 1000 이상(관측). 이미 넘은 시점은 데이터에서 뺀다.
분류기는 작은 MLP(과제 7 TensorRT 변환용).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import torch
from torch import nn

from tsdata.prep import PrepConfig
from tsmodel.features import device, SCHEDULE_FEATURES, TIME_FEATURES, time_features

LIMIT = 1000.0
AHEAD = 6           # 30분


def sensor_features(frame: pd.DataFrame, loc: str, normalizer: dict) -> pd.DataFrame:
    """CO₂·온도·습도의 현재 수준과 변화(과거 값만). 일정·시각 없음."""
    out = pd.DataFrame(index=frame.index)
    for it in ("co2", "temperature", "humidity"):
        st = normalizer["stats"][f"{loc}|{it}"]
        v = frame[it].ffill(limit=3)
        z = (v - st["mean"]) / st["std"]
        out[f"{it}_z"] = z
        for k in (1, 3, 6, 12):                      # 5·15·30·60분 변화
            out[f"{it}_d{k}"] = z - z.shift(k)
        out[f"{it}_above_min24h"] = z - z.rolling(288, min_periods=12).min()   # 하루 최저 대비
    return out


def occupancy_dataset(frame: pd.DataFrame, loc: str, normalizer: dict, cfg: PrepConfig) -> pd.DataFrame:
    x = sensor_features(frame, loc, normalizer)
    x["label"] = time_features(frame.index, cfg)["in_class"].to_numpy()
    x["rule"] = (frame["co2"] - frame["co2"].shift(3) >= 30).astype(float).to_numpy()   # 최근 15분 +30ppm
    x["observed"] = frame["co2_obs"].to_numpy()
    return x


def vent_dataset(frame: pd.DataFrame, loc: str, normalizer: dict, cfg: PrepConfig) -> pd.DataFrame:
    x = sensor_features(frame, loc, normalizer)
    tf = time_features(frame.index, cfg)[TIME_FEATURES + SCHEDULE_FEATURES]
    for c in tf.columns:
        x[c] = tf[c].to_numpy()
    co2 = frame["co2"].where(frame["co2_obs"])
    fut_max = pd.concat([co2.shift(-k) for k in range(1, AHEAD + 1)], axis=1).max(axis=1, skipna=True)
    fut_n = pd.concat([co2.shift(-k).notna() for k in range(1, AHEAD + 1)], axis=1).sum(axis=1)
    x["label"] = (fut_max >= LIMIT).astype(float)
    x["rule"] = (frame["co2"] >= 850).astype(float)
    x["co2_now"] = frame["co2"]
    # 이미 넘은 시점·현재값 없음·앞 30분 관측이 너무 적은 시점은 뺀다
    x["usable"] = (frame["co2"] < LIMIT) & frame["co2"].notna() & (fut_n >= AHEAD // 2)
    return x


class MLP(nn.Module):
    def __init__(self, n_in: int):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(n_in, 64), nn.ReLU(), nn.Linear(64, 32), nn.ReLU(), nn.Linear(32, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)


def train_mlp(x: np.ndarray, y: np.ndarray, epochs: int = 10, seed: int = 0, log=print) -> MLP:
    torch.manual_seed(seed)
    dev = device()
    model = MLP(x.shape[1]).to(dev)
    pos = max(y.mean(), 1e-4)
    lossf = nn.BCEWithLogitsLoss(pos_weight=torch.tensor((1 - pos) / pos, device=dev))   # 불균형 보정
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    xt, yt = torch.from_numpy(x.astype(np.float32)), torch.from_numpy(y.astype(np.float32))
    for ep in range(epochs):
        perm = torch.randperm(len(xt))
        tot = 0.0
        for i in range(0, len(perm), 1024):
            j = perm[i:i + 1024]
            loss = lossf(model(xt[j].to(dev)), yt[j].to(dev))
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += loss.item() * len(j)
        log(f"  epoch {ep}: {tot / len(xt):.4f}")
    return model.eval()


def proba(model: MLP, x: np.ndarray) -> np.ndarray:
    dev = next(model.parameters()).device
    with torch.no_grad():
        return torch.sigmoid(model(torch.from_numpy(x.astype(np.float32)).to(dev))).cpu().numpy()


def metrics(y: np.ndarray, p: np.ndarray) -> dict:
    y, p = y.astype(bool), p.astype(bool)
    tp, fp, fn, tn = (y & p).sum(), (~y & p).sum(), (y & ~p).sum(), (~y & ~p).sum()
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    prec0 = tn / (tn + fn) if tn + fn else 0.0
    rec0 = tn / (tn + fp) if tn + fp else 0.0
    f1_0 = 2 * prec0 * rec0 / (prec0 + rec0) if prec0 + rec0 else 0.0
    return {"n": int(len(y)), "pos_rate": float(y.mean()), "accuracy": float((y == p).mean()), "precision": float(prec),
            "recall": float(rec), "f1": float(f1), "macro_f1": float((f1 + f1_0) / 2),
            "false_occupied_rate": float(fp / (fp + tn)) if fp + tn else 0.0,
            "confusion": {"tp": int(tp), "fp": int(fp), "fn": int(fn), "tn": int(tn)}}
