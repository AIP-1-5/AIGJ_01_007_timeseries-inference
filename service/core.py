"""과제 8 상시 추론 서비스의 계산 한 주기. specs/008-service, 결과 형식: docs/reports/008-service/result-format.md

매분: 수집 DB(읽기 전용)에서 최근 26시간 → 학습과 같은 전처리(tsdata.prep.clean + to_space_series)
→ 공간마다 60분 예측·이상·상태 → 경보 중복 억제 → 결과(dict). 발행·API는 publish.py·api.py가 한다.

- 막 시작된 5분 칸(아직 1~2분치만 있음)은 쓰지 않는다. 학습 때 5분 칸은 늘 5분치 평균이었으므로,
  부분 칸을 넣으면 학습과 다른 입력이 된다. 그래서 전처리 범위 끝 = floor(지금, 5분)(완성된 칸까지)이고,
  예측 기준 시각은 마지막 완성 칸이다(최대 약 5분 늦음). 끊김·범위 밖·값 고정 규칙은 원본 행을 지금까지 본다.
- 모델 파일은 주기마다 수정 시각을 보고 바뀌었으면 다시 읽는다(재시작 없이 교체 → 12시간 무중단 유지).
  파일이 없거나 torch를 못 쓰면 fallback(예측 = 현재값 유지, 이상 = 규칙만)으로 계속 돈다.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from collector.config import load_config as load_collector_config
from collector.coverage import configured_interval
from collector.log import log
from collector.store import open_readonly
from tsdata.prep import PrepConfig, clean, load_config as load_prep_config, to_space_series

REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO / "config" / "service.toml"
ITEMS = ["co2", "temperature", "humidity"]
STEP = timedelta(minutes=5)


def _iso(t) -> str | None:
    if t is None or (isinstance(t, float) and np.isnan(t)):
        return None
    return pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v, nd: int = 2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else round(f, nd)


def floor5(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


# --- 설정 ----------------------------------------------------------------------

@dataclass
class ServiceConfig:
    raw: dict
    path: str

    def __getitem__(self, key):
        return self.raw[key]

    def repo_path(self, p: str) -> str:
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else str(REPO / p)

    def model_path(self, name: str) -> str:
        return os.path.join(os.path.expanduser(self.raw["models"]["dir"]), self.raw["models"][name])

    @property
    def spaces(self) -> dict[str, str]:
        return dict(self.raw["spaces"])

    def space_id(self, name: str) -> str:
        return self.spaces.get(name, name)

    def resolve_space(self, key: str) -> str | None:
        """영문 id 또는 한글 이름 → 한글 이름."""
        for name, sid in self.spaces.items():
            if key in (name, sid):
                return name
        return None


def load_service_config(path: str | os.PathLike = DEFAULT_CONFIG) -> ServiceConfig:
    return ServiceConfig(tomllib.loads(Path(path).read_text(encoding="utf-8")), str(path))


# --- 수집 DB 읽기 --------------------------------------------------------------

def read_recent(db_path: str, start: datetime, end: datetime) -> pd.DataFrame:
    """[start, end)의 측정값을 스냅샷과 같은 모양 [time, raw_value, device_id, location, item]으로."""
    conn = open_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT measured_at, value, device_id, location, item FROM readings "
            "WHERE measured_at >= ? AND measured_at < ?",
            (start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), end.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))).fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["time", "raw_value", "device_id", "location", "item"])
    df["time"] = pd.to_datetime(df["time"], utc=True, format="ISO8601")
    df["raw_value"] = df["raw_value"].astype(str)   # 학습 스냅샷처럼 문자열로 받아 정제에서 변환한다
    return df


def build_series(raw: pd.DataFrame, prep_cfg: PrepConfig, span: tuple[datetime, datetime]) -> pd.DataFrame:
    """학습과 같은 전처리. span = (시작, 마지막 완성 칸의 끝). 시작은 5분 경계로 내린다(격자가 칸과 맞아야 함)."""
    cleaned, _ = clean(raw, prep_cfg)
    return to_space_series(cleaned, prep_cfg, (pd.Timestamp(floor5(span[0])), pd.Timestamp(span[1])))


def db_status(db_path: str) -> dict:
    """공간별 마지막 측정 시각, 센서 목록, 수집기 마지막 heartbeat."""
    conn = open_readonly(db_path)
    try:
        last = {}
        for loc, ts in conn.execute("SELECT location, max(measured_at) FROM readings GROUP BY location, item"):
            if ts and (loc not in last or ts > last[loc]):
                last[loc] = ts
        devices = conn.execute("SELECT device_id, model, location FROM devices").fetchall()
        hb = conn.execute("SELECT max(ts) FROM collector_events WHERE kind = 'heartbeat'").fetchone()[0]
    finally:
        conn.close()
    return {"last_measured": last, "devices": devices, "heartbeat": hb}


# --- 모델 (수정 시각이 바뀌면 다시 읽음) ---------------------------------------

class ModelStore:
    LOADERS = ("forecast", "anomaly", "occupancy", "vent")

    def __init__(self, cfg: ServiceConfig):
        self.cfg = cfg
        self._cache: dict[str, tuple[float, object, dict]] = {}

    def get(self, name: str):
        """(모델, meta) 또는 None. 없거나 읽기 실패면 None → fallback."""
        path = self.cfg.model_path(name)
        if not os.path.exists(path):
            self._cache.pop(name, None)
            return None
        mtime = os.path.getmtime(path)
        cached = self._cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1], cached[2]
        try:
            model, meta = getattr(self, f"_load_{name}")(path)
        except Exception as e:  # torch 없음, 형식 오류 등. 다음 주기에 다시 시도한다
            log("WARN", "model_load_failed", model=name, error=repr(e)[:200])
            return None
        self._cache[name] = (mtime, model, meta)
        log("INFO", "model_loaded", model=name, path=path)
        return model, meta

    def _load_forecast(self, path):
        fc = importlib.import_module("tsmodel.forecast")
        return fc.load(path, "cpu")

    def _torch_ckpt(self, path):
        torch = importlib.import_module("torch")
        return torch.load(path, map_location="cpu", weights_only=False)

    def _load_anomaly(self, path):
        an = importlib.import_module("tsmodel.anomaly")
        ck = self._torch_ckpt(path)
        model = an.AutoEncoder()
        model.load_state_dict(ck["state_dict"])
        return model.eval(), ck["meta"]

    def _load_mlp(self, path):
        st = importlib.import_module("tsmodel.state")
        ck = self._torch_ckpt(path)
        model = st.MLP(ck["meta"]["n_in"])
        model.load_state_dict(ck["state_dict"])
        return model.eval(), ck["meta"]

    _load_occupancy = _load_mlp
    _load_vent = _load_mlp


# --- 공간별 계산 ---------------------------------------------------------------

def space_values(series: pd.DataFrame, loc: str) -> pd.DataFrame:
    """한 공간의 5분 표(columns: co2, temperature, humidity, *_obs). torch 없이 쓰는 최소 형태."""
    g = series[series["location"] == loc]
    if g.empty:
        return pd.DataFrame(columns=ITEMS)
    wide = g.pivot(index="time", columns="item", values="value").reindex(columns=ITEMS)
    obs = g.pivot(index="time", columns="item", values="observed").reindex(columns=ITEMS).fillna(False)
    for it in ITEMS:
        wide[f"{it}_obs"] = obs[it].astype(bool)
    return wide.sort_index()


def persistence_forecast(current: dict, steps: int = 12) -> dict:
    return {"model": "fallback", "step_min": 5, "minutes": [5 * (i + 1) for i in range(steps)],
            "co2": [_num(current.get("co2"), 1)] * steps, "temperature": [_num(current.get("temperature"))] * steps}


def minutes_to_limit(current_co2, forecast_co2: list, limit: float, step_min: int = 5) -> dict:
    """"몇 분 뒤 limit 초과" 경보 값. 이미 넘었으면 exceeded_now=True, minutes_to_exceed=0."""
    if current_co2 is not None and current_co2 >= limit:
        return {"limit": limit, "exceeded_now": True, "minutes_to_exceed": 0}
    for i, v in enumerate(forecast_co2):
        if v is not None and v >= limit:
            return {"limit": limit, "exceeded_now": False, "minutes_to_exceed": (i + 1) * step_min}
    return {"limit": limit, "exceeded_now": False, "minutes_to_exceed": None}


def forecast_space(models: ModelStore, series, loc, normalizer, prep_cfg, current) -> dict:
    got = models.get("forecast")
    if got is None or normalizer is None or current.get("co2") is None or current.get("temperature") is None:
        return persistence_forecast(current)
    model, meta = got
    fc = importlib.import_module("tsmodel.forecast")
    frame = fc.space_frame(series, loc, normalizer)
    if len(frame) < fc.SEQ_LEN:
        return persistence_forecast(current)
    inp = fc.build_inputs(frame, loc, meta["spaces"], prep_cfg, meta.get("schedule", True),
                          anchors=np.array([len(frame) - 1]), lags=meta.get("lags", False))
    pred = fc.to_values(fc.predict(model, inp["seq"], inp["static"]), inp["current"], np.array([loc]), normalizer)[0]
    return {"model": meta.get("variant", "gru"), "step_min": 5, "minutes": [5 * (i + 1) for i in range(len(pred))],
            "co2": [_num(v, 1) for v in pred[:, 0]], "temperature": [_num(v) for v in pred[:, 1]]}


def anomaly_space(models: ModelStore, frame: pd.DataFrame, loc, normalizer, prep_cfg) -> dict:
    got = models.get("anomaly")
    if got is None or normalizer is None or len(frame) < 12:
        return {"model": "fallback", "score": None, "threshold": None, "is_anomaly": False, "cause": None, "rules": {}}
    model, meta = got
    an = importlib.import_module("tsmodel.anomaly")
    out = {"model": "autoencoder", "score": None, "threshold": _num(meta.get("threshold"), 4), "is_anomaly": False,
           "cause": None, "rules": {}}
    if f"{loc}|co2" in normalizer.get("stats", {}):
        x, _ = an.windows(frame, loc, normalizer, prep_cfg)
        score, per_item = an.ae_scores(model, x[-1:])
        out["score"] = _num(score[0], 4)
        out["is_anomaly"] = bool(score[0] > meta["threshold"])
        out["cause"] = an.ITEMS[int(np.argmax(per_item[0]))] if out["is_anomaly"] else None
    ref = meta.get("rule_ref", {}).get(loc)
    if ref:
        ref = dict(ref, co2_hour_q={int(k): v for k, v in ref["co2_hour_q"].items()})
        flags = an.rule_flags(frame, prep_cfg, ref).iloc[-1]
        out["rules"] = {k: bool(v) for k, v in flags.items()}
    return out


def state_space(models: ModelStore, frame: pd.DataFrame, loc, normalizer, prep_cfg, current_co2, limit) -> dict:
    out = {"occupied": None, "vent_alert": None}
    if normalizer is None or len(frame) < 13:
        return out
    occ, vent = models.get("occupancy"), models.get("vent")
    if occ is None and vent is None:
        return out
    st = importlib.import_module("tsmodel.state")
    feats = st.sensor_features(frame, loc, normalizer)
    if occ is not None and loc in occ[1].get("spaces", []):
        model, meta = occ
        x = feats.reindex(columns=meta["features"]).iloc[-1:].fillna(0).to_numpy(np.float32)
        p = float(st.proba(model, x)[0])
        out["occupied"] = {"model": "mlp", "probability": round(p, 3), "value": p >= meta["threshold"]}
    if vent is not None and loc in vent[1].get("spaces", []):
        model, meta = vent
        if current_co2 is not None and current_co2 >= limit:
            out["vent_alert"] = {"model": "mlp", "probability": None, "value": True, "note": "이미 기준 초과"}
        else:
            feat_mod = importlib.import_module("tsmodel.features")
            tf = feat_mod.time_features(frame.index, prep_cfg)
            allf = feats.copy()
            for c in tf.columns:
                allf[c] = tf[c].to_numpy()
            x = allf.reindex(columns=meta["features"]).iloc[-1:].fillna(0).to_numpy(np.float32)
            p = float(st.proba(model, x)[0])
            out["vent_alert"] = {"model": "mlp", "probability": round(p, 3), "value": p >= meta["threshold"]}
    return out


# --- 서비스 규칙 경보 ----------------------------------------------------------

def rule_alerts(raw_now: pd.DataFrame, status: dict, now: datetime, cfg: ServiceConfig, prep_cfg: PrepConfig,
                collector_cfg) -> dict[tuple[str, str], dict]:
    """{(공간 이름, 종류): detail}. 종류: data_gap, collector_down(공간 "*"), stuck, out_of_range."""
    a = cfg["alerts"]
    out: dict[tuple[str, str], dict] = {}
    hb = status.get("heartbeat")
    if hb is None or (now - pd.Timestamp(hb).to_pydatetime()).total_seconds() > a["collector_heartbeat_max_s"]:
        out[("*", "collector_down")] = {"last_heartbeat": hb}
    intervals: dict[str, int] = {}
    for dev, model, loc in status.get("devices", []):
        iv = configured_interval(collector_cfg, dev, model) or 60
        intervals[loc] = max(intervals.get(loc, 0), iv)
    for loc in cfg.spaces:
        limit_s = max(a["data_gap_min"] * 60, 2 * intervals.get(loc, 60))
        last = status.get("last_measured", {}).get(loc)
        age = (now - pd.Timestamp(last).to_pydatetime()).total_seconds() if last else None
        if age is None or age > limit_s:
            out[(loc, "data_gap")] = {"last_measured": last, "age_s": None if age is None else int(age),
                                      "limit_s": limit_s}
    if raw_now.empty:
        return out
    vals = raw_now.assign(value=pd.to_numeric(raw_now["raw_value"], errors="coerce"))
    recent = vals[vals["time"] >= pd.Timestamp(now - timedelta(minutes=a["stuck_window_min"]))]
    for (loc, dev), g in recent[recent["item"] == "co2"].groupby(["location", "device_id"]):
        if len(g) >= 6 and g["value"].nunique() == 1:
            out[(loc, "stuck")] = {"device_id": dev, "item": "co2", "value": _num(g["value"].iloc[0], 1),
                                   "n": int(len(g))}
    recent = vals[vals["time"] >= pd.Timestamp(now - timedelta(minutes=a["out_of_range_window_min"]))]
    for item, rule in prep_cfg["clean"].items():
        g = recent[recent["item"] == item]
        bad = g[(g["value"] < rule["min"]) | (g["value"] > rule["max"]) | g["value"].isin(rule.get("drop_values", []))]
        for (loc, dev), b in bad.groupby(["location", "device_id"]):
            out[(loc, "out_of_range")] = {"device_id": dev, "item": item, "value": _num(b["value"].iloc[-1], 2),
                                          "n": int(len(b))}
    return out


# --- 경보 중복 억제 ------------------------------------------------------------

class AlertManager:
    """같은 (공간, 종류) 경보는 처음 한 번 new, 이어지는 동안 repeat_min마다 repeat, 사라지면 resolved.

    매분 같은 경보를 보내면 받는 쪽(사람·LLM 에이전트)이 알림을 끄거나 무시하게 된다. 그래서 상태가 바뀔 때와
    오래 이어질 때(30분마다 "아직 진행 중")만 보낸다.
    """

    def __init__(self, repeat_min: int):
        self.repeat = timedelta(minutes=repeat_min)
        self.active: dict[tuple[str, str], dict] = {}

    def update(self, current: dict[tuple[str, str], dict], now: datetime) -> list[dict]:
        events = []
        for key, detail in current.items():
            st = self.active.get(key)
            if st is None:
                self.active[key] = {"since": now, "last_sent": now, "detail": detail}
                events.append(self._event(key, "new", now, now, detail))
            else:
                st["detail"] = detail
                if now - st["last_sent"] >= self.repeat:
                    st["last_sent"] = now
                    events.append(self._event(key, "repeat", st["since"], now, detail))
        for key in [k for k in self.active if k not in current]:
            st = self.active.pop(key)
            events.append(self._event(key, "resolved", st["since"], now, st["detail"]))
        return events

    def for_space(self, space: str) -> list[dict]:
        return [{"type": k[1], "status": "active", "since": _iso(v["since"]), "detail": v["detail"]}
                for k, v in self.active.items() if k[0] in (space, "*")]

    @staticmethod
    def _event(key, status, since, now, detail) -> dict:
        return {"space": key[0], "type": key[1], "status": status, "since": _iso(since), "time": _iso(now),
                "detail": detail}


# --- 한 주기 -------------------------------------------------------------------

@dataclass
class ServiceState:
    """API가 읽는 최신 결과. 주기 스레드가 쓰고 API 스레드가 읽는다(lock)."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    spaces: dict = field(default_factory=dict)       # space_id → payload
    status: dict = field(default_factory=dict)
    events: list = field(default_factory=list)       # 최근 경보 이벤트(최대 200)
    cycles: int = 0
    failures: int = 0

    def snapshot(self) -> dict:
        with self.lock:
            return {"spaces": dict(self.spaces), "status": dict(self.status), "events": list(self.events),
                    "cycles": self.cycles, "failures": self.failures}


