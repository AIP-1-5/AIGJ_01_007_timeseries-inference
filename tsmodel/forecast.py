"""과제 3: 60분(5분 × 12칸) CO₂·온도 예측 GRU. specs/003-forecast-60min.

입력(기준 시각 t):
  seq    [24칸, 6]  과거 2시간의 CO₂·온도·습도(학습 기간 정규화)와 관측 여부
  static [S]        공간 one-hot + 시간·일정 특성(t, t+30분, t+60분 — 미리 아는 정보)
출력: [24] = CO₂ 12칸 + 온도 12칸의 "현재값 대비 변화량"(정규화 단위). 0이면 현재값 유지와 같다.
과제 7(변환)·8(서비스)이 같은 클래스와 build_inputs를 쓴다.
"""

from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd
import torch
from torch import nn

from tsdata.prep import PrepConfig, period_of
from tsmodel.features import time_features

ITEMS = ["co2", "temperature", "humidity"]
TARGETS = ["co2", "temperature"]
SEQ_LEN = 24          # 2시간
HORIZON = 12          # 60분
FUTURE_OFFSETS = (0, 6, 12)   # 시간·일정 특성을 붙일 칸: t, t+30분, t+60분


class ForecastGRU(nn.Module):
    def __init__(self, n_seq: int, n_static: int, hidden: int = 64, n_out: int = HORIZON * len(TARGETS)):
        super().__init__()
        self.gru = nn.GRU(n_seq, hidden, batch_first=True)
        self.head = nn.Sequential(nn.Linear(hidden + n_static, 128), nn.ReLU(), nn.Linear(128, n_out))

    def forward(self, seq: torch.Tensor, static: torch.Tensor) -> torch.Tensor:
        _, h = self.gru(seq)
        return self.head(torch.cat([h[-1], static], dim=1))


# --- 입력 만들기 ---------------------------------------------------------------

def space_frame(series: pd.DataFrame, loc: str, normalizer: dict) -> pd.DataFrame:
    """한 공간의 5분 격자 표: 원래 값, 정규화 값, 관측 여부."""
    g = series[series["location"] == loc]
    wide = g.pivot(index="time", columns="item", values="value").reindex(columns=ITEMS)
    obs = g.pivot(index="time", columns="item", values="observed").reindex(columns=ITEMS).fillna(False)
    out = pd.DataFrame(index=wide.index)
    for it in ITEMS:
        st = normalizer["stats"].get(f"{loc}|{it}", {"mean": 0.0, "std": 1.0})
        out[it] = wide[it]
        out[f"{it}_n"] = (wide[it] - st["mean"]) / st["std"]
        out[f"{it}_obs"] = obs[it].astype(bool)
    return out.sort_index()


def build_inputs(frame: pd.DataFrame, loc: str, spaces: list[str], cfg: PrepConfig, schedule: bool,
                 anchors: np.ndarray | None = None) -> dict:
    """기준 시각(anchors = frame 행 번호)마다 seq, static, 현재값, 정답(있으면)을 만든다.

    입력 창의 결측은 창 안에서 앞 값으로 채우고, 그래도 없으면 0(학습 평균)으로 둔다. 관측 여부를 함께 넣는다.
    """
    n = len(frame)
    if anchors is None:
        anchors = np.arange(SEQ_LEN - 1, n)
    vals = frame[[f"{it}_n" for it in ITEMS]].to_numpy(np.float32)
    obs = frame[[f"{it}_obs" for it in ITEMS]].to_numpy(np.float32)
    idx = anchors[:, None] + np.arange(-SEQ_LEN + 1, 1)[None, :]           # [N, 24]
    win = vals[idx]                                                        # [N, 24, 3]
    win = pd.DataFrame(win.reshape(-1, len(ITEMS))).groupby(np.repeat(np.arange(len(anchors)), SEQ_LEN)).ffill() \
        .to_numpy(np.float32).reshape(len(anchors), SEQ_LEN, len(ITEMS))
    win = np.nan_to_num(win, nan=0.0)
    seq = np.concatenate([win, obs[idx]], axis=2)

    times = frame.index
    feats = []
    step = pd.Timedelta(minutes=cfg.step_min)
    for off in FUTURE_OFFSETS:
        t = pd.DatetimeIndex(times[anchors]) + off * step
        feats.append(time_features(t, cfg, schedule=schedule).to_numpy(np.float32))
    onehot = np.zeros((len(anchors), len(spaces)), np.float32)
    onehot[:, spaces.index(loc)] = 1
    static = np.concatenate([onehot] + feats, axis=1)

    cur = frame[TARGETS].to_numpy(np.float32)[anchors]                     # [N, 2]
    fut_idx = anchors[:, None] + np.arange(1, HORIZON + 1)[None, :]
    valid_fut = fut_idx < n
    fut_idx = np.where(valid_fut, fut_idx, n - 1)
    fut = frame[TARGETS].to_numpy(np.float32)[fut_idx]                     # [N, 12, 2]
    fut_obs = frame[[f"{t}_obs" for t in TARGETS]].to_numpy(bool)[fut_idx] & valid_fut[:, :, None]
    return {"seq": seq, "static": static, "current": cur, "future": fut, "future_obs": fut_obs,
            "times": times[anchors]}


