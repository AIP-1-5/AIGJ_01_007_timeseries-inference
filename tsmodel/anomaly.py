"""과제 4: 이상 탐지 — 정상만 보고 배우는 오토인코더 + 규칙. specs/004-anomaly.

제공 키트(정답 있는 합성 고장)가 없어, 원문 표의 고장 5종을 실데이터 공간 시계열에 직접 심어 정답을 만든다.
검증 기간에 심은 고장으로 임계값을 고르고(선택), 시험 기간에는 다른 시드로 심어 한 번 평가한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import torch
from torch import nn

from tsdata.prep import PrepConfig, period_of
from tsmodel.features import time_features

ITEMS = ["co2", "temperature", "humidity"]
WIN = 12                     # 1시간 창
FAULTS = ["spike", "stuck", "drift", "hvac_stop", "vent_stop"]
FAULT_NAMES = {"spike": "튄 값", "stuck": "값 고정", "drift": "영점 틀어짐", "hvac_stop": "냉방 정지",
               "vent_stop": "환기 정지"}


# --- 고장 심기 -----------------------------------------------------------------

def inject(frame: pd.DataFrame, period_mask: np.ndarray, cfg: PrepConfig, rng: np.random.Generator,
           per_type: int = 4) -> tuple[pd.DataFrame, list[dict]]:
    """frame: 한 공간의 5분 표(columns: co2, temperature, humidity, *_obs). 고장을 심은 사본과 이벤트 목록."""
    f = frame.copy()
    n = len(f)
    tf = time_features(f.index, cfg)
    in_class = tf["in_class"].to_numpy().astype(bool)
    lt = pd.DatetimeIndex(f.index).tz_convert(cfg.tz)
    hour = lt.hour.to_numpy()
    cand_all = np.where(period_mask)[0]
    cand_all = cand_all[(cand_all > 300) & (cand_all < n - 400)]
    events, used = [], np.zeros(n, bool)

    def pick(mask, length):
        c = cand_all[mask[cand_all]]
        for _ in range(200):
            s = int(rng.choice(c))
            if not used[max(0, s - 36):s + length + 36].any() and f["co2"].iloc[s:s + length].notna().all():
                used[s:s + length] = True
                return s
        return None

    for kind in FAULTS:
        for _ in range(per_type):
            if kind == "spike":
                s = pick(np.ones(n, bool), 1)
                if s is None:
                    continue
                item = rng.choice(["co2", "temperature"])
                f.iloc[s, f.columns.get_loc(item)] += 800.0 if item == "co2" else 8.0
                e = s + 1
            elif kind == "stuck":
                length = int(rng.integers(12, 49))                     # 1~4시간
                s = pick(in_class, length)
                if s is None:
                    continue
                item = rng.choice(["co2", "temperature"])
                col = f.columns.get_loc(item)
                f.iloc[s:s + length, col] = f.iloc[s, col]
                e = s + length
            elif kind == "drift":
                length = 288                                           # 24시간(8시간에 걸쳐 +300ppm 뒤 유지)
                s = pick(np.ones(n, bool), length)
                if s is None:
                    continue
                item = "co2"
                ramp = np.minimum(np.arange(length) / 96, 1.0) * 300.0
                f.iloc[s:s + length, f.columns.get_loc("co2")] += ramp
                e = s + length
            elif kind == "hvac_stop":
                length = 36                                            # 수업 중 3시간 동안 +4℃
                s = pick(in_class & (hour < 15), length)
                if s is None:
                    continue
                item = "temperature"
                f.iloc[s:s + length, f.columns.get_loc("temperature")] += np.linspace(0, 4.0, length)
                e = s + length
            else:                                                      # vent_stop: 상승 폭 1.8배
                length = 36
                s = pick(in_class & (hour < 15), length)
                if s is None:
                    continue
                item = "co2"
                col = f.columns.get_loc("co2")
                seg = f.iloc[s:s + length, col].to_numpy()
                base = np.nanmin(f.iloc[max(0, s - 36):s, col].to_numpy()) if s > 0 else seg[0]
                f.iloc[s:s + length, col] = base + (seg - base) * np.linspace(1.0, 1.8, length)
                e = s + length
            events.append({"type": kind, "item": str(item), "start": s, "end": e,
                           "start_time": f.index[s], "end_time": f.index[min(e, n - 1)]})
    return f, events


# --- 입력과 오토인코더 ---------------------------------------------------------

def windows(frame: pd.DataFrame, loc: str, normalizer: dict, cfg: PrepConfig) -> tuple[np.ndarray, np.ndarray]:
    """끝 칸 t마다 [t−11..t] 창(정규화, 앞 값 채움) + 시간 특성. (X [N, 12·3 + 3], 끝 칸 번호)."""
    vals = np.stack([(frame[it].to_numpy() - normalizer["stats"][f"{loc}|{it}"]["mean"])
                     / normalizer["stats"][f"{loc}|{it}"]["std"] for it in ITEMS], axis=1).astype(np.float32)
    vals = pd.DataFrame(vals).ffill().to_numpy(np.float32)
    ends = np.arange(WIN - 1, len(frame))
    idx = ends[:, None] + np.arange(-WIN + 1, 1)[None, :]
    w = vals[idx]                                                         # [N, 12, 3]
    tf = time_features(frame.index[ends], cfg)[["tod_sin", "tod_cos", "workday"]].to_numpy(np.float32)
    x = np.concatenate([w.reshape(len(ends), -1), tf], axis=1)
    return x, ends


class AutoEncoder(nn.Module):
    def __init__(self, n_in: int = WIN * len(ITEMS) + 3, n_out: int = WIN * len(ITEMS)):
        super().__init__()
        self.enc = nn.Sequential(nn.Linear(n_in, 64), nn.ReLU(), nn.Linear(64, 8))
        self.dec = nn.Sequential(nn.Linear(8 + 3, 64), nn.ReLU(), nn.Linear(64, n_out))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.dec(torch.cat([self.enc(x), x[:, -3:]], dim=1))


def train_ae(x: np.ndarray, epochs: int = 15, seed: int = 0, log=print) -> AutoEncoder:
    torch.manual_seed(seed)
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    x = x[~np.isnan(x).any(axis=1)]
    model = AutoEncoder().to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    xt = torch.from_numpy(x)
    for ep in range(epochs):
        perm = torch.randperm(len(xt))
        tot = 0.0
        for i in range(0, len(perm), 1024):
            b = xt[perm[i:i + 1024]].to(dev)
            loss = ((model(b) - b[:, :-3]) ** 2).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += loss.item() * len(b)
        log(f"  AE epoch {ep}: {tot / len(xt):.4f}")
    return model.eval()


def ae_scores(model: AutoEncoder, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(창 전체 오차 [N], 항목별 오차 [N, 3]). NaN 창은 0."""
    dev = next(model.parameters()).device
    bad = np.isnan(x).any(axis=1)
    xx = np.nan_to_num(x)
    outs = []
    with torch.no_grad():
        for i in range(0, len(xx), 8192):
            outs.append(model(torch.from_numpy(xx[i:i + 8192]).to(dev)).cpu().numpy())
    rec = np.concatenate(outs)
    err = (rec - xx[:, :-3]) ** 2
    per_item = err.reshape(len(x), WIN, len(ITEMS)).mean(axis=1)
    per_item[bad] = 0
    return per_item.mean(axis=1), per_item


