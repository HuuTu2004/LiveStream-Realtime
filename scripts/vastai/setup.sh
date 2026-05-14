#!/usr/bin/env bash
# One-shot setup trên Vast.AI instance — chạy 1 lần sau khi clone repo.
set -euo pipefail
cd "$(dirname "$0")/../.."

echo "[Setup] LiveTalking Sales — Vast.AI install"

# System deps
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -yq
  apt-get install -yq --no-install-recommends \
    ffmpeg wget curl git unzip \
    libsndfile1 libgl1 libglib2.0-0 \
    build-essential pkg-config
fi

# Python deps (1 file all-in-one)
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# Models
bash scripts/vastai/download_models.sh

# Data dirs
mkdir -p data/avatars data/uploads/raw data/uploads/jobs data/uploads/previews

cat <<'EOF'

═══════════════════════════════════════════════════════════════════
 LiveTalking Sales — Setup complete!
═══════════════════════════════════════════════════════════════════

Bước tiếp theo:

 1. Set OPENAI_API_KEY (cho LLM brain):
      export OPENAI_API_KEY=sk-...
    Hoặc local LLM (Ollama):
      export LLM_URL=http://host:11434/v1
      export LLM_MODEL=qwen2.5:7b

 2. Start production:
      bash scripts/vastai/start.sh

 3. Truy cập admin:
      http://<vastai-ip>:8010/

TTS mặc định: VieNeu-TTS (Apache 2.0, realtime CPU/GPU).
    Đổi sang F5-TTS: export TTS_ENGINE=f5tts
EOF
