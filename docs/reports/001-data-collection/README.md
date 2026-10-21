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
| AC-2 | 보드 재부팅 후 수집 자동 재개 | 대기 (사용자 시험 T030). 참고: 프로세스 강제 종료 → 5초 뒤 자동 재시작, 9초 뒤 첫 행, 빠진 데이터 없음 | — | [fault-tests.md](fault-tests.md#자동-재시작-t028) |
| AC-3 | 24시간 커버율 95% 이상, 빠진 구간은 원인 설명 | 대기 (10/22 16:04 KST 이후 T035). 참고: 첫 15분 시험 보고에서 판정 항목 8개 100%, 회의실 3개 보류 | — | coverage-2026-10-22.md (예정) |
| D-1 | 수집 현황 보고(공간·항목별 커버율 표, 결측 구간과 추정 원인) | `report` 명령 완성. 24시간 보고서는 T035 | 도구 완료 | `python -m collector report` |
| D-2 | 형식 변경 대응 기록 1건 | 키 이름 변경 + 새 모델: 설정 7줄로 대응, 코드 변경 0줄 | **통과** | [format-change.md](format-change.md) |
| 기타 | 상주 메모리 100MB 이하 | 최대 19.9MB | **통과** | [fault-tests.md](fault-tests.md#상주-메모리-t043) |
| 기타 | 테스트 | 91개 통과 | **통과** | `.venv/bin/python -m pytest` |

## 회의실(10분 간격 센서) 판정

회의실은 AM103 한 대(약 10분 간격)만 있어서 1분 칸 커버율이 구조적으로 약 10%다. 2026-10-22 강사 확인 전까지는 1분 커버율, 간격 기준 커버율, 수신률을 모두 보고하고 판정은 "보류"로 둔다(decisions Q2). 답을 받으면 이 절과 spec Assumptions를 고친다.

## 미통과·대기 항목의 다음 행동

1. T029 (AC-1): `sudo apt install mosquitto` 후 quickstart 4a, 그리고 4b. 팀 공지 불필요(내 계정 트래픽만), 소요 약 15분.
2. T030 (AC-2): 팀 단톡방 공지 → 보드 B `sudo reboot`. 소요 약 5분.
3. T035 (AC-3): 10/22 16:04 KST 이후 `python -m collector report --start 2026-10-21T07:04Z --out docs/reports/001-data-collection/coverage-2026-10-22.md` → 해석 절 작성.
