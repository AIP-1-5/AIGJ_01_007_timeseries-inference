# Contract: 수집기 명령

모든 명령은 저장소 루트에서 `.venv/bin/python -m collector <명령>`으로 실행한다. 공통 옵션은 `--config PATH`(기본 `config/collector.toml`)다. 비밀값은 환경변수(`MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`)에서 읽는다. 환경변수가 없으면 `--env-file`(기본 `.env`)을 읽는다.

## `run`

상시 수집. systemd가 실행하는 명령이다.

- `--db PATH`: 설정의 `storage.db_path`를 덮어쓴다(로컬 브로커 시험용 DB 등).

- 종료 코드 0: SIGTERM/SIGINT로 정상 종료(큐를 비운 뒤)
- 종료 코드 2: 설정 오류(TOML 문법, 필수 키 없음, 설정 파일에 비밀값 키가 있음, 스키마 버전 불일치). systemd가 재시작해도 같은 오류가 나므로 로그에 원인을 분명히 남긴다.
- 브로커 연결 실패는 종료 사유가 아니다(계속 재시도한다).
- 로그(stdout → journald)에는 한 줄에 하나씩 `시각 수준 이벤트 key=value …`를 쓴다. 비밀값은 쓰지 않는다.

## `status`

지금 상태를 표로 보여 준다. 읽기 전용 연결만 쓴다.

```text
$ python -m collector status [--hours 24] [--json]
기간: 2026-10-20T06:00Z ~ 2026-10-21T06:00Z (24h, measured_at 기준)
수집기: 마지막 heartbeat 2026-10-21T05:59:41Z (20초 전), 마지막 연결 2026-10-21T00:01:03Z

공간      항목         건수   마지막 수신           1분 커버율
실습실    co2          4210   2026-10-21T05:59:48Z   99.6%
회의실    co2           144   2026-10-21T05:57:14Z   10.0%  (간격 600s 센서만 있음)
...

센서              모델        공간    받음/기대   건너뜀  수신률   간격  간격커버율  마지막 수신
24e124725d089152  AM103       회의실   144/144       0   100.0%   600s   100.0%     2026-10-21T05:57:14Z
...
경고: 24e124128c140101 마지막 수신 25분 전 (간격 600s의 2배 초과)
```

- 끊김 경고: 마지막 수신이 센서 간격의 2배(간격을 모르면 5분)보다 오래되면 표시한다.
- `--json`: 같은 내용을 JSON으로 출력한다(과제 2·8에서 재사용).

## `report`

수집 현황 보고(D-1) Markdown을 만든다.

```text
$ python -m collector report --start 2026-10-21T00:00Z --end 2026-10-22T00:00Z \
      --out docs/reports/001-data-collection/coverage-2026-10-21.md
```

보고서 구성:

1. 측정 조건: 기간, 실데이터, 수집 보드·서비스, 시각 기준(nsTime), 설정 파일 커밋
2. 공간·항목별 커버율 표(1분 칸), AC-3 판정(co2·temperature·humidity, 95%)
3. 센서별 수신률 표와 간격 기준 커버율
4. 결측 구간 표(≥3분): 시작, 끝, 길이, 추정 원인, 근거(이벤트·fCnt). 짧은 구간 건수
5. 수집기 이벤트 요약(재시작·끊김 횟수, 버린 메시지, 매핑 안 된 키)
6. 해석 칸: 자동으로 채우지 않는다. 사람이 쓴다(원칙 V)

## `replay`

브로커 없이 jsonl 파일의 메시지를 수집 경로(파싱 → epoch → 저장)에 그대로 넣는다. 테스트와 D-2 형식 변경 시연용이다.

```text
$ python -m collector replay tests/fixtures/uplink_sample.jsonl --db /tmp/x.db [--topic-template ...]
처리 30, 저장 메시지 28, 중복 무시 2, 버림 0, 매핑 안 된 키 0
```

- 줄마다 `{"topic": ..., "payload": {...}}` 또는 ChirpStack 업링크 JSON 하나다. 후자는 토픽을 `event/up`으로 간주한다.

## `check-config`

설정 파일만 검사한다(TOML 문법, 매핑, 비밀값 키 없음). D-2에서 설정을 바꾼 뒤 서비스를 재시작하기 전에 쓴다. 종료 코드 0 = 통과, 2 = 오류.
