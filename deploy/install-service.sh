#!/usr/bin/env bash
# 수집기를 systemd 시스템 유닛으로 설치하고 켠다. 다시 실행해도 안전하다(유닛을 덮어쓰고 재시작).
#
#   deploy/install-service.sh --print   # 채운 유닛을 출력만 (sudo 불필요)
#   sudo deploy/install-service.sh      # 설치 + daemon-reload + enable + restart
#
# 되돌리기:
#   sudo systemctl disable --now tsinfer-collector-<사용자>
#   sudo rm /etc/systemd/system/tsinfer-collector-<사용자>.service && sudo systemctl daemon-reload
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${SUDO_USER:-$(id -un)}"
NAME="tsinfer-collector-${TARGET_USER}"
UNIT_PATH="/etc/systemd/system/${NAME}.service"
TEMPLATE="${REPO}/deploy/tsinfer-collector.service.in"
cd "${REPO}"   # python -m collector 가 패키지를 찾도록

render() {
    sed -e "s|@USER@|${TARGET_USER}|g" -e "s|@REPO@|${REPO}|g" "${TEMPLATE}"
}

if [[ "${1:-}" == "--print" ]]; then
    render
    exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
    echo "오류: 설치에는 root 권한이 필요하다. 'sudo deploy/install-service.sh'로 실행한다." >&2
    echo "      먼저 'deploy/install-service.sh --print'로 유닛 내용을 확인할 수 있다." >&2
    exit 1
fi
if [[ "${TARGET_USER}" == "root" ]]; then
    echo "오류: root 계정으로 설치하지 않는다. 내 계정에서 'sudo deploy/install-service.sh'로 실행한다." >&2
    exit 1
fi
if [[ ! -f "${REPO}/.env" ]]; then
    echo "오류: ${REPO}/.env 가 없다. 'cp .env.example .env' 후 MQTT_* 값을 채운다." >&2
    exit 1
fi
if [[ ! -x "${REPO}/.venv/bin/python" ]]; then
    echo "오류: ${REPO}/.venv 가 없다. 'python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'" >&2
    exit 1
fi
# 설정이 깨진 채로 설치하면 재시작만 반복하므로 먼저 검사한다 (root가 아니라 내 계정 권한으로)
if ! runuser -u "${TARGET_USER}" -- "${REPO}/.venv/bin/python" -m collector check-config \
        --config "${REPO}/config/collector.toml" >/dev/null; then
    echo "오류: 설정 검사 실패. '.venv/bin/python -m collector check-config'로 원인을 본다." >&2
    exit 2
fi

render > "${UNIT_PATH}.tmp"
mv "${UNIT_PATH}.tmp" "${UNIT_PATH}"
chmod 644 "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable "${NAME}"
systemctl restart "${NAME}"
sleep 2
systemctl status "${NAME}" --no-pager || true
echo
echo "설치됨: ${UNIT_PATH}"
echo "로그:   journalctl -u ${NAME} -f"
