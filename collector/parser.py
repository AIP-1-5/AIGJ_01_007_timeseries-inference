"""ChirpStack 업링크/join 메시지 → 정규화된 메시지. research R1, data-model 검증 규칙."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config

_ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def to_utc_iso(text) -> str | None:
    """ISO 8601(나노초 포함 가능) → UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`. 나노초는 마이크로초로 자른다."""
    if not isinstance(text, str):
        return None
    m = _ISO.match(text.strip())
    if not m:
        return None
    base, frac, tz = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(microsecond=micro)
    except ValueError:
        return None
    if tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        dt -= sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6]))
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class UplinkMessage:
    device_id: str
    model: str
    location: str
    point: str
    spot: str
    f_cnt: int
    measured_at: str
    gw_time: str | None
    received_at: str
    gateway_ids: str
    items: tuple[tuple[str, float], ...]
    unmapped_keys: tuple[str, ...] = ()
    skipped_keys: tuple[str, ...] = ()  # 숫자가 아니라 건너뛴 키


@dataclass(frozen=True)
class RejectedMessage:
    reason: str


def classify_topic(topic: str) -> str | None:
    """측정값은 event/up, epoch 판정은 event/join. 나머지는 무시(FR-001)."""
    if topic.endswith("/event/up"):
        return "up"
    if topic.endswith("/event/join"):
        return "join"
    return None


def _is_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _tag(tags: dict, name: str) -> str:
    value = tags.get(name)
    return value if isinstance(value, str) else ""


def parse_uplink(topic: str, payload: bytes, received_at: str, cfg: Config) -> UplinkMessage | RejectedMessage:
    try:
        raw = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return RejectedMessage("not_json")
    if not isinstance(raw, dict):
        return RejectedMessage("not_object")

    info = raw.get("deviceInfo") if isinstance(raw.get("deviceInfo"), dict) else {}
    device_id = info.get("devEui")
    if not isinstance(device_id, str) or not device_id:
        return RejectedMessage("missing_field:devEui")

    f_cnt = raw.get("fCnt")
    if isinstance(f_cnt, bool) or not isinstance(f_cnt, int) or f_cnt < 0:
        return RejectedMessage("missing_field:fCnt")

    rx = raw.get("rxInfo") if isinstance(raw.get("rxInfo"), list) else []
    ns_times = [to_utc_iso(r.get("nsTime")) for r in rx if isinstance(r, dict)]
    ns_times = [t for t in ns_times if t]
    if not ns_times:
        return RejectedMessage("missing_field:rxInfo.nsTime")

    obj = raw.get("object")
    if not isinstance(obj, dict):
        return RejectedMessage("missing_field:object")

    model = info.get("deviceProfileName") if isinstance(info.get("deviceProfileName"), str) else ""
    model_cfg = cfg.models.get(model)
    mapping = model_cfg.keys if model_cfg else {}
    tags = info.get("tags") if isinstance(info.get("tags"), dict) else {}

    items, unmapped, skipped = [], [], []
    for key, value in obj.items():
        if not _is_number(value):
            skipped.append(key)
            continue
        if key in mapping:
            items.append((mapping[key], float(value)))
        else:
            unmapped.append(key)
            if cfg.unmapped_policy == "store":
                items.append((key, float(value)))

    gateways = [r.get("gatewayId") for r in rx if isinstance(r, dict) and isinstance(r.get("gatewayId"), str)]
    return UplinkMessage(
        device_id=device_id,
        model=model,
        location=_tag(tags, "location"),
        point=_tag(tags, "point"),
        spot=_tag(tags, "spot"),
        f_cnt=f_cnt,
        measured_at=min(ns_times),
        gw_time=to_utc_iso(raw.get("time")),
        received_at=received_at,
        gateway_ids=",".join(gateways),
        items=tuple(items),
        unmapped_keys=tuple(unmapped),
        skipped_keys=tuple(skipped),
    )


def parse_join(topic: str, payload: bytes) -> str | None:
    """join 이벤트의 devEui. 페이로드에서 못 찾으면 토픽 `.../device/<devEui>/event/join`에서 찾는다."""
    try:
        raw = json.loads(payload)
        dev = raw.get("deviceInfo", {}).get("devEui")
        if isinstance(dev, str) and dev:
            return dev
    except (ValueError, UnicodeDecodeError, AttributeError):
        pass
    parts = topic.split("/")
    if len(parts) >= 4 and "device" in parts:
        i = parts.index("device")
        if i + 1 < len(parts) and parts[i + 1]:
            return parts[i + 1]
    return None
