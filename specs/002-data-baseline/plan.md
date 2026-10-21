# Implementation Plan: 과제 2 데이터 이해와 기준선

**Branch**: `002-data-baseline` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)

## Summary

InfluxDB 과거 1년치를 스냅샷으로 고정하고, 공용 전처리 모듈(`tsdata/prep.py`)로 정제 → 센서별 5분 값 → 센서별 짧은 결측 채움(과거 값만) → 공간 대표값(중앙값) → 학습 기간 정규화를 만든다. 그 위에서 기준선 3종을 같은 평가 시점으로 재고(`tsdata/baselines.py`), 진단 표·그래프와 진단서 초안을 만든다(`tsdata/diagnose.py`).

## Technical Context

- **Language/Version**: Python 3.12 (보드 B, 저장소 `.venv`)
- **Primary Dependencies**: pandas 3.0, pyarrow 26(parquet), matplotlib 3.11(그래프). InfluxDB 조회는 표준 라이브러리 `urllib`
- **Storage**: 원본 스냅샷·가공 데이터는 `~/data/tsinfer/`(커밋 안 함). 결과 표·그래프는 `docs/reports/002-data-baseline/`
- **Testing**: pytest(합성 시계열로 정제·보간·정규화·기준선·평가 시점 검증)
- **Target Platform**: Jetson Orin Nano 보드 B. 무거운 학습 없음(팀 공지 불필요)
- **Performance Goals**: 전체 파이프라인 수 분 이내. 메모리는 원본 960만 행 기준 2GB 이하(추정)
- **Constraints**: 비밀값은 `.env`. 시험 기간 데이터는 평가에만 쓰고 규칙·기준값 선택에는 학습·검증만 쓴다

## Constitution Check

| 원칙 | 확인 |
|---|---|
| I 확인 기준 = 완료 조건 | spec에 AC-1~3, D-1·D-2 원문. tasks 마지막에 판정표 |
| II 누수 금지 | 시간순 분할(R5), 정규화는 학습 관측 칸만(R6, 테스트), 정답은 관측 칸만(R4), 정제 규칙은 학습 기간 분포와 센서 사양으로 정함 |
| III 전처리 한 곳 | `tsdata/prep.py` 하나. 기준선·진단·과제 3~8이 호출. 파라미터는 `prep_params.json` |
| IV 측정 조건 | 모든 표·그래프 머리에 데이터 기간·실데이터·시험 기간·전력 모드·커밋 |
| V 해석 | 진단서에 해석 절과 생각해 볼 점 두 답 |
| VI 자동 복구 | 해당 없음(일회성 분석) |
| VII 단순한 것부터 | 이 과제가 곧 기준선 측정 |

## Project Structure

```text
specs/002-data-baseline/   spec.md, plan.md, research.md, tasks.md, checklists/
config/prep.toml           정제 규칙, 간격, 결측, 기간, 대상 공간
tsdata/
  influx.py                InfluxDB → parquet 스냅샷 (R1)
  prep.py                  공용 전처리: 정제, 5분 대표값, 보간, 분할, 정규화 (R2~R6)
  baselines.py             기준선 3종, 평가 시점, MAE (R7)
  diagnose.py              결측률·제거 건수·패턴 그래프·일정 반응 (R8)
  __main__.py              python -m tsdata {prep, baseline, diagnose, all}
tests/unit/test_prep.py, test_baselines.py, test_diagnose.py
docs/reports/002-data-baseline/  diagnosis.md(D-1), baseline.md(D-2), figures/*.png, README.md(판정표)
```

## 주요 설계 결정 요약 (근거는 research.md)

| # | 결정 |
|---|---|
| R1 | 스냅샷 고정(parquet), 값은 문자열로 받아 정제에서 변환 |
| R2 | 공간 4곳, CO₂ 300~10000, 온도 -0.1 제거·-30~50, 습도 0~100 |
| R3 | 5분 간격, 센서 5분 평균 → 공간 중앙값 |
| R4 | 센서별 마지막 값 유지 최대 15분 → 공간 중앙값(선형 보간은 누수라 버림), 정답은 관측 칸만 |
| R5 | 학습 25-09-01~26-06-30, 검증 26-07-01~08-31, 시험 26-09-01~10-20 (KST) |
| R6 | 정규화 = 학습 관측 칸의 평균·표준편차, 파일 저장 |
| R7 | 기간·공간·항목·거리별 평가 시점을 세 기준선이 공유, 파일 저장 |
| R8 | 일정 자료 전까지 데이터로 추정한 일과 시각 사용 |

## 짚어 둘 것

- **데이터가 늘면**: 원본 960만 행을 한 번에 메모리에 올린다. 1년 더 쌓이면 2배라 항목별로 나눠 처리하면 충분하다(추정). 스냅샷이 parquet이라 열 단위로 읽는다.
- **동시성**: 수집기 DB와 InfluxDB 모두 읽기만 한다. 수집 서비스에 영향 없음.
- **재현성**: 같은 스냅샷 + 같은 `config/prep.toml` → 같은 결과(SC-006). 결과 파일에 설정 해시와 커밋을 적는다.

## 열린 질문 (10/22 강사 확인)

1. 일정 자료 출처 → 받으면 R8 1단계만 교체
2. 강사가 정한 시험 기간이 있는지 → 있으면 `config/prep.toml`의 기간만 교체

## Complexity Tracking

위반 없음.
