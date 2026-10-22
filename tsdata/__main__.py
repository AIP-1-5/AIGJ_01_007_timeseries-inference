"""과제 2 실행: python -m tsdata {prep, baseline, diagnose, all} [--config config/prep.toml]

prep      스냅샷 → 공간 시계열·정규화 파라미터 (~/data/tsinfer/prep/)
baseline  기준선 성능표(D-2)와 평가 시점 저장
diagnose  진단 수치 부록과 그래프 (docs/reports/002-data-baseline/)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import pandas as pd

from . import diagnose as dg
from .baselines import LABELS, best_table, evaluate
from .guard import refuse_on_board
from .prep import REPO, build, load_config

REPORT_DIR = REPO / "docs" / "reports" / "002-data-baseline"
PERIOD_NAMES = {"train": "학습", "val": "검증", "test": "시험"}
ITEM_NAMES = {"co2": "CO₂", "temperature": "온도", "humidity": "습도"}


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=REPO).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def conditions(cfg) -> dict:
    commit = _run(["git", "rev-parse", "--short", "HEAD"]) or "확인 못 함"
    if _run(["git", "status", "--porcelain", "--untracked-files=no"]):
        commit += " + 커밋 안 된 변경"
    power = next((ln.split(":", 1)[1].strip() for ln in _run(["nvpmodel", "-q"]).splitlines()
                  if ln.startswith("NV Power Mode")), "확인 못 함")
    p = cfg["periods"]
    return {"데이터": "실데이터 (InfluxDB iot-bucket 스냅샷, 2026-10-21 내려받음)",
            "기간(KST)": f"학습 {p['train'][0]} ~ {p['train'][1]} · 검증 {p['val'][0]} ~ {p['val'][1]} · "
                       f"시험 {p['test'][0]} ~ {p['test'][1]}",
            "분석 간격": f"{cfg.step_min}분, 센서별 마지막 값 유지 최대 {cfg['gaps']['max_fill_steps']}칸 → 공간 "
                     f"{cfg['resample']['space_aggregate']}",
            "보드": f"보드 B (전원 모드 {power})", "코드 커밋": commit,
            "설정": f"`config/prep.toml` (sha256 `{cfg.sha}`)"}


def caption(cfg) -> str:
    c = conditions(cfg)
    return f"실데이터 · {c['기간(KST)']} · {c['보드']} · 커밋 {c['코드 커밋']} · 설정 {cfg.sha}"


def cond_table(cfg) -> list[str]:
    return ["| 측정 조건 | 값 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in conditions(cfg).items()]


def load_prep(cfg):
    out = cfg.path_of("out_dir")
    return pd.read_parquet(os.path.join(out, "series.parquet")), pd.read_csv(os.path.join(out, "removed.csv"))


def cmd_prep(cfg) -> None:
    r = build(cfg)
    print(f"원본 {r['raw_rows']}행 → 정제 {len(r['cleaned'])}행, 공간 시계열 {len(r['series'])}행. "
          f"저장: {cfg.path_of('out_dir')}")


def cmd_baseline(cfg) -> None:
    series, _ = load_prep(cfg)
    perf, points = evaluate(series, cfg)
    out = cfg.path_of("out_dir")
    perf.to_csv(os.path.join(out, "baseline_perf.csv"), index=False)
    points.to_parquet(os.path.join(out, "eval_points.parquet"), index=False)
    best = best_table(perf)
    lines = ["# 기준선 성능표 (D-2)", "", *cond_table(cfg), "",
             "MAE = 평균 절대 오차(CO₂ ppm, 온도 ℃). 같은 기간·공간·항목·거리 안에서 세 기준선은 **같은 평가 시점**을 쓴다"
             "(정답이 실제 관측인 칸만). 평가 시점 목록: `~/data/tsinfer/prep/eval_points.parquet` — 과제 3 이후 모델도 이 목록으로 평가한다.",
             "", "기준선: 현재값 유지 ŷ(t+h)=y(t) · 최근 1시간 평균 ŷ(t+h)=mean(y[t−55분..t]) · 어제 같은 시각 ŷ(t+h)=y(t+h−24시간)", ""]
    for period in ("test", "val"):
        lines += [f"## {PERIOD_NAMES[period]} 기간", "",
                  "| 공간 | 항목 | 거리 | 현재값 유지 | 최근 1시간 평균 | 어제 같은 시각 | 최강 | 평가 시점 |",
                  "|---|---|---|---|---|---|---|---|"]
        sub = perf[perf["period"] == period]
        for (loc, item, h), g in sub.groupby(["location", "item", "horizon_min"], sort=True):
            m = g.set_index("baseline")["mae"]
            win = m.idxmin()
            fmt = (lambda x: f"{x:.1f}") if item == "co2" else (lambda x: f"{x:.2f}")
            cells = [f"**{fmt(m[b])}**" if b == win else fmt(m[b]) for b in ("persistence", "mean_1h", "yesterday")]
            lines.append(f"| {loc} | {ITEM_NAMES[item]} | {h}분 | " + " | ".join(cells)
                         + f" | {LABELS[win]} | {int(g['n'].iloc[0])} |")
        lines.append("")
    t60 = perf[(perf["period"] == "test") & (perf["item"] == "co2") & (perf["horizon_min"] == 60)
               & (perf["baseline"] == "persistence")]
    lines += ["## 과제 3 목표선 (시험 기간 60분 CO₂, 현재값 유지보다 30% 이상 작게)", "",
              "| 공간 | 현재값 유지 MAE | 목표(× 0.7) |", "|---|---|---|"]
    lines += [f"| {r.location} | {r.mae:.1f} | **{r.mae * 0.7:.1f}** |" for r in t60.itertuples()]
    wins = best["baseline"].value_counts().to_dict()
    lines += ["", f"최강 기준선 집계(기간·공간·항목·거리 {len(best)}칸): "
              + ", ".join(f"{LABELS[k]} {v}칸" for k, v in wins.items()), ""]
    (REPORT_DIR / "baseline.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"성능표: {REPORT_DIR / 'baseline.md'}")


def cmd_diagnose(cfg) -> None:
    series, removed = load_prep(cfg)
    out = cfg.path_of("out_dir")
    points = pd.read_parquet(os.path.join(out, "eval_points.parquet"))
    fig_dir = REPORT_DIR / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    cap = caption(cfg)
    sc = cfg["schedule"]

    miss = dg.missing_table(series, removed, cfg)
    monthly = dg.monthly_observed(series, cfg)
    prof = dg.weekday_profile(series, cfg, "co2")
    events = {loc: dg.profile_events(prof[loc].dropna(), cfg) for loc in prof.columns}
    days = dg.daily_reactions(series, cfg)
    auto = dg.autocorr_table(series, cfg)
    hours = dg.error_by_hour(series, points, cfg, "실습실", "co2", 60, "val")
    ywd = {loc: dg.yesterday_by_weekday(series, points, cfg, loc, "co2", 60, "val")
           for loc in cfg["baseline"]["spaces"]}

    dg.plot_profiles(series, cfg, str(fig_dir / "weekday_profile.png"), cap + " · 학습 기간")
    dg.plot_monthly(monthly, str(fig_dir / "monthly_observed.png"), cap)
    dg.plot_reactions(days, str(fig_dir / "co2_reaction.png"), cap + f" · {sc['space']} 평일 · 일정 = 가정")
    dg.plot_error_by_hour(hours, str(fig_dir / "error_by_hour.png"),
                          "실습실 60분 뒤 CO₂ 오차, 맞힐 시각별 (검증 기간)", cap)
    days.to_csv(os.path.join(out, "daily_reactions.csv"), index=False)

    L = ["# 데이터 진단 수치 (자동 생성)", "", "진단서 [diagnosis.md](diagnosis.md)의 근거 수치. `python -m tsdata diagnose`가 만든다.",
         "", *cond_table(cfg), ""]
    L += ["## 1. 결측률과 제거 건수 (분석 기간 전체, 5분 칸)", "",
          "| 공간 | 항목 | 관측률 | 결측률 | 학습 | 검증 | 시험 | 채움 뒤 값 있음 | 범위 밖 | 오류 값 | 센서 ID 없음 | 숫자 아님 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in miss.itertuples():
        g = lambda k: int(getattr(r, k, 0) or 0)  # noqa: E731
        L.append(f"| {r.location} | {ITEM_NAMES[r.item]} | {r.observed:.1%} | {1 - r.observed:.1%} | "
                 f"{r.observed_train:.1%} | {r.observed_val:.1%} | {r.observed_test:.1%} | {r.usable:.1%} | "
                 f"{g('out_of_range')} | {g('error_value')} | {g('no_device')} | {g('non_numeric')} |")
    other = removed[removed["rule"] == "other_space"].groupby("location", dropna=False)["n"].sum()
    L += ["", "분석 대상 밖으로 뺀 행: " + ", ".join(f"{loc or '(태그 없음)'} {n}" for loc, n in other.items()), ""]
    L += ["월별 CO₂ 관측률:", "", "| 월 | " + " | ".join(monthly.columns) + " |",
          "|---|" + "---|" * len(monthly.columns)]
    L += [f"| {m} | " + " | ".join(f"{v:.0%}" if pd.notna(v) else "-" for v in row) + " |"
          for m, row in monthly.iterrows()]
    L += ["", "## 2. 평일 CO₂ 중앙값 곡선의 시각 (학습 기간)", "",
          "| 공간 | 새벽 기준값 | 상승 시작(기준+100) | 점심 최저 | 오후 최고 | 절반 하락 |", "|---|---|---|---|---|---|"]
    for loc, e in events.items():
        L.append(f"| {loc} | {e['base']:.0f} | {dg._hhmm(e['onset'])} | {dg._hhmm(e['lunch_min'])} | "
                 f"{e['afternoon_peak']:.0f} | {dg._hhmm(e['half_drop'])} |")
    ok = days[days["status"].eq("정상") | days["status"].str.startswith("어긋남")]
    L += ["", f"## 3. 일정–CO₂ 반응 ({sc['space']}, 평일, 일정 = {sc['source']})", "",
          f"가정 일정: 수업 {sc['class_start']}, 점심 {sc['lunch_start']}~{sc['lunch_end']}, 종료 {sc['class_end']}. "
          f"어긋남 = 학습 기간 평일 중앙값(시작→상승 {days.attrs['ref_start']:.0f}분, 종료→절반 하락 "
          f"{days.attrs['ref_end']:.0f}분)보다 {sc['deviation_min']}분 넘게 다른 날", "",
          "| 기간 | 평일 | 정상 | 어긋남 | 상승 없음 | 데이터 부족 | 시작→상승 중앙값(분) | 점심→최저 중앙값(분) | 종료→절반 하락 중앙값(분) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for p in ("train", "val", "test"):
        d = days[days["period"] == p]
        o = ok[ok["period"] == p]
        L.append(f"| {PERIOD_NAMES[p]} | {len(d)} | {(d['status'] == '정상').sum()} | "
                 f"{d['status'].str.startswith('어긋남').sum()} | {(d['status'] == '상승 없음').sum()} | "
                 f"{(d['status'] == '데이터 부족').sum()} | {o['start_delay'].median():.0f} | "
                 f"{o['lunch_delay'].median():.0f} | {o['end_delay'].median():.0f} |")
    L += ["", "어긋난 날·상승 없는 날 목록:", ""]
    for r in days[~days["status"].isin(["정상", "데이터 부족"])].itertuples():
        L.append(f"- {r.date} ({'월화수목금'[r.dow]}, {PERIOD_NAMES.get(r.period, '-')}): {r.status}")
    L += ["", "## 4. 기준선 근거", "", "자기상관(학습 기간, 관측 칸끼리):", "",
          "| 공간 | 항목 | 1시간 뒤와 상관 | 24시간 뒤와 상관 |", "|---|---|---|---|"]
    L += [f"| {r.location} | {ITEM_NAMES[r.item]} | {r.lag_60min:.3f} | {r.lag_24h:.3f} |"
          for r in auto.itertuples() if r.item in ("co2", "temperature")]
    L += ["", "'어제 같은 시각' 60분 CO₂ MAE, 맞힐 날의 요일별 (검증 기간):", "",
          "| 공간 | 월 | 화 | 수 | 목 | 금 | 토 | 일 |", "|---|---|---|---|---|---|---|---|"]
    for loc, s in ywd.items():
        L.append(f"| {loc} | " + " | ".join(f"{s.get(i, float('nan')):.0f}" for i in range(7)) + " |")
    L += ["", "실습실 60분 CO₂ MAE, 맞힐 시각별 (검증 기간):", "",
          "| 시 | " + " | ".join(LABELS[b] for b in hours.columns) + " |", "|---|---|---|---|"]
    L += [f"| {h:02d} | " + " | ".join(f"{v:.0f}" for v in row) + " |" for h, row in hours.iterrows()]
    L.append("")
    (REPORT_DIR / "diagnosis-data.md").write_text("\n".join(L), encoding="utf-8")
    print(f"진단 수치: {REPORT_DIR / 'diagnosis-data.md'}, 그래프: {fig_dir}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tsdata", description="과제 2 데이터 진단과 기준선")
    p.add_argument("command", choices=["prep", "baseline", "diagnose", "all"])
    p.add_argument("--config", default=str(REPO / "config" / "prep.toml"))
    a = p.parse_args(argv)
    refuse_on_board(f"1년치 전처리·분석(tsdata {a.command})")
    cfg = load_config(a.config)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    steps = {"prep": [cmd_prep], "baseline": [cmd_baseline], "diagnose": [cmd_diagnose],
             "all": [cmd_prep, cmd_baseline, cmd_diagnose]}[a.command]
    for step in steps:
        step(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
