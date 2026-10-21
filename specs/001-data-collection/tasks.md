---
description: "과제 1 데이터 수집 체계 구현 작업 목록"
---

# Tasks: 과제 1 데이터 수집 체계

**Input**: `specs/001-data-collection/` (plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md)

**Prerequisites**: plan.md "열린 질문" 1~3은 2026-10-21 결정됨(join 구독 승인, docs/decisions.md Q1~Q3).

**실행 권한 (2026-10-21 사용자 지시)**: sudo·systemctl·iptables·git push는 Claude가 실행하지 않고 명령만 보여 준다. 표시 **[사용자]** 작업은 사용자가 직접 실행한다. 장애 시험(로컬 mosquitto, iptables, 재부팅)은 이번 구현에서 빼고 quickstart 3·4에 명령만 남긴다.

**Tests**: 포함한다. plan의 Testing 항목과 spec 각 스토리의 Independent Test가 자동 테스트와 장애 시험을 요구한다.

**Organization**: 사용자 스토리별로 묶었다. US1·US2는 P1, US3·US4는 P2다.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 다른 파일을 다루고 앞선 미완료 작업에 기대지 않아 동시에 할 수 있음
- **[Story]**: US1~US4 (spec.md)

---

## Phase 1: Setup

**Purpose**: 패키지 골격과 개발 환경

- [X] T001 plan.md의 Source Code 구조대로 빈 패키지·폴더를 만든다: `collector/__init__.py`, `config/`, `config/examples/`, `deploy/`, `tests/__init__.py`, `tests/unit/`, `tests/integration/`, `tests/fixtures/`, `docs/reports/001-data-collection/`
- [X] T002 [P] `requirements.txt`에 `paho-mqtt>=2,<3`, `requirements-dev.txt`에 `pytest>=8`을 쓰고, `pytest.ini`(testpaths=tests)를 만든다
- [X] T003 [P] `.venv`를 만들고 의존성을 설치한다(`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt`). paho-mqtt가 aarch64에서 import되는지 확인한다
- [X] T004 [P] `../uplink_raw.jsonl`(30건)에서 내부 ID를 지운 정리본 `tests/fixtures/uplink_sample.jsonl`을 만든다. 지울 것: `tenantId`, `tenantName`, `applicationId`, `applicationName`, `deviceProfileId`, `devAddr`, `data`, `rxInfo[].context`, `rxInfo[].location`. 남길 것: devEui, deviceProfileName, deviceName, tags, fCnt, time, deduplicationId, object, rxInfo[].gatewayId·gwTime·nsTime. 정리에 쓴 일회성 스크립트는 커밋하지 않는다. 원본 jsonl도 커밋하지 않는다(research R10)

---

## Phase 2: Foundational

**Purpose**: 모든 스토리가 쓰는 설정·파서·저장소·epoch 판정

**⚠️ CRITICAL**: 이 단계가 끝나야 스토리 작업을 시작한다

