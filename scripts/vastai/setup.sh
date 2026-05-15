#!/usr/bin/env bash
###############################################################################
#  LiveTalking — Vast.ai one-shot setup (idempotent, PRODUCTION 3-venv)
#
#  Architecture (1 instance, 3 venv, 3 process, ZERO pip/ABI conflict):
#
#    ┌──────────────────────────────────────────────────────────────┐
#    │ venv_lmdeploy (torch 2.4 cu121 + lmdeploy 0.9.0)              │
#    │   :23333 /v1/chat/completions  ← Qwen3 backbone bfloat16      │
#    └──────────────────────────────────────────────────────────────┘
#                          ↓ HTTP (text → audio_tokens)
#    ┌──────────────────────────────────────────────────────────────┐
#    │ venv_vieneu   (torch 2.4+ + vieneu remote + ONNX codec)       │
#    │   :23334 /infer_stream  ← codec decode tokens → PCM 24kHz     │
#    │   Hybrid: split sentences → tts.infer() batch each → stream   │
#    └──────────────────────────────────────────────────────────────┘
#                          ↓ HTTP length-prefixed f32le PCM
#    ┌──────────────────────────────────────────────────────────────┐
#    │ venv_talking  (torch 2.4 cu121 + wav2lip + soxr)              │
#    │   :8010 /  ← web + avatar + wsstream MPEG-TS                  │
#    └──────────────────────────────────────────────────────────────┘
#
#  Production knobs (đã tune sau ~15 lần fix):
#   - Codec: ONNX int8 (5x realtime, sạch hơn PyTorch neucodec với lmdeploy)
#   - Gen: tts.infer() batch per sentence (KHÔNG infer_stream → tránh artifacts)
#   - Sampling: temp 1.0, top_k 50, rep_penalty 1.2 (author API defaults)
#   - Chat template: passthrough JSON (model là raw completion, no chat)
#
#  Steps:
#    1. apt deps (ffmpeg, build tools)
#    2. venv_talking — reuse Vast /opt/conda torch 2.4 hoặc tạo mới
#    3. venv_lmdeploy — fresh venv + torch 2.4 cu121 + lmdeploy 0.9.0
#    4. venv_vieneu — fresh venv + torch 2.4+ + vieneu + onnxruntime
#    5. Verify torch.cuda + imports cả 3 venv
###############################################################################
set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_ROOT="$(pwd)"
VENV_DIR="${REPO_ROOT}/venv_talking"
VENV_LMDEPLOY_DIR="${REPO_ROOT}/venv_lmdeploy"
VENV_VIENEU_DIR="${REPO_ROOT}/venv_vieneu"

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
  echo "[setup] apt install system deps..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || true
  apt-get install -y -qq --no-install-recommends \
    build-essential pkg-config \
    git curl wget unzip \
    ffmpeg libsndfile1 libgl1 libglib2.0-0 \
    libssl-dev || true
fi

# ─── 2. venv_talking (wav2lip + web) ────────────────────────────────────────
if [[ -f /venv/main/bin/python ]] && /venv/main/bin/python -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
  PY_VER=$(/venv/main/bin/python -c "import torch; print(torch.__version__)")
  echo "[setup] venv_talking: re-using Vast /venv/main (torch ${PY_VER})"
  ln -sfn /venv/main "${VENV_DIR}"
elif [[ -f /opt/conda/bin/python ]] && /opt/conda/bin/python -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
  PY_VER=$(/opt/conda/bin/python -c "import torch; print(torch.__version__)")
  echo "[setup] venv_talking: re-using /opt/conda (torch ${PY_VER})"
  ln -sfn /opt/conda "${VENV_DIR}"
else
  if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[setup] venv_talking: no pre-installed torch — creating fresh..."
    python3 -m venv "${VENV_DIR}"
  fi
fi
if [[ -L "${VENV_DIR}" && "$(readlink -f ${VENV_DIR})" == "/opt/conda" ]]; then
  export PATH="${VENV_DIR}/bin:${PATH}"
else
  # shellcheck disable=SC1090
  source "${VENV_DIR}/bin/activate"
fi
python -m pip install --upgrade pip setuptools wheel

if ! python -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
  echo "[setup] venv_talking: installing torch from ${TORCH_INDEX}..."
  pip install --index-url "${TORCH_INDEX}" torch torchvision torchaudio
