# Plan: 과제 3 60분 예측

- **코드**: `tsmodel/forecast.py`(데이터셋·GRU·학습·평가), `tsmodel/features.py`(시간·일정 특성, 과제 4·5·8 공용), 실행 `python -m tsmodel forecast`.
- **데이터**: `~/data/tsinfer/prep/series.parquet`(과제 2), 정규화는 `prep_params.json`.
- **결정** (decisions.md 과제 3):
  - 입력 2시간: CO₂ 반응(점심 최저까지 25분, 퇴실 후 절반까지 55분)을 담기에 충분하고 계산이 작다.
  - 변화량 예측: 현재값 유지를 기본값으로 두고 그 위의 변화만 배운다(모델이 0을 내면 현재값 유지와 같음).
  - 한 번에 12칸 출력: 반복 예측은 오차가 쌓인다. 학습·추론이 한 번이다.
  - 공간 하나의 모델이 3공간을 함께 배운다(공간 one-hot). 데이터가 3배.
  - 손실: 정규화 단위 MAE(L1), CO₂·온도 합.
  - 선택: 검증 MAE(60분 CO₂)가 가장 낮은 에폭(조기 종료). 시험은 마지막에 한 번.
- **Constitution**: II 시간순·검증 선택·시험 1회, III 전처리는 tsdata.prep, IV 시드·커밋 기록, VII 기준선 비교.
