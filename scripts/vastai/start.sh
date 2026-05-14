#!/usr/bin/env bash
###############################################################################
#  LiveTalking — production start trên Vast.ai (env-driven)
#
#  Yêu cầu: đã chạy scripts/vastai/setup.sh trước.
#
#  Env vars (đều có default an toàn):
#    AVATAR_ID         (default: wav2lip256_avatar1)
#    AVATAR_MODEL      wav2lip | musetalk | ultralight   (default: wav2lip)
#    TTS_ENGINE        vieneu                              (default: vieneu)
#    VIENEU_MODE       gpu | standard | turbo | remote    (default: gpu)
#    TRANSPORT         webrtc | rtmp | virtualcam | rtcpush (default: webrtc)
#    LISTEN_PORT       (default: 8010)
#    BRAIN_ENABLED     true | false                       (default: false)
#    OPENAI_API_KEY    (cho LLM brain)
#    LLM_URL           (default: https://api.openai.com/v1)
#    LLM_MODEL         (default: gpt-4o-mini)
#    HF_TOKEN          (optional — tăng rate limit khi pull VieNeu model)
###############################################################################
set -euo pipefail
cd "$(dirname "$0")/../.."

REPO_ROOT="$(pwd)"
VENV_DIR="${REPO_ROOT}/venv_talking"

# Activate venv nếu chưa active
if [[ -z "${VIRTUAL_ENV:-}" ]] && [[ -f "${VENV_DIR}/bin/activate" ]]; then
  # shellcheck disable=SC1090
  source "${VENV_DIR}/bin/activate"
fi

AVATAR_ID="${AVATAR_ID:-wav2lip256_avatar1}"
AVATAR_MODEL="${AVATAR_MODEL:-wav2lip}"
TTS_ENGINE="${TTS_ENGINE:-vieneu}"
TRANSPORT="${TRANSPORT:-webrtc}"
LISTEN_PORT="${LISTEN_PORT:-8010}"
BRAIN_ENABLED="${BRAIN_ENABLED:-false}"
PRODUCTS_PATH="${PRODUCTS_PATH:-data/products.json}"
PERSONA="${PERSONA:-linh_vi}"
LLM_URL="${LLM_URL:-https://api.openai.com/v1}"
LLM_MODEL="${LLM_MODEL:-gpt-4o-mini}"
LLM_API_KEY="${OPENAI_API_KEY:-${LLM_API_KEY:-none}}"
STUDIO_ENABLED="${STUDIO_ENABLED:-true}"

VIENEU_MODE="${VIENEU_MODE:-turbo}"
VIENEU_EMOTION="${VIENEU_EMOTION:-natural}"
VIENEU_VOICE_ID="${VIENEU_VOICE_ID:-}"
VIENEU_PORT="${VIENEU_PORT:-23333}"
VIENEU_TP="${VIENEU_TP:-1}"
VIENEU_REF_AUDIO_DEFAULT="data/avatars/${AVATAR_ID}/voice/ref.wav"
VIENEU_REF_TEXT_FILE="data/avatars/${AVATAR_ID}/voice/ref.txt"
VIENEU_REF_AUDIO="${VIENEU_REF_AUDIO:-${VIENEU_REF_AUDIO_DEFAULT}}"
VIENEU_REF_TEXT="${VIENEU_REF_TEXT:-}"
[[ -z "${VIENEU_REF_TEXT}" && -f "${VIENEU_REF_TEXT_FILE}" ]] && VIENEU_REF_TEXT="$(cat "${VIENEU_REF_TEXT_FILE}")"

# Sanity
if [[ ! -d "data/avatars/${AVATAR_ID}" ]]; then
  echo "[WARN] data/avatars/${AVATAR_ID} chưa có. Server vẫn start nhưng /offer sẽ fail."
  echo "       Upload avatar qua scp hoặc dùng Studio tab Video để preprocess."
fi

ARGS=(
  --model "${AVATAR_MODEL}"
  --avatar_id "${AVATAR_ID}"
  --tts "${TTS_ENGINE}"
  --transport "${TRANSPORT}"
  --listenport "${LISTEN_PORT}"
  --brain_enabled "${BRAIN_ENABLED}"
  --products_path "${PRODUCTS_PATH}"
  --persona "${PERSONA}"
  --llm_url "${LLM_URL}"
  --llm_model "${LLM_MODEL}"
  --llm_api_key "${LLM_API_KEY}"
  --studio_enabled "${STUDIO_ENABLED}"
)

if [[ "${TTS_ENGINE}" == "vieneu" ]]; then
  ARGS+=(
    --vieneu_mode "${VIENEU_MODE}"
    --vieneu_emotion "${VIENEU_EMOTION}"
    --vieneu_port "${VIENEU_PORT}"
    --vieneu_tp "${VIENEU_TP}"
  )
  [[ -n "${VIENEU_VOICE_ID}" ]] && ARGS+=(--vieneu_voice_id "${VIENEU_VOICE_ID}")
  [[ -f "${VIENEU_REF_AUDIO}" ]] && ARGS+=(--vieneu_ref_audio "${VIENEU_REF_AUDIO}")
  [[ -n "${VIENEU_REF_TEXT}" ]] && ARGS+=(--vieneu_ref_text "${VIENEU_REF_TEXT}")
  [[ "${VIENEU_MODE}" == "gpu" ]] && \
    echo "[start] VieNeu GPU mode — auto-spawn lmdeploy:${VIENEU_PORT} (lần đầu ~30-60s)"
fi

if [[ "${TRANSPORT}" == "rtmp" ]]; then
  ARGS+=(--push_url "${RTMP_PUSH_URL:-rtmp://localhost/live/test}")
fi

echo "[start] python app.py ${ARGS[*]}"
echo "[start] open: http://\$PUBLIC_IPADDR:${LISTEN_PORT}/"
exec python -u app.py "${ARGS[@]}" 2>&1 | tee server.log
