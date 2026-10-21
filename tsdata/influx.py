"""InfluxDB 과거 데이터를 내려받아 원본 스냅샷(parquet)으로 저장한다. research(002) R1.

교육장 내부망이라 보드 B에서만 접속된다. 접속값은 `.env`의 INFLUX_*에서 읽는다(비밀값은 로그에 쓰지 않는다).
값 타입이 섞여 있어(일부 시리즈가 문자열) 여기서는 문자열 그대로 받고, 숫자 변환·정제는 tsdata.prep이 한다.

    python -m tsdata.influx --start 2025-08-01 --end 2026-10-21 --out ~/data/tsinfer/influx
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import pandas as pd

from collector.config import load_env_file

MEASUREMENTS = ("co2", "temperature", "humidity")
KEEP = ["_time", "_value", "device_id", "location", "point"]


def _env(env_file: str) -> dict[str, str]:
    file_env = load_env_file(env_file)
    out = {}
    for k in ("INFLUX_URL", "INFLUX_TOKEN", "INFLUX_ORG", "INFLUX_BUCKET"):
        v = os.environ.get(k) or file_env.get(k)
        if not v:
            raise SystemExit(f"{k} 가 없다(.env 또는 환경변수)")
        out[k] = v
    if "://" not in out["INFLUX_URL"]:  # .env에 주소만 적힌 경우(curl은 http로 간주한다)
        out["INFLUX_URL"] = "http://" + out["INFLUX_URL"]
    return out


def query_csv(env: dict, flux: str, timeout: int = 300) -> str:
    # 조직은 ID(16진 16자리)면 orgID, 아니면 이름으로 보낸다(environment.md: 토큰에 조직 목록 권한 없음)
    org = env["INFLUX_ORG"]
    key = "orgID" if len(org) == 16 and all(c in "0123456789abcdef" for c in org) else "org"
    req = urllib.request.Request(
        f"{env['INFLUX_URL']}/api/v2/query?{key}={org}", data=flux.encode("utf-8"), method="POST",
        headers={"Authorization": f"Token {env['INFLUX_TOKEN']}", "Accept": "application/csv",
                 "Content-Type": "application/vnd.flux"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def parse_tables(text: str) -> pd.DataFrame:
    """주석 없는 Flux CSV(테이블마다 머리줄이 다시 나옴)를 하나로 합친다."""
    rows, header = [], None
    for rec in csv.reader(io.StringIO(text)):
        if not rec or all(not x for x in rec):
            header = None
            continue
        if header is None or "_time" in rec and "_value" in rec and rec != header and rec[rec.index("_time")] == "_time":
            header = rec
            continue
        rows.append(dict(zip(header, rec)))
    if not rows:
        return pd.DataFrame(columns=KEEP)
    df = pd.DataFrame(rows)
    for col in KEEP:
        if col not in df:
            df[col] = ""
    return df[KEEP]


def month_starts(start: datetime, end: datetime) -> list[datetime]:
    out, cur = [], start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur < end:
        out.append(cur)
        cur = cur.replace(year=cur.year + 1, month=1) if cur.month == 12 else cur.replace(month=cur.month + 1)
    return out


def export(env: dict, start: datetime, end: datetime, out_dir: str, measurements=MEASUREMENTS) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for m in measurements:
        parts = []
        months = month_starts(start, end)
        for i, ms in enumerate(months):
            a = max(ms, start)
            b = min(months[i + 1] if i + 1 < len(months) else end, end)
            flux = (f'from(bucket: "{env["INFLUX_BUCKET"]}") '
                    f'|> range(start: {a.strftime("%Y-%m-%dT%H:%M:%SZ")}, stop: {b.strftime("%Y-%m-%dT%H:%M:%SZ")}) '
                    f'|> filter(fn: (r) => r._measurement == "{m}" and r._field == "value") '
                    f'|> keep(columns: {KEEP!r})'.replace("'", '"'))
            t0 = time.monotonic()
            df = parse_tables(query_csv(env, flux))
            parts.append(df)
            print(f"{m} {a:%Y-%m}: {len(df)}행 ({time.monotonic() - t0:.1f}초)", flush=True)
        df = pd.concat(parts, ignore_index=True)
        df["_time"] = pd.to_datetime(df["_time"], utc=True, format="ISO8601")
        df = df.rename(columns={"_time": "time", "_value": "raw_value"})
        df["item"] = m
        path = os.path.join(out_dir, f"{m}.parquet")
        df.to_parquet(path, index=False)
        print(f"저장: {path} ({len(df)}행)", flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="InfluxDB 과거 데이터 → parquet 스냅샷")
    p.add_argument("--start", required=True, help="UTC 날짜, 예: 2025-08-01")
    p.add_argument("--end", required=True, help="UTC 날짜(미포함), 예: 2026-10-21")
    p.add_argument("--out", default="~/data/tsinfer/influx")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--measurements", default=",".join(MEASUREMENTS))
    a = p.parse_args(argv)
    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
    export(_env(a.env_file), start, end, os.path.expanduser(a.out), a.measurements.split(","))
    return 0


if __name__ == "__main__":
    sys.exit(main())
