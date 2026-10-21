# Data Model: 과제 1 데이터 수집 체계

저장소: SQLite 파일 하나(기본 `~/data/tsinfer/collector.db`, WAL). 시각은 모두 UTC ISO 8601 문자열이다(`YYYY-MM-DDTHH:MM:SS.ffffffZ`). 문자열 정렬이 곧 시간 정렬이고, 앞 16자(`YYYY-MM-DDTHH:MM`)가 1분 칸이다.

## readings — 측정값 (항목마다 한 행)

| 컬럼 | 타입 | 필수 | 원천 | 비고 |
|---|---|---|---|---|
| device_id | TEXT | ✓ | `deviceInfo.devEui` | InfluxDB 태그 `device_id` |
| fcnt_epoch | INTEGER | ✓ | 수집기 판정 | 처음 0, 재가입마다 +1 (research R2) |
| f_cnt | INTEGER | ✓ | `fCnt` | |
| item | TEXT | ✓ | 매핑된 항목 이름 | InfluxDB measurement (`co2`, `temperature` …) |
| value | REAL | ✓ | `object[키]` | 숫자만. 숫자가 아니면 그 항목만 건너뜀 |
| location | TEXT | ✓ | `deviceInfo.tags.location` | 없으면 `''` |
| point | TEXT | ✓ | `deviceInfo.tags.point` | 없으면 `''` |
| spot | TEXT | ✓ | `deviceInfo.tags.spot` | 없으면 `''` |
| model | TEXT | ✓ | `deviceInfo.deviceProfileName` | |
| measured_at | TEXT | ✓ | `min(rxInfo[].nsTime)` | 측정 시각. 1분 칸 기준 |
| gw_time | TEXT | | `time` | 게이트웨이 시계(원본) |
| received_at | TEXT | ✓ | 수집기 시계 | 콜백에서 큐에 넣을 때 |
| gateway_ids | TEXT | | `rxInfo[].gatewayId` | 쉼표로 구분. 시계 오차 분석용 |

- **기본키**: `(device_id, fcnt_epoch, f_cnt, item)`. 넣을 때 `INSERT OR IGNORE`를 쓰므로 먼저 들어온 행(값, measured_at)이 남는다.
- **인덱스**: `(location, item, measured_at)`(커버율), `(measured_at)`(기간 조회).
- 빈 태그를 NULL이 아니라 `''`로 두는 이유: InfluxDB도 태그 없는 행은 빈 값으로 읽히고, `GROUP BY`·비교가 단순해진다.

## devices — 센서 상태 (epoch 판정용, 재시작 후에도 유지)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| device_id | TEXT PK | |
| model | TEXT | 마지막으로 본 값 |
| location, point, spot | TEXT | 마지막으로 본 값 |
| fcnt_epoch | INTEGER | 현재 epoch |
| last_f_cnt | INTEGER | 현재 epoch의 마지막 fCnt. join 직후 -1 |
| last_measured_at | TEXT | |
| last_received_at | TEXT | 현황 "마지막 수신"에 사용 |

**상태 전이 (센서 하나)**:

```text
처음 본 센서 ──► epoch=0, last_f_cnt=f_cnt
업링크 f_cnt:
  f_cnt ≥ last_f_cnt                         → 같은 epoch, last_f_cnt=f_cnt
  f_cnt < last_f_cnt 이고 리셋 조건 모두 참     → epoch+1, last_f_cnt=f_cnt
     (f_cnt ≤ reset_max, last_f_cnt−f_cnt ≥ min_drop, measured_at > last_measured_at)
  그 밖의 f_cnt < last_f_cnt (중복·늦게 옴)     → 같은 epoch, 상태 변경 없음
  measured_at ≤ last_measured_at (늦게 온 메시지) → 같은 epoch, 상태 변경 없음
     (구현 중 추가: 재가입 직후 이전 구간의 늦은 메시지가 큰 fCnt로 상태를 끌어올리지 않게 한다)
join 이벤트                                   → epoch+1, last_f_cnt=-1
```

readings 삽입과 devices 갱신은 **같은 트랜잭션**에서 한다(메시지 하나 = 트랜잭션 하나).

## collector_events — 수집기 이벤트

| 컬럼 | 타입 | 비고 |
|---|---|---|
| id | INTEGER PK | |
| ts | TEXT | 수집기 시계 |
| kind | TEXT | `start`, `stop`, `connect`, `connect_refused`, `disconnect`, `subscribe`, `heartbeat`, `join`, `epoch_bump`, `parse_error`, `unmapped_key`, `queue_drop`, `write_error` |
| device_id | TEXT | 해당하면 |
| detail | TEXT | 짧은 설명(JSON 문자열). 비밀값 금지 |

- `unmapped_key`는 (모델, 키) 조합마다 수집기 실행당 한 번만 기록해 행이 불어나지 않게 한다. 건수는 메모리에서 세어 heartbeat detail에 넣는다.
- 인덱스: `(kind, ts)`.

## rejected — 버린 메시지 (원인 조사용, 선택)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| id | INTEGER PK | |
| received_at | TEXT | |
| topic | TEXT | |
| reason | TEXT | `not_json`, `missing_field:<이름>` 등 |
| payload | TEXT | 앞 4KB까지 |

## schema_meta

| key | value |
|---|---|
| schema_version | `1` |

시작할 때 버전을 확인한다. 버전이 다르면 수집기가 멈추고 메시지를 띄운다(조용히 바꾸지 않음).

## 검증 규칙 (spec FR 연결)

| 규칙 | FR |
|---|---|
| topic이 `event/up`(측정) 또는 `event/join`(epoch)일 때만 처리 | FR-001 |
| `devEui`, `fCnt`, `rxInfo[].nsTime`(1개 이상), `object`가 없으면 rejected | FR-004, FR-007 |
| 숫자가 아닌 값(bool 포함)은 그 항목만 건너뛰고 이벤트로 남김 | Edge case |
| 메시지 하나의 모든 행 + devices 갱신을 한 트랜잭션으로 | FR-008 |
| 기본키 중복은 무시 | FR-009 |

## 파생 지표 (저장하지 않고 조회 때 계산)

- **CoverageRow**: (location, item, 기간) → 건수, 마지막 measured_at, 채워진 1분 칸, 전체 칸, 커버율
- **DeviceRate**: (device_id, 기간) → 받은 메시지 수, 기대 수, 건너뛴 수, 수신률, 관측 간격 중앙값, 간격 기준 커버율, 마지막 수신
- **Gap**: (location, item, 시작, 끝, 길이(분), 추정 원인, 근거)
