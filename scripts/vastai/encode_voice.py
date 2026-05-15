###############################################################################
#  encode_voice.py — encode ref audio thành voice_data (chạy 1 lần)
#
#  Lý do: ONNX codec (production) chỉ DECODE được, không ENCODE ref audio.
#  Phải dùng PyTorch neucodec để encode ref ONE-TIME, save voice_data file,
#  vieneu_server load voice_data + pass qua `voice=` (bypass ref_audio).
#
#  Usage:
#    /workspace/LiveTalking/venv_vieneu/bin/python scripts/vastai/encode_voice.py \
#      <ref_audio.wav> <ref_text_file_or_string> <output_voice.pkl>
#
#  Sau đó vieneu_server tự load voice.pkl từ data/avatars/<id>/voice/voice.pkl.
###############################################################################
import os, sys, pickle, time

if len(sys.argv) < 4:
    print(f"Usage: {sys.argv[0]} ref_audio.wav ref_text|ref_text.txt out_voice.pkl")
    sys.exit(1)

ref_audio = sys.argv[1]
ref_text_arg = sys.argv[2]
output = sys.argv[3]

# Read ref_text (file path or string)
if os.path.exists(ref_text_arg):
    with open(ref_text_arg, encoding='utf-8') as f:
        ref_text = f.read().strip()
else:
    ref_text = ref_text_arg

print(f"Loading vieneu với PyTorch codec để encode ref...")
print(f"  ref_audio: {ref_audio}")
print(f"  ref_text:  {ref_text[:60]!r}...")
t0 = time.perf_counter()

# Tải vieneu với PyTorch neucodec (mode remote vẫn được, miễn codec là PyTorch)
from vieneu import Vieneu
tts = Vieneu(
    mode='remote',
    api_base=os.environ.get('LMDEPLOY_URL', 'http://127.0.0.1:23333/v1'),
    model_name=os.environ.get('VIENEU_MODEL', 'pnnbao-ump/VieNeu-TTS-v2'),
    emotion='natural',
    codec_repo='neuphonic/distill-neucodec',  # PyTorch, có encode_code()
)
print(f"Vieneu loaded in {time.perf_counter()-t0:.1f}s")

t0 = time.perf_counter()
ref_codes = tts.encode_reference(ref_audio)
print(f"Encoded ref in {time.perf_counter()-t0:.1f}s, shape={getattr(ref_codes,'shape','?')}")

# Save voice_data: dict {ref_codes, ref_text}
voice_data = {"ref_codes": ref_codes, "ref_text": ref_text}
with open(output, "wb") as f:
    pickle.dump(voice_data, f)
print(f"Saved voice_data → {output}")
