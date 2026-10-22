"""데이터 진단: 결측률·제거 건수, 평일 시간대별 패턴, 일정–CO₂ 반응, 기준선 오차 분석. research(002) R8.

패턴과 일정 추정은 학습 기간으로만 만든다(시험 기간을 보고 규칙을 정하지 않는다, 원칙 II).
그래프는 docs/reports/002-data-baseline/figures/에 PNG로 쓴다.
"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .baselines import BASELINES, LABELS, predictions  # noqa: E402
from .prep import PrepConfig, period_of  # noqa: E402

plt.rcParams["font.family"] = ["Noto Sans CJK KR", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

SPACE_COLORS = {"실습실": "#1f77b4", "사무실": "#ff7f0e", "회의실": "#2ca02c", "사무실 밖": "#7f7f7f"}


def _local(series: pd.DataFrame, cfg: PrepConfig) -> pd.DataFrame:
    lt = series["time"].dt.tz_convert(cfg.tz)
    return series.assign(local=lt, date=lt.dt.strftime("%Y-%m-%d"), dow=lt.dt.dayofweek,
                         tod=lt.dt.hour * 60 + lt.dt.minute)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutes: float) -> str:
    if minutes is None or pd.isna(minutes):
        return "-"
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


# --- 결측률과 제거 건수 -------------------------------------------------------

def missing_table(series: pd.DataFrame, removed: pd.DataFrame, cfg: PrepConfig) -> pd.DataFrame:
    """공간·항목별: 기간별 관측 칸 비율(결측률 = 1 − 관측률), 채움 후 값이 있는 비율, 규칙별 제거 건수."""
    s = series.assign(period=period_of(series["time"], cfg))
    out = s.groupby(["location", "item"]).agg(slots=("observed", "size"), observed=("observed", "mean"),
                                              usable=("value", lambda v: v.notna().mean()))
    for p in ("train", "val", "test"):
        out[f"observed_{p}"] = s[s["period"] == p].groupby(["location", "item"])["observed"].mean()
    rules = removed[removed["location"].isin(cfg["data"]["spaces"])]
    pivot = rules.pivot_table(index=["location", "item"], columns="rule", values="n", aggfunc="sum", fill_value=0)
    out = out.join(pivot, how="left").fillna(0)
    return out.reset_index()


def monthly_observed(series: pd.DataFrame, cfg: PrepConfig, item: str = "co2") -> pd.DataFrame:
    s = _local(series[series["item"] == item], cfg)
    s["month"] = s["local"].dt.strftime("%Y-%m")
    return s.groupby(["month", "location"])["observed"].mean().unstack()


# --- 평일 시간대별 패턴 -------------------------------------------------------

def weekday_profile(series: pd.DataFrame, cfg: PrepConfig, item: str, period: str = "train",
                    weekday: bool = True, stat: str = "median") -> pd.DataFrame:
    """시각(분, KST) × 공간. 채운 값(최대 15분 유지)도 쓴다.

    관측 칸만 쓰면 10분 간격 센서(회의실)는 짝수·홀수 칸이 서로 다른 날짜 집합의 평균이 되어 톱니가 생긴다.
    """
    s = _local(series[(series["item"] == item) & series["value"].notna()], cfg)
    s = s[period_of(s["time"], cfg) == period]
    s = s[(s["dow"] < 5) if weekday else (s["dow"] >= 5)]
    return s.groupby(["tod", "location"])["value"].agg(stat).unstack()


def profile_events(profile: pd.Series, cfg: PrepConfig) -> dict:
    """중앙값 곡선에서 상승 시작, 점심 최저, 저녁 하락 시각(분)을 찾는다."""
    sc = cfg["schedule"]
    base = profile[(profile.index >= 300) & (profile.index < 420)].median()
    above = profile[(profile.index >= 360) & (profile > base + sc["rise_ppm"])]
    onset = above.index.min() if len(above) else np.nan
    noon = profile[(profile.index >= 600) & (profile.index <= 870)]
    lunch_min = noon.idxmin() if len(noon) else np.nan
    eve = profile[profile.index >= 900]
    peak = eve.max()
    half = base + (peak - base) / 2
    after_peak = eve[eve.index >= eve.idxmax()]
    drop = after_peak[after_peak < half].index.min() if len(after_peak) else np.nan
    return {"base": float(base), "onset": onset, "lunch_min": lunch_min, "afternoon_peak": float(peak),
            "half_drop": drop}


# --- 일정–CO₂ 반응 ------------------------------------------------------------

def daily_reactions(series: pd.DataFrame, cfg: PrepConfig) -> pd.DataFrame:
    """평일마다 일정 기준 CO₂ 반응 시각과 어긋남.

    start_delay = 아침 기준값 + rise_ppm을 처음 넘은 시각 − 수업 시작
    lunch_delay = 점심 앞뒤 30분 안 최저 시각 − 점심 시작
    end_delay   = 수업 종료 뒤 (오후 수준과 기준값의 중간) 아래로 떨어진 시각 − 수업 종료
    어긋남: 시작·퇴실 반응이 학습 기간 평일의 중앙값보다 deviation_min분 넘게 다른 날.
    (CO₂는 사람이 나간 뒤 수십 분에 걸쳐 빠지므로, 일정 시각 자체가 아니라 평소 반응과 비교한다.)
    """
    sc = cfg["schedule"]
    start, ls, le, end = (_minutes(sc[k]) for k in ("class_start", "lunch_start", "lunch_end", "class_end"))
    s = _local(series[(series["location"] == sc["space"]) & (series["item"] == "co2")], cfg)
    s = s[s["dow"] < 5]
    rows = []
    slots_per_day = 24 * 60 // cfg.step_min
    for date, d in s.groupby("date"):
        d = d[d["observed"]].set_index("tod")["value"]
        row = {"date": date, "dow": pd.Timestamp(date).dayofweek, "period": "", "coverage": len(d) / slots_per_day}
        if row["coverage"] < 0.8:
            rows.append({**row, "status": "데이터 부족"})
            continue
        base = d[(d.index >= 300) & (d.index < 420)].median()
        day_max = d[(d.index >= 360) & (d.index < 1200)].max()
        if pd.isna(base) or day_max < base + sc["rise_ppm"]:
            rows.append({**row, "base": base, "peak": day_max, "status": "상승 없음"})
            continue
        onset = d[(d.index >= 360) & (d > base + sc["rise_ppm"])].index.min()
        noon = d[(d.index >= ls - 30) & (d.index <= le + 30)]
        afternoon = d[(d.index >= le + 60) & (d.index < end)].median()
        half = base + (afternoon - base) / 2 if pd.notna(afternoon) else np.nan
        after = d[(d.index >= end - 60) & (d < half)]
        end_cross = after.index.min() if len(after) else np.nan
        row.update(base=base, peak=day_max, onset=onset, start_delay=onset - start,
                   lunch_delay=(noon.idxmin() - ls) if len(noon) else np.nan,
                   end_cross=end_cross, end_delay=end_cross - end if pd.notna(end_cross) else np.nan,
                   status="반응 있음")
        rows.append(row)
    out = pd.DataFrame(rows)
    if not len(out):
        return out
    times = pd.to_datetime(out["date"]).dt.tz_localize(cfg.tz).dt.tz_convert("UTC")
    out["period"] = period_of(times + pd.Timedelta(hours=12), cfg).to_numpy()
    reacted = out["status"] == "반응 있음"
    ref = out[reacted & (out["period"] == "train")]
    ref_start, ref_end = ref["start_delay"].median(), ref["end_delay"].median()
    out.attrs.update(ref_start=ref_start, ref_end=ref_end)
    for i in out.index[reacted]:
        r = out.loc[i]
        dev = []
        if abs(r["start_delay"] - ref_start) > sc["deviation_min"]:
            dev.append(f"시작 {_hhmm(r['onset'])}")
        if pd.notna(r["end_delay"]) and abs(r["end_delay"] - ref_end) > sc["deviation_min"]:
            dev.append(f"퇴실 반응 {_hhmm(r['end_cross'])}")
        out.loc[i, "status"] = "어긋남: " + ", ".join(dev) if dev else "정상"
    return out


# --- 기준선 오차 분석 ---------------------------------------------------------

def autocorr_table(series: pd.DataFrame, cfg: PrepConfig, period: str = "train") -> pd.DataFrame:
    """공간·항목별 1시간 뒤·24시간 뒤 값과의 상관(관측 칸끼리). 현재값 유지와 어제 같은 시각의 근거."""
    step = cfg.step_min
    rows = []
    for (loc, item), g in series.groupby(["location", "item"]):
        g = g.sort_values("time").reset_index(drop=True)
        v = g["value"].where(g["observed"])
        inp = period_of(g["time"], cfg) == period
        row = {"location": loc, "item": item}
        for name, lag in (("lag_60min", 60 // step), ("lag_24h", 24 * 60 // step)):
            pair = pd.DataFrame({"a": v, "b": v.shift(-lag)})[inp].dropna()
            row[name] = float(pair["a"].corr(pair["b"])) if len(pair) > 10 else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def error_by_hour(series: pd.DataFrame, points: pd.DataFrame, cfg: PrepConfig, loc: str, item: str,
                  h_min: int, period: str) -> pd.DataFrame:
    """맞힐 시각(KST)의 시간대별 MAE. 평가 시점은 성능표와 같은 것을 쓴다."""
    g = series[(series["location"] == loc) & (series["item"] == item)].sort_values("time").reset_index(drop=True)
    p = predictions(g["value"], g["observed"], h_min, cfg)
    p["time"] = g["time"]
    pts = points[(points["period"] == period) & (points["location"] == loc) & (points["item"] == item)
                 & (points["horizon_min"] == h_min)]
    p = p[p["time"].isin(pts["time"])]
    hour = (p["time"] + pd.Timedelta(minutes=h_min)).dt.tz_convert(cfg.tz).dt.hour
    return pd.DataFrame({b: (p[b] - p["target"]).abs().groupby(hour).mean() for b in BASELINES})


def yesterday_by_weekday(series: pd.DataFrame, points: pd.DataFrame, cfg: PrepConfig, loc: str, item: str,
                         h_min: int, period: str) -> pd.Series:
    """'어제 같은 시각' MAE를 맞힐 날의 요일별로. 월요일(어제 = 일요일)이 약한지 본다."""
    g = series[(series["location"] == loc) & (series["item"] == item)].sort_values("time").reset_index(drop=True)
    p = predictions(g["value"], g["observed"], h_min, cfg)
    p["time"] = g["time"]
    pts = points[(points["period"] == period) & (points["location"] == loc) & (points["item"] == item)
                 & (points["horizon_min"] == h_min)]
    p = p[p["time"].isin(pts["time"])]
    dow = (p["time"] + pd.Timedelta(minutes=h_min)).dt.tz_convert(cfg.tz).dt.dayofweek
    return (p["yesterday"] - p["target"]).abs().groupby(dow).mean()


# --- 그래프 -------------------------------------------------------------------

def plot_profiles(series: pd.DataFrame, cfg: PrepConfig, path: str, caption: str) -> None:
    sc = cfg["schedule"]
    fig, axes = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    for ax, item, unit in ((axes[0], "co2", "ppm"), (axes[1], "temperature", "℃")):
        wk = weekday_profile(series, cfg, item, stat="mean")
        we = weekday_profile(series, cfg, item, weekday=False, stat="mean")
        for loc in wk.columns:
            ax.plot(wk.index / 60, wk[loc], label=f"{loc} 평일", color=SPACE_COLORS.get(loc), lw=1.6)
            if loc in we.columns and item == "co2":
                ax.plot(we.index / 60, we[loc], color=SPACE_COLORS.get(loc), lw=0.8, ls="--", alpha=0.6,
                        label=f"{loc} 주말")
        for k in ("class_start", "lunch_start", "lunch_end", "class_end"):
            ax.axvline(_minutes(sc[k]) / 60, color="#999", lw=0.7, ls=":")
        ax.set_ylabel(f"{'CO₂' if item == 'co2' else '온도'} ({unit})")
        ax.grid(alpha=0.25)
        ax.legend(fontsize=7, ncol=4, loc="upper left")
    axes[1].set_xlabel("시각 (KST)")
    axes[1].set_xticks(range(0, 25, 3))
    axes[0].set_title("평일 시간대별 평균 (점선 세로줄: 가정 일정 09:00 · 12:00 · 13:00 · 18:00)", fontsize=10)
    fig.text(0.01, 0.005, caption, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_monthly(monthly: pd.DataFrame, path: str, caption: str) -> None:
    fig, ax = plt.subplots(figsize=(9, 3.2))
    for loc in monthly.columns:
        ax.plot(monthly.index, monthly[loc] * 100, marker="o", ms=3, label=loc, color=SPACE_COLORS.get(loc))
    ax.set_ylabel("관측 칸 비율 (%)")
    ax.set_title("월별 CO₂ 관측 칸 비율 (5분 칸)", fontsize=10)
    ax.tick_params(axis="x", rotation=45, labelsize=7)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    fig.text(0.01, 0.005, caption, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_reactions(days: pd.DataFrame, path: str, caption: str) -> None:
    ok = days[days["status"].isin(["정상"]) | days["status"].str.startswith("어긋남")]
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.2))
    for ax, col, title in ((axes[0], "start_delay", "수업 시작 → CO₂ 상승 (분)"),
                           (axes[1], "lunch_delay", "점심 시작 → CO₂ 최저 (분)"),
                           (axes[2], "end_delay", "수업 종료 → 절반 하락 (분)")):
        v = ok[col].dropna()
        ax.hist(v, bins=np.arange(-120, 125, 10), color="#1f77b4", alpha=0.8)
        ax.axvline(v.median(), color="#d62728", lw=1)
        ax.set_title(f"{title}\n중앙값 {v.median():.0f}분, n={len(v)}", fontsize=9)
        ax.grid(alpha=0.25)
    fig.text(0.01, 0.005, caption, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_error_by_hour(table: pd.DataFrame, path: str, title: str, caption: str) -> None:
    fig, ax = plt.subplots(figsize=(9, 3.4))
    for b in BASELINES:
        ax.plot(table.index, table[b], marker="o", ms=3, label=LABELS[b])
    ax.set_xlabel("맞힐 시각 (KST, 시)")
    ax.set_ylabel("MAE (ppm)")
    ax.set_xticks(range(0, 24, 2))
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    fig.text(0.01, 0.005, caption, fontsize=7, color="#555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=130)
    plt.close(fig)
