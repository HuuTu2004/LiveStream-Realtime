#!/usr/bin/env bash
###############################################################################
#  LiveTalking — Vast.ai one-shot setup (idempotent, PRODUCTION 2-venv FAST mode)
#
#  Architecture mặc định = FAST mode (per VieNeu docs khuyến nghị):
#    https://docs.vieneu.io/vi/docs/sdk/fast-mode
#
#  Fast mode = vieneu_server tự load Qwen3 backbone IN-PROCESS bằng LMDeploy
#  turbomind (pull tự động qua vieneu[gpu]). KHÔNG cần venv riêng cho
#  lmdeploy api_server. 2 venv = ít disk, ít VRAM contention, ít chỗ
#  để ABI conflict.
#
#    ┌──────────────────────────────────────────────────────────────┐
#    │ venv_vieneu   (vieneu[gpu] = torch 2.x + lmdeploy + ONNX codec)│
#    │   :23334 /infer_stream                                        │
#    │   - In-process turbomind: text → audio_tokens                 │
#    │   - ONNX int8 codec: audio_tokens → PCM 24kHz                 │
#    └──────────────────────────────────────────────────────────────┘
#                          ↓ HTTP length-prefixed f32le PCM
#    ┌──────────────────────────────────────────────────────────────┐
#    │ venv_talking  (torch 2.4+ cu121 + wav2lip + soxr)             │
#    │   :8010 /  ← web + avatar + wsstream MPEG-TS                  │
#    └──────────────────────────────────────────────────────────────┘
#
#  Production knobs:
#   - Codec: ONNX int8 (5x realtime, sạch hơn PyTorch neucodec)
#   - Gen:   tts.infer() batch per sentence
#   - Mode:  fast (default) — VieNeu docs khuyến nghị, single-GPU mượt nhất
#
#  Để dùng remote mode (lmdeploy api_server riêng) export:
#     SETUP_LMDEPLOY=true bash scripts/vastai/setup.sh
#
#  Steps:
#    1. apt deps (ffmpeg, build tools)
#    2. venv_talking — reuse Vast /opt/conda torch 2.4 hoặc tạo mới
#    3. venv_vieneu  — fresh venv + pip install vieneu[gpu]
#    4. (optional) venv_lmdeploy nếu SETUP_LMDEPLOY=true
#    5. Verify import cả 2-3 venv
###############################################################################
set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_ROOT="$(pwd)"
VENV_DIR="${REPO_ROOT}/venv_talking"
VENV_LMDEPLOY_DIR="${REPO_ROOT}/venv_lmdeploy"
VENV_VIENEU_DIR="${REPO_ROOT}/venv_vieneu"

SETUP_LMDEPLOY="${SETUP_LMDEPLOY:-false}"

GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo unknown)"
if echo "${GPU_NAME}" | grep -qE "RTX 50|GB200|B100|B200"; then
  TORCH_INDEX="https://download.pytorch.org/whl/cu128"
  TORCH_TAG="cu128 (Blackwell)"
else
  TORCH_INDEX="https://download.pytorch.org/whl/cu121"
  TORCH_TAG="cu121"
fi
echo "[setup] GPU: ${GPU_NAME} → torch ${TORCH_TAG}"
echo "[setup] mode: fast (default)  SETUP_LMDEPLOY=${SETUP_LMDEPLOY}"

# ─── uv bootstrap ──────────────────────────────────────────────────────────
# uv (Astral) là pip-compatible installer viết bằng Rust — parallel resolver
# + parallel download + hard-link cache. Trên Vast.AI cài vieneu[gpu] (178
# deps, ~6GB) test cho thấy:
#   pip:  17+ phút (resolver loop, --no-cache-dir = re-download mỗi version)
#   uv:   ~2-3 phút (resolver 40s, download song song ~15-20 MB/s tổng)
# Cài uv ở user-local nếu chưa có. Idempotent.
UV_BIN="$(command -v uv || true)"
if [[ -z "${UV_BIN}" ]]; then
  echo "[setup] cài uv (Astral fast pip replacement)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
  UV_BIN="$(command -v uv || true)"
fi
if [[ -n "${UV_BIN}" ]]; then
  echo "[setup] uv $(uv --version 2>/dev/null | awk '{print $2}') → ${UV_BIN}"
else
  echo "[setup] WARN: uv không cài được — fallback pip (sẽ chậm 5-10x)"
fi

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
# pypi.org direct (Fastly CDN, US edge ~50-100 Mbps; aliyun từ US/VN throttle).
pip install --no-cache-dir \
  -i https://pypi.org/simple/ \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu/ \
  -r "${REQ_FILE}"

# numpy có thể bị bump >=2.0 do dep tree → downgrade lại cho wav2lip
pip install --no-cache-dir 'numpy<2.0' >/dev/null 2>&1 || true

