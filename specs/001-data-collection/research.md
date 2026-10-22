# Research: 과제 1 데이터 수집 체계

기준일 2026-10-21, 보드 B(`team5-b`)에서 확인. 근거 자료: `../uplink_raw.jsonl`(30건), 교육장 InfluxDB `iot-bucket` 조회, `../docs/environment.md`.

## R1. 측정 시각 기준

- **Decision**: `measured_at = min(rxInfo[].nsTime)`. 원본 `time`은 `gw_time`, 수집기가 받은 시각은 `received_at`으로 따로 저장한다. 모두 UTC ISO 8601, 마이크로초까지(`2026-10-21T05:06:15.008186Z`). nsTime의 나노초는 마이크로초로 자른다.
- **Rationale**: 샘플에서 게이트웨이 `24e124fffef5dccc`만 거친 메시지는 `time`이 nsTime보다 약 4분 56초 빠르다(예: AM103 `time` 05:11:15.475, nsTime 05:06:19.435). 두 게이트웨이를 거친 메시지도 `time`은 어느 게이트웨이를 따르는지 일정하지 않다. nsTime은 네트워크 서버 하나의 시계라 모든 센서에 일관된다. InfluxDB `_time`도 05:57 조회 시 모든 센서가 현재 시각(05:57:41Z) 이전이었다. 게이트웨이 시계를 따르지 않으므로 우리 기준과 맞다.
  "네트워크가 30초 밀렸다 한꺼번에 도착" 문제: 밀린 구간이 브로커·수집기 쪽이면 nsTime은 원래 시각을 유지하고 `received_at`만 밀린다. 둘의 차이로 지연을 잴 수 있다.
- **Alternatives**: `time`(게이트웨이 시계 오차 5분), 수신 시각(수집기 지연·재전달이 시각을 왜곡), 게이트웨이별 보정값(시계가 언제 맞춰질지 모름).

## R2. 중복 판단과 fcnt_epoch 기준값

- **Decision**: 기본키 `(device_id, fcnt_epoch, f_cnt, item)`과 `INSERT OR IGNORE`(사용자 결정). 센서별 상태(`fcnt_epoch`, `last_f_cnt`, `last_measured_at`)를 DB에 두고 다음 규칙으로 epoch를 올린다.
  1. 해당 센서의 ChirpStack join 이벤트(`application/+/device/+/event/join`)를 받으면 `fcnt_epoch += 1`, `last_f_cnt = -1`. 재가입 뒤 첫 업링크가 규칙 2로 한 번 더 올라가지 않게 한다.
  2. 업링크에서 다음을 **모두** 만족하면 `fcnt_epoch += 1`:
     - `f_cnt <= reset_max` (기본 **1000**): "0 근처로 돌아감"
     - `last_f_cnt - f_cnt >= min_drop` (기본 **100**): "크게 줄어듦"
     - `measured_at > last_measured_at`: 새 메시지임. 늦게 온 메시지는 nsTime이 이전 것보다 이르다.
  3. 그 밖의 감소(중복, 늦게 온 메시지)는 epoch를 바꾸지 않는다. 이런 메시지는 원래 epoch에 그대로 저장되고, 이미 있으면 무시된다.
  4. 기준값은 `config/collector.toml`의 `[fcnt]`에 두고 바꿀 수 있다.
- **Rationale (기준값 근거)**:
  - 샘플의 fCnt 최솟값은 10,371이고 나머지는 22,663~212,970이다. 정상 운영 중인 센서는 1000 아래에 있지 않다. 1분 간격 센서도 1000을 넘는 데 16.7시간이 걸린다. 그래서 1000 이하는 최근 재가입 말고는 나오지 않는다.
  - 중복은 같은 fCnt(차이 0)로 온다(샘플 2건). 늦게 오는 메시지는 브로커 재전달을 포함해도 수 건 차이다. 100건 차이는 1분 간격 기준 100분 늦게 온다는 뜻이고, 그때도 세 번째 조건(nsTime이 이전보다 늦음)이 막는다.
  - 규칙 2가 놓치는 경우가 있다. 재가입한 지 100건이 안 돼 다시 재가입하면 감소폭이 100보다 작다. 이 경우는 규칙 1(join 이벤트)이 잡는다. 둘 다 놓쳐도 결과는 일부 새 메시지가 중복으로 버려지는 것뿐이다. 이는 센서별 수신률 급락으로 현황에 드러난다.
  - LoRaWAN 32비트 fCnt가 넘쳐 0으로 돌아가는 일은 1분 간격으로 8천 년이 걸려 고려하지 않는다.
