#!/usr/bin/env bash
###############################################################################
#  LiveTalking — PRODUCTION start (3-venv stack, env-driven)
#
#  Spawns 3 processes sequentially, waiting each healthy before next:
#
#    1. lmdeploy api_server  (venv_lmdeploy, torch 2.4 cu121, lmdeploy 0.9.0)
#       → :23333  Qwen3 backbone bfloat16, OpenAI-compat /v1/chat/completions
#       (custom passthrough chat-template vieneu_chat_template.json)
#    2. vieneu_server.py     (venv_vieneu, torch 2.4+, ONNX codec)
#       → :23334  /infer_stream
#       Hybrid: split sentences → tts.infer() batch each → stream HTTP chunks
#    3. app.py               (venv_talking, torch 2.4+, wav2lip + soxr)
#       → :8010  /  (web + avatar + wsstream MPEG-TS over WS)
#
#  Browser truy cập http://<PUBLIC_IPADDR>:8010 (chỉ port này expose public).
#
#  Env vars:
#    AVATAR_ID         (default: wav2lip256_avatar1)
#    AVATAR_MODEL      wav2lip | musetalk | ultralight   (default: wav2lip)
#    LMDEPLOY_PORT     (default: 23333)
#    LMDEPLOY_TP       Tensor-parallel size              (default: 1)
#    VIENEU_HTTP_PORT  (default: 23334)
#    VIENEU_EMOTION    natural | storytelling            (default: natural)
#    VIENEU_MODEL      HF repo                  (default: pnnbao-ump/VieNeu-TTS-v2)
#    TRANSPORT         wsstream | virtualcam             (default: wsstream)
#    LISTEN_PORT       (default: 8010)
#    BRAIN_ENABLED     true | false                      (default: false)
#    STUDIO_ENABLED    true | false                      (default: false)
#    OPENAI_API_KEY    (cho LLM sales brain — KHÔNG liên quan TTS)
###############################################################################
set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_ROOT="$(pwd)"
VENV_DIR="${REPO_ROOT}/venv_talking"
VENV_LMDEPLOY_DIR="${REPO_ROOT}/venv_lmdeploy"
VENV_VIENEU_DIR="${REPO_ROOT}/venv_vieneu"

# Activate venv_talking (cho exec app.py cuối cùng).
if [[ -z "${VIRTUAL_ENV:-}" ]] && [[ -f "${VENV_DIR}/bin/activate" ]]; then
  # shellcheck disable=SC1090
  source "${VENV_DIR}/bin/activate"
fi

# CUDA libs cho venv_talking (pytorch/pytorch image — bundle trong conda).
if [[ -d /opt/conda/lib/python3.11/site-packages/nvidia ]]; then
  TORCH_CUDA_LIBS="$(find /opt/conda/lib/python3.11/site-packages/nvidia -name '*.so*' 2>/dev/null | xargs -I{} dirname {} 2>/dev/null | sort -u | tr '\n' ':')"
  export LD_LIBRARY_PATH="${TORCH_CUDA_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# ─── Env defaults ──────────────────────────────────────────────────────
AVATAR_ID="${AVATAR_ID:-wav2lip256_avatar1}"
AVATAR_MODEL="${AVATAR_MODEL:-wav2lip}"
TRANSPORT="${TRANSPORT:-wsstream}"
LISTEN_PORT="${LISTEN_PORT:-8010}"
BRAIN_ENABLED="${BRAIN_ENABLED:-false}"
PRODUCTS_PATH="${PRODUCTS_PATH:-data/products.json}"
PERSONA="${PERSONA:-linh_vi}"
LLM_URL="${LLM_URL:-https://api.openai.com/v1}"
LLM_MODEL="${LLM_MODEL:-gpt-4o-mini}"
LLM_API_KEY="${OPENAI_API_KEY:-${LLM_API_KEY:-none}}"
STUDIO_ENABLED="${STUDIO_ENABLED:-false}"

