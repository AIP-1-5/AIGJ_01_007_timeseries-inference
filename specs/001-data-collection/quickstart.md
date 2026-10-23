# Quickstart: 과제 1 수집기 확인 절차

보드 B(`team5-b`), 계정 `yj`, 저장소 루트 기준이다. 명령 형식은 [contracts/cli.md](contracts/cli.md), 설정은 [contracts/config.md](contracts/config.md), 저장 구조는 [data-model.md](data-model.md)를 본다.

## 0. 준비

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # MQTT_* 값 채우기 (단톡방)
.venv/bin/python -m collector check-config     # → 종료 코드 0
.venv/bin/python -m pytest                     # 단위·통합 테스트 통과
```

## 1. 브로커 없이 저장 경로 확인 (US1)

```bash
.venv/bin/python -m collector replay tests/fixtures/uplink_sample.jsonl --db /tmp/qs.db
```

기대: 30건 처리, 중복 2건 무시, 저장 메시지 28건이다. 회의실 AM103 행의 `measured_at`이 `gw_time`보다 약 5분 이르다. 같은 명령을 한 번 더 실행해도 행 수가 그대로다.

## 2. 실제 브로커로 10분 수집 (US1)

```bash
.venv/bin/python -m collector run      # 10분 뒤 Ctrl+C
.venv/bin/python -m collector status --hours 1
```

기대: 4개 공간(실습실, 사무실, 회의실, 사무실 밖)과 센서 13대 안팎이 보인다. 1분 센서의 수신률이 100%에 가깝다.

## 3. 서비스 등록과 재부팅 (US2, AC-2)

> sudo·systemctl·재부팅은 사용자가 직접 실행한다(2026-10-21 지시). Claude는 명령만 준비한다.

```bash
deploy/install-service.sh --print       # 설치될 유닛 내용만 출력 (sudo 불필요, 먼저 눈으로 확인)
sudo deploy/install-service.sh          # /etc/systemd/system/tsinfer-collector-yj.service 설치 + enable --now
systemctl status tsinfer-collector-yj --no-pager   # active (running)
journalctl -u tsinfer-collector-yj -n 20 --no-pager # start, connect, subscribe 로그
sudo systemctl kill -s KILL tsinfer-collector-yj   # 5초 뒤 다시 active (Restart=always)
```

되돌리기: `sudo systemctl disable --now tsinfer-collector-yj && sudo rm /etc/systemd/system/tsinfer-collector-yj.service && sudo systemctl daemon-reload`

재부팅 시험(팀 단톡방에 먼저 알림, **보드 B만**, 보드 A 금지): `sudo reboot`. 그다음 로그인하지 않고 3분 기다린 뒤 SSH로 접속해 아래 값을 `docs/reports/001-data-collection/fault-tests.md`에 적는다.

```bash
journalctl --list-boots | tail -1                          # 부팅 시각
journalctl -b -u tsinfer-collector-yj --no-pager | head -5 # 서비스 시작 시각
sqlite3 -readonly ~/data/tsinfer/collector.db \
  "SELECT min(received_at) FROM readings WHERE received_at > (SELECT max(ts) FROM collector_events WHERE kind='start');"
                                                            # 부팅 후 첫 새 행 시각
```

## 4. 브로커 껐다 켜기 (US2, AC-1)

> 이번 구현에서는 실행하지 않는다. 사용자가 직접 실행한다(sudo 필요). 결과는 `fault-tests.md`에 회차별 초로 적는다.

**4a. 로컬 브로커**(원문 그대로의 시험). 운영 서비스와 따로, 시험용 DB로 수집기를 하나 더 띄운다.

```bash
sudo apt install mosquitto              # ARM64 패키지. 2.x 기본 설정은 localhost에서만 듣는다
# 창 1: 로컬 브로커를 보는 시험용 수집기 (환경변수가 .env보다 우선)
MQTT_HOST=localhost MQTT_PORT=1883 MQTT_USERNAME= MQTT_PASSWORD= \
  .venv/bin/python -m collector run --db ~/data/tsinfer/test-local.db
