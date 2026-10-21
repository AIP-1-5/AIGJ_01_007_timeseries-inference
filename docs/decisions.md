# 결정 기록

되돌릴 수 있는 기술 선택을 한 줄씩 남긴다. 형식: **결정 / 이유 / 버린 대안**. 근거 상세는 링크한 문서에 있다.

## 과제 1 데이터 수집 (`specs/001-data-collection`)

근거: [research.md](../specs/001-data-collection/research.md) R1~R10, [plan.md](../specs/001-data-collection/plan.md)

| # | 날짜 | 결정 | 이유 | 버린 대안 |
|---|---|---|---|---|
| R1 | 2026-10-21 | 측정 시각 = `min(rxInfo[].nsTime)`. 원본 `time`은 `gw_time`, 받은 시각은 `received_at`으로 따로 저장 | 게이트웨이 한 대(`…dccc`)의 시계가 약 5분 빠르다. nsTime은 네트워크 서버 하나의 시계라 모든 센서에 일관된다 | `time`(5분 오차), 수신 시각(지연·재전달이 시각을 흔듦), 게이트웨이별 보정(언제 맞춰질지 모름) |
| R2a | 2026-10-21 | 중복 판단: 기본키 `(device_id, fcnt_epoch, f_cnt, item)` + `INSERT OR IGNORE`, 먼저 온 값 유지 | 같은 센서·같은 fCnt 중복 2건은 deduplicationId·nsTime이 서로 달라 그것으로 못 거른다 | deduplicationId, `(device_id, measured_at)`, 값 비교 |
| R2b | 2026-10-21 | epoch(재가입 구간 번호)는 join 이벤트, 또는 `f_cnt ≤ 1000` ∧ 감소 ≥ 100 ∧ nsTime이 이전보다 늦음일 때 +1 | 정상 센서 fCnt 최솟값이 10,371이라 1000 이하는 재가입 직후뿐이다. 중복·늦은 메시지는 감소폭이 작고 nsTime이 이르다 | 감소만 보면 올림(늦은 메시지로 잘못 올라감), 기준값을 코드에 고정 |
| R3 | 2026-10-21 | paho-mqtt 2.x, QoS 1, `clean_session=False`, 재연결 1~10초, `on_connect`에서 재구독, client id에 사용자·호스트 | AC-1(60초) 안에 들어오고, 수집기만 잠깐 끊긴 동안의 메시지를 브로커가 다시 준다. 팀원과 id가 겹치지 않는다 | aiomqtt(의존성 추가), 재연결 직접 구현, `clean_session=True` |
| R4 | 2026-10-21 | 콜백은 큐(`queue.Queue(50000)`)에 넣기만, 쓰기 스레드 1개가 DB에 쓴다 | DB가 느려져도 MQTT 수신·keepalive가 막히지 않는다. 분당 15건이라 5만 건이면 이틀 넘게 버틴다 | 콜백에서 바로 쓰기, 멀티 프로세스 |
| R5 | 2026-10-21 | SQLite WAL, `synchronous=NORMAL`, `busy_timeout=5000`, 읽기는 `mode=ro` | 읽기가 쓰기를 막지 않는다. 하루 14MB·1년 5GB로 NVMe에 충분하다 | InfluxDB에 쓰기(권한 미정), CSV(중복·동시 읽기 어려움), PostgreSQL(메모리·운영 부담) |
| R6 | 2026-10-21 | 설정은 `config/collector.toml`(tomllib), 비밀값은 `.env`만. 설정에 비밀값 키가 있으면 시작 오류. 매핑 없는 키는 기본 `store` | 형식 변경을 코드 수정 없이 대응(원칙 VI). 새 항목이 조용히 사라지지 않게 한다 | YAML(외부 패키지), 코드 안 딕셔너리 |
| R7 | 2026-10-21 | systemd 시스템 유닛 `tsinfer-collector-yj.service`, `User=yj`, `Restart=always`, enable | 로그인 없이 부팅 때 시작(AC-2), 죽으면 5초 뒤 재시작. 이름에 사용자를 넣어 팀원과 안 겹친다 | 사용자 유닛+linger(이것도 sudo), cron `@reboot`(재시작 없음), tmux |
| R8 | 2026-10-21 | 커버율은 1분 칸 기준(원문 정의) + 센서 수신률 + 간격 기준 커버율. 결측 원인 4분류. heartbeat 60초 | 원문 정의를 지키면서 긴 간격 센서를 따로 판단한다. heartbeat로 비정상 종료 시간도 안다 | 1분 칸만, 센서 간격만 |
| R9 | 2026-10-21 | 장애 시험: 로컬 mosquitto 재시작 3회 + 내 계정(uid yj)의 1883 출력만 차단 + 보드 B 재부팅. **이번 구현에서는 명령만 준비하고 실행은 사용자가 한다** | 공용 브로커는 끌 수 없다. 팀원이 같은 보드를 써서 보드 전체 1883 차단은 남의 수집기도 끊는다 | 와이파이 전체 차단(Tailscale도 끊김), 보드 전체 1883 차단 |
| R10 | 2026-10-21 | 테스트 fixture는 `../uplink_raw.jsonl`의 정리본만(tenantId·applicationId·devAddr·data·context·좌표 삭제). 원본은 커밋 안 함 | 저장소가 Public이다. 필요 없는 내부 ID를 공개하지 않는다 | 원본 그대로 커밋, 합성 메시지만 사용 |
| Q1 | 2026-10-21 | `event/join`도 구독한다. 측정값은 저장하지 않고 epoch 판정·이벤트 기록에만 쓴다 (사용자 승인) | 재가입을 fCnt 규칙보다 확실하게 잡는다. `join_topic = ""`로 끌 수 있다 | `event/up`만 구독(fCnt 규칙만 남음) |
| Q2 | 2026-10-21 | 회의실처럼 간격이 긴 센서의 AC-3 판정은 10/22 강사 확인 전까지 1분 커버율·간격 커버율·수신률을 모두 보고 (사용자 승인) | 원문 정의를 바꾸지 않으면서 판단 근거를 다 남긴다 | 지금 판정 기준을 임의로 정함 |
| Q3 | 2026-10-21 | DB 위치는 `~/data/tsinfer/collector.db`. `/srv/team5`가 생기면 설정만 바꾼다 (사용자 승인) | 보드 B에 `/srv/team5`가 아직 없다 | 공용 폴더를 내가 만들기(sudo, 팀 결정 사항) |
| I1 | 2026-10-21 | sudo·systemctl·iptables·git push는 Claude가 실행하지 않고 명령만 보여 준다 (사용자 지시) | 보드 B를 팀이 같이 쓰고, 되돌리기 어렵다 | Claude가 직접 실행 |