LMDEPLOY_PORT="${LMDEPLOY_PORT:-23333}"
LMDEPLOY_TP="${LMDEPLOY_TP:-1}"
# KV cache size = fraction of TOTAL VRAM lmdeploy pre-allocate. Default 0.8
# = 80% GPU dành cho concurrent serving — phí phạm cho TTS single-stream.
#
# Sequence length thực tế: 1-2 sentences ≈ 512 audio tokens = 11MB cache đủ.
# 0.1 = ~2.4GB cache + 600MB Qwen3-0.3B weights + ~1GB workspace = ~4GB total
# → match doc minimum "4GB+ VRAM", free ~20GB cho avatar pipeline.
# (musetalk ~6GB, wav2lip ~3GB)
LMDEPLOY_CACHE="${LMDEPLOY_CACHE:-0.1}"
VIENEU_HTTP_PORT="${VIENEU_HTTP_PORT:-23334}"
VIENEU_EMOTION="${VIENEU_EMOTION:-natural}"
VIENEU_MODEL="${VIENEU_MODEL:-pnnbao-ump/VieNeu-TTS-v2}"
VIENEU_VOICE_ID="${VIENEU_VOICE_ID:-}"
VIENEU_REF_AUDIO_DEFAULT="data/avatars/${AVATAR_ID}/voice/ref.wav"
VIENEU_REF_TEXT_FILE="data/avatars/${AVATAR_ID}/voice/ref.txt"
VIENEU_REF_AUDIO="${VIENEU_REF_AUDIO:-${VIENEU_REF_AUDIO_DEFAULT}}"
VIENEU_REF_TEXT="${VIENEU_REF_TEXT:-}"
[[ -z "${VIENEU_REF_TEXT}" && -f "${VIENEU_REF_TEXT_FILE}" ]] && VIENEU_REF_TEXT="$(cat "${VIENEU_REF_TEXT_FILE}")"

if [[ ! -d "data/avatars/${AVATAR_ID}" ]]; then
  echo "[WARN] data/avatars/${AVATAR_ID} chưa có — server start nhưng render thread sẽ fail."
fi

mkdir -p logs

