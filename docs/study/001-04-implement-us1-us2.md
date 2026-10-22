# 학습 노트: 과제 1 구현 — US1 수집 저장, US2 서비스 등록 준비

날짜: 2026-10-21 · 코드: `collector/`, `deploy/`, `tests/` · 작업: tasks.md T001~T025

## 처음 나온 용어

| 용어 | 비유 | 쉬운 정의 | 백엔드 대응 |
|---|---|---|---|
| 스레드 | 한 식당 안의 직원 여러 명 | 한 프로세스 안에서 동시에 도는 실행 흐름 | 톰캣 요청 스레드, `@Async` |
| `queue.Queue` | 주방 주문표 꽂이 | 스레드 사이에 안전하게 일을 넘기는 대기열. 가득 차면 `put_nowait`가 바로 실패한다 | `BlockingQueue`, `LinkedBlockingQueue(capacity)` |
| `BEGIN IMMEDIATE` | 장부를 펴자마자 "내가 쓴다" 표시 | 트랜잭션 시작과 동시에 쓰기 잠금을 잡는 SQLite 방식 | `SELECT ... FOR UPDATE`로 시작하는 트랜잭션 |
| `paho` 루프 스레드 | 전화 받는 전담 직원 | MQTT 네트워크 송수신·재연결을 맡는 백그라운드 스레드(`loop_start`) | Netty 이벤트 루프 |
| fixture (pytest) | 시험 전에 미리 깔아 두는 재료 | 테스트마다 같은 설정·데이터를 만들어 주는 함수 | JUnit `@BeforeEach`, `@TestConfiguration` |
| `systemd-analyze verify` | 서류 제출 전 맞춤법 검사 | 유닛 파일 문법을 설치 없이 검사 | `mvn validate` |

## 무엇을 했나

- **설정** `collector/config.py`: TOML 읽기·검증, 비밀값 키 금지, `.env` 파서(환경변수가 우선).
- **파서** `collector/parser.py`: 업링크 → 측정 시각(`min(nsTime)`), 태그, 항목 매핑. 숫자가 아닌 값은 그 항목만 뺀다.
- **epoch** `collector/epoch.py:15`: 순수 함수. 구현 중 규칙 하나를 더했다(`epoch.py:23`): 측정 시각이 마지막보다 이른 메시지는 상태를 바꾸지 않는다.
- **저장소** `collector/store.py`: WAL, 메시지 1건 = 트랜잭션 1개(`store.py:123`), `INSERT OR IGNORE`(`store.py:168`).
- **쓰기 스레드** `collector/writer.py`: 큐 → 파싱 → 저장, 60초 heartbeat(`writer.py:190`), 종료 시 큐 비우기.
- **MQTT** `collector/mqtt_client.py`: 재연결 1~10초(`:38`), 연결될 때마다 재구독(`:63`), 첫 연결 실패도 계속 재시도(`:85`).
- **CLI** `run`, `replay`, `check-config`. **서비스** `deploy/tsinfer-collector.service.in`, `deploy/install-service.sh`(`--print`로 미리보기).
- 테스트 66개 통과. 실제 브로커로 10분 수집(T021) 결과는 `docs/reports/001-data-collection/fault-tests.md`.

## 왜 이렇게 했나

- epoch 판정을 순수 함수로 뺀 이유: DB 없이 상태 전이만 따로 테스트할 수 있다(테스트 11개).
- `replay`와 `run`이 같은 `Pipeline`을 쓰는 이유: 시험(replay)에서 통과한 저장 경로가 운영(run)과 똑같아야 시험이 의미 있다.
- 연결 이벤트도 큐로 보내는 이유: DB에 쓰는 스레드를 하나로 유지해 잠금 경쟁을 없앤다.
- 로그에서 MQTT 주소를 뺐다: 이 과제에서는 주소도 `.env`에만 두는 값이다.

## 짚어 둘 것 (트랜잭션·동시성·인덱스·병목)

- **롤백**: `_tx`가 예외 때 `ROLLBACK` 후 다시 던진다. Writer는 그 예외를 잡아 `write_error`로 남기고 다음 메시지로 간다(테스트 `test_failure_rolls_back_whole_message`, `test_bad_message_does_not_kill_thread`).
- **동시성**: DB 연결은 하나, 쓰는 스레드도 하나다. MQTT 스레드는 큐에 넣기만 한다. 큐가 가득 차면 버리고 센다(막히지 않음). 버린 수는 heartbeat·`queue_drop` 이벤트에 남는다.
- **인덱스**: readings에 PK + 인덱스 2개. 행 하나 넣을 때 B-tree 3개를 고친다. 분당 약 70행이라 부담이 없다(추정).
- **데이터가 늘면**: 쓰기보다 `collector_events`가 먼저 커진다(heartbeat 하루 1,440행). 1년 약 53만 행이라 `(kind, ts)` 인덱스로 충분하다(추정). 긴 읽기 트랜잭션이 WAL 파일을 키우는 위험은 그대로다.
- **알려진 한계**: 재가입 직전 구간의 늦은 메시지는 새 epoch 번호로 저장된다. fCnt가 크게 달라 키는 안 겹치지만, epoch 번호만 보고 구간을 나누면 그 한 건이 섞인다(드묾, 추정).

## 말로 설명할 수 있어야 할 것 3개

1. 같은 메시지를 두 번 replay해도 행 수가 그대로인 이유(PK + `INSERT OR IGNORE`, 첫 값 유지).
2. 브로커가 끊겼다가 돌아왔을 때 코드 어디서 무엇이 일어나는가(paho 루프가 재연결 → `_on_connect` → 재구독 → `connect`/`subscribe` 이벤트).
3. `systemctl kill -s KILL`로 죽였을 때 `stop` 이벤트가 없는데도 꺼진 시간을 아는 방법(heartbeat 간격 공백 + 다음 `start`).

## 스스로 확인할 질문 2개

1. 쓰기 스레드가 예외로 죽으면 `run` 프로세스는 어떻게 되고, systemd는 무엇을 하는가? (`__main__.py`의 `writer_dead`)
2. `clean_session=False`인데 같은 client id로 수집기 두 개를 동시에 띄우면 무슨 일이 생기는가?