- [X] T005 [P] `tests/unit/test_config.py`: 정상 로드, TOML 문법 오류 → `ConfigError`, 설정에 `password`/`username`/`token`/`host` 키가 있으면 오류, `.env` 파서(주석·빈 줄·따옴표), 환경변수가 `.env`보다 우선하는지를 테스트한다
- [X] T006 `collector/config.py`를 구현한다: `tomllib`로 `config/collector.toml` 로드, contracts/config.md의 섹션(`mqtt`, `storage`, `fcnt`, `unmapped`, `models.<모델>.keys`, `models.<모델>.interval_s`, `devices."<devEui>".interval_s`, `report`) 검증, 기본값 채우기, `~` 경로 확장. 비밀값 키 금지. MQTT 접속값은 환경변수 `MQTT_HOST`/`MQTT_PORT`/`MQTT_USERNAME`/`MQTT_PASSWORD`, 없으면 `--env-file`(기본 `.env`)에서 읽는다. 설정 오류는 `ConfigError`(CLI 종료 코드 2)
- [X] T007 [P] `config/collector.toml`을 contracts/config.md 예시 그대로 만든다(모델 6종 키 매핑, 모델별 interval_s, 회의실 AM103 `24e124725d089152`와 실습실 AM107 `24e124128c140101`의 `interval_s = 600`, `[fcnt] reset_max = 1000, min_drop = 100`, `[unmapped] policy = "store"`, `db_path = "~/data/tsinfer/collector.db"`)
- [X] T008 [P] `tests/unit/test_parser.py`: fixture 30건을 파싱해 (a) `measured_at == min(rxInfo[].nsTime)`이고 나노초를 마이크로초로 자른 `YYYY-MM-DDTHH:MM:SS.ffffffZ` 형식인지, (b) 게이트웨이 `24e124fffef5dccc`만 거친 AM103 `24e124725d081175`의 `gw_time`이 `measured_at`보다 4~6분 늦은지, (c) tags 누락 센서(`24e124785c389818`, `24e124725d089152`)의 point·spot이 `''`인지, (d) 매핑된 항목 이름, (e) 숫자가 아닌 값(문자열·bool)은 그 항목만 빠지는지, (f) JSON 아님·`devEui`/`fCnt`/`rxInfo[].nsTime`/`object` 누락 시 `RejectedMessage(reason)`인지, (g) 매핑에 없는 키가 policy `store`면 키 이름 그대로, `drop`이면 빠지고 둘 다 unmapped 목록에 들어가는지를 테스트한다
- [X] T009 `collector/parser.py`를 구현한다: `parse_uplink(topic, payload_bytes, received_at, cfg) -> UplinkMessage | RejectedMessage`, `parse_join(topic, payload_bytes) -> device_id`. `UplinkMessage`는 device_id, model, location, point, spot, f_cnt, measured_at, gw_time, received_at, gateway_ids(쉼표 연결), items[(item, value)], unmapped_keys를 가진다. 토픽이 `.../event/up`, `.../event/join`이 아니면 무시한다(FR-001)
- [X] T010 [P] `tests/unit/test_epoch.py`: data-model.md 상태 전이를 모두 테스트한다. 처음 본 센서 → epoch 0, 증가 → 같은 epoch, 같은 fCnt(중복) → 변화 없음, 소폭 감소(예: 212969 → 212965) → 변화 없음, `f_cnt ≤ 1000` ∧ 감소 ≥ 100 ∧ measured_at이 더 늦음 → epoch+1, 같은 감소인데 measured_at이 더 이름(늦게 온 메시지) → 변화 없음, f_cnt=5000으로 감소(reset_max 초과) → 변화 없음, join → epoch+1 및 `last_f_cnt=-1` 후 f_cnt=0이 들어와도 추가 증가 없음
- [X] T011 `collector/epoch.py`를 순수 함수로 구현한다: `decide(state: DeviceState | None, f_cnt, measured_at, reset_max, min_drop) -> (epoch, new_state, bumped: bool)`, `on_join(state) -> new_state`(research R2)
- [X] T012 [P] `tests/integration/test_store.py`: 임시 DB에서 PRAGMA(`journal_mode=wal`, `synchronous=NORMAL`=1, `busy_timeout=5000`) 확인, 스키마 생성, `schema_meta.schema_version='1'`, 버전이 다르면 오류, 메시지 1건 저장 시 항목 수만큼 행 생성, 같은 메시지 재저장 시 행 수·`measured_at` 불변, 저장 도중 예외가 나면 그 메시지의 행과 devices 갱신이 모두 없는지(FR-008), `mode=ro` 연결로 읽는 동안 쓰기가 성공하는지(FR-021)를 테스트한다
- [X] T013 `collector/store.py`를 구현한다: data-model.md의 테이블 `readings`(PK `(device_id, fcnt_epoch, f_cnt, item)`, 인덱스 `(location, item, measured_at)`, `(measured_at)`, 빈 태그는 NULL이 아니라 `''`), `devices`, `collector_events`(인덱스 `(kind, ts)`), `rejected`(payload 앞 4KB), `schema_meta`. `save_uplink(msg)`는 `BEGIN IMMEDIATE` → devices 상태 읽기 → `epoch.decide` → `INSERT OR IGNORE` 행들 → devices 갱신 → (epoch이 올랐으면) `epoch_bump` 이벤트 → COMMIT 순서로 한다. `save_join(device_id)`, `log_event(kind, device_id, detail)`, `save_rejected(...)`, `open_readonly(path)`도 만든다. DB 폴더가 없으면 만든다