- **사용자 결정과의 차이(승인 필요)**: 사용자 결정은 "event/up만 구독"이다. 하지만 "join 이벤트가 올 때 epoch를 올린다"를 하려면 `event/join`도 구독해야 한다. join은 **측정값으로 저장하지 않고** epoch 갱신과 이벤트 기록에만 쓴다. 구독하지 않기로 하면 규칙 2만 남는다(`[mqtt].join_topic = ""`로 끌 수 있게 둔다).
- **Alternatives**: `deduplicationId`(중복 2건이 서로 다른 값을 가짐), `(device_id, measured_at)`(중복 2건의 nsTime이 0.2ms~8ms 다름), 값 비교(값이 같아도 다른 측정일 수 있음).

## R3. MQTT 클라이언트와 재연결

- **Decision**: `paho-mqtt` 2.x(순수 파이썬, ARM64 문제 없음), MQTT v3.1.1, `loop_start()` 백그라운드 스레드를 쓴다. `reconnect_delay_set(min_delay=1, max_delay=10)`으로 끊김 후 최대 10초 간격으로 다시 시도한다. 재구독은 `on_connect` 안에서 한다(재연결마다 다시 구독). 첫 연결이 실패해도(`connect_async`) 루프가 계속 다시 시도한다.
  - QoS 1로 구독하고 `clean_session=False`, 고정 client id `tsinfer-collector-<사용자>-<호스트>`를 쓴다. 브로커가 세션을 유지하면 수집기만 잠깐 끊긴 동안의 메시지를 다시 받는다. 다시 받은 메시지는 기본키로 걸러진다. 팀원 수집기와 client id가 겹치면 서로 끊어 내므로 사용자·호스트를 넣는다.
- **Rationale**: AC-1은 "브로커 복구 후 60초 안"이다. 백오프 상한 10초 + 연결·구독 1~2초 + 센서 전송 간격(1분 센서 기준 최대 60초) 중 앞의 둘만 수집기 몫이다. 판정은 "다음 메시지가 들어왔을 때 바로 저장되는가"로 한다(quickstart 참고). keepalive는 30초로 두어 끊김을 빨리 알아챈다.
- **Alternatives**: `asyncio-mqtt`/`aiomqtt`(의존성 추가, 이점 없음), 재연결을 직접 구현(paho 기능으로 충분), `clean_session=True`(끊김 중 메시지 손실이 더 큼).

## R4. 수신과 저장 분리

- **Decision**: paho 콜백은 `(topic, payload bytes, received_at)`을 `queue.Queue(maxsize=50000)`에 넣기만 한다. 쓰기 스레드 하나가 꺼내 파싱 → epoch 판정 → 메시지 하나를 트랜잭션 하나로 저장한다. 큐가 가득 차면 버리고 `queue_drop` 이벤트를 센다. 종료 신호(SIGTERM)를 받으면 큐를 비우고 닫는다.
- **Rationale**: 메시지량은 센서 13대 × 분당 1건 이하 ≈ 분당 15건이다. 50,000건이면 쓰기가 완전히 멈춰도 2일 넘게 버틴다. 메모리는 건당 약 2KB로 최대 100MB다. 콜백에서 디스크를 기다리지 않으므로 keepalive가 밀려 끊기는 일이 없다.
- **Alternatives**: 콜백에서 바로 쓰기(DB 잠금 시 MQTT 루프가 멈춤), 멀티 프로세스(이 규모에 과함, 원칙 VII).

## R5. 저장소: SQLite WAL

