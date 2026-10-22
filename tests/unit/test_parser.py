import json
import re
from dataclasses import replace

from collector.parser import (
    RejectedMessage,
    UplinkMessage,
    classify_topic,
    parse_join,
    parse_uplink,
    to_utc_iso,
)

RECV = "2026-10-21T05:10:00.000000Z"
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")


def parse_all(sample_lines, cfg):
    return [parse_uplink(t, p, RECV, cfg) for t, p in sample_lines]


def first_payload(sample_lines):
    return json.loads(sample_lines[0][1])


def test_to_utc_iso():
    assert to_utc_iso("2026-10-21T05:06:15.008186349+00:00") == "2026-10-21T05:06:15.008186Z"
    assert to_utc_iso("2026-10-21T05:06:14.992+00:00") == "2026-10-21T05:06:14.992000Z"
    assert to_utc_iso("2026-10-21T14:06:15+09:00") == "2026-10-21T05:06:15.000000Z"
    assert to_utc_iso("2026-10-21T05:06:15.5Z") == "2026-10-21T05:06:15.500000Z"
    assert to_utc_iso("not a time") is None


def test_measured_at_is_min_nstime(sample_lines, cfg):
    for (topic, payload), msg in zip(sample_lines, parse_all(sample_lines, cfg)):
        assert isinstance(msg, UplinkMessage)
        raw = json.loads(payload)
        expected = min(to_utc_iso(r["nsTime"]) for r in raw["rxInfo"])
        assert msg.measured_at == expected
        assert ISO.match(msg.measured_at)
        assert msg.received_at == RECV


def test_gateway_clock_offset(sample_lines, cfg):
    # 게이트웨이 24e124fffef5dccc만 거친 AM103: time이 nsTime보다 약 5분 빠름(늦은 값)
    msgs = [m for m in parse_all(sample_lines, cfg) if m.device_id == "24e124725d081175"]
    assert msgs
    for m in msgs:
        assert m.gateway_ids == "24e124fffef5dccc"
        assert m.gw_time > m.measured_at
        assert m.gw_time[:13] == m.measured_at[:13]
        mm = lambda s: int(s[14:16]) * 60 + float(s[17:26])
        assert 4 * 60 <= mm(m.gw_time) - mm(m.measured_at) <= 6 * 60


def test_missing_tags_are_empty(sample_lines, cfg):
    msgs = {m.device_id: m for m in parse_all(sample_lines, cfg)}
    for dev in ("24e124785c389818", "24e124725d089152"):
        assert msgs[dev].point == "" and msgs[dev].spot == ""
        assert msgs[dev].location != ""


def test_mapped_items(sample_lines, cfg):
    m = parse_all(sample_lines, cfg)[0]  # EM500-CO2
    assert m.model == "EM500-CO2"
    assert m.location == "실습실" and m.point == "후방 오른쪽" and m.spot == "후방 우측"
    assert m.f_cnt == 104884
    assert dict(m.items) == {"humidity": 57.0, "co2": 765.0, "temperature": 22.9, "pressure": 1011.2}
    assert m.unmapped_keys == ()


def test_non_numeric_value_skips_only_that_item(sample_lines, cfg):
    raw = first_payload(sample_lines)
    raw["object"]["co2"] = "765"
    raw["object"]["humidity"] = True
    raw["object"]["pressure"] = None
    m = parse_uplink("a/1/device/x/event/up", json.dumps(raw).encode(), RECV, cfg)
    assert isinstance(m, UplinkMessage)
    assert dict(m.items) == {"temperature": 22.9}
    assert set(m.skipped_keys) == {"co2", "humidity", "pressure"}


def test_rejected_messages(sample_lines, cfg):
    topic = "a/1/device/x/event/up"
    r = parse_uplink(topic, b"{not json", RECV, cfg)
    assert isinstance(r, RejectedMessage) and r.reason == "not_json"
    assert parse_uplink(topic, b"[1,2]", RECV, cfg).reason == "not_object"

    def without(mutate):
        raw = first_payload(sample_lines)
        mutate(raw)
        return parse_uplink(topic, json.dumps(raw).encode(), RECV, cfg)

    assert without(lambda r: r["deviceInfo"].pop("devEui")).reason == "missing_field:devEui"
    assert without(lambda r: r.pop("fCnt")).reason == "missing_field:fCnt"
    assert without(lambda r: r.update(fCnt="12")).reason == "missing_field:fCnt"
    assert without(lambda r: r.update(rxInfo=[])).reason == "missing_field:rxInfo.nsTime"
    assert without(lambda r: r["rxInfo"][0].pop("nsTime")).reason == "missing_field:rxInfo.nsTime"
    assert without(lambda r: r.pop("object")).reason == "missing_field:object"


def test_unmapped_policy(sample_lines, cfg):
    raw = first_payload(sample_lines)
    raw["object"]["co2_new"] = 1.0
    payload = json.dumps(raw).encode()
    m = parse_uplink("a/1/device/x/event/up", payload, RECV, cfg)
    assert ("co2_new", 1.0) in m.items
    assert m.unmapped_keys == ("co2_new",)

    drop = replace(cfg, unmapped_policy="drop")
    m = parse_uplink("a/1/device/x/event/up", payload, RECV, drop)
    assert "co2_new" not in dict(m.items)
    assert m.unmapped_keys == ("co2_new",)


def test_unknown_model_all_keys_unmapped(sample_lines, cfg):
    raw = first_payload(sample_lines)
    raw["deviceInfo"]["deviceProfileName"] = "NEW-MODEL"
    m = parse_uplink("a/1/device/x/event/up", json.dumps(raw).encode(), RECV, cfg)
    assert set(m.unmapped_keys) == {"humidity", "co2", "temperature", "pressure"}
    assert dict(m.items)["co2"] == 765.0  # store 정책: 키 이름 그대로


def test_classify_topic(cfg):
    assert classify_topic("application/1/device/ab/event/up") == "up"
    assert classify_topic("application/1/device/ab/event/join") == "join"
    for t in ("application/1/device/ab/event/status", "gateway/gw/state/conn",
              "tsinfer/team1/result", "xflow/indoor/1"):
        assert classify_topic(t) is None


def test_parse_join():
    payload = json.dumps({"deviceInfo": {"devEui": "24e1"}, "devAddr": "x"}).encode()
    assert parse_join("application/1/device/24e1/event/join", payload) == "24e1"
    # 페이로드가 깨져도 토픽에서 devEui를 찾는다
    assert parse_join("application/1/device/abcd/event/join", b"oops") == "abcd"
    assert parse_join("weird/topic", b"oops") is None
