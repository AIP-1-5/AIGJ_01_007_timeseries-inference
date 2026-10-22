# Implementation Plan: 과제 1 데이터 수집 체계

**Branch**: `001-data-collection` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-data-collection/spec.md`

## Summary

교육장 MQTT 브로커에서 ChirpStack 센서 업링크를 받아 센서 단위 원본 측정값을 항목마다 한 행씩 보드 B 로컬 SQLite(WAL)에 저장하는 상시 수집기를 만든다. 측정 시각은 `min(rxInfo[].nsTime)`으로 정한다(게이트웨이 시계 5분 오차 회피). 중복은 `(device_id, fcnt_epoch, f_cnt, item)` 기본키와 `INSERT OR IGNORE`로 거른다. 모델별 키 매핑은 TOML 설정으로 둔다. 수신과 쓰기는 큐로 분리하고 자동 재연결하며, systemd로 상시 실행한다. 현황 명령과 보고서 생성기로 확인 기준 AC-1~3과 산출물 D-1·D-2를 확인한다.

## Technical Context

**Language/Version**: Python 3.12.3 (보드 B 시스템 파이썬, `tomllib`·`sqlite3` 기본 포함)

**Primary Dependencies**: `paho-mqtt>=2,<3` (유일한 런타임 의존성). 개발용 `pytest>=8`

**Storage**: SQLite 3, WAL, 단일 파일 `~/data/tsinfer/collector.db`(설정으로 변경. 보드 B에 `/srv/team5`가 아직 없음)

**Testing**: pytest. 단위(파서, epoch 판정, 커버율·결측 원인), 통합(replay → DB, 큐·쓰기 스레드). 장애 시험(로컬 mosquitto, 내 계정만 막는 iptables, 재부팅)은 이번 구현에서 빼고 quickstart에 명령만 둔다(사용자가 직접 실행)

**Target Platform**: Jetson Orin Nano (aarch64), Ubuntu 24.04, systemd 255, 보드 B

**Project Type**: 상시 실행 데몬 + CLI (단일 파이썬 패키지)

**Performance Goals**: 분당 약 15 메시지(센서 13대). 브로커 복구 후 60초 안에 수집 재개(AC-1)

**Constraints**: 상주 메모리 100MB 이하(목표 50MB. 같은 보드에 LLM·임베딩 모델이 올라감). 비밀값은 `.env`에만 둔다. 읽기가 쓰기를 막지 않아야 한다

**Scale/Scope**: 하루 약 68,000행·14MB, 1년 약 5GB (research R5)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| 원칙 | 판정 | 근거 |
|---|---|---|
| I. 확인 기준이 곧 완료 조건 | 통과 | spec "완료 조건"에 AC-1~3, D-1~2를 원문 그대로 옮김. tasks에 산출물 2개를 포함함 |
| II. 누수 금지 | 해당 없음 | 학습·평가 없음. 수집 데이터에 기간 구분을 하지 않음(과제 2에서 시간순 분할) |
| III. 전처리는 한 곳에서 | 통과 | 수집기는 원본만 저장하고 정제하지 않음. 정제는 과제 2 공용 모듈의 몫. 컬럼을 InfluxDB 구조에 맞춰 한 로더로 읽게 함 |
| IV. 측정 조건 기록 | 통과 | 보고서에 기간·실데이터·보드·시각 기준·설정 커밋을 자동으로 넣음. 장애 시험 결과에 회차·초 기록 |
| V. 해석 | 통과 | 보고서에 해석 칸을 두고 사람이 씀. 결측 원인 판정 근거를 표에 남김. "생각해 볼 점" 두 질문은 research R1·R5에 답이 있고 보고서로 옮김 |
| VI. 사람 손 없이 복구 | 통과 | systemd `Restart=always` + enable, paho 자동 재연결 + on_connect 재구독, 형식 변경은 TOML만 수정, heartbeat로 중단 시간 드러냄 |
| VII. 단순한 것부터 | 통과 | 의존성 1개, 단일 프로세스·스레드 2개, SQLite. 제공 키트는 아직 없음(CLAUDE.md). 키트가 들어오면 먼저 실행해 보고 비교함 |
| 운영 제약 | 통과 | 보드 B에서만 실험·재부팅. 비밀값 `.env`. DB는 저장소 밖(`.gitignore`에도 `*.db`). 원본 샘플은 정리본만 커밋(research R10) |

**Phase 1 재확인**: data-model·contracts가 위 판정을 바꾸지 않는다. 한 가지는 **사용자 승인이 필요**하다. epoch 판정용으로 `event/join`을 추가 구독한다(research R2). 측정값은 계속 `event/up`에서만 저장한다.

## Project Structure

### Documentation (this feature)

```text
specs/001-data-collection/
├── plan.md
├── research.md          # R1~R10 결정과 근거
├── data-model.md        # readings / devices / collector_events / rejected
├── quickstart.md        # 확인 절차 (AC-1~3, D-1~2)
├── contracts/
│   ├── cli.md           # run / status / report / replay / check-config
│   └── config.md        # collector.toml 구조, 형식 변경 대응 규칙
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
collector/
├── __init__.py
├── __main__.py          # CLI 진입점 (run, status, report, replay, check-config)
├── config.py            # TOML 로드·검증, .env 읽기, 비밀값 키 금지
├── parser.py            # ChirpStack 업링크/join → 정규화된 메시지 (measured_at, tags, items)
├── epoch.py             # fcnt_epoch 판정 (순수 함수, research R2)
├── store.py             # SQLite 스키마·PRAGMA, 메시지 1건 = 트랜잭션 1개, 이벤트 기록, 읽기 전용 연결
├── writer.py            # 큐 소비 스레드, heartbeat, 종료 시 큐 비우기
├── mqtt_client.py       # paho 설정, 재연결·재구독, 콜백 → 큐
├── coverage.py          # 1분 칸 커버율, 센서 수신률, 간격 커버율, 결측 구간·원인
└── report.py            # status 표, Markdown 보고서