**Checkpoint**: 설정·파서·epoch·저장소 단위 테스트 통과

---

## Phase 3: User Story 1 - 센서 메시지를 끊김 없이 저장한다 (Priority: P1) 🎯 MVP

**Goal**: MQTT 업링크를 받아 측정값을 항목마다 한 행씩, 중복 없이 저장한다

**Independent Test**: `replay`로 fixture 30건 → 저장 메시지 28건, 중복 2건 무시. 실제 브로커로 10분 `run` 뒤 4개 공간과 센서 13대 안팎의 행이 생긴다(quickstart 1·2)

- [X] T014 [P] [US1] `tests/integration/test_replay.py`: fixture를 replay하면 처리 30, 저장 메시지 28(고유 `(device_id, f_cnt)`), 중복 무시 2가 나오고, 한 번 더 replay해도 `readings` 행 수가 같으며, 중복 쌍(`24e124126d152862` fCnt 104884, `24e124136d151836` fCnt 212969)의 measured_at이 처음 들어온 줄 값인지를 테스트한다
- [X] T015 [P] [US1] `tests/integration/test_writer.py`: 큐에 메시지 1,000건을 넣고 쓰기 스레드가 모두 저장하는지, 큐 가득 참 시 `queue_drop`을 세고 수신 쪽이 막히지 않는지(FR-003), heartbeat가 `heartbeat_s` 간격으로 기록되는지(테스트에서는 1초로 설정), 종료 신호 시 큐를 비운 뒤 `stop` 이벤트를 남기는지를 테스트한다
- [X] T016 [US1] `collector/writer.py`를 구현한다: `Writer(store, cfg)` 스레드. `queue.Queue(maxsize=storage.queue_max)`에서 `(topic, payload, received_at)`을 꺼내 parser → store로 넘긴다. RejectedMessage는 `save_rejected` + `parse_error` 이벤트로, unmapped 키는 (모델, 키)별 실행당 1회 `unmapped_key` 이벤트로 남기고 건수는 메모리에서 센다. `heartbeat_s`마다 `heartbeat`(detail에 처리·저장·중복·버림·unmapped·queue_drop 누적 건수)를 쓴다. `stop()` 시 큐를 비우고 `stop` 이벤트를 쓴다. 한 메시지 처리 예외가 스레드를 죽이지 않게 한다
- [X] T017 [US1] `collector/__main__.py`에 `replay` 명령을 구현한다(contracts/cli.md). 줄마다 `{"topic","payload"}` 또는 업링크 JSON 하나를 받고, `--db`로 DB 경로를 덮어쓴다. 끝에 `처리 N, 저장 메시지 N, 중복 무시 N, 버림 N, 매핑 안 된 키 N`을 출력한다. 쓰기 경로는 Writer와 같은 함수를 공유한다
- [X] T018 [US1] `collector/mqtt_client.py`를 구현한다: paho-mqtt 2.x `CallbackAPIVersion.VERSION2`, MQTTv311, `client_id = f"{client_id_prefix}-{사용자}-{호스트}"`, `clean_session` 설정값, 계정은 환경변수, `on_message`는 `(msg.topic, msg.payload, 현재 UTC)`를 `put_nowait`만 한다(가득 차면 drop 카운트). 측정값 토픽은 `mqtt.uplink_topic`만 구독한다. 다른 토픽(`gateway/`, `tsinfer/`, `xflow/`, `event/status`)은 구독하지 않는다(FR-001)
- [X] T019 [US1] `collector/__main__.py`에 `run` 명령을 구현한다: 설정 로드(오류면 종료 코드 2) → store 열기 → `start` 이벤트 → Writer 시작 → MQTT 시작 → SIGTERM/SIGINT 시 MQTT 정지 → Writer 정지 → 종료 코드 0. 로그는 stdout에 `시각 수준 이벤트 key=value` 한 줄씩 쓰고, 비밀값은 출력하지 않는다(FR-002, FR-016)
- [X] T020 [US1] join 처리를 연결한다: `mqtt.join_topic`이 비어 있지 않으면 구독하고, writer가 join 토픽을 `store.save_join` + `join` 이벤트로 처리한다(측정값 저장 없음). plan 열린 질문 1이 "구독 안 함"으로 결정되면 `config/collector.toml`에 `join_topic = ""`로 둔다
- [X] T021 [US1] 보드 B에서 실제 브로커로 `run`을 10분 실행한 뒤, `readings`를 직접 쿼리한다. 공간 4곳(실습실, 사무실, 회의실, 사무실 밖), 센서 수, 항목 이름이 InfluxDB measurement 이름과 같은지, 같은 `(device_id, fcnt_epoch, f_cnt, item)` 중복이 0인지 확인하고, 결과를 `docs/reports/001-data-collection/fault-tests.md`의 "기본 수집 확인" 절에 숫자로 남긴다