fi

REQ_FILE="scripts/vastai/requirements_vast.txt"
[[ ! -f "${REQ_FILE}" ]] && REQ_FILE="requirements.txt"
echo "[setup] venv_talking: pip install -r ${REQ_FILE}"
pip install --no-cache-dir \
  -i https://mirrors.aliyun.com/pypi/simple/ \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu/ \
  --extra-index-url https://pypi.org/simple/ \
  -r "${REQ_FILE}"

# numpy có thể bị bump >=2.0 do dep tree → downgrade lại cho wav2lip
pip install --no-cache-dir 'numpy<2.0' >/dev/null 2>&1 || true

# ─── 3. venv_lmdeploy (TTS backbone server, torch 2.4 cu121) ────────────────
# Fresh venv vì lmdeploy 0.9.0 kéo theo xgrammar+tvm_ffi+torch_c_dlpack_ext
# (ABI cxx11) — phải match đúng torch 2.4 cu121 wheels, không share venv khác.
PYBIN="$(command -v python3.11 || command -v python3.10 || command -v python3)"
echo "[setup] venv_lmdeploy: using ${PYBIN}"
if [[ ! -f "${VENV_LMDEPLOY_DIR}/bin/python" ]]; then
  echo "[setup] creating fresh venv_lmdeploy at ${VENV_LMDEPLOY_DIR}..."
  "${PYBIN}" -m venv --without-pip "${VENV_LMDEPLOY_DIR}" || \
    "${PYBIN}" -m venv "${VENV_LMDEPLOY_DIR}"
  if ! "${VENV_LMDEPLOY_DIR}/bin/python" -m pip --version >/dev/null 2>&1; then
    "${VENV_LMDEPLOY_DIR}/bin/python" -m ensurepip --upgrade || \
    curl -sS https://bootstrap.pypa.io/get-pip.py | "${VENV_LMDEPLOY_DIR}/bin/python"
  fi
fi
PY_LMD="${VENV_LMDEPLOY_DIR}/bin/python"
${PY_LMD} -m pip install --upgrade pip setuptools wheel

# Torch 2.4.1 cu121 — lmdeploy 0.9.0 + xgrammar 0.2.0 + tvm_ffi build against
# torch 2.4 ABI (libtorch_c_dlpack_ext-torch24-cuda.so). Khác version = ABI break.
if ! ${PY_LMD} -c "import torch; assert torch.__version__.startswith('2.4')" 2>/dev/null; then
  echo "[setup] venv_lmdeploy: installing torch 2.4.1 cu121..."
  ${PY_LMD} -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 \
    torch==2.4.1 torchvision==0.19.1 torchaudio==2.4.1
fi

echo "[setup] venv_lmdeploy: pip install -r scripts/vastai/requirements_lmdeploy.txt"
${PY_LMD} -m pip install --no-cache-dir \
  -i https://mirrors.aliyun.com/pypi/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  -r scripts/vastai/requirements_lmdeploy.txt

# Post-install: lmdeploy bug đôi khi không khai báo deps đầy đủ
${PY_LMD} -m pip install --no-cache-dir partial_json_parser >/dev/null 2>&1 || true

# ─── 4. venv_vieneu (vieneu remote + ONNX codec, torch 2.4+) ───────────────
# Production dùng ONNX codec → KHÔNG cần torch 2.6 cho PyTorch neucodec nữa.
# Vẫn fresh venv để cô lập dep với venv_lmdeploy (vieneu pull nhiều deps).
echo "[setup] venv_vieneu: using ${PYBIN}"
if [[ ! -f "${VENV_VIENEU_DIR}/bin/python" ]]; then
  echo "[setup] creating fresh venv_vieneu at ${VENV_VIENEU_DIR}..."
  "${PYBIN}" -m venv --without-pip "${VENV_VIENEU_DIR}" || \
    "${PYBIN}" -m venv "${VENV_VIENEU_DIR}"
  if ! "${VENV_VIENEU_DIR}/bin/python" -m pip --version >/dev/null 2>&1; then
    "${VENV_VIENEU_DIR}/bin/python" -m ensurepip --upgrade || \
    curl -sS https://bootstrap.pypa.io/get-pip.py | "${VENV_VIENEU_DIR}/bin/python"
  fi
