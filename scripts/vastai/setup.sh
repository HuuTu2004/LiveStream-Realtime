#!/usr/bin/env bash
###############################################################################
#  LiveTalking — Vast.ai one-shot setup (idempotent)
#
#  Chạy trên instance Vast sau khi git clone repo. An toàn để chạy lại nhiều
#  lần (skip step đã xong).
#
#  Steps:
#    1. apt deps (ffmpeg, build tools — KHÔNG cần libsrtp/opus/vpx vì WebRTC đã loại bỏ)
#    2. Python venv tại venv_talking/
#    3. PyTorch + CUDA 12.8 (Blackwell sm_120 hỗ trợ — RTX 50xx)
#    4. requirements.txt
#    5. Verify torch.cuda
###############################################################################
set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_ROOT="$(pwd)"
VENV_DIR="${REPO_ROOT}/venv_talking"
# CUDA wheel index — chọn theo arch GPU (auto-detect):
#   Blackwell (RTX 50xx, sm_120) → cu128
#   Ada/Ampere/Hopper             → cu121 (ổn định nhất)
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo unknown)"
if echo "${GPU_NAME}" | grep -qE "RTX 50|GB200|B100|B200"; then
  TORCH_INDEX="https://download.pytorch.org/whl/cu128"
  TORCH_TAG="cu128 (Blackwell)"
else
  TORCH_INDEX="https://download.pytorch.org/whl/cu121"
  TORCH_TAG="cu121"
fi
echo "[setup] GPU: ${GPU_NAME} → torch ${TORCH_TAG}"

# ─── 1. System deps ─────────────────────────────────────────────────────────
if command -v apt-get >/dev/null 2>&1; then
  echo "[setup] apt install deps..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    python3.12-dev python3.12-venv build-essential pkg-config \
    git curl wget unzip \
    ffmpeg libsndfile1 libgl1 libglib2.0-0 \
    libssl-dev
fi

# ─── 2. Python venv ─────────────────────────────────────────────────────────
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[setup] creating venv at ${VENV_DIR}..."
  python3 -m venv "${VENV_DIR}"
fi
# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip setuptools wheel

# ─── 3. PyTorch + CUDA ─────────────────────────────────────────────────────
# Install torch riêng trước requirements.txt để pin đúng CUDA wheel
if ! python -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
  echo "[setup] installing torch from ${TORCH_INDEX}..."
  pip install --index-url "${TORCH_INDEX}" torch torchvision torchaudio
fi

# ─── 4. requirements.txt ───────────────────────────────────────────────────
echo "[setup] pip install -r requirements.txt..."
pip install -r requirements.txt

# ─── 5. Data dirs ──────────────────────────────────────────────────────────
mkdir -p data/avatars data/uploads/raw data/uploads/jobs data/uploads/previews models

# ─── 6. Download các model còn thiếu (wav2lip + musetalk + whisper) ────────
# wav2lip.pth ưu tiên đã được SCP lên trước; nếu chưa có, thử HF mirror.
if [[ ! -f models/wav2lip.pth ]]; then
  echo "[setup] tải wav2lip.pth từ HF mirror..."
  wget -q --show-progress -O models/wav2lip.pth \
    "https://huggingface.co/lipku/livetalking/resolve/main/wav2lip.pth" \
    || { rm -f models/wav2lip.pth; echo "[WARN] HF mirror fail — scp manual từ local"; }
fi

# musetalk/whisper chỉ cần nếu opt.model=musetalk (skip default wav2lip)
# Để giảm setup time, KHÔNG tự pull. Chạy scripts/vastai/download_models.sh
# riêng nếu cần musetalk.

# ─── 7. Verify torch.cuda ──────────────────────────────────────────────────
python - <<'PY'
import torch
print(f"[verify] torch={torch.__version__}  cuda={torch.version.cuda}  available={torch.cuda.is_available()}")
if torch.cuda.is_available():
    cap = torch.cuda.get_device_capability(0)
    print(f"[verify] GPU={torch.cuda.get_device_name(0)}  sm_{cap[0]}{cap[1]}")
else:
    raise SystemExit("[ERROR] CUDA không khả dụng — torch wheel sai version")
PY

# ─── 8. Sample products.json (cho demo brain) ──────────────────────────────
if [[ ! -f data/products.json ]]; then
  cat > data/products.json <<'JSON'
{
  "products": [
    {
      "id": "demo-01",
      "name": "Áo thun cotton basic",
      "price": "299.000đ",
      "description": "Áo thun cotton 100% basic, form rộng unisex.",
      "attributes": {"Màu": "đen, trắng, xám", "Size": "S, M, L, XL"},
      "selling_points": ["Form rộng 4 mùa", "Giặt máy không co rút"],
      "faq": {"giá bao nhiêu": "299 ngàn 1 cái bạn nhé"}
    }
  ]
}
JSON
fi

cat <<EOF

═══════════════════════════════════════════════════════════════════
 LiveTalking — Setup OK
═══════════════════════════════════════════════════════════════════
 Venv     : ${VENV_DIR}
 Torch    : ${TORCH_TAG}
 Activate : source ${VENV_DIR}/bin/activate
 Start    : bash scripts/vastai/start.sh
 Open     : http://\$PUBLIC_IPADDR:8010/  (port 8010 đã forward chưa?)
═══════════════════════════════════════════════════════════════════
EOF
