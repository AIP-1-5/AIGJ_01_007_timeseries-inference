"""설정 파일(TOML)과 비밀값(.env) 읽기. 계약: contracts/config.md, research R6."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 설정 파일에 있으면 안 되는 키(비밀값은 .env에만 둔다)
SECRET_KEYS = {"password", "username", "token", "host"}
POLICIES = ("store", "drop")


class ConfigError(Exception):
    """설정 오류. CLI는 종료 코드 2로 끝낸다."""


@dataclass(frozen=True)
class MqttCfg:
    uplink_topic: str = "application/+/device/+/event/up"
    join_topic: str = "application/+/device/+/event/join"
    qos: int = 1
    clean_session: bool = False
    client_id_prefix: str = "tsinfer-collector"
    keepalive_s: int = 30
    reconnect_min_s: int = 1
    reconnect_max_s: int = 10


@dataclass(frozen=True)
class StorageCfg:
    db_path: str = "~/data/tsinfer/collector.db"
    queue_max: int = 50000
    heartbeat_s: float = 60


@dataclass(frozen=True)
class FcntCfg:
    reset_max: int = 1000
    min_drop: int = 100


@dataclass(frozen=True)
class ReportCfg:
    judged_items: tuple[str, ...] = ("co2", "temperature", "humidity")
    coverage_target: float = 0.95
    gap_list_min_minutes: int = 3


@dataclass(frozen=True)
class ModelCfg:
    keys: dict[str, str] = field(default_factory=dict)
    interval_s: int | None = None


@dataclass(frozen=True)
class DeviceCfg:
    interval_s: int | None = None


@dataclass(frozen=True)
class Config:
    mqtt: MqttCfg
    storage: StorageCfg
    fcnt: FcntCfg
    report: ReportCfg
    unmapped_policy: str
    models: dict[str, ModelCfg]
    devices: dict[str, DeviceCfg]
    path: str = ""


@dataclass(frozen=True)
class MqttConnection:
    host: str
    port: int
    username: str
    password: str = field(repr=False)


def _find_secret_keys(table: dict, where: str) -> list[str]:
    found = []
    for key, value in table.items():
        here = f"{where}.{key}" if where else key
        if key.lower() in SECRET_KEYS:
            found.append(here)
        # models.<모델>.keys 아래는 메시지 키 이름이라 검사하지 않는다
        if isinstance(value, dict) and not (where.startswith("models.") and key == "keys"):
            found.extend(_find_secret_keys(value, here))
    return found


def _section(raw: dict, name: str, cls, types: dict[str, type | tuple]):
    table = raw.get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] 는 테이블이어야 한다")
    values = {}
    for key, value in table.items():
        if key not in types:
            raise ConfigError(f"[{name}] 에 모르는 키: {key}")
        expected = types[key]
        # bool은 int의 하위 타입이라 따로 막는다
        if isinstance(value, bool) and expected is not bool:
            raise ConfigError(f"[{name}].{key} 타입이 틀렸다: {value!r}")
        if not isinstance(value, expected):
            raise ConfigError(f"[{name}].{key} 타입이 틀렸다: {value!r}")
        values[key] = value
    return cls(**values)


def _interval(value, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where}.interval_s 는 양의 정수여야 한다: {value!r}")
    return value


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"설정 파일이 없다: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML 문법 오류 ({path}): {e}") from None

    secrets = _find_secret_keys(raw, "")
    if secrets:
        raise ConfigError(f"설정 파일에 비밀값 키가 있다(.env로 옮길 것): {', '.join(secrets)}")

    allowed = {"mqtt", "storage", "fcnt", "unmapped", "models", "devices", "report"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"모르는 섹션: {', '.join(sorted(unknown))}")

    mqtt = _section(raw, "mqtt", MqttCfg, {
        "uplink_topic": str, "join_topic": str, "qos": int, "clean_session": bool,
        "client_id_prefix": str, "keepalive_s": int, "reconnect_min_s": int, "reconnect_max_s": int,
    })
    if mqtt.qos not in (0, 1, 2):
        raise ConfigError(f"[mqtt].qos 는 0, 1, 2 중 하나: {mqtt.qos}")
    if not mqtt.uplink_topic:
        raise ConfigError("[mqtt].uplink_topic 이 비어 있다")

    storage = _section(raw, "storage", StorageCfg, {
        "db_path": str, "queue_max": int, "heartbeat_s": (int, float),
    })
    storage = StorageCfg(
        db_path=os.path.expanduser(storage.db_path),
        queue_max=storage.queue_max,
        heartbeat_s=storage.heartbeat_s,
    )
    fcnt = _section(raw, "fcnt", FcntCfg, {"reset_max": int, "min_drop": int})
    if fcnt.reset_max < 0 or fcnt.min_drop <= 0:
        raise ConfigError("[fcnt] reset_max ≥ 0, min_drop > 0 이어야 한다")

    report_raw = dict(raw.get("report", {}))
    if "judged_items" in report_raw:
        if not isinstance(report_raw["judged_items"], list):
            raise ConfigError("[report].judged_items 는 목록이어야 한다")
        report_raw["judged_items"] = tuple(report_raw["judged_items"])
    report = _section({"report": report_raw}, "report", ReportCfg, {
        "judged_items": tuple, "coverage_target": (int, float), "gap_list_min_minutes": int,
    })

    policy = raw.get("unmapped", {}).get("policy", "store")
    if policy not in POLICIES:
        raise ConfigError(f"[unmapped].policy 는 {POLICIES} 중 하나: {policy!r}")

    models = {}
    for name, table in raw.get("models", {}).items():
        where = f"[models.{name}]"
        if not isinstance(table, dict):
            raise ConfigError(f"{where} 는 테이블이어야 한다")
        extra = set(table) - {"keys", "interval_s"}
        if extra:
            raise ConfigError(f"{where} 에 모르는 키: {', '.join(sorted(extra))}")
        keys = table.get("keys", {})
        if not isinstance(keys, dict) or not all(isinstance(v, str) and v for v in keys.values()):
            raise ConfigError(f"{where}.keys 는 '메시지 키 = \"항목 이름\"' 문자열 매핑이어야 한다")
        models[name] = ModelCfg(keys=dict(keys), interval_s=_interval(table.get("interval_s"), where))

    devices = {}
    for dev, table in raw.get("devices", {}).items():
        where = f"[devices.\"{dev}\"]"
        if not isinstance(table, dict) or set(table) - {"interval_s"}:
            raise ConfigError(f"{where} 에는 interval_s 만 둔다")
        devices[dev] = DeviceCfg(interval_s=_interval(table.get("interval_s"), where))

    return Config(mqtt=mqtt, storage=storage, fcnt=fcnt, report=report, unmapped_policy=policy,
                  models=models, devices=devices, path=str(path))


def load_env_file(path: str | os.PathLike) -> dict[str, str]:
    """KEY=VALUE 줄만 읽는다. 주석·빈 줄·잘못된 줄은 건너뛴다. 없는 파일은 빈 dict."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or " " in key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


def mqtt_connection(env_file: str | os.PathLike = ".env") -> MqttConnection:
    """환경변수를 먼저 보고, 없으면 .env에서 MQTT 접속값을 읽는다."""
    file_env = load_env_file(env_file)

    def get(name: str, default: str = "") -> str:
        if name in os.environ:
            return os.environ[name]
        return file_env.get(name, default)

    host = get("MQTT_HOST")
    if not host:
        raise ConfigError("MQTT_HOST 가 없다(.env 또는 환경변수)")
    port_s = get("MQTT_PORT", "1883") or "1883"
    try:
        port = int(port_s)
    except ValueError:
        raise ConfigError(f"MQTT_PORT 가 숫자가 아니다: {port_s!r}") from None
    return MqttConnection(host=host, port=port, username=get("MQTT_USERNAME"),
                          password=get("MQTT_PASSWORD"))