# ─── Cleanup hook ──────────────────────────────────────────────────────
LMDEPLOY_PID=""
VIENEU_SRV_PID=""
cleanup() {
  echo "[start] cleanup..."
  for pid in "${VIENEU_SRV_PID}" "${LMDEPLOY_PID}"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill -TERM "${pid}" 2>/dev/null || true
      pkill -P "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

# Helper: compute LD_LIBRARY_PATH for a given venv (torch bundled CUDA libs)
venv_cuda_libs() {
  local venv="$1"
  local site
  site="$("${venv}/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
  if [[ -d "${site}/nvidia" ]]; then
    find "${site}/nvidia" -name '*.so*' 2>/dev/null | xargs -I{} dirname {} 2>/dev/null | sort -u | tr '\n' ':'
  fi
}

# ───────────────────────────────────────────────────────────────────
#  STEP 1 — lmdeploy api_server (venv_lmdeploy)
# ───────────────────────────────────────────────────────────────────
if [[ ! -f "${VENV_LMDEPLOY_DIR}/bin/python" ]]; then
  echo "[ERROR] venv_lmdeploy chưa có. Chạy: bash scripts/vastai/setup.sh"
  exit 1
fi
echo "[start] === Step 1/3: lmdeploy api_server (venv_lmdeploy) ==="
echo "[start] model=${VIENEU_MODEL}  port=${LMDEPLOY_PORT}  tp=${LMDEPLOY_TP}"

LMD_CUDA_LIBS="$(venv_cuda_libs "${VENV_LMDEPLOY_DIR}")"
LD_LIBRARY_PATH="${LMD_CUDA_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
nohup "${VENV_LMDEPLOY_DIR}/bin/python" -u -m lmdeploy \
  serve api_server "${VIENEU_MODEL}" \
  --server-name 127.0.0.1 \
  --server-port "${LMDEPLOY_PORT}" \
  --tp "${LMDEPLOY_TP}" \
  --chat-template "${LMDEPLOY_CHAT_TEMPLATE:-scripts/vastai/vieneu_chat_template.json}" \
  --cache-max-entry-count "${LMDEPLOY_CACHE}" \
  > logs/lmdeploy.log 2>&1 &
LMDEPLOY_PID=$!
echo "[start] lmdeploy pid=${LMDEPLOY_PID} → logs/lmdeploy.log"

echo "[start] waiting lmdeploy /v1/models (max 300s, model load ~30-90s lần đầu)..."
for i in $(seq 1 150); do
  if curl -sf "http://127.0.0.1:${LMDEPLOY_PORT}/v1/models" >/dev/null 2>&1; then
    echo "[start] lmdeploy READY @ :${LMDEPLOY_PORT}"
    break
  fi
  if ! kill -0 "${LMDEPLOY_PID}" 2>/dev/null; then
    echo "[ERROR] lmdeploy died early. Last 50 lines logs/lmdeploy.log:"
    tail -n 50 logs/lmdeploy.log || true
    exit 1
  fi
  sleep 2
done
if ! curl -sf "http://127.0.0.1:${LMDEPLOY_PORT}/v1/models" >/dev/null 2>&1; then
  echo "[ERROR] lmdeploy timeout sau 300s. Check logs/lmdeploy.log"
  exit 1
fi

# ───────────────────────────────────────────────────────────────────
#  STEP 2 — vieneu_server.py (venv_vieneu)
# ───────────────────────────────────────────────────────────────────
if [[ ! -f "${VENV_VIENEU_DIR}/bin/python" ]]; then
  echo "[ERROR] venv_vieneu chưa có. Chạy: bash scripts/vastai/setup.sh"
  exit 1
fi
echo "[start] === Step 2/3: vieneu_server.py (venv_vieneu) ==="
echo "[start] http_port=${VIENEU_HTTP_PORT}  backbone=http://127.0.0.1:${LMDEPLOY_PORT}/v1"

VIENEU_CUDA_LIBS="$(venv_cuda_libs "${VENV_VIENEU_DIR}")"
LD_LIBRARY_PATH="${VIENEU_CUDA_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
nohup "${VENV_VIENEU_DIR}/bin/python" -u scripts/vastai/vieneu_server.py \
  --port "${VIENEU_HTTP_PORT}" \
  --host 127.0.0.1 \
  --model "${VIENEU_MODEL}" \
  --emotion "${VIENEU_EMOTION}" \
  --lmdeploy_url "http://127.0.0.1:${LMDEPLOY_PORT}/v1" \
  > logs/vieneu_server.log 2>&1 &
VIENEU_SRV_PID=$!
echo "[start] vieneu_server pid=${VIENEU_SRV_PID} → logs/vieneu_server.log"

echo "[start] waiting vieneu_server /health (max 180s, codec load lần đầu)..."
for i in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:${VIENEU_HTTP_PORT}/health" >/dev/null 2>&1; then
    echo "[start] vieneu_server READY @ :${VIENEU_HTTP_PORT}"
    break
  fi
  if ! kill -0 "${VIENEU_SRV_PID}" 2>/dev/null; then
    echo "[ERROR] vieneu_server died early. Last 50 lines logs/vieneu_server.log:"
    tail -n 50 logs/vieneu_server.log || true
    exit 1
  fi
  sleep 2
done

# ───────────────────────────────────────────────────────────────────
#  STEP 3 — app.py (venv_talking, foreground exec)
# ───────────────────────────────────────────────────────────────────
ARGS=(
  --model "${AVATAR_MODEL}"
  --avatar_id "${AVATAR_ID}"
  --tts vieneu_http
  --transport "${TRANSPORT}"
  --listenport "${LISTEN_PORT}"
  --brain_enabled "${BRAIN_ENABLED}"
  --products_path "${PRODUCTS_PATH}"
  --persona "${PERSONA}"
  --llm_url "${LLM_URL}"
  --llm_model "${LLM_MODEL}"
  --studio_enabled "${STUDIO_ENABLED}"
  --vieneu_http_host 127.0.0.1
  --vieneu_http_port "${VIENEU_HTTP_PORT}"
)
# Security: KHÔNG đưa LLM_API_KEY vào ARGS (visible qua `ps aux`).
# Export sang env để app.py đọc qua os.environ — config.py có fallback.
export OPENAI_API_KEY="${LLM_API_KEY}"
export LLM_API_KEY="${LLM_API_KEY}"
[[ -n "${VIENEU_VOICE_ID}" ]] && ARGS+=(--vieneu_voice_id "${VIENEU_VOICE_ID}")
[[ -f "${VIENEU_REF_AUDIO}" ]] && ARGS+=(--vieneu_ref_audio "${VIENEU_REF_AUDIO}")
[[ -n "${VIENEU_REF_TEXT}" ]] && ARGS+=(--vieneu_ref_text "${VIENEU_REF_TEXT}")
# Production perf knobs (env-overridable)
[[ -n "${BATCH_SIZE:-}" ]]              && ARGS+=(--batch_size "${BATCH_SIZE}")
[[ -n "${VIENEU_HTTP_PREBUFFER:-}" ]]   && ARGS+=(--vieneu_http_prebuffer "${VIENEU_HTTP_PREBUFFER}")

echo "[start] === Step 3/3: app.py (venv_talking) ==="
echo "[start] open: http://\$PUBLIC_IPADDR:${LISTEN_PORT}/"
exec python -u app.py "${ARGS[@]}" 2>&1 | tee server.log