# --- 규칙 ----------------------------------------------------------------------

def rule_flags(frame: pd.DataFrame, cfg: PrepConfig, ref: dict) -> pd.DataFrame:
    """칸별 규칙 경보(bool). ref: 학습 기간에서 구한 기준값(rule_reference)."""
    co2, temp = frame["co2"], frame["temperature"]
    tf = time_features(frame.index, cfg)
    hour = pd.DatetimeIndex(frame.index).tz_convert(cfg.tz).hour.to_numpy()
    out = pd.DataFrame(index=frame.index)
    out["spike"] = (co2.diff().abs() > ref["spike_co2"]) | (temp.diff().abs() > ref["spike_temp"])
    # 값 고정: 1시간 동안 값이 완전히 같음(CO₂), 온도는 0.1℃ 단위라 낮에만
    out["stuck"] = (co2.rolling(WIN).std() == 0) | ((temp.rolling(WIN).std() == 0) & tf["in_class"].astype(bool).to_numpy())
    # 영점 틀어짐: 새벽(03~06시) CO₂가 학습 기간 새벽 수준보다 높음
    dawn = (hour >= 3) & (hour < 6)
    out["drift"] = dawn & (co2 > ref["dawn_co2"] + ref["drift_margin"])
    # 냉방 정지: 2시간 온도 상승이 학습 기간 99.9% 분위수보다 큼
    out["hvac"] = temp.diff(24) > ref["temp_rise_2h"]
    # 환기 정지: CO₂가 그 시간대 학습 기간 99.9% 분위수보다 높음
    out["vent"] = co2.to_numpy() > np.array([ref["co2_hour_q"].get(h, np.inf) for h in hour])
    return out.fillna(False)


def rule_reference(frame: pd.DataFrame, train_mask: np.ndarray, cfg: PrepConfig) -> dict:
    tr = frame[train_mask]
    hour = pd.DatetimeIndex(tr.index).tz_convert(cfg.tz).hour
    dawn = tr["co2"][(hour >= 3) & (hour < 6)]
    return {"spike_co2": float(tr["co2"].diff().abs().quantile(0.9999)) + 50,
            "spike_temp": float(tr["temperature"].diff().abs().quantile(0.9999)) + 0.5,
            "dawn_co2": float(dawn.quantile(0.99)), "drift_margin": 100.0,
            "temp_rise_2h": float(tr["temperature"].diff(24).quantile(0.999)),
            "co2_hour_q": {int(h): float(v) for h, v in tr["co2"].groupby(hour).quantile(0.999).items()}}


# --- 이벤트 단위 평가 -----------------------------------------------------------

def alarm_events(flags: np.ndarray, merge_gap: int = 6) -> list[tuple[int, int]]:
    """칸별 경보 → 경보 이벤트 [(시작, 끝)], merge_gap칸(30분) 안의 경보는 하나로 묶는다(중복 억제)."""
    idx = np.where(flags)[0]
    if not len(idx):
        return []
    out, s, p = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - p > merge_gap:
            out.append((s, p))
            s = i
        p = i
    out.append((s, p))
    return out


def evaluate_events(flags: np.ndarray, events: list[dict], period_mask: np.ndarray, tol: int = 6) -> dict:
    """탐지율·지연(분)·헛경보 수. 고장 [시작, 끝+30분] 안의 경보면 탐지, 어떤 고장과도 안 겹치는 경보 이벤트는 헛경보."""
    flags = flags & period_mask
    alarms = alarm_events(flags)
    det = []
    for e in events:
        hit = np.where(flags[e["start"]:e["end"] + tol])[0]
        det.append({**e, "detected": bool(len(hit)), "delay_min": int(hit[0]) * 5 if len(hit) else None})
    windows_ = [(e["start"], e["end"] + tol) for e in events]
    fa = [a for a in alarms if not any(a[0] <= w1 and a[1] >= w0 for w0, w1 in windows_)]
    days = period_mask.sum() / 288
    return {"events": det, "false_alarms": len(fa), "days": float(days), "fa_per_day": len(fa) / days if days else 0}
