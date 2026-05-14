#!/usr/bin/env bash
# Vast.AI "On-start Script" — paste vào field khi tạo instance.
# Tự clone repo + setup + start server.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/YOUR_FORK/LiveTalking.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
WORKDIR="${WORKDIR:-/workspace/LiveTalking}"

cd /workspace

if [[ ! -d "${WORKDIR}/.git" ]]; then
  git clone --depth 1 -b "${REPO_BRANCH}" "${REPO_URL}" "${WORKDIR}"
else
  cd "${WORKDIR}" && git pull
fi
cd "${WORKDIR}"

if [[ ! -f .setup_done ]]; then
  bash scripts/vastai/setup.sh
  touch .setup_done
fi

nohup bash scripts/vastai/start.sh > /workspace/server.log 2>&1 &

echo "[onstart] LiveTalking started. Logs: /workspace/server.log"
echo "[onstart] Open: http://<vastai-public-ip>:8010/"
