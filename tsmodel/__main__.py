"""과제 3~ 실행: python -m tsmodel forecast [--epochs N]

forecast  과제 3: GRU(일정 포함/제외) 학습 → 과제 2 평가 시점에서 기준선과 비교 → docs/reports/003-forecast-60min/
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

from tsdata.__main__ import conditions
from tsdata.baselines import LABELS
from tsdata.prep import REPO, load_config

MODEL_DIR = os.path.expanduser("~/data/tsinfer/models")
ITEM_NAMES = {"co2": "CO₂", "temperature": "온도"}


def load_prep(cfg):
    out = cfg.path_of("out_dir")
    series = pd.read_parquet(os.path.join(out, "series.parquet"))
    points = pd.read_parquet(os.path.join(out, "eval_points.parquet"))
    with open(os.path.join(out, "prep_params.json"), encoding="utf-8") as f:
        params = json.load(f)
    base = pd.read_csv(os.path.join(out, "baseline_perf.csv"))
    return series, points, params["normalizer"], base


def cmd_forecast(cfg, epochs: int) -> None:
    import torch

    from tsmodel import forecast as fc

    series, points, norm, base = load_prep(cfg)
    rep = REPO / "docs" / "reports" / "003-forecast-60min"
    rep.mkdir(parents=True, exist_ok=True)
    perf = [base]
    histories = {}
    for variant, schedule in (("gru_schedule", True), ("gru_no_schedule", False)):
        print(f"[{variant}] 데이터셋", flush=True)
        ds = fc.make_dataset(series, norm, cfg, schedule, lags=True)
        print(f"  학습 {len(ds['train']['seq'])}, 검증 {len(ds['val']['seq'])} (시험은 평가에만)", flush=True)
        model, hist = fc.train(ds, norm, epochs=epochs, log=lambda s: print(s, flush=True))
        histories[variant] = hist
        meta = {"variant": variant, "schedule": schedule, "n_seq": ds["train"]["seq"].shape[2],
                "n_static": ds["train"]["static"].shape[1], "hidden": 64, "seq_len": fc.SEQ_LEN,
                "horizon": fc.HORIZON, "spaces": cfg["baseline"]["spaces"], "config_sha": cfg.sha,
                "lags": True, "best_val_mae60_co2": min(h["val_mae60_co2"] for h in hist), "seed": 0,
                "device": str(next(model.parameters()).device), "torch": torch.__version__}
        fc.save(model, os.path.join(MODEL_DIR, f"forecast_{variant}_v2.pt"), meta)   # 서비스(v1 사용 중)와 겹치지 않게
        for period in ("val", "test"):
            perf.append(fc.evaluate_on_points(model, series, points, norm, cfg, schedule, period, variant, lags=True))
        del ds
    perf = pd.concat(perf, ignore_index=True)
    perf.to_csv(os.path.join(cfg.path_of("out_dir"), "forecast_perf.csv"), index=False)
    with open(rep / "training_history.json", "w", encoding="utf-8") as f:
        json.dump(histories, f, ensure_ascii=False, indent=2)
    write_table(cfg, perf, rep)
    print(f"비교표: {rep / 'comparison.md'}")


NAMES = {**LABELS, "gru_schedule": "GRU(일정 포함)", "gru_no_schedule": "GRU(일정 제외)"}
ORDER = ["persistence", "mean_1h", "yesterday", "gru_no_schedule", "gru_schedule"]


def write_table(cfg, perf: pd.DataFrame, rep) -> None:
    c = conditions(cfg)
    L = ["# 모델 비교표 (과제 3 D-1)", "", "| 측정 조건 | 값 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in c.items()]
    L += ["| 모델 | GRU 1층(은닉 64), 입력 2시간 + 어제 같은 시각 값, 60분 한 번에 출력(변화량), 시드 0, 학습 기간 학습·검증 기간 조기 종료 |", "",
          "MAE(CO₂ ppm, 온도 ℃). 같은 기간·공간·항목·거리 안에서 모든 방법이 과제 2의 **같은 평가 시점**을 쓴다.", ""]
    for period in ("test", "val"):
        L += [f"## {'시험' if period == 'test' else '검증'} 기간", "",
              "| 공간 | 항목 | 거리 | " + " | ".join(NAMES[n] for n in ORDER) + " | GRU(일정) / 현재값 유지 | 시점 |",
              "|---|---|---|" + "---|" * len(ORDER) + "---|---|"]
        sub = perf[perf["period"] == period]
        for (loc, item, h), g in sub.groupby(["location", "item", "horizon_min"]):
            m = g.set_index("baseline")["mae"]
            best = m.idxmin()
            fmt = (lambda x: f"{x:.1f}") if item == "co2" else (lambda x: f"{x:.3f}")
            cells = [f"**{fmt(m[n])}**" if n == best else fmt(m[n]) for n in ORDER]
            ratio = m["gru_schedule"] / m["persistence"]
            L.append(f"| {loc} | {ITEM_NAMES[item]} | {h}분 | " + " | ".join(cells)
                     + f" | {ratio:.0%} | {int(g['n'].iloc[0])} |")
        L.append("")
    (rep / "comparison.md").write_text("\n".join(L), encoding="utf-8")


def _save(model, path, meta):
    import torch
    os.makedirs(os.path.dirname(path), exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "meta": meta}, path)
    with open(path.replace(".pt", ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=str)


def cmd_anomaly(cfg) -> None:
    import numpy as np

    from tsdata.prep import period_of
    from tsmodel import anomaly as an
    from tsmodel.forecast import space_frame

    series, _, norm, _ = load_prep(cfg)
    spaces = cfg["baseline"]["spaces"]
    rep = REPO / "docs" / "reports" / "004-anomaly"
    rep.mkdir(parents=True, exist_ok=True)
    frames = {loc: space_frame(series, loc, norm) for loc in spaces}
    per = {loc: period_of(pd.Series(fr.index), cfg).to_numpy() for loc, fr in frames.items()}

    xs = []
    for loc, fr in frames.items():
        x, ends = an.windows(fr, loc, norm, cfg)
        xs.append(x[per[loc][ends] == "train"])
    print("[AE] 학습(학습 기간, 고장 안 심음)", flush=True)
    ae = an.train_ae(np.concatenate(xs), log=lambda s: print(s, flush=True))
    refs = {loc: an.rule_reference(fr, per[loc] == "train", cfg) for loc, fr in frames.items()}

    def run(period, seed):
        rng = np.random.default_rng(seed)
        out = {}
        for loc, fr in frames.items():
            mask = per[loc] == period
            inj, events = an.inject(fr, mask, cfg, rng)
            x, ends = an.windows(inj, loc, norm, cfg)
            score, items = an.ae_scores(ae, x)
            s_full = np.zeros(len(inj))
            s_full[ends] = score
            it_full = np.zeros((len(inj), 3))
            it_full[ends] = items
            rules = an.rule_flags(inj, cfg, refs[loc])
            out[loc] = {"mask": mask, "events": events, "score": s_full, "items": it_full, "rules": rules}
        return out

    val, test = run("val", 1), run("test", 2)

    def total(res, thr, use_model=True, use_rules=True):
        det, fa, days, rows = 0, 0, 0.0, []
        for loc, r in res.items():
            flags = np.zeros(len(r["score"]), bool)
            if use_model:
                flags |= r["score"] > thr
            if use_rules:
                flags |= r["rules"].any(axis=1).to_numpy()
            ev = an.evaluate_events(flags, r["events"], r["mask"])
            det += sum(e["detected"] for e in ev["events"])
            fa += ev["false_alarms"]
            days = ev["days"]
            rows += [{**e, "location": loc} for e in ev["events"]]
        n = sum(len(r["events"]) for r in res.values())
        return {"detect_rate": det / n, "fa_per_day": fa / days, "fa": fa, "days": days, "events": rows, "n": n}

    # 임계값: 검증 기간(심은 고장)에서, 결합 헛경보 하루 0.7회 이하 중 탐지율 최대
    val_scores = np.concatenate([r["score"][r["mask"]] for r in val.values()])
    sweep = []
    for q in (0.99, 0.995, 0.998, 0.999, 0.9995, 0.9998, 0.9999):
        thr = float(np.quantile(val_scores[val_scores > 0], q))
        m, c = total(val, thr, use_rules=False), total(val, thr)
        sweep.append({"quantile": q, "threshold": thr, "model_detect": m["detect_rate"], "model_fa": m["fa_per_day"],
                      "combined_detect": c["detect_rate"], "combined_fa": c["fa_per_day"]})
    ok = [s for s in sweep if s["combined_fa"] <= 0.7] or [min(sweep, key=lambda s: s["combined_fa"])]
    chosen = max(ok, key=lambda s: (s["combined_detect"], s["quantile"]))
    thr = chosen["threshold"]
    print(f"[AE] 임계값 {thr:.4f} (검증 분위수 {chosen['quantile']})", flush=True)

    res = {"model": total(test, thr, use_rules=False), "rule": total(test, thr, use_model=False),
           "combined": total(test, thr)}
    test_sweep = [{"quantile": s["quantile"], "threshold": s["threshold"],
                   **{f"{k}_{m}": v for k in ("model", "combined")
                      for m, v in (("detect", total(test, s["threshold"], use_rules=(k == "combined"))["detect_rate"]),
                                   ("fa", total(test, s["threshold"], use_rules=(k == "combined"))["fa_per_day"]))}}
                  for s in sweep]
    # 원인 가리키기: 모델이 잡은 고장에서 항목별 오차가 가장 큰 항목 = 심은 항목인가
    cause_hit, cause_n = 0, 0
    for loc, r in test.items():
        for e in r["events"]:
            seg = r["items"][e["start"]:e["end"] + 6]
            flag = r["score"][e["start"]:e["end"] + 6] > thr
            if flag.any():
                guess = an.ITEMS[int(seg[flag].sum(axis=0).argmax())]
                cause_hit += guess == e["item"]
                cause_n += 1
    _save(ae, os.path.join(MODEL_DIR, "anomaly_ae.pt"),
          {"threshold": thr, "rule_ref": refs, "spaces": spaces, "win": an.WIN, "config_sha": cfg.sha})
    summary = {"threshold": thr, "chosen": chosen, "val_sweep": sweep, "test_sweep": test_sweep,
               "cause": {"hit": cause_hit, "n": cause_n},
               "test": {k: {kk: vv for kk, vv in v.items() if kk != "events"} for k, v in res.items()},
               "events": {k: v["events"] for k, v in res.items()}}
    with open(rep / "anomaly_results.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    for k, v in res.items():
        print(f"{k}: 탐지율 {v['detect_rate']:.0%} ({v['n']}건), 헛경보 {v['fa_per_day']:.2f}/일", flush=True)
    print(f"원인 항목 일치 {cause_hit}/{cause_n}")


def cmd_state(cfg) -> None:
    import numpy as np

    from tsdata.prep import period_of
    from tsmodel import state as stt
    from tsmodel.forecast import space_frame

    series, _, norm, _ = load_prep(cfg)
    rep = REPO / "docs" / "reports" / "005-state"
    rep.mkdir(parents=True, exist_ok=True)
    out = {}

    def split(df):
        per = period_of(pd.Series(df.index), cfg).to_numpy()
        return {p: df[per == p] for p in ("train", "val", "test")}

    # 사용 여부(실습실, 일정 기반 정답, 입력에 일정·시각 없음)
    fr = space_frame(series, "실습실", norm)
    occ = stt.occupancy_dataset(fr, "실습실", norm, cfg)
    feats = [c for c in occ.columns if c not in ("label", "rule", "observed")]
    occ = occ[occ["observed"].astype(bool) & occ[feats].notna().all(axis=1)]
    sp = split(occ)
    print("[사용 여부] 학습", len(sp["train"]), flush=True)
    m_occ = stt.train_mlp(sp["train"][feats].to_numpy(), sp["train"]["label"].to_numpy(),
                          log=lambda s: print(s, flush=True))
    pv = stt.proba(m_occ, sp["val"][feats].to_numpy())
    thr_occ = max(np.arange(0.1, 0.95, 0.05),
                  key=lambda t: stt.metrics(sp["val"]["label"].to_numpy(), pv > t)["macro_f1"])
    pt = stt.proba(m_occ, sp["test"][feats].to_numpy())
    out["occupancy"] = {"threshold": float(thr_occ),
                        "model": stt.metrics(sp["test"]["label"].to_numpy(), pt > thr_occ),
                        "rule": stt.metrics(sp["test"]["label"].to_numpy(), sp["test"]["rule"].to_numpy() > 0)}
    te = sp["test"].assign(pred=(pt > thr_occ).astype(int))
    lt = pd.DatetimeIndex(te.index).tz_convert(cfg.tz)
    te = te.assign(hour=lt.hour, err=(te["pred"] != te["label"]).astype(int))
    out["occupancy"]["error_by_hour"] = te.groupby("hour")["err"].mean().round(3).to_dict()
    _save(m_occ, os.path.join(MODEL_DIR, "state_occupancy.pt"),
          {"features": feats, "threshold": float(thr_occ), "n_in": len(feats), "spaces": ["실습실"]})

    # 환기 선제 경보(3공간)
    parts = []
    for loc in cfg["baseline"]["spaces"]:
        f = space_frame(series, loc, norm)
        d = stt.vent_dataset(f, loc, norm, cfg)
        d["location"] = loc
        parts.append(d)
    vent = pd.concat(parts)
    vfeats = [c for c in vent.columns if c not in ("label", "rule", "co2_now", "usable", "location")]
    vent = vent[vent["usable"].astype(bool) & vent[vfeats].notna().all(axis=1)]
    sp = split(vent)
    print("[환기 경보] 학습", len(sp["train"]), "양성 비율", round(sp["train"]["label"].mean(), 4), flush=True)
    m_v = stt.train_mlp(sp["train"][vfeats].to_numpy(), sp["train"]["label"].to_numpy(),
                        log=lambda s: print(s, flush=True))
    pv = stt.proba(m_v, sp["val"][vfeats].to_numpy())
    yv = sp["val"]["label"].to_numpy()
    cands = [t for t in np.arange(0.05, 0.96, 0.01) if stt.metrics(yv, pv > t)["recall"] >= 0.88]
    thr_v = float(max(cands)) if cands else 0.05          # 검증 재현율 88% 이상 중 가장 높은 임계값(여유 3%p)
    pt = stt.proba(m_v, sp["test"][vfeats].to_numpy())
    yt = sp["test"]["label"].to_numpy()
    out["vent"] = {"threshold": thr_v, "model": stt.metrics(yt, pt > thr_v),
                   "rule": stt.metrics(yt, sp["test"]["rule"].to_numpy() > 0),
                   "accuracy_all_negative": float(1 - yt.mean()),
                   "by_space": {loc: {"model": stt.metrics(yt[g], (pt > thr_v)[g]),
                                      "rule": stt.metrics(yt[g], sp["test"]["rule"].to_numpy()[g] > 0)}
                                for loc in cfg["baseline"]["spaces"]
                                for g in [(sp["test"]["location"] == loc).to_numpy()]}}
    _save(m_v, os.path.join(MODEL_DIR, "state_vent.pt"),
          {"features": vfeats, "threshold": thr_v, "n_in": len(vfeats), "spaces": cfg["baseline"]["spaces"]})
    with open(rep / "state_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    for k in ("occupancy", "vent"):
        print(k, {m: {kk: round(vv, 3) for kk, vv in out[k][m].items() if isinstance(vv, float)}
                  for m in ("model", "rule")}, flush=True)


def cmd_pretrained(cfg) -> None:
    from tsmodel import pretrained

    series, points, norm, _ = load_prep(cfg)
    r = pretrained.run(series, points, norm, cfg, str(REPO / "docs" / "reports" / "006-pretrained"))
    print(json.dumps({k: v for k, v in r.items() if k != "rows"}, ensure_ascii=False))
    for row in r["rows"]:
        print(row)


def cmd_edge(cfg) -> None:
    import math

    import numpy as np
    import torch

    from tsdata.prep import period_of
    from tsmodel import anomaly as an
    from tsmodel import edge
    from tsmodel import forecast as fc
    from tsmodel import state as stt

    series, _, norm, _ = load_prep(cfg)
    loc = "실습실"
    fr = fc.space_frame(series, loc, norm)
    per = period_of(pd.Series(fr.index), cfg).to_numpy()
    test_idx = np.where((per == "test") & fr["co2"].notna().to_numpy())[0]
    anchors = test_idx[(test_idx >= fc.SEQ_LEN) & (test_idx < len(fr) - 13)][::20][:400]
    inp = fc.build_inputs(fr, loc, cfg["baseline"]["spaces"], cfg, True, anchors=anchors)
    x_ae, ends = an.windows(fr, loc, norm, cfg)
    x_ae = np.nan_to_num(x_ae[np.isin(ends, anchors)]).astype(np.float32)
    meta = torch.load(os.path.join(MODEL_DIR, "state_vent.pt"), weights_only=False)["meta"]
    vd = stt.vent_dataset(fr, loc, norm, cfg)
    vd = vd[(period_of(pd.Series(vd.index), cfg).to_numpy() == "test")].dropna(subset=meta["features"])
    x_v = vd[meta["features"]].to_numpy(np.float32)[:4000]
    samples = {"forecast": [inp["seq"], inp["static"]], "anomaly": [x_ae], "vent": [x_v]}
    thr = meta["threshold"]
    scale = {"forecast": norm["stats"][f"{loc}|co2"]["std"], "vent_logit": math.log(thr / (1 - thr))}
    r = edge.benchmark(samples, scale, str(REPO / "docs" / "reports" / "007-edge"))
    for row in r["rows"]:
        print(row)
    print(json.dumps(r["diffs"], ensure_ascii=False))
    print(json.dumps(r["tegrastats"], ensure_ascii=False))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tsmodel")
    p.add_argument("command", choices=["forecast", "anomaly", "state", "pretrained", "edge"])
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--config", default=str(REPO / "config" / "prep.toml"))
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    if a.command == "forecast":
        cmd_forecast(cfg, a.epochs)
    elif a.command == "anomaly":
        cmd_anomaly(cfg)
    elif a.command == "state":
        cmd_state(cfg)
    elif a.command == "pretrained":
        cmd_pretrained(cfg)
    elif a.command == "edge":
        cmd_edge(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