- **Decision**: 파이썬 기본 `sqlite3`를 쓰고 다음 PRAGMA를 건다: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`. 쓰기는 수집기 프로세스 하나만 한다. 현황·분석은 `file:<path>?mode=ro` URI로 읽기 전용 연결을 쓴다. DB 경로는 설정에 두고, 기본값은 `~/data/tsinfer/collector.db`다(저장소 밖).
- **Rationale**: WAL에서는 읽기가 쓰기를 막지 않는다. "수집과 학습·추론이 같은 저장소를 동시에 쓰면" 생기는 잠금 문제를 피한다. 남는 위험은 긴 읽기 트랜잭션 동안 WAL 파일이 커지는 것뿐이다. 그래서 분석은 내보낸 사본(과제 2)이나 짧은 쿼리로 한다. `synchronous=NORMAL`이면 전원이 끊길 때 마지막 몇 트랜잭션을 잃을 수 있지만, DB가 깨지지는 않는다.
  크기 추정: 하루 메시지 약 15,000건 × 평균 항목 4.5개 ≈ 68,000행이다. 행+인덱스가 약 200B라 하루 약 14MB, 1년 약 5GB다. NVMe 여유 공간이 207GB라 문제없다(심화 보관 정책은 범위 밖).
- **`/srv/team5`**: 보드 B에는 이 폴더가 아직 없다(2026-10-21 `ls` 확인). 그래서 개인 홈 아래 기본값을 쓰고, 폴더가 생기면 설정만 바꾼다.
- **Alternatives**: InfluxDB에 쓰기(내부망 의존, 쓰기 권한 미정), CSV 파일(중복 판단·동시 읽기 어려움), PostgreSQL(보드 메모리·운영 부담).

## R6. 설정 파일과 비밀값

- **Decision**: `config/collector.toml`을 파이썬 3.12 기본 `tomllib`으로 읽는다. 모델별 `object` 키 → 항목 이름 매핑, 장치별 전송 간격(커버율 보조 판정용), fcnt 기준값, 토픽, DB 경로를 담는다. 비밀값(MQTT 주소·계정·비밀번호)은 `.env`에만 둔다. systemd는 `EnvironmentFile=`로, 수동 실행은 작은 `.env` 파서로 읽는다(추가 의존성 없음). 설정 파일에는 비밀값 키를 허용하지 않는다(있으면 시작 시 오류).
- **항목 이름**: InfluxDB의 measurement 이름은 object 키와 같다(`co2`, `temperature`, `humidity`, `pressure`, `tvoc`, `illumination`, `infrared`, `infrared_and_visible`, `activity`, `battery`, `LAeq`, `LAI`, `LAImax`). 그래서 기본 매핑은 키 이름을 그대로 쓴다. InfluxDB에는 `rssi`, `snr`, `channel` 같은 무선 메타도 measurement로 있지만, 이번 저장 대상에서는 뺀다(측정값이 아님. 필요하면 설정으로 추가).
- **설정에 없는 키**: `[unmapped] policy = "store"`(기본: 키 이름 그대로 저장하고 경고) 또는 `"drop"`이다. 기본을 store로 둬서 새 항목이 조용히 사라지지 않게 한다.
- **Alternatives**: YAML(외부 패키지 필요), 코드 안 딕셔너리(원칙 VI 위반).

## R7. 상시 실행: systemd

- **Decision**: 시스템 유닛 `tsinfer-collector-yj.service`를 쓴다(`User=yj`, `WorkingDirectory=`저장소, `EnvironmentFile=`저장소/.env, `ExecStart=`.venv의 python `-m collector run`). `Restart=always`, `RestartSec=5`, `After=network-online.target`, `Wants=network-online.target`, `WantedBy=multi-user.target`이고, `systemctl enable --now`로 켠다. 이름에 사용자를 넣어 팀원 서비스와 겹치지 않게 한다. 설치는 `deploy/install-service.sh`(sudo 한 번)로 한다.
- **Rationale**: yj 계정은 sudo 그룹이다. 시스템 유닛은 로그인 없이 부팅 때 시작된다(AC-2). 네트워크가 늦게 올라와도 R3의 재연결이 처리한다.
- **Alternatives**: 사용자 유닛 + `loginctl enable-linger`(현재 Linger=no, 이것도 sudo 필요, 장점 없음), cron `@reboot`(죽었을 때 재시작 없음), tmux(재부팅 후 수동).

## R8. 커버율·수신률·결측 원인

- **Decision**:
  - **1분 칸 커버율 (원문 정의)**: 기간 [start, end)를 UTC 1분 칸으로 나눈다. (location, item)마다 `measured_at`이 그 칸에 든 행이 하나라도 있으면 채워진 칸이다. 커버율 = 채워진 칸 ÷ 전체 칸.
  - **센서 수신률**: 센서마다 기간 안 (epoch, f_cnt)의 고유 개수 ÷ 기대 개수다. 기대 개수는 epoch별 (max f_cnt − min f_cnt + 1)의 합이다. 건너뛴 수 = 기대 − 받은 수다.
  - **전송 간격 기준 커버율 (보조)**: 센서마다 설정의 `interval_s`(없으면 관측 간격 중앙값) 칸으로 같은 계산을 한다. 회의실 AM103 판정용이다(spec Assumptions, 10/22 확인 대기).
  - **결측 구간**: (location, item)별로 연속된 빈 칸을 하나의 구간으로 묶는다. 표에는 길이 ≥ 3분인 구간만 나열하고, 짧은 구간은 건수만 적는다. EM500-CO2만 있는 공간의 2분 간격 빈칸이 표를 덮지 않게 하기 위해서다. 원인 판정 순서는 다음과 같다.
    1. 그 구간에 수집기 이벤트로 "꺼짐"(heartbeat 공백 > 90초, 또는 start/stop 사이) 또는 "브로커 끊김"이 겹치면 **수집기 중단/연결 끊김**이다.
    2. 그 공간 센서들의 fCnt가 구간을 사이에 두고 건너뛰었으면 **센서·무선 쪽 손실**이다.
    3. fCnt가 이어지는데 그 구간에 저장된 행이 없으면(재전달로 나중에 들어온 경우 포함) **수집기 쪽 손실**이다.
    - **2026-10-21 수정 (decisions R8b)**: 3은 틀렸다. fCnt가 이어지면 그 사이 센서가 보내지 않은 것(**전송 간격**)이다. 수집기 쪽 손실은 `queue_drop`·`write_error` 이벤트로 판정하고, 순서는 꺼짐/끊김 → 수집기 쪽 손실 → fCnt 건너뜀·재가입 → 전송 간격 → 원인 미상이다.
    4. 그 공간의 모든 센서가 구간 동안 아무것도 보내지 않았고 fCnt 정보도 없으면(구간 앞뒤에 데이터 없음) **원인 미상**이다.
  - **수집기 heartbeat**: 쓰기 스레드가 60초마다 `collector_events`에 `heartbeat`를 쓴다(하루 1,440행). 프로세스가 비정상 종료해 stop 기록이 없을 때 꺼진 시간을 알아내기 위해서다.
- **판정 대상 항목**: AC-3 판정은 과제 데이터 항목 `co2`, `temperature`, `humidity`로 한다. 다른 항목도 표에는 나오지만 판정에서는 뺀다(보고서에 명시).

## R9. 시험 방법 (AC-1, AC-2)

- **AC-1 브로커 껐다 켜기**: 교육장 공용 브로커는 우리가 끌 수 없다. 그래서 두 가지로 확인한다.
  1. **로컬 브로커 시험(원문 그대로)**: 보드 B에 `mosquitto` 브로커를 설치하고(`sudo apt install mosquitto`, ARM64 패키지 있음) 시험용 설정으로 수집기를 띄운다. 시험용 발행기가 샘플 메시지를 fCnt·nsTime을 바꿔 5초마다 발행한다. `systemctl stop mosquitto` → 30초 대기 → `start`를 3회 반복하고, 브로커 시작 시각부터 첫 새 행까지를 잰다.
  2. **실제 브로커 경로 차단 (내 계정만)**: `sudo iptables -I OUTPUT -p tcp --dport 1883 -m owner --uid-owner yj -j REJECT --reject-with tcp-reset`로 2분 끊었다가 같은 규칙을 지우고, 첫 새 행까지를 잰다. `-m owner --uid-owner yj`는 uid yj 프로세스가 보낸 패킷만 막는다. 팀원도 같은 보드 B에서 수집기를 돌리므로 보드 전체의 1883을 막으면 남의 수집기까지 끊긴다(2026-10-21 사용자 지시로 변경). 서비스는 `User=yj`로 돌기 때문에 이 규칙에 걸린다. 와이파이 전체를 끊으면 Tailscale SSH도 끊기므로 쓰지 않는다.
- **AC-2 재부팅**: 보드 B `sudo reboot`(보드 B는 실험용이라 허용. 보드 A는 금지) 후 로그인하지 않은 채 기다린다. 이후 `systemctl status`와 DB의 첫 새 행 시각을 확인한다. 재부팅 전에 팀에 알린다.
- **이번 구현 범위 (2026-10-21 사용자 지시)**: 로컬 mosquitto 설치, iptables, 재부팅은 이번 구현에서 실행하지 않는다. sudo가 필요하고 팀이 같이 쓰는 보드에 영향을 준다. quickstart 4·3에 명령만 남기고, 사용자가 직접 실행한 뒤 결과를 `fault-tests.md`에 적는다. 시험 보조 스크립트(`tests/broker/`)도 이번에는 만들지 않는다.

## R10. 테스트 데이터와 공개 저장소

- **Decision**: `tests/fixtures/`에는 `../uplink_raw.jsonl`에서 만든 **정리본**만 넣는다. tenantId·applicationId·deviceProfileId·devAddr·data·context·위치 좌표를 지운다. 테스트에 필요한 devEui·tags·fCnt·object·rxInfo(gatewayId, nsTime)·time·deduplicationId는 남긴다. 원본 jsonl은 커밋하지 않는다.
- **Rationale**: 저장소가 Public이다. ID류는 비밀값은 아니지만 필요 없는 내부 식별자를 공개하지 않는다.