**Checkpoint**: 수동 실행으로 실제 데이터가 쌓인다(MVP)

---

## Phase 4: User Story 2 - 장애 뒤 사람 손 없이 다시 수집한다 (Priority: P1)

**Goal**: 브로커 재시작·네트워크 끊김·프로세스 종료·재부팅 뒤 자동 복구 (AC-1, AC-2)

**Independent Test**: 로컬 브로커 재시작 3회와 1883 차단(uid yj만) 시험에서 매번 60초 안에 새 행이 생긴다. 재부팅 뒤 로그인 없이 수집이 이어진다(quickstart 3·4, 사용자 실행)

- [X] T022 [P] [US2] `tests/unit/test_mqtt_client.py`: 가짜 paho 클라이언트로 `on_connect`마다 구독을 다시 거는지, `reconnect_delay_set(min_delay=reconnect_min_s, max_delay=reconnect_max_s)`가 호출되는지, `on_disconnect`/`on_connect`가 `disconnect`/`connect`/`subscribe` 이벤트를 남기는지, 첫 연결 실패(`connect_async` 후 거부) 시 예외로 종료하지 않는지를 테스트한다
- [X] T023 [US2] `collector/mqtt_client.py`에 복구 동작을 넣는다: `connect_async` + `loop_start`, `reconnect_delay_set(1, 10)`(설정값), keepalive `keepalive_s`(30), `on_connect`에서 구독 재등록 및 `connect`·`subscribe` 이벤트, `on_disconnect`에서 `disconnect` 이벤트(reason code 포함). 이벤트는 writer 큐를 통해 같은 쓰기 스레드가 기록한다(DB 쓰기는 한 스레드만)
- [X] T024 [P] [US2] `deploy/tsinfer-collector.service.in`을 만든다: `User=@USER@`, `WorkingDirectory=@REPO@`, `EnvironmentFile=@REPO@/.env`, `ExecStart=@REPO@/.venv/bin/python -m collector run --config @REPO@/config/collector.toml`, `Restart=always`, `RestartSec=5`, `After=network-online.target`, `Wants=network-online.target`, `[Install] WantedBy=multi-user.target`
- [X] T025 [US2] `deploy/install-service.sh`를 만든다(Claude는 만들기와 `--print` 확인까지만, sudo 실행은 사용자): `--print`면 채운 유닛을 출력만 한다. 그 밖에는 현재 사용자·저장소 경로로 틀을 채워 `/etc/systemd/system/tsinfer-collector-<사용자>.service`에 설치하고, `daemon-reload` → `enable --now` → `systemctl status`를 실행한다. `.env`·`.venv`가 없으면 중단하고 안내한다. 다시 실행해도 안전하다(멱등)
- [ ] ~~T026~~ (범위 밖, 2026-10-21) 로컬 브로커 시험 도구를 만든다: `tests/broker/collector-local.toml`(localhost:1883, 시험용 DB `~/data/tsinfer/test-local.db`), `tests/broker/publisher.py`(fixture 메시지를 5초마다 fCnt +1, nsTime=현재 시각으로 바꿔 `application/test/device/<devEui>/event/up`에 발행, 브로커가 꺼져 있으면 재시도), `tests/broker/restart_test.sh N`(N회 `sudo systemctl stop mosquitto` → 30초 → `start`, start 시각과 시험 DB의 다음 `received_at` 차이를 초로 출력)
- [ ] ~~T027~~ (범위 밖, 2026-10-21. 명령은 quickstart 4b, uid yj만 차단) `tests/broker/block_1883.sh SEC`를 만든다: `sudo iptables -I OUTPUT -p tcp --dport 1883 -j REJECT` → SEC초 대기 → 같은 규칙 삭제(`trap`으로 중단 시에도 삭제) → 해제 시각과 운영 DB의 다음 `received_at` 차이를 출력한다. 와이파이 전체를 끊지 않는다(Tailscale SSH 유지)
- [x] T028 [US2] **[사용자]** 보드 B에서 서비스를 설치하고(`sudo deploy/install-service.sh`) `sudo systemctl kill -s KILL tsinfer-collector-yj`로 자동 재시작을 확인한다
- [ ] T029 [US2] **[사용자]** AC-1 시험(quickstart 4a·4b 명령, iptables는 `-m owner --uid-owner yj`): `sudo apt install mosquitto` 뒤 quickstart 4a(재시작 3회)와 4b(120초 차단)를 실행하고, 회차별 초를 `docs/reports/001-data-collection/fault-tests.md`에 측정 조건(보드, 전력 모드, 날짜, 커밋)과 함께 기록한다. 60초 이상인 회차가 있으면 원인과 함께 미통과로 적는다
- [ ] T030 [US2] **[사용자]** AC-2 시험: 팀에 알린 뒤 **보드 B만** `sudo reboot`를 하고 로그인 없이 대기한다. `journalctl -b -u tsinfer-collector-yj`의 시작 시각, 부팅 시각, DB의 첫 새 행 시각을 `docs/reports/001-data-collection/fault-tests.md`에 기록한다