# 창 2: 5초마다 fCnt를 1씩 올리고 nsTime=현재 시각으로 발행
f=1; while true; do
  .venv/bin/python -c "import json,sys,datetime as d;m=json.loads(open('tests/fixtures/uplink_sample.jsonl').readline());m['fCnt']=int(sys.argv[1]);t=d.datetime.now(d.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ');[r.update(nsTime=t) for r in m['rxInfo']];print(json.dumps(m))" $f \
    | mosquitto_pub -h localhost -t application/test/device/test/event/up -q 1 -s
  f=$((f+1)); sleep 5; done
# 창 3: 3회 반복 (stop → 30초 → start), 회차마다 start 시각 기록
for i in 1 2 3; do
  sudo systemctl stop mosquitto; sleep 30
  sudo systemctl start mosquitto; date -u +%Y-%m-%dT%H:%M:%S.%6NZ; sleep 60
done
# 회차별 "start → 첫 새 행" 확인: 위에서 찍힌 시각을 <T>에 넣는다
sqlite3 -readonly ~/data/tsinfer/test-local.db "SELECT min(received_at) FROM readings WHERE received_at > '<T>';"
```

정리: `sudo systemctl disable --now mosquitto` (시험 뒤 브로커를 켜 두지 않는다).

기대: 3회 모두 60초 미만이다.

**4b. 실제 브로커 경로 차단 — 내 계정(uid yj)만**. 팀원 수집기(`collector-jaeung`, `ts-collector`)가 같은 보드에서 돌기 때문에 보드 전체의 1883을 막지 않는다.

4a·4b는 `bash deploy/fault-test-t029.sh`(4b만: 인자 `4b`)로 한 번에 실행한다. 4b 차단 부분은 아래와 같다.

```bash
sudo nft add table inet t029
sudo nft add chain inet t029 out '{ type filter hook output priority 0 ; }'
sudo nft add rule inet t029 out meta skuid "$(id -u yj)" tcp dport 1883 drop
sleep 120
sudo nft delete table inet t029; date -u +%FT%TZ   # 해제 시각 = <T>
sqlite3 -readonly ~/data/tsinfer/collector.db "SELECT min(received_at) FROM readings WHERE received_at > '<T>';"
```

- Jetson 커널에 iptables owner 매치(`xt_owner`)와 nft reject가 없어 `iptables -m owner ... REJECT`는 실패한다(10/23 확인). nft `meta skuid`는 nf_tables 기본 기능이라 된다. reject 대신 drop이라 수집기는 keepalive 만료(약 45~60초)로 끊김을 안다.
- 시험 전용 테이블 `t029`를 지우면 규칙도 같이 지워진다. 스크립트는 종료할 때 항상 지운다.

기대: 해제 후 첫 센서 메시지가 오는 즉시 저장된다. 판정은 "해제 후 60초 안 새 행"이다. 로그에 `disconnect`/`connect`/`subscribe`가 남는다.

## 5. 24시간 커버율 (US3, AC-3, D-1)

```bash
.venv/bin/python -m collector report --start <시작Z> --end <시작+24h Z> \
    --out docs/reports/001-data-collection/coverage-<날짜>.md
```

기대: co2·temperature·humidity의 공간별 1분 커버율이 95% 이상이다. 95% 미만이면 결측 구간마다 추정 원인이 있다. 회의실은 spec Assumptions에 따라 간격 기준 커버율과 수신률을 함께 본다. 보고서의 "해석" 칸은 사람이 쓴다.

## 6. 형식 변경 대응 (US4, D-2)

1. `tests/fixtures/format_change.jsonl`(EM300-TH의 `temperature` 키를 `temp`로 바꾼 메시지)을 replay하면 `temp`가 그대로 저장되고 `unmapped_key` 경고가 남는다.
2. `config/collector.toml`의 `[models.EM300-TH.keys]`에 `temp = "temperature"`를 추가하고 `check-config`를 실행한다.
3. 다시 replay하면 `temperature`로 저장된다. `git diff --stat`에는 설정 파일만 나온다.
4. 결과를 `docs/reports/001-data-collection/format-change.md`에 기록한다(상황, 변경 전·후 설정, 코드 변경 0줄, 저장 결과).
