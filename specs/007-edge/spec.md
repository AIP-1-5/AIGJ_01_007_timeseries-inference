# Feature Specification: 과제 7 엣지 최적화

**Branch**: `002-data-baseline` · **Created**: 2026-10-21

## 완료 조건 (원문 그대로)
- **AC-1**: 변환 후 CO₂ 예측 차이 **1ppm 이하**, 분류 결과가 바뀐 비율 0.1% 이하
- **AC-2**: 측정 조건(전력 모드, 클럭 고정 여부, 반복 횟수, 워밍업)을 함께 기록했다

산출물: **D-1** 최적화 보고서: 실행 방식 × 모델별 지연·오차·메모리·전력 표, 결론

## 요구사항
- FR-001 예측 GRU·오토인코더·환기 분류기를 ONNX로 내보내고, PyTorch CPU / PyTorch GPU / ONNX Runtime / TensorRT FP16의 지연(평균·p95)을 배치 1·4로 잰다(가능한 실행 방식만, 안 되면 이유 기록).
- FR-002 변환 전후 차이를 원래 단위로(CO₂ ppm, 분류 결과 바뀐 비율). FR-003 측정 중 tegrastats로 전력·GPU·메모리 기록.