**Checkpoint**: AC-1·AC-2 판정 숫자가 기록됨

---

## Phase 5: User Story 3 - "지금 잘 쌓이고 있는가"를 한눈에 본다 (Priority: P2)

**Goal**: 현황 조회와 수집 현황 보고(D-1), 24시간 커버율 판정(AC-3)

**Independent Test**: 손으로 만든 작은 DB에서 커버율·수신률·결측 원인이 손계산과 같다. 24시간 실데이터로 보고서가 나온다(quickstart 5)

- [x] T031 [P] [US3] `tests/unit/test_coverage.py`: 합성 행으로 (a) 1분 칸 커버율(공간 안 센서 둘 중 하나만 있어도 채워짐, [start, end) 경계), (b) 센서 수신률(epoch 두 개에 걸친 기대 수 = epoch별 `max−min+1`의 합, 건너뜀 수), (c) 간격 기준 커버율(interval_s=600), (d) 결측 구간 묶기와 `gap_list_min_minutes=3` 미만은 건수만, (e) 원인 6분류(decisions R8b: 수집기 중단, 연결 끊김, queue_drop·write_error → 수집기 쪽 손실, fCnt 건너뜀·재가입 → 센서·무선 손실, fCnt 이어짐 → 전송 간격, 근거 없음 → 원인 미상), (f) 끊김 경고(마지막 수신이 간격의 2배 초과, 간격 모르면 5분)를 테스트한다
- [x] T032 [US3] `collector/coverage.py`를 구현한다: 읽기 전용 연결로 `coverage_by_location_item(start, end)`, `device_rates(start, end)`(관측 간격 중앙값 포함, 설정 interval_s 우선), `gaps(start, end)`(원인과 근거 문자열), `collector_downtime(start, end)`(heartbeat·start·stop·disconnect·connect 이벤트로 계산)를 만든다. 1분 칸은 `substr(measured_at,1,16)`으로 한다(research R8)
- [x] T033 [US3] `collector/report.py`와 `__main__.py`에 `status` 명령을 구현한다(contracts/cli.md 출력 형식, `--hours`, `--json`, 수집기 마지막 heartbeat·연결, 끊김 경고)
- [x] T034 [US3] `collector/report.py`와 `__main__.py`에 `report` 명령을 구현한다: contracts/cli.md의 보고서 구성 1~6을 Markdown으로 쓴다. 측정 조건(기간, 실데이터, 보드 hostname, 서비스 이름, 시각 기준 nsTime, `git rev-parse HEAD`, 설정 파일 해시), `report.judged_items`(co2·temperature·humidity)의 95% 판정, 회의실처럼 간격이 긴 센서만 있는 공간·항목에는 "간격 기준 커버율·수신률 병기, 판정 기준 10/22 확인 대기" 주석을 단다. "해석" 절은 빈 틀로 둔다
- [ ] T035 [US3] 서비스가 24시간 이상 돈 뒤 `report`로 `docs/reports/001-data-collection/coverage-<날짜>.md`를 만든다. 95% 미만 공간·항목과 결측 구간마다 원인을 확인해 해석 절에 쓴다(왜 그렇게 나왔는가, 무엇을 해야 하는가). "생각해 볼 점" 두 질문(측정 시각 기준, 수집·학습 동시 접근)에 research R1·R5를 근거로 답한다

