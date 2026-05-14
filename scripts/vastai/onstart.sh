#!/usr/bin/env bash
###############################################################################
#  Vast.ai "On-start Script" — paste vào ô khi tạo instance.
#  Tự clone repo + setup + chờ user upload model/avatar rồi start manual.
###############################################################################
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/HuuTu2004/LiveStream-Realtime.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
WORKDIR="${WORKDIR:-/workspace/LiveTalking}"

mkdir -p /workspace
cd /workspace

if [[ ! -d "${WORKDIR}/.git" ]]; then
  echo "[onstart] git clone ${REPO_URL} (${REPO_BRANCH})"
  git clone --depth 1 -b "${REPO_BRANCH}" "${REPO_URL}" "${WORKDIR}"
else
  echo "[onstart] repo exists — git pull"
  cd "${WORKDIR}" && git pull --rebase --autostash || true
fi
cd "${WORKDIR}"

if [[ ! -f .setup_done ]]; then
  bash scripts/vastai/setup.sh && touch .setup_done
fi

# KHÔNG auto-start: cần user scp wav2lip.pth + avatar trước, hoặc upload qua
# Studio Portal. Xem scripts/vastai/README.md.
echo "[onstart] Setup xong. SSH vào và chạy:"
echo "[onstart]   cd ${WORKDIR} && bash scripts/vastai/start.sh"
