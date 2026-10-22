#!/usr/bin/env bash
# 추론 서비스를 systemd 시스템 유닛으로 설치하고 켠다. 다시 실행해도 안전하다(유닛을 덮어쓰고 재시작).
#
#   deploy/install-inference-service.sh --print   # 채운 유닛을 출력만 (sudo 불필요)
#   sudo deploy/install-inference-service.sh      # 설치 + daemon-reload + enable + restart
#
# 되돌리기:
#   sudo systemctl disable --now tsinfer-service-<사용자>
#   sudo rm /etc/systemd/system/tsinfer-service-<사용자>.service && sudo systemctl daemon-reload
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${SUDO_USER:-$(id -un)}"
NAME="tsinfer-service-${TARGET_USER}"
UNIT_PATH="/etc/systemd/system/${NAME}.service"
TEMPLATE="${REPO}/deploy/tsinfer-service.service.in"
cd "${REPO}"

render() {
    sed -e "s|@USER@|${TARGET_USER}|g" -e "s|@REPO@|${REPO}|g" "${TEMPLATE}"
}

if [[ "${1:-}" == "--print" ]]; then
    render
    exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
    echo "오류: 설치에는 root 권한이 필요하다. 'sudo deploy/install-inference-service.sh'로 실행한다." >&2
    exit 1
fi
if [[ "${TARGET_USER}" == "root" ]]; then
    echo "오류: root 계정으로 설치하지 않는다. 내 계정에서 sudo로 실행한다." >&2
    exit 1
fi
if [[ ! -f "${REPO}/.env" || ! -x "${REPO}/.venv/bin/python" ]]; then
    echo "오류: ${REPO}/.env 또는 ${REPO}/.venv 가 없다." >&2
    exit 1
fi
# 한 주기를 발행 없이 먼저 돌려 본다(설정·DB·전처리가 깨진 채로 설치하지 않기 위해)
if ! runuser -u "${TARGET_USER}" -- "${REPO}/.venv/bin/python" -m service once --no-publish \
        --config "${REPO}/config/service.toml" >/dev/null; then
    echo "오류: 시험 주기 실패. '.venv/bin/python -m service once --no-publish'로 원인을 본다." >&2
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
echo "API:    curl -s localhost:8807/health"