**Checkpoint**: AC-3 판정 숫자와 D-1 보고서가 있음

---

## Phase 6: User Story 4 - 설정만 바꿔 형식 변경·새 센서에 대응한다 (Priority: P2)

**Goal**: 형식 변경을 설정 파일 수정만으로 대응하고 기록한다(D-2)

**Independent Test**: 키 이름이 바뀐 메시지를 replay → 설정만 바꿈 → 같은 항목으로 저장되고, `git diff --stat`에 설정 파일만 나온다(quickstart 6)

- [x] T036 [P] [US4] `tests/fixtures/format_change.jsonl`을 만든다: fixture의 EM300-TH 메시지 3건에서 `object.temperature` 키를 `temp`로 바꾸고 fCnt를 겹치지 않게 바꾼다. 가상의 새 모델 `EM300-TH-v2`(키 `temp`, `humi`) 메시지 2건도 넣는다
- [x] T037 [P] [US4] `config/examples/format-change.toml`을 만든다: 운영 설정에 `[models.EM300-TH.keys] temp = "temperature"`와 `[models.EM300-TH-v2]`(keys, interval_s)를 더한 사본
- [x] T038 [P] [US4] `tests/integration/test_format_change.py`: 운영 설정으로 replay하면 `temp`/`humi`가 키 이름 그대로 저장되고 `unmapped_key` 이벤트가 남는지, 예시 설정으로 replay하면 `temperature`/`humidity`로 저장되고 unmapped가 0인지를 테스트한다
- [X] T039 [US4] `__main__.py`에 `check-config` 명령을 구현한다(종료 코드 0/2, 오류 위치 출력). quickstart 0과 install-service.sh가 써서 US1 때 먼저 구현함
- [x] T040 [US4] D-2 시연을 하고 `docs/reports/001-data-collection/format-change.md`에 기록한다: 가정한 상황, 변경 전·후 설정 diff, 바꾸기 전·후 replay 결과(항목별 행 수), 코드 변경 0줄 근거(`git diff --stat`), 운영 서비스 적용 절차(`check-config` → `systemctl restart`), 설정으로 대응 못 하는 한계(contracts/config.md 마지막 절)

