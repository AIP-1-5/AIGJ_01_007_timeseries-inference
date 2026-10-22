# Feature Specification: 과제 8 상시 추론 서비스

**Feature Branch**: `002-data-baseline` (과제 3~8을 같은 브랜치에서 빠르게 진행, 2026-10-21 사용자 지시) · **Created**: 2026-10-21 · **Status**: Draft

## 완료 조건 (원문 그대로)

- **AC-1**: **12시간 무중단** 가동 기록
- **AC-2**: 가동 중 수집기를 10분 멈췄을 때 '데이터 끊김' 경보가 나고, 다시 켜면 정상으로 돌아온다
- **AC-3**: 조회 API로 공간별 예측·이상·상태를 받을 수 있다
- **AC-4**: 결과 형식 문서(토픽·필드·단위·예시)

산출물: **D-1** 예측 서비스 v1(상시 운영 상태) · **D-2** 12시간 가동 로그 요약(주기별 처리 시간, 경보 발생 내역, 장애 시험 결과) · **D-3** 결과 형식 문서

## User Story (P1)

다른 팀의 대시보드·LLM 에이전트·냉난방 제어가 MQTT(retained)나 조회 API로 공간별 최신 판단(60분 예측과 "몇 분 뒤 1,000ppm 초과", 이상 여부·원인, 사용 여부·환기 선제 경보, 데이터 끊김)을 받는다. 서비스는 매분 스스로 돌고, 끊김·실패·재부팅 뒤에도 사람 손 없이 이어진다.

## Requirements

- **FR-001** 매분(벽시계 05초) 계산. 입력 = 수집 DB 최근 26시간, 전처리 = `tsdata.prep`(학습과 같은 함수). 막 시작된 5분 칸은 제외.
- **FR-002** 공간별 결과: 60분 예측(CO₂·온도, 5분 간격)과 1,000ppm 도달 분, 이상 점수·원인 항목·규칙, 사용 여부(실습실)·환기 경보, 경보 목록.
- **FR-003** MQTT `tsinfer/yj/<공간 id>`(retained, QoS 1), `tsinfer/yj/status`, `tsinfer/yj/alerts`(이벤트).
- **FR-004** 조회 API `/health`, `/spaces`, `/spaces/<id>`, `/spaces/<id>/forecast?minutes=`, `/alerts`.
- **FR-005** 끊김 경보: 수집기 heartbeat 3분 초과 → `collector_down`, 공간 마지막 측정이 max(5분, 센서 간격 × 2) 초과 → `data_gap`. 복구 시 `resolved`.
- **FR-006** 주기·공간 단위 예외 격리, 모델 없음 → fallback, 모델 파일 변경 → 재시작 없이 다시 읽기.
- **FR-007** 경보 중복 억제: new → 30분마다 repeat → resolved.
- **FR-008** 주기마다 `~/data/tsinfer/service/cycles.jsonl`에 처리 시간·상태·경보를 남긴다(D-2 근거).
- **FR-009** systemd `tsinfer-service-yj`(재부팅 자동 시작, `Restart=always`).
- **FR-010** 멈춤 감지: 주기마다 systemd watchdog 신호, 300초 없으면 재시작(10/22 추가, R11).

## Plan

- **코드**: `service/core.py`(주기 계산·모델 저장소·규칙·경보 억제), `service/publish.py`(paho, retained), `service/api.py`(표준 라이브러리 HTTP), `service/__main__.py`(`run`, `once --no-publish`), 설정 `config/service.toml`, 배포 `deploy/tsinfer-service.service.in`·`deploy/install-inference-service.sh`.
- **결정**: 입력은 InfluxDB가 아니라 우리 수집 DB(과제 1, 같은 보드, 읽기 전용 연결이라 수집을 막지 않음). API는 의존성 없는 `http.server`(ThreadingHTTPServer). 결과는 메모리 한 벌을 lock으로 공유.
- **Constitution**: III 전처리 공용(`tsdata.prep` 호출), VI 자동 복구·끊김을 숨기지 않음, IV 주기 로그.

## Tasks

- [x] T001 `service/` 패키지, 설정, `once --no-publish` 시험 주기
- [x] T002 단위 테스트 `tests/unit/test_service.py`(전처리 동일성, fallback, 1,000ppm 분, 경보 억제, 끊김, API, 핫 리로드)
- [x] T003 결과 형식 문서 `docs/reports/008-service/result-format.md`
- [x] T004 **[사용자]** `sudo deploy/install-inference-service.sh` → 12시간 가동 시작 (10/21 17:30 설치, 10/21 18:57~10/22 09:07 14시간 10분 무중단)
- [ ] T004b **[사용자]** watchdog 적용 재설치(R11) `sudo deploy/install-inference-service.sh`
- [ ] T005 **[사용자]** 가동 중 `sudo systemctl stop tsinfer-collector-yj` 10분 → `start`, 경보·복구를 `cycles.jsonl`로 확인
- [x] T006 12시간 뒤 `cycles.jsonl` 요약(주기 처리 시간 평균·최대, 실패 수, 경보 내역) → `docs/reports/008-service/uptime.md`, 판정표 (AC-2는 T005 뒤 채움)
