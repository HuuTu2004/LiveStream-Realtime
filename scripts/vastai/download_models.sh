#!/usr/bin/env bash
# Download model weights — idempotent (skip nếu đã có).
set -euo pipefail
cd "$(dirname "$0")/../.."

MODELS_DIR="$(pwd)/models"
mkdir -p "${MODELS_DIR}"
echo "[Models] Target: ${MODELS_DIR}"

download() {
  local url="$1" target="$2"
  [[ -f "${target}" ]] && { echo "[skip] ${target}"; return 0; }
  echo "[get ] ${url} → ${target}"
  mkdir -p "$(dirname "${target}")"
  wget -q --show-progress -O "${target}" "${url}" || { rm -f "${target}"; return 1; }
}

hf_snapshot() {
  local repo="$1" target="$2"
  if [[ -d "${target}" ]] && [[ -n "$(ls -A "${target}" 2>/dev/null)" ]]; then
    echo "[skip] HF ${repo}"
    return 0
  fi
  echo "[hf  ] ${repo} → ${target}"
  python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='${repo}', local_dir='${target}', local_dir_use_symlinks=False)
"
}

# ─── Wav2Lip ──────────────────────────────────────────────────────────
download "https://huggingface.co/lipku/livetalking/resolve/main/wav2lip.pth" \
         "${MODELS_DIR}/wav2lip.pth" || true

# ─── MuseTalk + dependencies (VAE, whisper) ──────────────────────────
hf_snapshot "TMElyralab/MuseTalk" "${MODELS_DIR}/musetalk_raw" || true
if [[ -d "${MODELS_DIR}/musetalk_raw" ]]; then
  mkdir -p "${MODELS_DIR}/musetalk" "${MODELS_DIR}/sd-vae-ft-mse" "${MODELS_DIR}/whisper"
  cp -nr "${MODELS_DIR}/musetalk_raw/musetalk"/* "${MODELS_DIR}/musetalk/" 2>/dev/null || true
  cp -nr "${MODELS_DIR}/musetalk_raw/sd-vae-ft-mse"/* "${MODELS_DIR}/sd-vae-ft-mse/" 2>/dev/null || true
  cp -nr "${MODELS_DIR}/musetalk_raw/whisper"/* "${MODELS_DIR}/whisper/" 2>/dev/null || true
fi
if [[ ! -f "${MODELS_DIR}/whisper/tiny.pt" ]]; then
  download "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e7e3aa630fdf1f15b0a3540c0/tiny.pt" \
           "${MODELS_DIR}/whisper/tiny.pt" || true
fi

# ─── VieNeu-TTS (default, GPU mode = lmdeploy + TurboMind) ───────────
# Pre-cache để start lmdeploy nhanh hơn — repo phải có safetensors cho TurboMind.
echo "[VieNeu] Pre-caching pnnbao-ump/VieNeu-TTS-v2 (HF cache)..."
python -c "
import os
os.environ.setdefault('HF_HOME', '/workspace/.cache/huggingface')
try:
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id='pnnbao-ump/VieNeu-TTS-v2', local_dir_use_symlinks=False)
    print('[VieNeu] cached vào HF_HOME')
except Exception as e:
    print('[VieNeu] pre-cache fail (sẽ download khi lmdeploy start):', e)
"
# Quick check lmdeploy đã cài
if python -c "import lmdeploy" 2>/dev/null; then
  echo "[VieNeu] lmdeploy OK — GPU mode ready"
else
  echo "[VieNeu] WARN: lmdeploy chưa cài. Sửa requirements.txt rồi pip install -r requirements.txt"
fi

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