class Service:
    def __init__(self, cfg: ServiceConfig):
        self.cfg = cfg
        self.prep_cfg = load_prep_config(cfg.repo_path(cfg["paths"]["prep_config"]))
        self.collector_cfg = load_collector_config(cfg.repo_path(cfg["paths"]["collector_config"]))
        self.db_path = self.collector_cfg.storage.db_path
        self.models = ModelStore(cfg)
        self.alerts = AlertManager(cfg["alerts"]["repeat_min"])
        self.state = ServiceState()

    def normalizer(self) -> dict | None:
        path = self.cfg.repo_path(self.cfg["paths"]["normalizer"])
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)["normalizer"]
        except (OSError, KeyError, ValueError):
            return None

    def cycle(self, now: datetime | None = None) -> dict:
        """한 주기 계산. 공간 하나가 실패해도 나머지는 계속한다. 결과 dict를 돌려주고 state를 갱신한다."""
        now = now or datetime.now(timezone.utc)
        t0 = time.monotonic()
        hours = self.cfg["service"]["history_hours"]
        span_end = floor5(now)                      # 마지막 완성 칸까지(부분 칸 제외)
        span_start = span_end - timedelta(hours=hours)
        raw = read_recent(self.db_path, span_start, now + timedelta(seconds=1))
        series = build_series(raw, self.prep_cfg, (span_start, span_end))
        status = db_status(self.db_path)
        norm = self.normalizer()
        limit = self.cfg["forecast"]["co2_limit"]

        current_alerts = rule_alerts(raw, status, now, self.cfg, self.prep_cfg, self.collector_cfg)
        payloads = {}
        for loc in self.cfg.spaces:
            sid = self.cfg.space_id(loc)
            try:
                p = self._space(series, loc, norm, limit, now, span_end)
                if p["anomaly"].get("is_anomaly"):
                    current_alerts[(loc, "anomaly")] = {"score": p["anomaly"]["score"], "cause": p["anomaly"]["cause"]}
                for rule, hit in p["anomaly"].get("rules", {}).items():
                    if hit:
                        current_alerts[(loc, f"rule_{rule}")] = {}
                if p["co2_alert"] and p["co2_alert"]["minutes_to_exceed"] is not None:
                    current_alerts[(loc, "co2_limit")] = dict(p["co2_alert"])
                vent = (p["state"] or {}).get("vent_alert")
                if vent and vent["value"]:
                    current_alerts[(loc, "vent_soon")] = {"probability": vent["probability"]}
            except Exception as e:
                log("ERROR", "space_failed", space=loc, error=repr(e)[:200])
                p = {"space": loc, "space_id": sid, "time": _iso(now), "status": "error", "error": repr(e)[:200]}
            payloads[sid] = p

        events = self.alerts.update(current_alerts, now)
        for loc in self.cfg.spaces:
            sid = self.cfg.space_id(loc)
            payloads[sid]["alerts"] = self.alerts.for_space(loc)
            if payloads[sid].get("status") == "ok" and any(a["type"] in ("data_gap", "collector_down")
                                                         for a in payloads[sid]["alerts"]):
                payloads[sid]["status"] = "degraded"
        duration_ms = int((time.monotonic() - t0) * 1000)
        result = {"time": _iso(now), "data_time": _iso(span_end - STEP), "duration_ms": duration_ms,
                  "spaces": payloads, "events": events,
                  "models": {n: ("loaded" if self.models.get(n) else "fallback") for n in ModelStore.LOADERS}}
        with self.state.lock:
            self.state.spaces = payloads
            self.state.status = {"time": result["time"], "data_time": result["data_time"],
                                 "duration_ms": duration_ms, "models": result["models"], "status": "ok"}
            self.state.events = (self.state.events + events)[-200:]
            self.state.cycles += 1
        return result

    def _space(self, series, loc, norm, limit, now, span_end) -> dict:
        frame = space_values(series, loc)
        last = frame.iloc[-1] if len(frame) else pd.Series(dtype=float)
        current = {it: _num(last.get(it)) for it in ITEMS}
        p = {"space": loc, "space_id": self.cfg.space_id(loc), "time": _iso(now), "data_time": _iso(span_end - STEP),
             "status": "ok", "current": current, "forecast": None, "co2_alert": None,
             "anomaly": {"model": "fallback", "score": None, "threshold": None, "is_anomaly": False, "cause": None,
                         "rules": {}}, "state": None}
        if loc in self.cfg["forecast"]["spaces"]:
            p["forecast"] = forecast_space(self.models, series, loc, norm, self.prep_cfg, current)
            p["co2_alert"] = minutes_to_limit(current["co2"], p["forecast"]["co2"], limit)
            p["state"] = state_space(self.models, frame, loc, norm, self.prep_cfg, current["co2"], limit)
        if len(frame):
            p["anomaly"] = anomaly_space(self.models, frame, loc, norm, self.prep_cfg)
        return p

    def mark_failure(self, error: str) -> None:
        with self.state.lock:
            self.state.failures += 1
            self.state.status = dict(self.state.status, status="error", last_error=error[:200])
