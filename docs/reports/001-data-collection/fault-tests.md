# 과제 1 시험 기록

## 기본 수집 확인 (T021, US1)

**측정 조건**: 보드 B, 전원 모드 MAXN_SUPER, 실데이터(교육장 MQTT 브로커), 2026-10-21 06:28:25Z ~ 06:38:25Z (10분, `timeout -s INT 600 .venv/bin/python -m collector run`), 수동 실행(서비스 아님), 설정 `config/collector.toml`, 코드: `ecbd64c` 위 커밋 전 작업 트리, 시각 기준 `min(rxInfo[].nsTime)`.

| 확인 | 결과 | 판정 |
|---|---|---|
| 공간 4곳 | 실습실, 사무실, 사무실 밖, 회의실 | 통과 |
| 센서 수 | 13대 (AM103 2, AM107 2, EM300-TH 4, EM320-TH 2, EM500-CO2 2, WS302 1) | 통과 (13대 안팎) |
| 저장 | 처리 98건, 저장 메시지 94건, 중복 무시 4건, 버림 0, 매핑 안 된 키 0, 큐 버림 0, 오류 0 | 통과 |
| 행 수 | 349행 (실습실 255, 사무실 70, 사무실 밖 20, 회의실 4) | — |
| 항목 이름 | `co2`, `temperature`, `humidity`, `pressure`, `tvoc`, `illumination`, `infrared`, `infrared_and_visible`, `activity`, `battery`, `LAeq`, `LAI`, `LAImax` = InfluxDB measurement 이름(research R6) | 통과 |
| 중복 `(device_id, fcnt_epoch, f_cnt, item)` | 0건 | 통과 |
| 센서별 수신률 | 13대 모두 받은 수 = fCnt 범위(건너뜀 0) | 통과 |
| 수신 지연 (`received_at − measured_at`) | 평균 0.23초, 최대 0.24초 | — |
| 이벤트 | start 1, connect 1, subscribe 1, heartbeat 9, disconnect 1(정상 종료), stop 1 | 통과 |
| epoch | 모든 센서 0 (재가입 없음) | — |

**해석**

- 10분 동안 중복이 4건 들어왔고 모두 걸러졌다. 샘플(30건 중 2건)과 비슷한 비율이다. 중복 거르기가 없으면 건수가 약 4% 부풀었을 것이다.
- 지연이 0.2초대라 측정 시각(nsTime)과 받은 시각이 거의 같다. 수집기 쪽 밀림은 없었다.
- **새로 안 사실**: 실습실 EM300-TH 한 대(끝 4자리 `1523`)도 10분 동안 1건만 보냈다. 전송 간격이 약 10분일 가능성이 있다(추정, 24시간 데이터로 확인 필요). 맞다면 `config/collector.toml`의 `[devices]`에 `interval_s = 600`을 추가해야 간격 기준 커버율이 맞게 나온다. 실습실은 같은 항목을 재는 다른 센서가 있어 공간 단위 1분 커버율에는 영향이 적다(추정).
- 회의실 AM103(`9152`), 실습실 AM107(`0101`)도 1건씩이었다. 설정에 이미 600초로 들어 있는 센서다.

## AC-1 브로커 껐다 켜기 (T029, 사용자 실행 대기)

명령: [quickstart.md](../../../specs/001-data-collection/quickstart.md) 4a(로컬 mosquitto 재시작 3회), 4b(uid yj의 1883만 120초 차단).

| 회차 | 방법 | 복구 시각 | 첫 새 행 시각 | 걸린 초 | 60초 미만 |
|---|---|---|---|---|---|
| 1 | 로컬 브로커 재시작 | | | | |
| 2 | 로컬 브로커 재시작 | | | | |
| 3 | 로컬 브로커 재시작 | | | | |
| 4 | 1883 차단 해제 (uid yj) | | | | |

## AC-2 재부팅 (T030, 사용자 실행 대기)

| 부팅 시각 | 서비스 시작 시각 | 첫 새 행 시각 | 부팅 → 첫 행(초) | 사람 개입 |
|---|---|---|---|---|
| | | | | 없음 |

## 자동 재시작 (T028, 사용자 실행 대기)

`sudo systemctl kill -s KILL tsinfer-collector-yj` 뒤 다시 active가 될 때까지의 초:
