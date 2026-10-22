# AIGJ_01_007_timeseries-inference

교육장 센서(CO₂·온도·습도) 데이터로 60분 예측, 이상 탐지, 사용 여부 추정을 하고 Jetson Orin Nano에서 상시 추론 서비스로 운영하는 과제 저장소.

## 진행 상황 (2026-10-22 16시)

결과 보고서 묶음: [docs/reports/final-report.md](docs/reports/final-report.md) · 일정과 현재 위치: [docs/roadmap.md](docs/roadmap.md) · 결정 기록: [docs/decisions.md](docs/decisions.md) · 학습 노트: [docs/study/](docs/study/)

| 과제 | 결과 | 판정 |
|---|---|---|
| 1 데이터 수집 | [001-data-collection](docs/reports/001-data-collection/README.md) | 24시간 커버율 통과(98% 이상), 재부팅 자동 재개 통과. 브로커 재시작 시험만 대기 |
| 2 데이터 이해·기준선 | [002-data-baseline](docs/reports/002-data-baseline/README.md) | 통과 (최강 기준선 = 현재값 유지) |
| 3 60분 예측 | [003-forecast-60min](docs/reports/003-forecast-60min/README.md) | 30% 기준 미통과(−29/−22/−20%), 온도 통과 |
| 4 이상 탐지 | [004-anomaly](docs/reports/004-anomaly/README.md) | 탐지율 80% 통과, 헛경보 1.36회/일 미통과 |
| 5 상태 추정 | [005-state](docs/reports/005-state/README.md) | 환기 경보 통과, 사용 여부는 직접 관찰 없음 |
| 6 사전학습 모델 | [006-pretrained](docs/reports/006-pretrained/README.md) | 통과 (Chronos-Bolt 채택 안 함) |
| 7 엣지 최적화 | [007-edge](docs/reports/007-edge/README.md) | ONNX Runtime 통과, TensorRT FP16 미측정 |
| 8 상시 서비스 | [008-service](docs/reports/008-service/uptime.md) | 통과 (14시간 10분 무중단, 수집기 중지 경보·복구) |

## 브랜치

- **`main`이 제출본이다.** 단계가 끝날 때마다 작업 브랜치를 PR로 `main`에 합친다.
- 작업 브랜치는 Spec Kit이 과제 번호로 만든다(`001-data-collection`, `002-data-baseline`). 과제 3~8은 시간 때문에 `002-data-baseline`에서 이어서 했다. 합친 뒤에도 브랜치는 기록으로 남긴다.
- 학습은 맥(MPS)에서만, 코드 수정은 보드 B에서 한다. 보드 A는 `main`을 `git pull`만 한다.

## 구성

| 경로 | 내용 |
|---|---|
| `docs/reports/` | 과제별 결과 보고 |
| `CLAUDE.md` | Claude Code 작업 규칙, 장비 정보 |
| `.specify/memory/constitution.md` | Spec Kit 원칙 (모든 과제가 지킬 규칙) |
| `.claude/skills/` | Spec Kit 명령 (`/speckit-*`) |
| `specs/` | 과제별 spec·plan·tasks (Spec Kit이 만든다) |

## 시작하기

과제 안내 원문은 팀 Private 저장소 `AIP-1-5/docs`에 있다. 두 저장소를 나란히 clone한다.

```bash
mkdir -p ~/work && cd ~/work
git clone git@github.com:AIP-1-5/docs.git
git clone git@github.com:AIP-1-5/AIGJ_01_007_timeseries-inference.git
cd AIGJ_01_007_timeseries-inference
cp .env.example .env            # 값은 팀 단톡방에서 받는다
claude --add-dir ../docs        # 과제 문서도 읽을 수 있게 Claude Code 실행
```

과제 진행 순서 (과제 하나씩):

1. `/speckit-specify` — `../docs/assignment.md`의 과제 N 부분을 기준으로 spec 작성
2. `/speckit-plan` → `/speckit-tasks` — 계획과 할 일 목록. 여기서 멈추고 검토
3. `/speckit-implement` — 구현
4. `/speckit-converge` — 확인 기준을 충족할 때까지 반복

## 과제 1 수집기 (`collector/`)

교육장 MQTT 센서 메시지를 SQLite(`~/data/tsinfer/collector.db`)에 쌓는 상시 수집기. 상세: [specs/001-data-collection/quickstart.md](specs/001-data-collection/quickstart.md), 결과: [docs/reports/001-data-collection/](docs/reports/001-data-collection/README.md)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                               # MQTT_* 값 (단톡방). 커밋 금지
.venv/bin/python -m collector check-config         # 설정 검사 (종료 코드 0 = 정상)
.venv/bin/python -m pytest                         # 테스트

sudo deploy/install-service.sh                     # systemd 서비스 tsinfer-collector-<사용자> 설치·시작
.venv/bin/python -m collector status --hours 24    # 공간·항목별 커버율, 센서별 수신률, 끊김 경고 (--json 가능)
.venv/bin/python -m collector report --start 2026-10-21T07:04Z \
    --out docs/reports/001-data-collection/coverage-2026-10-22.md   # 24시간 수집 현황 보고(D-1)
.venv/bin/python -m collector replay <파일.jsonl> --db /tmp/x.db     # 브로커 없이 저장 경로 시험
```

메시지 형식이 바뀌면 코드가 아니라 `config/collector.toml`의 매핑을 고친다([형식 변경 기록](docs/reports/001-data-collection/format-change.md)).