fi
PY_VIENEU="${VENV_VIENEU_DIR}/bin/python"
${PY_VIENEU} -m pip install --upgrade pip setuptools wheel

# Torch 2.4 cu121 cho venv_vieneu (production dùng ONNX codec, không cần
# PyTorch neucodec 2.6+ nữa). vieneu lib chỉ cần transformers tokenizer.
if ! ${PY_VIENEU} -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
  echo "[setup] venv_vieneu: installing torch 2.4 cu121..."
  ${PY_VIENEU} -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 \
    torch==2.4.1 torchvision==0.19.1 torchaudio==2.4.1
fi

echo "[setup] venv_vieneu: pip install -r scripts/vastai/requirements_vieneu.txt"
${PY_VIENEU} -m pip install --no-cache-dir \
  -i https://mirrors.aliyun.com/pypi/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  -r scripts/vastai/requirements_vieneu.txt

# Post-install: vieneu pull transformers 5.x đôi khi → downgrade <5.0.
# Fix bug "torch.int1 attribute error" và "HubertModel import fail".
${PY_VIENEU} -m pip install --no-cache-dir 'transformers>=4.51,<5.0' 'torchao>=0.9,<0.14' >/dev/null 2>&1 || true

# ─── 5. Data dirs ──────────────────────────────────────────────────────────
mkdir -p data/avatars data/uploads/raw data/uploads/jobs data/uploads/previews models logs

# ─── 6. Download wav2lip.pth nếu thiếu ─────────────────────────────────────
if [[ ! -f models/wav2lip.pth ]]; then
  echo "[setup] tải wav2lip.pth từ HF mirror..."
  wget -q --show-progress -O models/wav2lip.pth \
    "https://huggingface.co/lipku/livetalking/resolve/main/wav2lip.pth" \
    || { rm -f models/wav2lip.pth; echo "[WARN] HF mirror fail — scp manual từ local"; }
fi

# ─── 7. Verify cả 3 venv ───────────────────────────────────────────────────
echo "[verify] === venv_talking ==="
python - <<'PY'
import torch
print(f"[talking] torch={torch.__version__}  cuda={torch.cuda.is_available()}  GPU={torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE'}")
assert torch.cuda.is_available(), "CUDA không khả dụng trong venv_talking"
PY

echo "[verify] === venv_lmdeploy ==="
"${VENV_LMDEPLOY_DIR}/bin/python" - <<'PY'
import torch, lmdeploy
print(f"[lmdeploy] torch={torch.__version__}  cuda={torch.cuda.is_available()}")
print(f"[lmdeploy] lmdeploy={lmdeploy.__version__}")
assert torch.cuda.is_available(), "CUDA không khả dụng trong venv_lmdeploy"
PY

echo "[verify] === venv_vieneu ==="
"${VENV_VIENEU_DIR}/bin/python" - <<'PY'
import torch
print(f"[vieneu] torch={torch.__version__}  cuda={torch.cuda.is_available()}")
import vieneu
print(f"[vieneu] vieneu={getattr(vieneu, '__version__', '?')}")
import onnxruntime as ort
print(f"[vieneu] onnxruntime={ort.__version__}")
PY

# ─── 8. Sample products.json ───────────────────────────────────────────────
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
 LiveTalking — Setup OK  (PRODUCTION 3-venv)
═══════════════════════════════════════════════════════════════════
 venv_lmdeploy : ${VENV_LMDEPLOY_DIR}
                  torch 2.4.1 cu121 + lmdeploy 0.9.0 + transformers 4.5x
 venv_vieneu   : ${VENV_VIENEU_DIR}
                  torch 2.4.1 cu121 + vieneu[gpu] + ONNX codec
 venv_talking  : ${VENV_DIR}
                  torch ${TORCH_TAG} + wav2lip + soxr

 Start         : bash scripts/vastai/start.sh
 Stack         :
   ├─ lmdeploy api_server → :23333 /v1  (Qwen3 bfloat16 backbone)
   │  └─ chat-template: scripts/vastai/vieneu_chat_template.json
   ├─ vieneu_server.py    → :23334 /infer_stream
   │  └─ Hybrid: split sentences + tts.infer() batch each + ONNX codec
   └─ app.py              → :8010 /  (web + avatar + wsstream MPEG-TS)

 Open          : http://\$PUBLIC_IPADDR:8010/
═══════════════════════════════════════════════════════════════════
EOF