# Post-install fix: vieneu>=1.1.0 trong requirements có thể bump torch
# (typically lên 2.10+cu128) → torchaudio bị bump theo lên 2.11+cu13 ABI
# mismatch (expects libcudart.so.13). Detect torch version + cuda tag,
# force-pin torchaudio để khớp. Idempotent — skip nếu đã match.
TALKING_TORCH=$(python -c "import torch; print(torch.__version__)" 2>/dev/null || true)
TALKING_TA=$(python -c "import torchaudio; print(torchaudio.__version__)" 2>/dev/null || true)
if [[ -n "$TALKING_TORCH" && -n "$TALKING_TA" ]]; then
  T_VER=${TALKING_TORCH%%+*}; T_CUDA=${TALKING_TORCH##*+}
  TA_VER=${TALKING_TA%%+*}
  if [[ "$T_VER" != "$TA_VER" ]]; then
    echo "[setup] venv_talking: torch=$TALKING_TORCH ≠ torchaudio=$TALKING_TA — pin to match"
    pip install --no-cache-dir --index-url "https://download.pytorch.org/whl/${T_CUDA}" \
      "torchaudio==${T_VER}" || echo "[WARN] torchaudio==${T_VER} cu=${T_CUDA} wheel không có"
  fi
fi

# ─── 3. venv_vieneu (FAST mode — vieneu[gpu] pull torch + lmdeploy in-process) ─
# Theo official docs (https://docs.vieneu.io/vi/docs/getting-started/installation):
#   pip install vieneu[gpu]
# vieneu[gpu] tự pull đúng torch + lmdeploy + neucodec compatible với nhau.
# KHÔNG cài torch trước (sẽ bị resolver upgrade → phí 2GB download).
PYBIN="$(command -v python3.11 || command -v python3.10 || command -v python3)"
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

echo "[setup] venv_vieneu: install -r scripts/vastai/requirements_vieneu.txt"
echo "[setup]   (vieneu[gpu] sẽ tự pull torch + lmdeploy + neucodec — 178 deps ~6GB)"
if [[ -n "${UV_BIN}" ]]; then
  # uv: parallel resolver + parallel download, ~5-10x nhanh hơn pip.
  # KHÔNG dùng --no-cache-dir — uv cache (~/.cache/uv) hard-link sang venv,
  # disk savings + re-run đỡ tốn bandwidth.
  echo "[setup]   sử dụng uv (parallel resolver + hard-link cache)"
  "${UV_BIN}" pip install --python "${PY_VIENEU}" \
    --index-url https://pypi.org/simple/ \
    -r scripts/vastai/requirements_vieneu.txt
else
  echo "[setup]   fallback pip (chậm — uv không khả dụng)"
  ${PY_VIENEU} -m pip install --no-cache-dir \
    -i https://pypi.org/simple/ \
    -r scripts/vastai/requirements_vieneu.txt
fi

# Post-install: torchaudio ABI fix. vieneu[gpu] có thể pull torch 2.10.0+cu128
# nhưng resolver bump torchaudio lên 2.11.0+cu13 (expects libcudart.so.13 —
# KHÔNG có trong môi trường cu12). Detect torch full version + cuda tag,
# force-pin torchaudio để khớp.
VIENEU_TORCH=$(${PY_VIENEU} -c "import torch; print(torch.__version__)" 2>/dev/null || true)
VIENEU_TA=$(${PY_VIENEU} -c "import torchaudio; print(torchaudio.__version__)" 2>/dev/null || true)
if [[ -n "$VIENEU_TORCH" && -n "$VIENEU_TA" ]]; then
  VT_VER=${VIENEU_TORCH%%+*}; VT_CUDA=${VIENEU_TORCH##*+}
  VTA_VER=${VIENEU_TA%%+*}
  if [[ "$VT_VER" != "$VTA_VER" ]]; then
    echo "[setup] venv_vieneu: torch=$VIENEU_TORCH ≠ torchaudio=$VIENEU_TA — pin to match"
    ${PY_VIENEU} -m pip install --no-cache-dir --index-url "https://download.pytorch.org/whl/${VT_CUDA}" \
      "torchaudio==${VT_VER}" || echo "[WARN] torchaudio==${VT_VER} cu=${VT_CUDA} wheel không có"
  fi
fi

# ─── 4. (Optional) venv_lmdeploy — chỉ cài khi user explicit opt-in ────────
# Default FAST mode = không cần. Chỉ enable nếu user muốn dùng remote mode
# (separate lmdeploy api_server cho multi-tenancy hoặc swap LLM).
if [[ "${SETUP_LMDEPLOY}" == "true" ]]; then
  echo "[setup] === venv_lmdeploy (SETUP_LMDEPLOY=true → remote mode) ==="
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

  # Torch 2.4.1 cu121 — lmdeploy 0.9.0 ABI build against torch 2.4.
  if ! ${PY_LMD} -c "import torch; assert torch.__version__.startswith('2.4')" 2>/dev/null; then
    echo "[setup] venv_lmdeploy: installing torch 2.4.1 cu121..."
    ${PY_LMD} -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 \
      torch==2.4.1 torchvision==0.19.1 torchaudio==2.4.1
  fi

  echo "[setup] venv_lmdeploy: install -r scripts/vastai/requirements_lmdeploy.txt"
  if [[ -n "${UV_BIN}" ]]; then
    "${UV_BIN}" pip install --python "${PY_LMD}" \
      --index-url https://pypi.org/simple/ \
      -r scripts/vastai/requirements_lmdeploy.txt
    "${UV_BIN}" pip install --python "${PY_LMD}" partial_json_parser >/dev/null 2>&1 || true
  else
    ${PY_LMD} -m pip install --no-cache-dir \
      -i https://pypi.org/simple/ \
      -r scripts/vastai/requirements_lmdeploy.txt
    ${PY_LMD} -m pip install --no-cache-dir partial_json_parser >/dev/null 2>&1 || true
  fi
fi

# ─── 5. Data dirs ──────────────────────────────────────────────────────────
mkdir -p data/avatars data/uploads/raw data/uploads/jobs data/uploads/previews models logs

# ─── 6. Download wav2lip.pth nếu thiếu ─────────────────────────────────────
if [[ ! -f models/wav2lip.pth ]]; then
  echo "[setup] tải wav2lip.pth từ HF mirror..."
  wget -q --show-progress -O models/wav2lip.pth \
    "https://huggingface.co/lipku/livetalking/resolve/main/wav2lip.pth" \
    || { rm -f models/wav2lip.pth; echo "[WARN] HF mirror fail — scp manual từ local"; }
fi

# ─── 7. Verify venv ───────────────────────────────────────────────────────
echo "[verify] === venv_talking ==="
python - <<'PY'
import torch
print(f"[talking] torch={torch.__version__}  cuda={torch.cuda.is_available()}  GPU={torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE'}")
assert torch.cuda.is_available(), "CUDA không khả dụng trong venv_talking"
PY

echo "[verify] === venv_vieneu ==="
"${VENV_VIENEU_DIR}/bin/python" - <<'PY'
import torch
print(f"[vieneu] torch={torch.__version__}  cuda={torch.cuda.is_available()}")
import vieneu
print(f"[vieneu] vieneu={getattr(vieneu, '__version__', '?')}")
try:
    import lmdeploy
    print(f"[vieneu] lmdeploy={lmdeploy.__version__} (in-process, fast mode)")
except Exception as e:
    print(f"[vieneu] WARN: lmdeploy import fail: {e} — fast mode sẽ KHÔNG chạy được")
import onnxruntime as ort
print(f"[vieneu] onnxruntime={ort.__version__}  providers={ort.get_available_providers()}")
PY

if [[ "${SETUP_LMDEPLOY}" == "true" ]]; then
  echo "[verify] === venv_lmdeploy ==="
  "${VENV_LMDEPLOY_DIR}/bin/python" - <<'PY'
import torch, lmdeploy
print(f"[lmdeploy] torch={torch.__version__}  cuda={torch.cuda.is_available()}")
print(f"[lmdeploy] lmdeploy={lmdeploy.__version__}")
assert torch.cuda.is_available(), "CUDA không khả dụng trong venv_lmdeploy"
PY
fi

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
 LiveTalking — Setup OK  (FAST mode, 2-venv default)
═══════════════════════════════════════════════════════════════════
 venv_vieneu   : ${VENV_VIENEU_DIR}
                  vieneu[gpu] + lmdeploy in-process + ONNX codec
 venv_talking  : ${VENV_DIR}
                  torch ${TORCH_TAG} + wav2lip/musetalk + soxr
$( [[ "${SETUP_LMDEPLOY}" == "true" ]] && echo " venv_lmdeploy : ${VENV_LMDEPLOY_DIR}
                  torch 2.4.1 cu121 + lmdeploy 0.9.0 (remote mode opt-in)" )

 Start         : bash scripts/vastai/start.sh
 Stack (fast)  :
   ├─ vieneu_server.py    → :23334 /infer_stream
   │  └─ in-process turbomind + ONNX codec
   └─ app.py              → :8010 /  (web + avatar + wsstream MPEG-TS)

 Open          : http://\$PUBLIC_IPADDR:8010/

 Remote mode (opt-in):
   SETUP_LMDEPLOY=true bash scripts/vastai/setup.sh
   VIENEU_MODE=remote bash scripts/vastai/start.sh
═══════════════════════════════════════════════════════════════════
EOF
