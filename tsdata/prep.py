"""공용 전처리: 정제 → 센서별 5분 값 → 센서별 짧은 결측 채움(마지막 값 유지) → 공간 대표값 → 기간 분할 → 정규화.

과제 2~8의 학습·평가·서비스가 모두 이 모듈을 쓴다(원칙 III). 근거: specs/002-data-baseline/research.md R2~R6.
시계열은 긴 형식 DataFrame [time(UTC), location, item, value, observed, n_sensors, n_observed]이다.
value는 시각 t까지의 정보만으로 만든 값(서비스와 같은 계산)이고, observed는 그 칸에 실제 관측이 한 건이라도
있었는지다. 평가 정답은 observed 칸만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO / "config" / "prep.toml"


@dataclass(frozen=True)
class PrepConfig:
    raw: dict
    path: str
    sha: str

    def __getitem__(self, key):
        return self.raw[key]

    @property
    def tz(self) -> str:
        return self.raw["data"]["timezone"]

    @property
    def freq(self) -> str:
        return f"{self.raw['resample']['interval_min']}min"

    @property
    def step_min(self) -> int:
        return self.raw["resample"]["interval_min"]

    def path_of(self, key: str) -> str:
        return os.path.expanduser(self.raw["data"][key])


def load_config(path: str | os.PathLike = DEFAULT_CONFIG) -> PrepConfig:
    data = Path(path).read_bytes()
    raw = tomllib.loads(data.decode("utf-8"))
    for item in raw["data"]["items"]:
        if item not in raw["clean"]:
            raise ValueError(f"[clean.{item}] 규칙이 없다")
    return PrepConfig(raw, str(path), hashlib.sha256(data).hexdigest()[:12])


# --- 정제 (R2) ----------------------------------------------------------------

def clean(raw: pd.DataFrame, cfg: PrepConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """원본 [time, raw_value, device_id, location, item] → (정제된 [time, device_id, location, item, value], 제거 건수).

    제거 건수는 [location, item, rule, n]. 대상 밖 공간은 rule="other_space"로 공간 이름별로 센다.
    """
    removed = []

    def count(mask: pd.Series, rule: str, df: pd.DataFrame) -> None:
        if mask.any():
            c = df[mask].groupby(["location", "item"], dropna=False).size().reset_index(name="n")
            c["rule"] = rule
            removed.append(c)

    df = raw.copy()
    df["location"] = df["location"].fillna("").astype(str)
    df["device_id"] = df["device_id"].fillna("").astype(str)

    other = ~df["location"].isin(cfg["data"]["spaces"])
    count(other, "other_space", df)
    df = df[~other]

    no_dev = df["device_id"] == ""
    count(no_dev, "no_device", df)
    df = df[~no_dev]

    df["value"] = pd.to_numeric(df["raw_value"], errors="coerce")
    nonnum = df["value"].isna()
    count(nonnum, "non_numeric", df)
    df = df[~nonnum]

    keep = pd.Series(True, index=df.index)
    for item, rule in cfg["clean"].items():
        is_item = df["item"] == item
        bad_val = is_item & df["value"].isin(rule.get("drop_values", []))
        count(bad_val & keep, "error_value", df)
        keep &= ~bad_val
        out = is_item & ((df["value"] < rule["min"]) | (df["value"] > rule["max"]))
        count(out & keep, "out_of_range", df)
        keep &= ~out
    df = df[keep]

    removed_df = (pd.concat(removed, ignore_index=True) if removed
                  else pd.DataFrame(columns=["location", "item", "n", "rule"]))
    return df[["time", "device_id", "location", "item", "value"]].reset_index(drop=True), \
        removed_df[["location", "item", "rule", "n"]]


# --- 5분 공간 대표값 (R3) -----------------------------------------------------

def analysis_span(cfg: PrepConfig) -> tuple[pd.Timestamp, pd.Timestamp]:
    """학습 시작 ~ 시험 끝(끝 날짜 포함)을 UTC [start, end)로."""
    p = cfg["periods"]
    start = pd.Timestamp(p["train"][0], tz=cfg.tz).tz_convert("UTC")
    end = (pd.Timestamp(p["test"][1], tz=cfg.tz) + pd.Timedelta(days=1)).tz_convert("UTC")
    return start, end


def to_space_series(df: pd.DataFrame, cfg: PrepConfig,
                    span: tuple[pd.Timestamp, pd.Timestamp] | None = None) -> pd.DataFrame:
    """센서별 5분 평균 → 센서별로 짧은 결측을 마지막 값으로 채움 → 공간 중앙값.

    센서별로 먼저 채우는 이유: 간격이 다른 센서(1·2·10분)가 섞인 공간에서 칸마다 센서 구성이 바뀌면,
    센서 간 차이(실습실 CO₂ 200ppm 이상) 때문에 대표값이 톱니처럼 뛴다(research R3).
    채움은 과거 값만 쓴다(선형 보간은 다음 관측을 끌어와 누수가 된다, R4).
    """
    start, end = span or analysis_span(cfg)
    df = df[(df["time"] >= start) & (df["time"] < end)].copy()
    df["slot"] = df["time"].dt.floor(cfg.freq)
    per_sensor = df.groupby(["location", "item", "device_id", "slot"], observed=True)["value"].mean()
    agg = cfg["resample"]["space_aggregate"]
    limit = cfg["gaps"]["max_fill_steps"]

    grid = pd.date_range(start, end, freq=cfg.freq, inclusive="left")
    parts = []
    for (loc, item), g in per_sensor.groupby(level=["location", "item"]):
        wide = g.droplevel(["location", "item"]).unstack("device_id").reindex(grid)
        n_obs = wide.notna().sum(axis=1)
        filled = wide.apply(lambda col: fill_short_gaps(col, limit))
        n_sensors = filled.notna().sum(axis=1)
        value = getattr(filled, agg)(axis=1, skipna=True).where(n_sensors > 0)
        parts.append(pd.DataFrame({"time": grid, "location": loc, "item": item, "value": value.to_numpy(),
                                   "observed": (n_obs > 0).to_numpy(), "n_sensors": n_sensors.to_numpy(),
                                   "n_observed": n_obs.to_numpy()}))
    if not parts:
        return pd.DataFrame(columns=["time", "location", "item", "value", "observed", "n_sensors", "n_observed"])
    return pd.concat(parts, ignore_index=True)


# --- 짧은 결측 채움 (R4) ------------------------------------------------------

def fill_short_gaps(values: pd.Series, max_steps: int) -> pd.Series:
    """마지막 관측값을 최대 max_steps칸까지 유지한다. 과거 값만 쓰므로 서비스에서도 같은 값이 나온다.

    긴 공백도 처음 max_steps칸은 채운다. 그 시각에는 공백이 길어질지 알 수 없기 때문이다.
    """
    return values.ffill(limit=max_steps)


# --- 기간 분할과 정규화 (R5, R6) ----------------------------------------------

def period_of(times: pd.Series, cfg: PrepConfig) -> pd.Series:
    """UTC 시각 → 'train' | 'val' | 'test' | '' (기간 밖). KST 날짜, 끝 날짜 포함."""
    local = pd.to_datetime(times).dt.tz_convert(cfg.tz).dt.strftime("%Y-%m-%d")
    out = pd.Series("", index=times.index, dtype=object)
    for name, (a, b) in cfg["periods"].items():
        out[(local >= a) & (local <= b)] = name
    return out


def fit_normalizer(series: pd.DataFrame, cfg: PrepConfig) -> dict:
    """공간·항목별 평균·표준편차. 학습 기간의 관측 칸만 쓴다(AC-1)."""
    train = series[(period_of(series["time"], cfg) == "train") & series["observed"]]
    stats = {}
    for (loc, item), g in train.groupby(["location", "item"]):
        stats[f"{loc}|{item}"] = {"mean": float(g["value"].mean()), "std": float(g["value"].std(ddof=0)),
                                  "n": int(len(g))}
    return {"period": cfg["periods"]["train"], "basis": "train, observed only", "stats": stats}


def normalize(series: pd.DataFrame, normalizer: dict) -> pd.Series:
    key = series["location"] + "|" + series["item"]
    mean = key.map(lambda k: normalizer["stats"].get(k, {}).get("mean", np.nan))
    std = key.map(lambda k: normalizer["stats"].get(k, {}).get("std", np.nan))
    return (series["value"] - mean) / std


def save_params(path: str, cfg: PrepConfig, normalizer: dict, extra: dict | None = None) -> None:
    params = {"config_path": os.path.relpath(cfg.path, REPO), "config_sha": cfg.sha,
              "clean": cfg["clean"], "resample": cfg["resample"], "gaps": cfg["gaps"],
              "periods": cfg["periods"], "timezone": cfg.tz, "normalizer": normalizer, **(extra or {})}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)


# --- 전체 실행 ----------------------------------------------------------------

def load_snapshot(cfg: PrepConfig) -> pd.DataFrame:
    parts = [pd.read_parquet(os.path.join(cfg.path_of("snapshot_dir"), f"{item}.parquet"))
             for item in cfg["data"]["items"]]
    return pd.concat(parts, ignore_index=True)


def build(cfg: PrepConfig) -> dict:
    """스냅샷 → 정제 → 공간 시계열(보간) → 정규화. 결과를 out_dir에 쓰고 돌려준다."""
    out_dir = cfg.path_of("out_dir")
    os.makedirs(out_dir, exist_ok=True)
    span = analysis_span(cfg)
    raw = load_snapshot(cfg)
    raw = raw[(raw["time"] >= span[0]) & (raw["time"] < span[1])]  # 제거 건수도 분석 기간 기준
    cleaned, removed = clean(raw, cfg)
    series = to_space_series(cleaned, cfg, span)
    normalizer = fit_normalizer(series, cfg)
    series.to_parquet(os.path.join(out_dir, "series.parquet"), index=False)
    removed.to_csv(os.path.join(out_dir, "removed.csv"), index=False)
    save_params(os.path.join(out_dir, "prep_params.json"), cfg, normalizer,
                {"snapshot_rows": int(len(raw)), "span_utc": [str(span[0]), str(span[1])]})
    return {"raw_rows": len(raw), "cleaned": cleaned, "removed": removed, "series": series,
            "normalizer": normalizer}
