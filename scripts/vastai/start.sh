#!/usr/bin/env bash
# Production start — env-driven, tối ưu Vast.AI.
#
# Env vars chính:
#   AVATAR_ID         (default: demo)
#   AVATAR_MODEL      (default: wav2lip)            musetalk | wav2lip | ultralight
#   TTS_ENGINE        (default: vieneu)             vieneu | f5tts
#   TRANSPORT         (default: webrtc)             webrtc | rtmp | virtualcam | rtcpush
#   RTMP_PUSH_URL     (chỉ cho rtmp)
#   BRAIN_ENABLED     (default: true)
#   OPENAI_API_KEY    (cho LLM brain)
#   LLM_URL / LLM_MODEL
#   LISTEN_PORT       (default: 8010)
#
# VieNeu TTS:
#   VIENEU_MODE       (default: standard)           standard | turbo | remote
#   VIENEU_VOICE_ID   (default: empty)              vd: 'truc_ly_north_female'
#   VIENEU_REF_AUDIO  (override: voice cloning)
#   VIENEU_REF_TEXT
#
# F5-TTS (chỉ khi TTS_ENGINE=f5tts):
#   F5_REF_AUDIO / F5_REF_TEXT

set -euo pipefail
cd "$(dirname "$0")/../.."

AVATAR_ID="${AVATAR_ID:-demo}"
AVATAR_MODEL="${AVATAR_MODEL:-wav2lip}"
TTS_ENGINE="${TTS_ENGINE:-vieneu}"
TRANSPORT="${TRANSPORT:-webrtc}"
RTMP_PUSH_URL="${RTMP_PUSH_URL:-rtmp://localhost/live/test}"
BRAIN_ENABLED="${BRAIN_ENABLED:-true}"
PRODUCTS_PATH="${PRODUCTS_PATH:-data/products.json}"
PERSONA="${PERSONA:-linh_vi}"
LLM_URL="${LLM_URL:-https://api.openai.com/v1}"
LLM_MODEL="${LLM_MODEL:-gpt-4o-mini}"
LLM_API_KEY="${OPENAI_API_KEY:-${LLM_API_KEY:-none}}"
LISTEN_PORT="${LISTEN_PORT:-8010}"
STUDIO_ENABLED="${STUDIO_ENABLED:-true}"

# VieNeu — default gpu (auto-spawn lmdeploy, max GPU perf)
VIENEU_MODE="${VIENEU_MODE:-gpu}"
VIENEU_EMOTION="${VIENEU_EMOTION:-natural}"
VIENEU_VOICE_ID="${VIENEU_VOICE_ID:-}"
VIENEU_PORT="${VIENEU_PORT:-23333}"
VIENEU_TP="${VIENEU_TP:-1}"
VIENEU_REF_AUDIO_DEFAULT="data/avatars/${AVATAR_ID}/voice/ref.wav"
VIENEU_REF_TEXT_FILE="data/avatars/${AVATAR_ID}/voice/ref.txt"
VIENEU_REF_AUDIO="${VIENEU_REF_AUDIO:-${VIENEU_REF_AUDIO_DEFAULT}}"
VIENEU_REF_TEXT="${VIENEU_REF_TEXT:-}"
[[ -z "${VIENEU_REF_TEXT}" && -f "${VIENEU_REF_TEXT_FILE}" ]] && VIENEU_REF_TEXT="$(cat "${VIENEU_REF_TEXT_FILE}")"

# F5
F5_REF_AUDIO="${F5_REF_AUDIO:-${VIENEU_REF_AUDIO_DEFAULT}}"
F5_REF_TEXT="${F5_REF_TEXT:-${VIENEU_REF_TEXT}}"

# Sanity
if [[ ! -d "data/avatars/${AVATAR_ID}" ]]; then
  echo "[WARN] data/avatars/${AVATAR_ID} không tồn tại. Vào http://<host>:${LISTEN_PORT}/ tab Video để upload + preprocess avatar."
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

  if [[ "${VIENEU_MODE}" == "gpu" ]]; then
    echo "[Start] VieNeu GPU mode — plugin sẽ auto-spawn lmdeploy ở port ${VIENEU_PORT} (mất ~30-60s lần đầu để load model)"
  fi
elif [[ "${TTS_ENGINE}" == "f5tts" ]]; then
  ARGS+=(--f5_ref_audio "${F5_REF_AUDIO}" --f5_ref_text "${F5_REF_TEXT}")
fi

if [[ "${TRANSPORT}" == "rtmp" ]]; then
  ARGS+=(--push_url "${RTMP_PUSH_URL}")
fi

echo "[Start] python app.py ${ARGS[*]}"
exec python -u app.py "${ARGS[@]}" 2>&1 | tee server.log
