"""결과 보고서 그래프 4장을 이미 저장된 결과(json, 비교표)에서 그린다. 학습·추론 없음.

    .venv/bin/python docs/reports/make_figures.py
"""
import json
import re
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams["font.family"] = ["Noto Sans CJK KR", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

R = Path(__file__).resolve().parent
OUT = R / "figures"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
GRAY, INK, MUTED, GRID = "#8c8b86", "#0b0b0b", "#52514e", "#e6e5e1"


def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _save(fig, name, caption):
    fig.text(0.01, 0.01, caption, fontsize=7, color=MUTED)
    fig.savefig(OUT / name, dpi=130, facecolor="white")
    plt.close(fig)


def forecast_horizon():
    """과제 3: 거리별 시험 CO₂ MAE, 현재값 유지 vs GRU(일정 포함), 60분 목표선."""
    rows = {}
    text = (R / "003-forecast-60min/comparison.md").read_text().split("## 시험 기간")[1].split("\n## ")[0]
    for line in text.splitlines():
        m = re.match(r"\| (\S+) \| CO₂ \| (\d+)분 \| ([\d.]+) \|.*?\| \**([\d.]+)\** \| \**([\d.]+)\** \| \d+% \|", line)
        if m:
            sp, h, pers, _, gru = m.groups()
            rows.setdefault(sp, []).append((int(h), float(pers), float(gru)))
    spaces = ["실습실", "사무실", "회의실"]
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.4), sharey=False)
    for ax, sp in zip(axes, spaces):
        h, pers, gru = zip(*sorted(rows[sp]))
        ax.plot(h, pers, color=GRAY, lw=2, marker="o", ms=5, label="현재값 유지")
        ax.plot(h, gru, color=BLUE, lw=2, marker="o", ms=5, label="GRU(일정 포함)")
        target = pers[-1] * 0.7
        ax.plot([52, 64], [target, target], color=ORANGE, lw=2, ls="--", label="60분 목표(×0.7)")
        ax.annotate(f"{pers[-1]:.1f}", (60, pers[-1]), xytext=(4, 0), textcoords="offset points", fontsize=8, color=MUTED, va="center")
        ax.annotate(f"{gru[-1]:.1f} ({gru[-1] / pers[-1] - 1:+.0%})", (60, gru[-1]), xytext=(4, -4), textcoords="offset points",
                    fontsize=8, color=INK, va="top")
        ax.annotate(f"목표 {target:.1f}", (52, target), xytext=(-2, 0), textcoords="offset points", fontsize=7, color=MUTED,
                    ha="right", va="center")
        ax.set_title(sp, fontsize=10, color=INK)
        ax.set_xticks([5, 15, 30, 60])
        ax.set_xlim(0, 78)
        ax.set_ylim(0, max(pers) * 1.15)
        ax.set_xlabel("몇 분 뒤", fontsize=8, color=MUTED)
        _style(ax)
    axes[0].set_ylabel("CO₂ 오차 MAE (ppm)", fontsize=8, color=MUTED)
    axes[0].legend(fontsize=7, frameon=False, loc="upper left")
    fig.suptitle("과제 3. 60분 예측: 거리별 CO₂ 오차 (시험 기간)", fontsize=11, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    _save(fig, "fig3_forecast_horizon.png",
          "실데이터(InfluxDB) · 시험 2026-09-01~10-20 KST · 5분 칸 · 같은 평가 시점 · 학습은 맥, 비교표 003-forecast-60min/comparison.md")


def anomaly_by_type():
    """과제 4: 고장 종류별 탐지율, 규칙 vs 오토인코더."""
    a = json.loads((R / "004-anomaly/anomaly_results.json").read_text())
    order = ["hvac_stop", "spike", "drift", "stuck", "vent_stop"]
    names = {"hvac_stop": "냉방 정지", "spike": "튄 값", "drift": "영점 틀어짐", "stuck": "값 고정", "vent_stop": "환기 정지"}

    def rate(kind):
        hit, n = Counter(), Counter()
        for e in a["events"][kind]:
            n[e["type"]] += 1
            hit[e["type"]] += bool(e["detected"])
        return [hit[t] / n[t] for t in order], [f"{hit[t]}/{n[t]}" for t in order]

    rule, rule_lbl = rate("rule")
    model, model_lbl = rate("model")
    fig, ax = plt.subplots(figsize=(8, 3.4))
    y = range(len(order))
    ax.barh([i - 0.19 for i in y], rule, height=0.34, color=BLUE, label="규칙")
    ax.barh([i + 0.19 for i in y], model, height=0.34, color=ORANGE, label="오토인코더")
    for i in y:
        ax.text(rule[i] + 0.01, i - 0.19, rule_lbl[i], va="center", fontsize=7, color=INK)
        ax.text(model[i] + 0.01, i + 0.19, model_lbl[i], va="center", fontsize=7, color=MUTED)
    ax.axvline(0.8, color=MUTED, ls="--", lw=1)
    ax.text(0.8, len(order) - 0.45, " 기준 80%", fontsize=7, color=MUTED)
    ax.set_yticks(list(y), [names[t] for t in order])
    ax.invert_yaxis()
    ax.set_xlim(0, 1.1)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    _style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.legend(fontsize=7, frameon=False, loc="lower right")
    t = a["test"]["combined"]
    ax.set_title(f"과제 4. 고장 종류별 탐지율 (시험 기간, 합침 {t['detect_rate']:.0%} · 헛경보 하루 {t['fa_per_day']:.2f}회)",
                 fontsize=11, loc="left")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    _save(fig, "fig4_anomaly_by_type.png",
          "실데이터 + 직접 심은 고장 60건(공간 3 × 종류 5 × 4, 시드 2) · 시험 2026-09-01~10-20 KST · 임계값은 검증 기간에서 선택")


def state_confusion():
    """과제 5: 사용 여부·환기 경보 혼동 행렬(모델)."""
    s = json.loads((R / "005-state/state_results.json").read_text())
    fig, axes = plt.subplots(1, 2, figsize=(8.4, 3.6))
    panels = [("occupancy", "사용 여부 (실습실)", ["비어 있음", "사용 중"]),
              ("vent", "환기 경보 (30분 안 1,000ppm)", ["경보 없음", "경보"])]
    for ax, (key, title, lab) in zip(axes, panels):
        m = s[key]["model"]
        c = m["confusion"]
        grid = [[c["tn"], c["fp"]], [c["fn"], c["tp"]]]
        for i in range(2):
            row = sum(grid[i])
            for j in range(2):
                share = grid[i][j] / row
                ax.add_patch(plt.Rectangle((j, i), 0.97, 0.97, color=BLUE, alpha=0.12 + 0.75 * share))
                ax.text(j + 0.485, i + 0.42, f"{grid[i][j]:,}", ha="center", fontsize=11,
                        color="white" if share > 0.55 else INK)
                ax.text(j + 0.485, i + 0.66, f"행의 {share:.0%}", ha="center", fontsize=7,
                        color="white" if share > 0.55 else MUTED)
        ax.set_xlim(0, 2)
        ax.set_ylim(2, 0)
        ax.set_xticks([0.485, 1.485], [f"예측: {lab[0]}", f"예측: {lab[1]}"], fontsize=8)
        ax.set_yticks([0.485, 1.485], [f"정답: {lab[0]}", f"정답: {lab[1]}"], fontsize=8)
        ax.tick_params(length=0, colors=MUTED)
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.set_title(f"{title}\n재현율 {m['recall']:.1%} · 정밀도 {m['precision']:.1%} · macro-F1 {m['macro_f1']:.3f}",
                     fontsize=9, color=INK)
    fig.suptitle("과제 5. 상태 추정 혼동 행렬 (시험 기간, 모델)", fontsize=11, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    _save(fig, "fig5_state_confusion.png",
          "실데이터 · 시험 2026-09-01~10-20 KST · 사용 여부 정답 = 일정 기반(직접 관찰 없음) · 임계값은 검증 기간에서 선택")


def edge_latency():
    """과제 7: 예측 모델 1건 지연(평균, p95), 실행 방식별. 로그 축."""
    e = json.loads((R / "007-edge/edge_results.json").read_text())
    rows = [r for r in e["rows"] if r["model"] == "forecast" and r["batch"] == 1 and "mean_ms" in r]
    names = {"pytorch_cpu": "PyTorch CPU", "pytorch_gpu": "PyTorch GPU", "onnxruntime_cpu": "ONNX Runtime CPU"}
    fig, ax = plt.subplots(figsize=(7, 2.8))
    for i, r in enumerate(rows):
        ax.barh(i, r["mean_ms"], height=0.5, color=BLUE if r["backend"] == "onnxruntime_cpu" else GRAY)
        ax.text(r["mean_ms"] * 1.15, i, f"평균 {r['mean_ms']:.2f}ms · p95 {r['p95_ms']:.2f}ms", va="center", fontsize=8, color=INK)
    ax.set_yticks(range(len(rows)), [names[r["backend"]] for r in rows])
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlim(0.05, 2000)
    ax.set_xlabel("1건 지연 (ms, 로그 축)", fontsize=8, color=MUTED)
    _style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_title("과제 7. GRU 예측 1건 지연 (TensorRT FP16은 빌드 실패로 미측정)", fontsize=11, loc="left")
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    _save(fig, "fig7_edge_latency.png",
          f"보드 B · MAXN_SUPER · 배치 1 · 워밍업 {e['warmup']}회 뒤 {e['runs']}회 · 출력 차이 최대 0.0003ppm(ONNX Runtime)")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    forecast_horizon()
    anomaly_by_type()
    state_confusion()
    edge_latency()
    print("저장:", *sorted(p.name for p in OUT.glob("fig*.png")))
