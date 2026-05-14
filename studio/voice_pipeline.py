"""Voice pipeline: upload reference WAV + transcript cho VieNeu-TTS voice cloning.

Validate file:
- Format: WAV (PCM 16/24-bit), mono hoặc stereo (sẽ down-mix).
- Duration: 3-30s (VieNeu optimal cho zero-shot, 3-5s là đủ).
- Sample rate: bất kỳ (sẽ resample về 24kHz khi infer).

Lưu vào: data/avatars/{avatar_id}/voice/ref.wav + ref.txt
"""

from __future__ import annotations

import logging
import os
import shutil
from io import BytesIO

import numpy as np
import resampy
import soundfile as sf

log = logging.getLogger(__name__)

REF_SR = 24000  # VieNeu-TTS native
MIN_SECS = 3.0
MAX_SECS = 30.0


def validate_and_save(
    avatar_id: str,
    wav_bytes: bytes,
    transcript: str,
) -> dict:
    """Validate WAV bytes + lưu vào avatar dir. Trả về metadata dict."""
    voice_dir = os.path.join("data", "avatars", avatar_id, "voice")
    os.makedirs(voice_dir, exist_ok=True)

    # Load & validate
    audio, sr = sf.read(BytesIO(wav_bytes), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    duration = len(audio) / sr if sr else 0
    if duration < MIN_SECS:
        raise ValueError(f"Audio quá ngắn ({duration:.1f}s). Cần ≥ {MIN_SECS}s.")
    if duration > MAX_SECS:
        log.warning("Audio dài %.1fs, cắt còn %ds đầu", duration, int(MAX_SECS))
        audio = audio[: int(MAX_SECS * sr)]
        duration = MAX_SECS

    # Resample to 24kHz nếu cần
    if sr != REF_SR:
        audio = resampy.resample(audio, sr_orig=sr, sr_new=REF_SR)

    # Normalize (peak -1dB)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        target_peak = 0.89  # -1 dBFS
        if peak > target_peak:
            audio = audio * (target_peak / peak)

    out_wav = os.path.join(voice_dir, "ref.wav")
    sf.write(out_wav, audio, REF_SR, subtype="PCM_16")

    out_txt = os.path.join(voice_dir, "ref.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write((transcript or "").strip())

    return {
        "avatar_id": avatar_id,
        "ref_audio": out_wav,
        "ref_text": out_txt,
        "duration_secs": float(duration),
        "sample_rate": REF_SR,
        "transcript_len": len(transcript or ""),
    }


def delete_voice(avatar_id: str) -> bool:
    voice_dir = os.path.join("data", "avatars", avatar_id, "voice")
    if not os.path.isdir(voice_dir):
        return False
    try:
        shutil.rmtree(voice_dir)
        return True
    except Exception:
        log.exception("delete_voice")
        return False
