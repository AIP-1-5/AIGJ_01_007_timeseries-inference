# 형식 변경 대응 기록 (D-2)

| 측정 조건 | 값 |
|---|---|
| 날짜 | 2026-10-21 16:13 KST |
| 보드 | 보드 B (`team5-b`) |
| 커밋 | `af757c9` + 작업 중 변경(US3·US4, 메시지 처리 경로는 변경 없음) |
| 데이터 | **합성**: 실데이터 fixture(`uplink_sample.jsonl`)의 EM300-TH 3건을 고친 것 + 가상 모델 2건 (`tests/fixtures/format_change.jsonl`) |
| 재현 | `pytest tests/integration/test_format_change.py`, 또는 아래 "시연" 명령 |

## 1. 가정한 상황

1. **키 이름 변경**: 센서 펌웨어 업데이트로 EM300-TH의 `object.temperature` 키가 `temp`로 바뀌었다.
2. **새 모델 추가**: 키 이름이 다른 새 모델 `EM300-TH-v2`(`temp`, `humi`)가 실습실에 들어왔다.

두 경우 모두 그대로 두면 학습 데이터에서 `temperature`·`humidity` 항목이 그 시점부터 끊긴 것처럼 보인다.

## 2. 바꾼 것: 설정 파일만

`config/collector.toml` → `config/examples/format-change.toml` (주석 제외 7줄 추가)

```diff
 [models.EM300-TH.keys]
 temperature = "temperature"
 humidity = "humidity"
+temp = "temperature"     # 형식 변경: 새 키 이름 → 같은 항목
+
+[models.EM300-TH-v2]     # 새 모델
+interval_s = 60
+[models.EM300-TH-v2.keys]
+temp = "temperature"
+humi = "humidity"
```

## 3. 결과 (같은 메시지 5건을 설정만 바꿔 replay)

| | 바꾸기 전 (운영 설정) | 바꾼 뒤 (예시 설정) |
|---|---|---|
| replay 출력 | 처리 5, 저장 메시지 5, 매핑 안 된 키 3 | 처리 5, 저장 메시지 5, 매핑 안 된 키 **0** |
| EM300-TH 저장 항목 | `temp` 3, `humidity` 3 | `temperature` 3, `humidity` 3 |
| EM300-TH-v2 저장 항목 | `temp` 2, `humi` 2 | `temperature` 2, `humidity` 2 |
| `unmapped_key` 이벤트 | 3건 (EM300-TH/temp, EM300-TH-v2/temp, EM300-TH-v2/humi) | 0건 |

바꾸기 전에도 값은 **버려지지 않았다**. `[unmapped] policy = "store"`라서 키 이름 그대로 저장하고, 로그(`WARN unmapped_key`)와 `collector_events`에 경고를 남긴다(decisions R6).

## 4. 코드 변경 0줄 근거

```text
$ git diff --stat af757c9 -- collector/parser.py collector/writer.py collector/store.py \
      collector/epoch.py collector/config.py collector/mqtt_client.py
(출력 없음)
```

메시지 처리 경로(파싱 → epoch → 저장)의 파일은 커밋 `af757c9`와 비교해 바뀐 것이 없다. 이번에 추가한 파일은 설정 사본, fixture, 테스트뿐이다.

## 5. 운영 서비스에 적용하는 절차

1. `config/collector.toml`에 매핑을 추가한다.
2. `.venv/bin/python -m collector check-config` → "설정 정상"이 나와야 한다(오류면 종료 코드 2, 서비스를 재시작하지 않는다).
3. `sudo systemctl restart tsinfer-collector-yj` (사용자 실행). 재시작하는 몇 초 동안 온 메시지는 잃을 수 있다(브로커가 끊긴 동안의 메시지를 보관하지 않음, fault-tests.md AC-1). 센서 간격이 1분이라 보통 0~1건이다(추정).
4. `python -m collector status --hours 1`에서 `temperature`가 다시 늘어나는지, `journalctl -u tsinfer-collector-yj`에 `unmapped_key`가 더 나오지 않는지 확인한다.
5. 매핑 전에 키 이름 그대로 저장된 행은 한 번에 고칠 수 있다(필요할 때만, 사용자 승인 뒤):
   `UPDATE readings SET item = 'temperature' WHERE model = 'EM300-TH' AND item = 'temp';`
   기본키에 item이 들어 있어 같은 메시지에 `temperature`와 `temp`가 같이 있으면 충돌한다. 실제로는 한 메시지에 둘 중 하나만 있으므로 충돌하지 않는다(추정, 실행 전 `SELECT`로 확인). 한 문장이 트랜잭션 하나라 중간에 실패하면 전부 롤백된다.

## 6. 설정으로 대응할 수 없는 것 (contracts/config.md)

- `object`가 중첩 구조로 바뀌는 경우(예: `{"th": {"temperature": 23.8}}`). 매핑은 한 단계 키만 본다. 지금은 숫자가 아닌 값으로 보고 건너뛰며 `non_numeric_value` 경고를 남긴다.
- 페이로드 골격(`deviceInfo`, `rxInfo`, `fCnt`)이 바뀌는 경우. 버림 테이블(`rejected`)에 `missing_field:…`로 쌓이므로 알아챌 수는 있지만, 파서를 고쳐야 한다.
- 단위가 바뀌는 경우(예: ℃ → ℉). 키 이름이 같으면 경고가 나지 않는다. 값의 범위를 보는 검사는 과제 2의 정제 규칙에서 다룬다.

## 7. 해석

- **왜 이렇게 대응했나**: 형식 변경의 대부분은 "키 이름"과 "새 모델"이라서, 그 둘을 설정으로 빼 두면 코드 배포 없이 설정 한 줄과 재시작만으로 끝난다(원칙 VI).
- **그래서 무엇을 해야 하나**: 매핑이 없는 키를 버리지 않고 저장하는 기본값(`store`) 덕분에 대응이 늦어도 데이터는 남는다. 다만 항목 이름이 갈라진 채로 쌓이므로, `status`의 항목 목록이나 `unmapped_key` 경고를 정기적으로 확인해야 한다. 과제 8 서비스에서는 이 경고를 알림으로 올리는 것을 검토한다.
