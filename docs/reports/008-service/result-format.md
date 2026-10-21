# 결과 형식 문서: 교육장 공기질 추론 서비스 v1 (나영준, 007)

보드 B(`team5-b`)의 `tsinfer-service-yj`가 **매분 05초**에 공간별 60분 예측·이상·상태를 계산해 MQTT로 발행하고, 같은 결과를 조회 API로 돌려준다. 이 문서만 보고 쓸 수 있게 적었다.

## 1. MQTT

| 토픽 | retained | 내용 |
|---|---|---|
| `tsinfer/yj/lab` · `office` · `meeting` · `outside` | 예 | 공간별 결과(아래 2절). 실습실·사무실·회의실·사무실 밖 |
| `tsinfer/yj/status` | 예 | 서비스 상태: `time`, `data_time`, `duration_ms`(주기 처리 시간), `models`(모델별 `loaded`/`fallback`) |
| `tsinfer/yj/alerts` | 아니오 | 경보 이벤트(4절). 상태가 바뀔 때만 온다 |

- QoS 1, JSON(UTF-8). 브로커는 교육장 브로커(수집기와 같음).
- **retained**라서 새로 구독한 쪽도 접속하자마자 각 공간의 마지막 결과를 받는다.

## 2. 공간 결과 필드

| 필드 | 타입 | 단위·뜻 |
|---|---|---|
| `space`, `space_id` | 문자열 | 한글 이름, 영문 id |
| `time` | ISO 8601 UTC | 계산한 시각 |
| `data_time` | ISO 8601 UTC | 입력에 쓴 **마지막 완성 5분 칸의 시작**. 예측은 이 칸 끝(+5분)부터 5분 간격 |
| `status` | `ok` / `degraded` / `error` | `degraded` = 데이터 끊김·수집기 중단 경보가 있음 |
| `current.co2` · `temperature` · `humidity` | 수 | ppm · °C · % (5분 공간 대표값, 센서 중앙값) |
| `forecast.minutes` | 정수 12개 | 5, 10, …, 60 |
| `forecast.co2`, `forecast.temperature` | 수 12개 | 각 분 뒤 예측값(ppm, °C). 사무실 밖은 `null`(예측 대상 아님) |
| `forecast.model` | 문자열 | `gru_schedule` 등 모델 이름, 모델이 없으면 `fallback`(= 현재값 유지) |
| `co2_alert` | 객체 | `limit`(1000ppm), `minutes_to_exceed`(60분 안에 넘는 첫 시점의 분, 없으면 `null`), `exceeded_now` |
| `anomaly` | 객체 | `score`(재구성 오차), `threshold`, `is_anomaly`, `cause`(가장 큰 오차 항목 `co2`/`temperature`/`humidity`), `rules`(규칙별 true/false), `model`(`autoencoder`/`fallback`) |
| `state.occupied` | 객체 또는 `null` | 실습실만. `probability`(0~1), `value`(사용중 여부) |
| `state.vent_alert` | 객체 또는 `null` | "30분 안에 1,000ppm 초과" 선제 경보. `probability`, `value` |
| `alerts` | 배열 | 지금 켜져 있는 경보(`type`, `since`, `detail`) |

예시(`tsinfer/yj/lab`, 모델 적재 전 fallback 상태, 배열 일부 생략):

```json
{"space": "실습실", "space_id": "lab", "time": "2026-10-21T08:23:28Z", "data_time": "2026-10-21T08:15:00Z",
 "status": "ok", "current": {"co2": 823.6, "temperature": 23.23, "humidity": 48.25},
 "forecast": {"model": "fallback", "step_min": 5, "minutes": [5, 10, 15, "…"], "co2": [823.6, 823.6, 823.6, "…"],
              "temperature": [23.23, 23.23, 23.23, "…"]},
 "co2_alert": {"limit": 1000, "exceeded_now": false, "minutes_to_exceed": null},
 "anomaly": {"model": "fallback", "score": null, "threshold": null, "is_anomaly": false, "cause": null, "rules": {}},
 "state": {"occupied": null, "vent_alert": null}, "alerts": []}
```

## 3. 조회 API (`http://<보드 B>:8807`)

| 요청 | 응답 |
|---|---|
| `GET /health` | `{"status": "ok"|"stale", "last_cycle", "age_s", "cycles", "failures", "models"}`. 마지막 주기가 3분보다 오래되면 `stale` |
| `GET /spaces` | 공간 목록(`id`, `name`, `status`, `data_time`) |
| `GET /spaces/lab` (또는 `/spaces/실습실`) | 2절의 공간 결과 전체 |
| `GET /spaces/meeting/forecast?minutes=30` | `{"space": "회의실", "minutes": 30, "co2": 845.2, "temperature": 21.1, "unit": {...}, "model", "data_time"}`. `minutes`는 5~60의 5의 배수(아니면 400) |
| `GET /alerts` | `active`(켜진 경보), `recent_events`(최근 이벤트 50개) |

## 4. 경보

| `type` | 뜻 | 원인 |
|---|---|---|
| `collector_down` (공간 `*`) | 수집기 heartbeat가 3분 넘게 없음 | 수집기 중단 |
| `data_gap` | 그 공간의 마지막 측정이 max(5분, 센서 간격 × 2)보다 오래됨 | 센서·무선·수집기 |
| `stuck` | 한 센서의 CO₂가 60분 동안 완전히 같음(`detail.device_id` = 원인 센서) | 센서 멈춤 |
| `out_of_range` | 최근 10분에 정제 범위 밖 값(`detail.device_id`, `item`) | 센서 오류 |
| `anomaly`, `rule_*` | 오토인코더 이상, 규칙(튄 값·값 고정·영점·냉방·환기) | 과제 4 |
| `co2_limit` · `vent_soon` | 60분 안 1,000ppm 초과 예측 · 30분 안 초과 분류기 경보 | 환기 필요 |

**중복 억제**: 같은 (공간, 종류)는 처음 한 번 `status: "new"`, 계속 켜져 있으면 **30분마다** `repeat`, 꺼지면 `resolved`로 `tsinfer/yj/alerts`에 보낸다. 매분 보내면 받는 쪽이 알림을 끄게 되기 때문이다. 공간 결과의 `alerts`에는 매분 켜진 경보가 다 들어 있다.

## 5. 동작 규칙

- **갱신**: 매분. 입력은 수집 DB의 최근 26시간이고, 학습과 **같은 전처리**(`tsdata.prep`)를 거친다. 막 시작된 5분 칸(1~2분치)은 쓰지 않는다. 학습 때 5분 칸은 늘 5분치 평균이었기 때문이다. 그래서 `data_time`은 최대 약 10분 전이다.
- **fallback**: 모델 파일이 없거나 읽기에 실패하면 예측은 현재값 유지, 이상 탐지는 규칙만으로 계속 돈다(`model: "fallback"`). 모델 파일을 바꾸면 다음 주기에 서비스 재시작 없이 새 모델을 쓴다.
- **데이터 끊김**: 수집이 끊겨도 서비스는 매분 돈다. 입력이 15분 넘게 비면 그 값은 `null`이고, `data_gap`/`collector_down` 경보와 `status: "degraded"`로 알린다. 수집이 돌아오면 다음 주기에 `resolved`가 오고 `ok`로 돌아온다.
- **장애**: 한 주기가 실패해도 다음 주기는 정상으로 돈다(`/health`의 `failures`). 보드 재부팅 뒤 systemd가 자동으로 시작한다.
