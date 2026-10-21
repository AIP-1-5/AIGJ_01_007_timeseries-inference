# 과제 2 데이터 이해와 기준선: 확인 기준 판정

spec: [specs/002-data-baseline/spec.md](../../../specs/002-data-baseline/spec.md) · 재현: `python -m tsdata.influx --start 2025-08-01 --end 2026-10-21` 후 `python -m tsdata all`

| 측정 조건 | 값 |
|---|---|
| 데이터 | 실데이터, InfluxDB `iot-bucket` 스냅샷(2026-10-21) |
| 기간 (KST) | 학습 2025-09-01 ~ 2026-06-30 · 검증 2026-07-01 ~ 2026-08-31 · 시험 2026-09-01 ~ 2026-10-20 |
| 보드 | 보드 B, 전원 모드 MAXN_SUPER |

## 판정표

| 기준 | 내용 | 결과 | 판정 | 근거 |
|---|---|---|---|---|
| AC-1 | 정규화 값을 학습 기간 데이터로만 계산 | `fit_normalizer`가 학습 기간 관측 칸만 사용. 검증·시험 값을 99,999·−5·10⁶으로 바꿔도 평균·표준편차·개수가 같음(테스트) | **통과** | `tests/unit/test_prep.py::test_normalizer_uses_train_observed_only`, `prep_params.json`의 `normalizer.basis` |
| AC-2 | 기준선과 이후 모델이 같은 시점·같은 조건 | 기간·공간·항목·거리마다 평가 시점 목록 하나를 세 기준선이 공유하고 파일로 저장. 정답은 실제 관측 칸만 | **통과** | `eval_points.parquet`, `test_baselines.py::test_evaluate_same_points_for_all_baselines` |
| AC-3 | 최강 기준선과 이유 | 현재값 유지가 48칸 모두 최강. 이유: 1시간 상관 0.80~0.94 vs 24시간 0.45~0.58, 1시간 평균의 지연, 어제 값의 요일·공휴일 불일치 | **통과** | [diagnosis.md §4](diagnosis.md#4-기준선과-가장-강한-기준선-ac-3) |
| D-1 | 데이터 진단서(정제 규칙, 분할 기준, 패턴 그래프, 일정–CO₂ 반응) | 네 항목과 생각해 볼 점 두 답 포함. 일정은 가정(10/22 일정 자료 확인 대기) | **통과**(일정 자료 받으면 §3 다시 돌림) | [diagnosis.md](diagnosis.md) |
| D-2 | 기준선 성능표 | 3공간 × 2항목 × 4거리 × 3기준선 × 2기간 = 144개 MAE, 평가 시점 수 | **통과** | [baseline.md](baseline.md) |

## 과제 3으로 넘기는 것

- 공용 전처리 `tsdata/prep.py`와 파라미터 `~/data/tsinfer/prep/prep_params.json`(정규화 포함)
- 평가 시점 `~/data/tsinfer/prep/eval_points.parquet`
- 목표선(시험 60분 CO₂): 실습실 45.4, 사무실 38.1, 회의실 68.0ppm