def target_delta(inp: dict, stds: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """정답 변화량(정규화 단위) [N, 24]과 마스크. 정답이 실제 관측이고 현재값이 있을 때만 학습에 쓴다."""
    d = (inp["future"] - inp["current"][:, None, :]) / stds[None, None, :]   # [N, 12, 2]
    m = inp["future_obs"] & ~np.isnan(d) & ~np.isnan(inp["current"])[:, None, :]
    d = np.nan_to_num(d, nan=0.0)
    return d.transpose(0, 2, 1).reshape(len(d), -1), m.transpose(0, 2, 1).reshape(len(m), -1)


def stds_for(loc: str, normalizer: dict) -> np.ndarray:
    return np.array([normalizer["stats"][f"{loc}|{t}"]["std"] for t in TARGETS], np.float32)


# --- 데이터셋 ------------------------------------------------------------------

def make_dataset(series: pd.DataFrame, normalizer: dict, cfg: PrepConfig, schedule: bool) -> dict:
    """기간별(train/val/test) 텐서. 공간 3곳을 합친다."""
    spaces = cfg["baseline"]["spaces"]
    parts: dict[str, list] = {"train": [], "val": [], "test": []}
    for loc in spaces:
        fr = space_frame(series, loc, normalizer)
        inp = build_inputs(fr, loc, spaces, cfg, schedule)
        y, m = target_delta(inp, stds_for(loc, normalizer))
        per = period_of(pd.Series(inp["times"]), cfg).to_numpy()
        ok = ~np.isnan(inp["current"]).any(axis=1)
        for p in parts:
            sel = (per == p) & ok
            parts[p].append({"seq": inp["seq"][sel], "static": inp["static"][sel], "y": y[sel], "m": m[sel],
                             "current": inp["current"][sel], "times": inp["times"][sel],
                             "loc": np.array([loc] * int(sel.sum()))})
    return {p: {k: np.concatenate([d[k] for d in v]) for k in v[0]} for p, v in parts.items()}


# --- 학습 ----------------------------------------------------------------------

def _device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def predict(model: ForecastGRU, seq: np.ndarray, static: np.ndarray, batch: int = 4096) -> np.ndarray:
    dev = next(model.parameters()).device
    model.eval()
    outs = []
    with torch.no_grad():
        for i in range(0, len(seq), batch):
            outs.append(model(torch.from_numpy(seq[i:i + batch]).to(dev),
                              torch.from_numpy(static[i:i + batch]).to(dev)).cpu().numpy())
    return np.concatenate(outs) if outs else np.zeros((0, HORIZON * len(TARGETS)), np.float32)


def to_values(delta: np.ndarray, current: np.ndarray, locs: np.ndarray, normalizer: dict) -> np.ndarray:
    """변화량(정규화) [N, 24] → 예측값 [N, 12, 2](원래 단위)."""
    stds = np.stack([stds_for(loc, normalizer) for loc in locs])                 # [N, 2]
    d = delta.reshape(len(delta), len(TARGETS), HORIZON).transpose(0, 2, 1)      # [N, 12, 2]
    return current[:, None, :] + d * stds[:, None, :]


def val_mae_60(model, ds: dict, normalizer: dict) -> float:
    """검증 기간 60분 CO₂ MAE(ppm), 정답이 관측인 칸만. 에폭 선택 기준."""
    pred = to_values(predict(model, ds["seq"], ds["static"]), ds["current"], ds["loc"], normalizer)
    y, m = ds["y"].reshape(-1, 2, HORIZON), ds["m"].reshape(-1, 2, HORIZON)
    stds = np.stack([stds_for(loc, normalizer) for loc in ds["loc"]])
    true60 = ds["current"][:, 0] + y[:, 0, -1] * stds[:, 0]
    sel = m[:, 0, -1]
    return float(np.abs(pred[sel, -1, 0] - true60[sel]).mean())


def train(ds: dict, normalizer: dict, epochs: int = 20, patience: int = 4, seed: int = 0, lr: float = 1e-3,
          batch: int = 512, log=print) -> tuple[ForecastGRU, list[dict]]:
    torch.manual_seed(seed)
    np.random.seed(seed)
    dev = _device()
    tr, va = ds["train"], ds["val"]
    model = ForecastGRU(tr["seq"].shape[2], tr["static"].shape[1]).to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    seq, st = torch.from_numpy(tr["seq"]), torch.from_numpy(tr["static"])
    y, m = torch.from_numpy(tr["y"]), torch.from_numpy(tr["m"].astype(np.float32))
    best, best_state, history, bad = float("inf"), None, [], 0
    for ep in range(epochs):
        t0 = time.time()
        model.train()
        perm = torch.randperm(len(seq))
        total = 0.0
        for i in range(0, len(perm), batch):
            j = perm[i:i + batch]
            out = model(seq[j].to(dev), st[j].to(dev))
            mm = m[j].to(dev)
            loss = ((out - y[j].to(dev)).abs() * mm).sum() / mm.sum().clamp(min=1)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item() * len(j)
        v = val_mae_60(model, va, normalizer)
        history.append({"epoch": ep, "train_loss": total / len(perm), "val_mae60_co2": v, "sec": time.time() - t0})
        log(f"  epoch {ep}: train {total / len(perm):.4f}, val 60분 CO₂ MAE {v:.2f}ppm ({time.time() - t0:.0f}s)")
        if v < best - 1e-3:
            best, bad = v, 0
            best_state = {k: t.detach().cpu().clone() for k, t in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    model.load_state_dict(best_state)
    return model, history


def save(model: ForecastGRU, path: str, meta: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "meta": meta}, path)
    with open(path.replace(".pt", ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def load(path: str, device: str = "cpu") -> tuple[ForecastGRU, dict]:
    ck = torch.load(path, map_location=device, weights_only=False)
    meta = ck["meta"]
    model = ForecastGRU(meta["n_seq"], meta["n_static"], meta.get("hidden", 64))
    model.load_state_dict(ck["state_dict"])
    return model.to(device).eval(), meta


# --- 평가(과제 2 평가 시점) -----------------------------------------------------

def evaluate_on_points(model, series: pd.DataFrame, points: pd.DataFrame, normalizer: dict, cfg: PrepConfig,
                       schedule: bool, period: str, name: str) -> pd.DataFrame:
    """[period, location, item, horizon_min, baseline(=name), mae, n]. 평가 시점은 기준선과 같다."""
    spaces = cfg["baseline"]["spaces"]
    rows = []
    for loc in spaces:
        fr = space_frame(series, loc, normalizer)
        pos = pd.Series(np.arange(len(fr)), index=fr.index)
        pts = points[(points["period"] == period) & (points["location"] == loc)]
        anchors_all = np.unique(pos.reindex(pts["time"]).dropna().astype(int).to_numpy())
        inp = build_inputs(fr, loc, spaces, cfg, schedule, anchors=anchors_all)
        pred = to_values(predict(model, inp["seq"], inp["static"]), inp["current"],
                         np.array([loc] * len(anchors_all)), normalizer)
        at = pd.Series(np.arange(len(anchors_all)), index=fr.index[anchors_all])
        for (item, h), g in pts.groupby(["item", "horizon_min"]):
            k = h // cfg.step_min
            ti = TARGETS.index(item)
            r = at.reindex(g["time"]).to_numpy().astype(int)
            truth = fr[item].to_numpy()[anchors_all[r] + k]
            err = np.abs(pred[r, k - 1, ti] - truth)
            rows.append({"period": period, "location": loc, "item": item, "horizon_min": int(h),
                         "baseline": name, "mae": float(np.nanmean(err)), "n": int(len(err))})
    return pd.DataFrame(rows)
