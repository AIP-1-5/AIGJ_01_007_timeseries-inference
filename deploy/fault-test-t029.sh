#!/usr/bin/env bash
# 과제 1 T029 장애 시험(quickstart 4a·4b)을 한 번에 실행한다.
# yj 계정으로 실행한다(sudo로 실행하지 않는다). sudo 비밀번호는 처음 한 번 묻는다.
#   bash deploy/fault-test-t029.sh        # 4a + 4b
#   bash deploy/fault-test-t029.sh 4b     # 4b만
# 4a: 로컬 mosquitto 30초 정지 → 시작, 3회. 시험용 수집기·DB를 따로 띄운다.
# 4b: uid yj의 1883 출력만 120초 차단 → 해제. 운영 수집기(tsinfer-collector-yj)로 판정한다.
#     Jetson 커널에 iptables owner 매치(xt_owner)가 없어 nft의 meta skuid로 막는다(decisions R15).
#     nft reject도 커널에 없어 drop을 쓴다. 끊김은 keepalive(30초) 만료로 감지된다.
# 결과: ~/data/tsinfer/fault-test-<시각>.txt (회차, 복구 시각, 첫 새 행, 걸린 초)
set -euo pipefail
cd "$(dirname "$0")/.."

TEST_DB=~/data/tsinfer/test-local.db
PROD_DB=~/data/tsinfer/collector.db
OUT=~/data/tsinfer/fault-test-$(date +%Y%m%d-%H%M%S).txt
MODE=${1:-all}
NFT_TABLE="inet t029"   # 시험 전용 테이블. 지우면 규칙도 같이 사라진다
PIDS=()

block_on() {
    sudo nft add table $NFT_TABLE
    sudo nft add chain $NFT_TABLE out '{ type filter hook output priority 0 ; }'
    sudo nft add rule $NFT_TABLE out meta skuid "$(id -u yj)" tcp dport 1883 drop
}
block_off() { sudo nft delete table $NFT_TABLE 2>/dev/null || true; }

cleanup() {
    # 차단 규칙이 남으면 운영 수집이 계속 끊기므로 어떤 경우에도 지운다
    block_off
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
    sudo systemctl disable --now mosquitto >/dev/null 2>&1 || true
    sudo nft list tables | grep -q t029 && echo "경고: 차단 테이블 t029가 남아 있다" || echo "정리 완료: 차단 규칙 없음, mosquitto 꺼짐"
}
trap cleanup EXIT

now() { date -u +%Y-%m-%dT%H:%M:%S.%6NZ; }

# T 이후 첫 행을 최대 90초 기다린다. 출력: "첫행시각 초" 또는 "없음 -"
first_row() {
    local db=$1 t=$2 row
    for _ in $(seq 90); do
        row=$(sqlite3 -readonly "$db" "SELECT min(received_at), round((julianday(min(received_at))-julianday('$t'))*86400,1) FROM readings WHERE received_at > '$t';" 2>/dev/null || true)
        [[ -n "$row" && "$row" != "|" ]] && { echo "${row/|/ }"; return; }
        sleep 1
    done
    echo "없음 -"
}

record() {  # 회차 방법 복구시각 첫행 초
    local ok="아니오"
    [[ "$5" != "-" ]] && awk "BEGIN{exit !($5 < 60)}" && ok="예"
    printf '| %s | %s | %s | %s | %s | %s |\n' "$1" "$2" "$3" "$4" "$5" "$ok" | tee -a "$OUT"
}

sudo -v
{
    echo "# T029 원자료 $(date -Iseconds)"
    echo "보드 $(hostname), 전원 모드 $(nvpmodel -q 2>/dev/null | head -1 || echo '확인 불가'), 커밋 $(git rev-parse --short HEAD)"
    echo
    echo "| 회차 | 방법 | 복구 시각 | 첫 새 행 시각 | 걸린 초 | 60초 미만 |"
    echo "|---|---|---|---|---|---|"
} | tee "$OUT"

# ---- 4a 로컬 브로커 ----
if [[ $MODE != 4b ]]; then
dpkg -s mosquitto >/dev/null 2>&1 || sudo apt-get install -y mosquitto >/dev/null
sudo systemctl start mosquitto

MQTT_HOST=localhost MQTT_PORT=1883 MQTT_USERNAME= MQTT_PASSWORD= \
    .venv/bin/python -m collector run --db "$TEST_DB" >/dev/null 2>&1 &
PIDS+=($!)

(   # 가짜 센서: 5초마다 fCnt +1, nsTime = 현재 시각
    f=$(( $(date +%s) % 1000000 ))
    while true; do
        .venv/bin/python -c "import json,sys,datetime as d;m=json.loads(open('tests/fixtures/uplink_sample.jsonl').readline());m['fCnt']=int(sys.argv[1]);t=d.datetime.now(d.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ');[r.update(nsTime=t) for r in m['rxInfo']];print(json.dumps(m))" $f \
            | mosquitto_pub -h localhost -t application/test/device/test/event/up -q 1 -s 2>/dev/null || true
        f=$((f+1)); sleep 5
    done
) &
PIDS+=($!)

sleep 20   # 수집기 연결·첫 저장 대기
for i in 1 2 3; do
    echo "4a 회차 $i: 브로커 정지 30초" >&2
    sudo systemctl stop mosquitto; sleep 30
    sudo systemctl start mosquitto; T=$(now)
    read -r first sec < <(first_row "$TEST_DB" "$T")
    record "$i" "로컬 브로커 재시작" "$T" "$first" "$sec"
    sleep 30
done
for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done; PIDS=()
sudo systemctl stop mosquitto
fi

# ---- 4b 실제 브로커 경로 차단 (uid yj만) ----
echo "4b: uid yj의 1883 출력 120초 차단" >&2
B=$(now); block_on; echo "차단 시작 $B" >> "$OUT"
sleep 120
block_off; T=$(now)
read -r first sec < <(first_row "$PROD_DB" "$T")
record 4 "1883 차단 해제 (uid yj)" "$T" "$first" "$sec"

echo >> "$OUT"
sqlite3 -readonly "$PROD_DB" "SELECT ts, kind, coalesce(detail,'') FROM collector_events
    WHERE ts > '$B' AND kind NOT IN ('heartbeat') ORDER BY ts;" >> "$OUT" || true
journalctl -u tsinfer-collector-yj --since "-5 min" --no-pager 2>/dev/null \
    | grep -E "disconnect|connect|subscribe" >> "$OUT" || true
echo "결과 파일: $OUT" >&2
