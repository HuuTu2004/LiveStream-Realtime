#!/usr/bin/env bash
# Download model weights — idempotent (skip nếu đã có).
# Per memory: TMElyralab/MuseTalk repo chỉ còn musetalkV15/ subdir.
# sd-vae lấy từ stabilityai/sd-vae-ft-mse → models/sd-vae
# whisper lấy từ openai/whisper-tiny (HF format) → models/whisper
set -euo pipefail
cd "$(dirname "$0")/../.."

MODELS_DIR="$(pwd)/models"
mkdir -p "${MODELS_DIR}"
echo "[Models] Target: ${MODELS_DIR}"

# Pick a python that has huggingface_hub (venv_talking after setup.sh)
if [[ -f ./venv_talking/bin/python ]]; then
  PY=./venv_talking/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi
echo "[Models] Using ${PY}"

# hf_transfer fails nếu pkg chưa cài → unset.
unset HF_HUB_ENABLE_HF_TRANSFER

hf_snapshot() {
  local repo="$1" target="$2"
  shift 2
  if [[ -d "${target}" ]] && [[ -n "$(ls -A "${target}" 2>/dev/null)" ]]; then
    echo "[skip] HF ${repo} (${target} non-empty)"
    return 0
  fi
  echo "[hf  ] ${repo} → ${target}"
  ${PY} - "$repo" "$target" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo, target = sys.argv[1], sys.argv[2]
snapshot_download(repo_id=repo, local_dir=target, local_dir_use_symlinks=False)
PY
}

# Ensure huggingface_hub có sẵn trong PY (venv_talking đã có sau setup; fallback install)
if ! ${PY} -c "import huggingface_hub" 2>/dev/null; then
  ${PY} -m pip install -q huggingface_hub || true
fi

# ─── Wav2Lip (giữ làm backup, nhỏ ~400MB) ─────────────────────────────
if [[ ! -f "${MODELS_DIR}/wav2lip.pth" ]]; then
  echo "[get ] wav2lip.pth"
  wget -q --show-progress -O "${MODELS_DIR}/wav2lip.pth" \
    "https://huggingface.co/lipku/livetalking/resolve/main/wav2lip.pth" \
    || { rm -f "${MODELS_DIR}/wav2lip.pth"; echo "[WARN] wav2lip.pth fail"; }
fi

# ─── MuseTalk V15 (unet.pth + musetalk.json) ──────────────────────────
hf_snapshot "TMElyralab/MuseTalk" "${MODELS_DIR}/_musetalk_raw" || true
# Symlink _musetalk_raw/musetalkV15 → models/musetalkV15 (code expects này)
if [[ -d "${MODELS_DIR}/_musetalk_raw/musetalkV15" ]] && [[ ! -e "${MODELS_DIR}/musetalkV15" ]]; then
  ln -sfn "${MODELS_DIR}/_musetalk_raw/musetalkV15" "${MODELS_DIR}/musetalkV15"
  echo "[link] models/musetalkV15 → _musetalk_raw/musetalkV15"
fi
# Compat: nếu code cũ còn import models/musetalk thì symlink luôn
if [[ -d "${MODELS_DIR}/_musetalk_raw/musetalk" ]] && [[ ! -e "${MODELS_DIR}/musetalk" ]]; then
  ln -sfn "${MODELS_DIR}/_musetalk_raw/musetalk" "${MODELS_DIR}/musetalk"
fi

# ─── sd-vae-ft-mse → models/sd-vae (code path) ────────────────────────
hf_snapshot "stabilityai/sd-vae-ft-mse" "${MODELS_DIR}/sd-vae" || true

# ─── whisper-tiny (HF format, Audio2Feature dùng WhisperModel.from_pretrained) ──
hf_snapshot "openai/whisper-tiny" "${MODELS_DIR}/whisper" || true

# ─── whisper tiny.pt cho openai-whisper transcribe (setup_mau_voice.sh) ────
# Đây là format pickle, KHÁC HF safetensors. Lấy từ HF mirror.
if [[ ! -f "${MODELS_DIR}/whisper/tiny.pt" ]]; then
  echo "[get ] whisper/tiny.pt (openai-whisper format)"
  wget -q --show-progress -O "${MODELS_DIR}/whisper/tiny.pt" \
    "https://huggingface.co/openai/whisper-tiny/resolve/main/original-encoder.bin" \
    2>/dev/null || \
  ${PY} - <<'PY' || true
# Fallback: openai-whisper sẽ tự download khi gọi whisper.load_model("tiny")
print("[whisper] tiny.pt không có trên HF mirror, sẽ download lúc transcribe.")
PY
fi

# ─── VieNeu-TTS pre-cache (HF cache, để lmdeploy load nhanh hơn) ─────
echo "[VieNeu] Pre-caching pnnbao-ump/VieNeu-TTS-v2 (HF cache)..."
unset HF_HUB_ENABLE_HF_TRANSFER
${PY} - <<'PY' || true
import os
os.environ.setdefault('HF_HOME', os.path.expanduser('~/.cache/huggingface'))
try:
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id='pnnbao-ump/VieNeu-TTS-v2')
    print('[VieNeu] cached vào HF_HOME')
except Exception as e:
    print('[VieNeu] pre-cache fail (sẽ download khi lmdeploy start):', e)
PY

# ─── Sample products.json ─────────────────────────────────────────────
if [[ ! -f "data/products.json" ]]; then
  mkdir -p data
  cat > data/products.json <<'EOF'
{
  "products": [
    {
      "id": "demo-01",
      "name": "Áo thun cotton basic",
      "price": "299.000đ",
      "description": "Áo thun cotton 100% basic, form rộng unisex, mặc được 4 mùa.",
      "attributes": {
        "Màu sắc": "đen, trắng, xám",
        "Kích cỡ": "S, M, L, XL",
        "Chất liệu": "cotton 100%, 220gsm"
      },
      "selling_points": [
        "Form rộng unisex mặc được 4 mùa",
        "Giặt máy không co rút",
        "Bán chạy nhất tháng"
      ],
      "faq": {
        "giá bao nhiêu": "299 ngàn 1 cái bạn nhé",
        "size nào phù hợp": "Shop có từ S đến XL, bạn cao bao nhiêu cân để Linh tư vấn",
        "ship bao lâu": "2-3 ngày toàn quốc"
      }
    }
  ]
}
EOF
  echo "[demo] created data/products.json"
fi

echo "[DONE] Models ready"
ls -lah "${MODELS_DIR}" 2>/dev/null || true
