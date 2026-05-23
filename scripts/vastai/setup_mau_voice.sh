#!/usr/bin/env bash
# Extract reference voice from mau.mp4 → data/avatars/mau/voice/{ref.wav,ref.txt}
# Run AFTER setup.sh (needs venv_talking) but BEFORE start.sh (so encode_voice
# can run if lmdeploy is up first).
set -euo pipefail
cd "$(dirname "$0")/../.."

VIDEO="${1:-data/uploads/mau.mp4}"
AVATAR_ID="${2:-mau}"
VOICE_DIR="data/avatars/${AVATAR_ID}/voice"
REF_WAV="${VOICE_DIR}/ref.wav"
REF_TXT="${VOICE_DIR}/ref.txt"

mkdir -p "${VOICE_DIR}"

if [[ ! -f "${VIDEO}" ]]; then
  echo "[ERR] Video không tồn tại: ${VIDEO}" >&2
  exit 1
fi

# ─── 1. Extract clean mono 24kHz audio (VieNeu native sample rate) ──────
# Limit 8s — VieNeu khuyến nghị ref 3-10s.
echo "[voice] Extract ${VIDEO} → ${REF_WAV} (24kHz mono, 8s max)"
ffmpeg -y -loglevel error -i "${VIDEO}" -ac 1 -ar 24000 -t 8 \
  -af "highpass=f=80,lowpass=f=11000,loudnorm=I=-16:TP=-1.5:LRA=11" \
  "${REF_WAV}"

# ─── 2. Transcribe via whisper (cài kèm musetalk models, openai-whisper lib) ─
# Nếu chưa cài whisper trong venv_talking, install nhanh.
PY=./venv_talking/bin/python
if ! ${PY} -c "import whisper" 2>/dev/null; then
  echo "[voice] cài openai-whisper (transcribe ref audio)..."
  ${PY} -m pip install --no-cache-dir -q openai-whisper >/dev/null
fi

# Sử dụng tiny.pt local (đã download cho musetalk audio2feature).
WHISPER_LOCAL=./models/whisper/tiny.pt
if [[ -f "${WHISPER_LOCAL}" ]]; then
  WHISPER_MODEL="${WHISPER_LOCAL}"
else
  WHISPER_MODEL="tiny"
fi

echo "[voice] Transcribe (whisper, language=vi)..."
${PY} - <<PY
import whisper
m = whisper.load_model("${WHISPER_MODEL}")
r = m.transcribe("${REF_WAV}", language="vi", fp16=False)
text = (r.get("text") or "").strip()
if not text:
    text = "Xin chào, mình là Linh, hôm nay mình sẽ giới thiệu sản phẩm với mọi người."
with open("${REF_TXT}", "w", encoding="utf-8") as f:
    f.write(text)
print(f"[voice] transcript: {text!r}")
PY

echo "[voice] ref.wav + ref.txt sẵn sàng:"
ls -la "${VOICE_DIR}"
echo
echo "Sau khi lmdeploy api_server :23333 đã up, encode voice cho ONNX codec:"
echo "  ./venv_vieneu/bin/python scripts/vastai/encode_voice.py \\"
echo "    ${REF_WAV} ${REF_TXT} ${VOICE_DIR}/voice.pkl"
