# 과제 1 데이터 수집 체계: 확인 기준 판정

spec: [specs/001-data-collection/spec.md](../../../specs/001-data-collection/spec.md) · 확인 절차: [quickstart.md](../../../specs/001-data-collection/quickstart.md)

| 측정 조건 | 값 |
|---|---|
| 보드 | 보드 B (`team5-b`), Jetson Orin Nano Super 8GB, JetPack 7.2.1, 전원 모드 MAXN_SUPER |
| 서비스 | `tsinfer-collector-yj` (systemd, 2026-10-21 16:03:57 KST부터 가동) |
| 데이터 | 실데이터 (교육장 MQTT 브로커, `application/+/device/+/event/up`) |
| 시각 기준 | `measured_at = min(rxInfo[].nsTime)` |

## 판정표

| 기준 | 내용 | 결과 | 판정 | 근거 |
|---|---|---|---|---|
| AC-1 | 브로커를 껐다 켜도 1분 안에 수집 재개 | 대기 (사용자 시험 T029) | — | [fault-tests.md](fault-tests.md#ac-1-브로커-껐다-켜기-t029-사용자-실행-대기) |
| AC-2 | 보드 재부팅 후 수집 자동 재개 | 계획하지 않은 실제 재부팅 2번(10/21 18:55, 10/22 09:32). 사람 개입 없이 부팅 → 첫 행 약 21초, 서비스 시작 → 첫 행 6~15초. 참고: 강제 종료 → 5초 뒤 재시작 | **통과** | [fault-tests.md](fault-tests.md#ac-2-재부팅-t030-대신-실제-재부팅-2번) |
| AC-3 | 24시간 커버율 95% 이상, 빠진 구간은 원인 설명 | 10-21 16:04 ~ 10-22 16:04 KST. 판정 11개 모두 통과: 1분 칸 98.3~98.8%, 회의실 간격 기준 98.6%. 빠진 구간 = 보드 멈춤 2번, 의도한 수집기 중지 1번(과제 8 시험), 회의실 무선 손실 3건 | **통과** | [coverage-2026-10-22.md](coverage-2026-10-22.md) |
| D-1 | 수집 현황 보고(공간·항목별 커버율 표, 결측 구간과 추정 원인) | 24시간 보고서와 해석 | **통과** | [coverage-2026-10-22.md](coverage-2026-10-22.md) |
| D-2 | 형식 변경 대응 기록 1건 | 키 이름 변경 + 새 모델: 설정 7줄로 대응, 코드 변경 0줄 | **통과** | [format-change.md](format-change.md) |
| 기타 | 상주 메모리 100MB 이하 | 최대 19.9MB | **통과** | [fault-tests.md](fault-tests.md#상주-메모리-t043) |
| 기타 | 테스트 | 91개 통과 | **통과** | `.venv/bin/python -m pytest` |

## 회의실(10분 간격 센서) 판정

회의실은 AM103 한 대(약 10분 간격)만 있어서 1분 칸 커버율이 구조적으로 약 10%다. 그래서 **센서 전송 간격 기준 커버율 95%로 판정**한다(2026-10-21 사용자 결정, decisions Q2b). 1분 커버율과 수신률도 함께 보고한다.

## 미통과·대기 항목의 다음 행동

1. T029 (AC-1): `sudo apt install mosquitto` 후 quickstart 4a, 그리고 4b. 팀 공지 불필요(내 계정 트래픽만), 소요 약 15분.
2. T030 (AC-2): 팀 단톡방 공지 → 보드 B `sudo reboot`. 소요 약 5분.
3. T035 (AC-3): 10/22 16:04 KST 이후 `python -m collector report --start 2026-10-21T07:04Z --out docs/reports/001-data-collection/coverage-2026-10-22.md` → 해석 절 작성.