**Checkpoint**: D-2 기록 완료

---

## Phase 7: Polish & 확인 기준 판정

- [x] T041 [P] `README.md`에 수집기 사용법을 짧게 넣는다(설치, `.env`, 서비스 설치, `status`, `report`)
- [x] T042 [P] 커밋 전 점검: `git status`와 `git diff --cached`에 `.env`, `*.db`, 원본 `uplink_raw.jsonl`, 내부 ID(tenantId·applicationId)가 없는지 `grep`으로 확인한다
- [x] T043 `.venv/bin/python -m pytest` 전체를 통과시킨다(broker 마커 제외). 상주 메모리를 `systemctl status`의 Memory 또는 `ps -o rss`로 재고 100MB 이하인지 `fault-tests.md`에 기록한다
- [ ] T044 확인 기준 판정표를 `docs/reports/001-data-collection/README.md`에 쓴다: AC-1(회차별 초), AC-2(부팅→첫 행 초), AC-3(공간·항목별 커버율과 95% 통과/미통과, 원인), D-1·D-2 링크, 미통과 항목의 원인과 다음 행동. 회의실 판정은 10/22 강사 답을 반영해 spec Assumptions도 함께 고친다

---

## Dependencies & Execution Order

- **Phase 1 → Phase 2 → 스토리들**. Phase 2(T005~T013)가 모든 스토리를 막는다.
- **US1 (T014~T021)**: Phase 2 뒤. MVP다.
- **US2 (T022~T030)**: T018·T019(US1의 MQTT·run)에 기댄다. T024는 US1과 동시에 만들 수 있다. T026·T027은 범위 밖, T028~T030은 사용자 실행.
- **US3 (T031~T035)**: 저장소(T013)만 있으면 T031~T034를 할 수 있다. T035는 US2 서비스가 24시간 돈 뒤에 한다.
- **US4 (T036~T040)**: replay(T017)와 config(T006)만 있으면 된다. US2·US3과 독립이다.
- **Phase 7**: T044는 T029·T030·T035·T040 뒤에 한다.

```text
Setup ─► Foundational ─► US1 ─► US2 ─► (24h 운영) ─► T035 ─► T044
                          │                 ▲
                          ├─► US3 (T031~T034)
                          └─► US4
```

## Parallel Examples

- Phase 2: T005·T008·T010·T012(테스트 4개)를 동시에 쓰고, 그다음 T006·T009·T011·T013을 구현한다. T007은 언제든 할 수 있다.
- US1: T014·T015를 동시에 작성한다.
- US2: T022·T024를 동시에 만든다(서로 다른 파일).
- US2의 24시간 운영이 도는 동안 US3(T031~T034)과 US4(T036~T040)를 진행한다.

## Implementation Strategy

1. **MVP**: Phase 1~3. 수동 실행으로 실데이터가 쌓이는 것을 확인한다.
2. **바로 상시화**: US2 T023~T028로 서비스를 등록해 24시간 카운트를 일찍 시작한다. AC-3에 연속 24시간이 필요하므로 가장 먼저 시계를 돌린다.
3. 운영이 도는 동안 US3·US4를 만들고, 장애 시험(T029·T030)은 24시간 측정 구간 **밖**에서 하거나, 시험으로 생긴 결측을 보고서에 "의도한 중단"으로 표시한다.
4. 마지막에 T044 판정표로 통과/미통과를 숫자와 함께 보고한다.
