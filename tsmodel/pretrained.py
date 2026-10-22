"""과제 6: 사전학습 모델(Chronos-Bolt) 비교. specs/006-pretrained.

시험 기간 60분 CO₂, 과제 2 평가 시점에서 공간마다 같은 표본(시드 0)을 뽑아 기준선·GRU·Chronos를 같은 시점으로 비교한다.
Chronos는 CO₂ 과거 값만 받는다(추가 학습 없음). 10~90% 구간 적중률, 1회 추론 시간, 메모리를 잰다.
"""

from __future__ import annotations

import json
import os
import resource
import time

import numpy as np
import pandas as pd
import torch

from tsmodel import forecast as fc

MODEL_ID = "amazon/chronos-bolt-small"
CONTEXT = 512          # 5분 × 512 ≈ 42시간


def run(series, points, norm, cfg, out_dir, per_space: int = 800) -> dict:
    from chronos import BaseChronosPipeline

    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.time()
    pipe = BaseChronosPipeline.from_pretrained(MODEL_ID, device_map="cpu", torch_dtype=torch.float32)
    load_s = time.time() - t0
    gru, _ = fc.load(os.path.expanduser("~/data/tsinfer/models/forecast_gru_schedule.pt"))
    rng = np.random.default_rng(0)
    rows, lat = [], []
    for loc in cfg["baseline"]["spaces"]:
        fr = fc.space_frame(series, loc, norm)
        pos = pd.Series(np.arange(len(fr)), index=fr.index)
        pts = points[(points.period == "test") & (points.location == loc) & (points.item == "co2")
                     & (points.horizon_min == 60)]
        pick = np.sort(rng.choice(len(pts), min(per_space, len(pts)), replace=False))
        anchors = pos.reindex(pts["time"].iloc[pick]).astype(int).to_numpy()
        co2 = fr["co2"].ffill().to_numpy(np.float32)
        truth = fr["co2"].to_numpy()[anchors + 12]
        ctx = [torch.tensor(np.nan_to_num(co2[max(0, a - CONTEXT + 1):a + 1], nan=float(np.nanmean(co2))))
               for a in anchors]
        q, med = [], []
        for i in range(0, len(ctx), 64):
            qq, mm = pipe.predict_quantiles(ctx[i:i + 64], prediction_length=12, quantile_levels=[0.1, 0.5, 0.9])
            q.append(qq[:, -1, :].numpy())
            med.append(mm[:, -1].numpy())
        q, med = np.concatenate(q), np.concatenate(med)
        inp = fc.build_inputs(fr, loc, cfg["baseline"]["spaces"], cfg, True, anchors=anchors)
        g = fc.to_values(fc.predict(gru, inp["seq"], inp["static"]), inp["current"], np.array([loc] * len(anchors)), norm)
        persist = fr["co2"].to_numpy()[anchors]
        rows.append({"location": loc, "n": int(len(anchors)),
                     "persistence": float(np.mean(np.abs(persist - truth))),
                     "gru": float(np.mean(np.abs(g[:, -1, 0] - truth))),
                     "chronos": float(np.mean(np.abs(med - truth))),
                     "interval_coverage": float(np.mean((truth >= q[:, 0]) & (truth <= q[:, 2])))})
        for a in anchors[:30]:                             # 1회(배치 1) 추론 시간
            c = torch.tensor(co2[max(0, a - CONTEXT + 1):a + 1])
            s = time.perf_counter()
            pipe.predict_quantiles([c], prediction_length=12, quantile_levels=[0.1, 0.5, 0.9])
            lat.append((time.perf_counter() - s) * 1000)
    lat = np.array(lat[5:])                                # 앞 5회는 워밍업
    res = {"model": MODEL_ID, "context_steps": CONTEXT, "device": "cpu", "load_s": load_s, "rows": rows,
           "latency_ms_mean": float(lat.mean()), "latency_ms_p95": float(np.percentile(lat, 95)), "latency_runs": len(lat),
           "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
           "rss_before_mb": rss0 / 1024, "params_m": sum(p.numel() for p in pipe.inner_model.parameters()) / 1e6}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "pretrained_results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    return res
