# Contract: 설정 파일 `config/collector.toml`

비밀값(MQTT 주소·계정·비밀번호)은 이 파일에 두지 않는다. `password`, `username`, `token`, `host` 키가 있으면 시작 시 오류다. 바꾼 설정은 `check-config`로 검사한 뒤 서비스를 재시작해 적용한다.

```toml
[mqtt]
uplink_topic = "application/+/device/+/event/up"
join_topic   = "application/+/device/+/event/join"   # "" 이면 구독 안 함 (research R2)
qos = 1
clean_session = false
client_id_prefix = "tsinfer-collector"   # 실제 id: <prefix>-<사용자>-<호스트>
keepalive_s = 30
reconnect_min_s = 1
reconnect_max_s = 10

[storage]
db_path = "~/data/tsinfer/collector.db"
queue_max = 50000
heartbeat_s = 60

[fcnt]                 # 근거: research R2
reset_max = 1000       # 이 값 이하로 돌아가야 "0 근처"
min_drop = 100         # 이만큼 이상 줄어야 "크게"

[unmapped]
policy = "store"       # "store": 키 이름 그대로 저장 + 경고 / "drop": 버리고 경고

# 모델별 object 키 → 저장 항목 이름(InfluxDB measurement와 같게)
# 목록에 있는 키만 저장하려면 policy="drop"
[models.AM107.keys]
co2 = "co2"
temperature = "temperature"
humidity = "humidity"
pressure = "pressure"
tvoc = "tvoc"
illumination = "illumination"
infrared = "infrared"
infrared_and_visible = "infrared_and_visible"
activity = "activity"

[models.AM103.keys]
co2 = "co2"
temperature = "temperature"
humidity = "humidity"
battery = "battery"

[models.EM500-CO2.keys]
co2 = "co2"
temperature = "temperature"
humidity = "humidity"
pressure = "pressure"

[models.EM320-TH.keys]
temperature = "temperature"
humidity = "humidity"
battery = "battery"

[models.EM300-TH.keys]
temperature = "temperature"
humidity = "humidity"

[models.WS302.keys]
LAeq = "LAeq"
LAI = "LAI"
LAImax = "LAImax"
battery = "battery"

# 모델 기본 전송 간격(커버율 보조 판정용, 초)과 장치별 덮어쓰기
[models.AM107]
interval_s = 60
[models.AM103]
interval_s = 60
[models.EM500-CO2]
interval_s = 120
[models.EM320-TH]
interval_s = 60
[models.EM300-TH]
interval_s = 60
[models.WS302]
interval_s = 60

[devices."24e124725d089152"]   # 회의실 AM103
interval_s = 600
[devices."24e124128c140101"]   # 실습실 전방 우측 AM107
interval_s = 600

[report]
judged_items = ["co2", "temperature", "humidity"]
coverage_target = 0.95
gap_list_min_minutes = 3
```

(위는 계약 예시다. 실제 TOML에서 `[models.AM107]`와 `[models.AM107.keys]`는 같은 테이블 아래 함께 쓸 수 있다.)

## 형식 변경 대응 규칙 (D-2)

| 상황 | 바꾸는 것 |
|---|---|
| 모델 X의 키 이름 변경 (예: `temperature` → `temp`) | `[models.X.keys]`에 `temp = "temperature"` 추가 |
| 새 센서 모델 Y 추가 | `[models.Y.keys]`, `[models.Y] interval_s` 추가 |
| 항목 하나를 더 이상 저장하지 않음 | 그 키를 매핑에서 빼고 `policy = "drop"` |
| 센서 전송 간격 변경 | `[devices."<devEui>"] interval_s` |

코드 변경 없이 대응되지 **않는** 것은 `object`가 중첩 구조로 바뀌는 경우와 토픽 체계 자체가 바뀌는 경우다. 토픽은 설정에 있지만 페이로드 골격(deviceInfo, rxInfo)이 바뀌면 파서를 고쳐야 한다. 이 한계는 D-2 기록에 적는다.