config/
├── collector.toml       # 운영 설정
└── examples/format-change.toml   # D-2 시연용 변경본

deploy/
├── tsinfer-collector.service.in  # 사용자·경로를 채우는 유닛 틀
└── install-service.sh            # 유닛 생성·설치·enable --now (sudo)

tests/
├── fixtures/
│   ├── uplink_sample.jsonl       # ../uplink_raw.jsonl 정리본 (내부 ID 제거)
│   └── format_change.jsonl       # D-2용 키 변경 메시지
├── unit/                 # test_parser, test_epoch, test_config, test_coverage
└── integration/          # test_replay_store, test_writer_queue
                          # 장애 시험 보조 스크립트(tests/broker/)는 이번 범위 밖. 명령은 quickstart 3·4

docs/reports/001-data-collection/
├── coverage-<날짜>.md    # D-1
├── format-change.md      # D-2
└── fault-tests.md        # AC-1·AC-2 시험 기록

requirements.txt, requirements-dev.txt, pytest.ini
```

**Structure Decision**: 단일 파이썬 패키지 `collector/`를 둔다. 과제 2 이후의 공용 전처리·로더는 별도 패키지(`tsinfer/` 등)로 만들고, 이 저장소의 `readings` 테이블과 InfluxDB 내보내기를 같은 형식으로 읽는다. 이번 과제에서는 만들지 않는다.

## 주요 설계 결정 요약 (근거는 research.md)

| 주제 | 결정 | 근거 |
|---|---|---|
| 측정 시각 | `min(rxInfo[].nsTime)`, `time`·수신 시각 별도 저장 | R1 |
| 중복 | PK `(device_id, fcnt_epoch, f_cnt, item)` + `INSERT OR IGNORE`, 첫 값 유지 | R2 |
| epoch | join 이벤트 또는 (`f_cnt ≤ 1000` ∧ 감소 ≥ 100 ∧ nsTime이 이전보다 늦음) | R2 |
| MQTT | paho 2.x, QoS 1, clean_session=False, 재연결 1~10초, on_connect 재구독 | R3 |
| 분리 | 콜백 → `queue.Queue(50000)` → 쓰기 스레드 1개 | R4 |
| 저장 | SQLite WAL, synchronous=NORMAL, 읽기는 `mode=ro` | R5 |
| 설정 | TOML(tomllib), 비밀값은 `.env`·EnvironmentFile | R6 |
| 상시 실행 | 시스템 유닛 `tsinfer-collector-yj`, Restart=always, enable | R7 |
| 현황 | 1분 칸 커버율 + 센서 수신률 + 간격 커버율, 결측 원인 4분류 | R8 |
| 장애 시험 | 로컬 mosquitto 재시작 3회 + uid yj의 1883만 차단, 보드 B 재부팅. 명령만 준비, 실행은 사용자 | R9 |

## 열린 질문 (2026-10-21 모두 결정, [docs/decisions.md](../../docs/decisions.md) Q1~Q3)

1. **`event/join` 추가 구독**: **승인**. 측정값 저장은 하지 않고 epoch 판정에만 쓴다. 끄려면 `join_topic = ""`.
2. **회의실처럼 간격이 긴 센서의 AC-3 판정**: 2026-10-22 강사 확인 대기(spec Assumptions). 그전까지는 1분 커버율·간격 커버율·수신률을 모두 보고한다(승인).
3. **DB 위치**: `~/data/tsinfer/collector.db`(승인). 공용 폴더가 생기면 설정만 바꾼다.

## 실행 권한 (2026-10-21 사용자 지시)

sudo, systemctl(시스템 유닛 조작), iptables, git push는 Claude가 실행하지 않고 명령만 보여 준다. 커밋은 단계마다 사용자 승인 뒤에 한다.

## Complexity Tracking

위반 없음.
