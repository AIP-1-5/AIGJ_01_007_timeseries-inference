# Tasks: 과제 2 데이터 이해와 기준선

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md)

## Phase 1: 준비

- [x] T001 `requirements.txt`에 pandas, pyarrow, matplotlib 추가, `.venv`에 설치
- [x] T002 `tsdata/influx.py`: InfluxDB → `~/data/tsinfer/influx/<항목>.parquet` 스냅샷 (R1). 2025-08-01 ~ 2026-10-21 00:00Z 내려받기
- [x] T003 `config/prep.toml`: 대상 공간, 항목별 허용 범위·제거 값, 간격 5분, 보간 최대 3칸, 기간 경계, 평가 거리(5·15·30·60분)

## Phase 2: US1 공간별 시계열 (P1)

- [x] T004 [P] [US1] `tests/unit/test_prep.py`: 범위·오류값 제거와 규칙별 건수, 센서 5분 평균 → 공간 중앙값, 3칸 이하만 보간·`observed` 표시, 같은 입력 두 번 → 같은 결과
- [x] T005 [US1] `tsdata/prep.py`: `load_config`, `clean(raw, cfg) -> (df, removed)`, `to_space_series(df, cfg)`, `fill_short_gaps(series, cfg)`, `build(cfg)`(스냅샷 → 공간 시계열 parquet)

## Phase 3: US2 분할과 정규화 (P1)

- [x] T006 [P] [US2] `tests/unit/test_prep.py`: 기간 경계(KST 날짜 포함), 검증·시험 값을 바꿔도 정규화 값이 같음(AC-1), 보간 칸은 정규화 계산에서 제외
- [x] T007 [US2] `tsdata/prep.py`: `period_of(ts, cfg)`, `fit_normalizer(series, cfg)`, `save_params(path, ...)`(규칙·간격·기간·정규화·설정 해시·계산 기간)

## Phase 4: US3 기준선 (P1)

- [x] T008 [P] [US3] `tests/unit/test_baselines.py`: 세 기준선 예측값 손계산, 1시간 평균은 관측 6칸 이상일 때만, 평가 시점 = 세 기준선·네 거리 모두 가능 + 정답 관측, MAE 손계산
- [x] T009 [US3] `tsdata/baselines.py`: `predict(series, kind, h)`, `eval_points(series, period, cfg)`, `evaluate(...) -> 성능표`, 평가 시점 저장
- [x] T010 [US3] `docs/reports/002-data-baseline/baseline.md`(D-2): 공간 × 항목 × 거리 × 기준선 × 기간 MAE, 평가 시점 수, 최강 표시, 측정 조건

## Phase 5: US4 진단서 (P2)

- [x] T011 [P] [US4] `tests/unit/test_diagnose.py`: 결측률 계산, 일과 시각 추정과 어긋난 날 판정(합성 하루 곡선)
- [x] T012 [US4] `tsdata/diagnose.py`: 결측률·제거 건수 표, 평일 시간대별 평균 그래프(공간별 CO₂·온도), 일과 시각 추정·날별 반응 시각·어긋난 날, 기준선 오차의 시간대별 그래프
- [x] T013 [US4] `tsdata/__main__.py`: `python -m tsdata all`로 prep → baseline → diagnose
- [x] T014 [US4] `docs/reports/002-data-baseline/diagnosis.md`(D-1, 2쪽 이내): 정제 규칙, 분석 간격, 분할 기준, 패턴 그래프, 일정–CO₂ 반응, 최강 기준선과 이유(AC-3), 생각해 볼 점 2개, 해석

## Phase 6: 마무리

- [x] T015 전체 테스트 통과, 커밋 전 점검(데이터·비밀값 없음)
- [x] T016 `docs/reports/002-data-baseline/README.md` 판정표: AC-1~3, D-1·D-2 통과/미통과와 숫자
- [x] T017 decisions.md, 학습 노트(`docs/study/002-*.md`), roadmap 갱신

## 의존 관계

T001~T003 → US1(T004~T005) → US2(T006~T007) → US3(T008~T010) → US4(T011~T014) → T015~T017. 테스트([P])는 같은 단계의 구현보다 먼저 쓴다.
