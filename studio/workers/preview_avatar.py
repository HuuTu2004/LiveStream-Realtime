"""Render 5s preview clip cho avatar mới train xong.

CLI:
  python -m studio.workers.preview_avatar --avatar_id NAME --text "Xin chào" --output preview.mp4

Worker này dùng F5-TTS sinh audio + avatar inference offline (không qua webrtc).
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def emit(progress: float, msg: str = "", **kw):
    payload = {"progress": float(max(0.0, min(1.0, progress))), "msg": msg}
    payload.update(kw)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar_id", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--model", default="wav2lip")
    ap.add_argument("--ref_audio", default="")
    ap.add_argument("--ref_text", default="")
    args = ap.parse_args()

    emit(0.05, "loading F5-TTS")
    try:
        from f5_tts.api import F5TTS
        import soundfile as sf
        import numpy as np
    except Exception as e:
        emit(0.0, f"F5-TTS not installed: {e}")
        sys.exit(1)

    voice_dir = os.path.join("data", "avatars", args.avatar_id, "voice")
    ref_audio = args.ref_audio or os.path.join(voice_dir, "ref.wav")
    ref_text_path = os.path.join(voice_dir, "ref.txt")
    ref_text = args.ref_text
    if not ref_text and os.path.exists(ref_text_path):
        with open(ref_text_path, encoding="utf-8") as f:
            ref_text = f.read().strip()

    if not os.path.exists(ref_audio):
        emit(0.0, f"ref audio not found: {ref_audio}")
        sys.exit(2)

    emit(0.2, "synthesizing audio")
    tts = F5TTS(model="hf://hynt/F5-TTS-Vietnamese-ViVoice")
    wav, sr, _ = tts.infer(ref_file=ref_audio, ref_text=ref_text, gen_text=args.text, show_info=lambda *_: None)

    audio_path = os.path.splitext(args.output)[0] + ".wav"
    if hasattr(wav, "cpu"):
        wav = wav.cpu().numpy()
    sf.write(audio_path, np.asarray(wav), sr, subtype="PCM_16")
    emit(0.55, "audio written", audio=audio_path)

    # Render video qua subprocess wav2lip/musetalk inference CLI có sẵn (nếu user setup)
    # — đây là điểm extend, hiện tại chỉ chạy ffmpeg ghép audio với 1 frame loop.
    emit(0.6, "rendering preview video")
    frames_dir = os.path.join("data", "avatars", args.avatar_id, "full_imgs")
    if not os.path.isdir(frames_dir):
        emit(0.0, "full_imgs not found — chạy preprocess trước")
        sys.exit(3)

    import subprocess
    # Loop frames qua duration audio
    cmd = [
        "ffmpeg", "-y",
        "-framerate", "25",
        "-pattern_type", "glob",
        "-i", os.path.join(frames_dir, "*.png"),
        "-i", audio_path,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        args.output,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        emit(0.0, f"ffmpeg fail: {res.stderr[:300]}")
        sys.exit(4)

    emit(1.0, "done", output=args.output)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        emit(0.0, f"FAILED: {e}")
        raise
