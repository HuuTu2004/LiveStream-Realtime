"""Voice library — quản lý nhiều giọng clone độc lập với avatar.

Storage: data/voices/{voice_id}/
  - ref.wav      (24 kHz mono normalized, 3-30s)
  - ref.txt      (transcript khớp với ref.wav)
  - voice.pkl    (pre-encoded {ref_codes, ref_text, mode} — chuẩn vieneu_server)
  - meta.json    ({id, name, created_at, duration_secs, transcript_chars, encoded})

Theo chuẩn VieNeu TTS project:
  - vieneu_http.py auto-detect voice.pkl cạnh ref.wav → gửi voice_pkl path cho
    vieneu_server.py → server load + pass {ref_codes, ref_text} vào tts.infer_stream.
  - encode_voice.py (scripts/vastai/) là canonical generator; ta replicate inline
    bằng cách gọi _VieNeuSingleton.encode_reference (cùng kết quả).
  - In-process vieneu.py cũng đọc voice.pkl trước khi fallback encode_reference.

Voice active track qua opt.vieneu_ref_audio + opt.vieneu_ref_text. Brain
auto-restart khi field đổi (server/config_routes.py brain_fields).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from io import BytesIO
from typing import Optional

import numpy as np
import resampy
import soundfile as sf

log = logging.getLogger(__name__)

VOICES_DIR = os.path.join("data", "voices")
REF_SR = 24000
MIN_SECS = 3.0
MAX_SECS = 30.0

_ID_RE = re.compile(r"^[a-z0-9_\-]{2,40}$")


def _slugify(name: str) -> str:
    """Convert tên tiếng Việt → id slug. Bỏ dấu + chuẩn hóa."""
    import unicodedata
    s = unicodedata.normalize("NFD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:40] or ""


def _voice_dir(voice_id: str) -> str:
    return os.path.join(VOICES_DIR, voice_id)


def _ensure_dir() -> None:
    os.makedirs(VOICES_DIR, exist_ok=True)


def _load_meta(voice_id: str) -> Optional[dict]:
    path = os.path.join(_voice_dir(voice_id), "meta.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("[VoiceLib] read meta failed: %s", path)
        return None


def _save_meta(voice_id: str, meta: dict) -> None:
    path = os.path.join(_voice_dir(voice_id), "meta.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def list_voices() -> list[dict]:
    """Liệt kê voice library — đọc meta.json mỗi voice dir."""
    _ensure_dir()
    out: list[dict] = []
    for entry in sorted(os.listdir(VOICES_DIR)):
        d = _voice_dir(entry)
        if not os.path.isdir(d):
            continue
        meta = _load_meta(entry)
        if meta is None:
            # legacy / không có meta — fallback
            ref_wav = os.path.join(d, "ref.wav")
            if not os.path.exists(ref_wav):
                continue
            meta = {"id": entry, "name": entry, "created_at": 0}
        out.append(meta)
    # Newest first
    out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return out


def get_voice(voice_id: str) -> Optional[dict]:
    if not _ID_RE.match(voice_id):
        return None
    return _load_meta(voice_id)


def get_ref_paths(voice_id: str) -> tuple[str, str]:
    d = _voice_dir(voice_id)
    return os.path.join(d, "ref.wav"), os.path.join(d, "ref.txt")


def save_voice(
    name: str,
    wav_bytes: bytes,
    transcript: str,
    voice_id: Optional[str] = None,
) -> dict:
    """Validate WAV + lưu vào library. Trả meta dict.

    Voice id ưu tiên theo user, fallback slug từ name, cuối cùng UUID.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Cần nhập tên voice")
    transcript = (transcript or "").strip()
    if not transcript:
        raise ValueError("Cần nhập transcript khớp với audio")

    # Resolve voice_id
    if voice_id:
        voice_id = voice_id.strip().lower()
    else:
        voice_id = _slugify(name) or ""
    if not _ID_RE.match(voice_id):
        voice_id = "v_" + uuid.uuid4().hex[:8]

    # Unique check — append suffix nếu trùng
    if os.path.isdir(_voice_dir(voice_id)):
        base = voice_id
        for i in range(2, 100):
            voice_id = f"{base}_{i}"
            if not os.path.isdir(_voice_dir(voice_id)):
                break

    d = _voice_dir(voice_id)
    os.makedirs(d, exist_ok=True)

    # Validate + normalize audio
    try:
        audio, sr = sf.read(BytesIO(wav_bytes), dtype="float32")
    except Exception as e:
        raise ValueError(f"File audio không đọc được (cần WAV PCM): {e}")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    duration = len(audio) / sr if sr else 0.0
    if duration < MIN_SECS:
        raise ValueError(f"Audio quá ngắn ({duration:.1f}s). Cần ≥ {MIN_SECS}s.")
    if duration > MAX_SECS:
        log.info("[VoiceLib] cắt audio %.1fs → %ds", duration, int(MAX_SECS))
        audio = audio[: int(MAX_SECS * sr)]
        duration = MAX_SECS

    if sr != REF_SR:
        audio = resampy.resample(audio, sr_orig=sr, sr_new=REF_SR)

    # Peak normalize -1 dBFS
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        target = 0.89
        if peak > target:
            audio = audio * (target / peak)

    sf.write(os.path.join(d, "ref.wav"), audio, REF_SR, subtype="PCM_16")
    with open(os.path.join(d, "ref.txt"), "w", encoding="utf-8") as f:
        f.write(transcript)

    ref_wav_path = os.path.join(d, "ref.wav")

    # Pre-encode + save voice.pkl theo chuẩn vieneu_server (cross-process compat).
    encoded_info = _try_encode_voice_pkl(ref_wav_path, transcript, d)

    meta = {
        "id": voice_id,
        "name": name,
        "created_at": int(time.time()),
        "duration_secs": round(float(duration), 2),
        "transcript_chars": len(transcript),
        "sample_rate": REF_SR,
        "encoded": encoded_info,  # {mode, codec_repo, size_bytes} hoặc None
    }
    _save_meta(voice_id, meta)

    log.info("[VoiceLib] Saved voice %s (%s, %.1fs, encoded=%s)",
             voice_id, name, duration, bool(encoded_info))
    return meta


def _try_encode_voice_pkl(ref_audio_path: str, transcript: str,
                          voice_dir: str) -> Optional[dict]:
    """Pre-encode ref → save voice.pkl theo format vieneu_server.py.

    Format: {"ref_codes": <np.ndarray|torch.Tensor>, "ref_text": str,
             "mode": str, "codec_repo": str, "version": 1}

    Best-effort: nếu vieneu singleton chưa load (app start trước khi user tới
    voice page) → skip, fallback runtime encoding khi speak. Đây cũng warm
    cache singleton cho speak đầu nhanh.

    Theo standard vieneu/base.py encode_reference():
      - PyTorch codec (standard/remote/gpu mode): torch.Tensor int tokens
      - ONNX codec (turbo mode): np.ndarray 128-dim float embedding

    Format khác nhau giữa modes → ta tag `mode` để loader validate.
    """
    import pickle
    try:
        from tts.vieneu import _VieNeuSingleton
        inst = _VieNeuSingleton._instance
        if inst is None:
            log.debug("[VoiceLib] singleton chưa init — skip pre-encode")
            return None

        # encode_reference cache vào _voice_cache nên speak sau cũng nhanh
        codes = inst.encode_reference(ref_audio_path)

        voice_pkl = os.path.join(voice_dir, "voice.pkl")
        codec_repo = getattr(inst.tts, "_codec_repo", "") or ""
        payload = {
            "ref_codes": codes,
            "ref_text": transcript,
            "mode": inst.mode,
            "codec_repo": codec_repo,
            "version": 1,
        }
        with open(voice_pkl, "wb") as f:
            pickle.dump(payload, f)
        size = os.path.getsize(voice_pkl)
        log.info("[VoiceLib] voice.pkl saved (%s, mode=%s, %d bytes)",
                 voice_pkl, inst.mode, size)
        return {"mode": inst.mode, "codec_repo": codec_repo, "size_bytes": size}
    except Exception:
        log.exception("[VoiceLib] pre-encode failed (fallback runtime encode)")
        return None


def delete_voice(voice_id: str) -> bool:
    if not _ID_RE.match(voice_id):
        return False
    d = _voice_dir(voice_id)
    if not os.path.isdir(d):
        return False
    try:
        shutil.rmtree(d)
        log.info("[VoiceLib] Deleted voice %s", voice_id)
        return True
    except Exception:
        log.exception("[VoiceLib] delete failed: %s", voice_id)
        return False


def activate_voice(opt, voice_id: str) -> dict:
    """Set opt.vieneu_ref_audio + ref_text → voice_id. Trả meta.

    Brain auto-reload khi field này đổi (config_routes brain_fields).
    """
    meta = _load_meta(voice_id)
    if meta is None:
        raise KeyError(f"voice_id '{voice_id}' không tồn tại")
    ref_wav, ref_txt_path = get_ref_paths(voice_id)
    if not os.path.exists(ref_wav):
        raise FileNotFoundError(f"ref.wav missing for {voice_id}")
    transcript = ""
    if os.path.exists(ref_txt_path):
        with open(ref_txt_path, "r", encoding="utf-8") as f:
            transcript = f.read().strip()
    opt.vieneu_ref_audio = os.path.abspath(ref_wav)
    opt.vieneu_ref_text = transcript
    # Clear preset voice_id để ref mới có tác dụng
    opt.vieneu_voice_id = ""
    log.info("[VoiceLib] Activated voice %s → %s", voice_id, ref_wav)
    return meta


def current_active(opt) -> Optional[str]:
    """Trả về voice_id đang active (suy ra từ opt.vieneu_ref_audio path)."""
    ref = (getattr(opt, "vieneu_ref_audio", "") or "").strip()
    if not ref:
        return None
    # Normalize path so windows / linux đều khớp
    ref_norm = os.path.normpath(os.path.abspath(ref))
    voices_norm = os.path.normpath(os.path.abspath(VOICES_DIR))
    if not ref_norm.startswith(voices_norm):
        return None
    rel = os.path.relpath(ref_norm, voices_norm)
    voice_id = rel.split(os.sep, 1)[0]
    if _ID_RE.match(voice_id) and os.path.isdir(_voice_dir(voice_id)):
        return voice_id
    return None


def synth_preview_adhoc(wav_bytes: bytes, ref_text: str, sample_text: str) -> bytes:
    """Synthesize TTS preview với audio CHƯA SAVE vào library.

    Dùng cho 'Nghe thử clone' UI — user record xong muốn test quality trước
    khi commit save. Audio được ghi tạm vào temp file → encode + synth → xóa.

    Pipeline giống save_voice nhưng không persist gì cả.
    """
    if not wav_bytes:
        raise ValueError("Cần audio bytes")
    ref_text = (ref_text or "").strip()
    if not ref_text:
        raise ValueError("Cần transcript khớp với audio")

    sample_text = (sample_text or "").strip()
    if not sample_text:
        sample_text = "Xin chào, đây là giọng nói được nhân bản từ thư viện."
    if len(sample_text) > 300:
        sample_text = sample_text[:300]

    # Validate + normalize audio (cùng pipeline save_voice — nhưng không lưu)
    try:
        audio, sr = sf.read(BytesIO(wav_bytes), dtype="float32")
    except Exception as e:
        raise ValueError(f"Audio không đọc được: {e}")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    duration = len(audio) / sr if sr else 0.0
    if duration < MIN_SECS:
        raise ValueError(f"Audio quá ngắn ({duration:.1f}s). Cần ≥ {MIN_SECS}s.")
    if duration > MAX_SECS:
        audio = audio[: int(MAX_SECS * sr)]
    if sr != REF_SR:
        audio = resampy.resample(audio, sr_orig=sr, sr_new=REF_SR)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.89:
        audio = audio * (0.89 / peak)

    # Ghi temp file để TTS lib có path hợp lệ (lib dùng librosa.load(path))
    import tempfile
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    try:
        sf.write(tmp_wav.name, audio, REF_SR, subtype="PCM_16")

        from server.session_manager import session_manager
        avatar = session_manager.get_session("0")
        if avatar is None or not hasattr(avatar, "tts"):
            raise RuntimeError("TTS chưa init — session 0 chưa sẵn sàng")
        tts = avatar.tts
        if not hasattr(tts, "_stream_one"):
            raise RuntimeError(
                f"TTS plugin {type(tts).__name__} không hỗ trợ preview"
            )

        chunks: list[np.ndarray] = []
        for pcm in tts._stream_one(sample_text, "", tmp_wav.name, ref_text):
            if pcm is None:
                continue
            arr = np.asarray(pcm, dtype=np.float32)
            if arr.size:
                chunks.append(arr)
        if not chunks:
            raise RuntimeError("TTS không trả audio")

        full = np.concatenate(chunks)
        buf = BytesIO()
        sr_out = getattr(tts, "SR_NATIVE", REF_SR)
        sf.write(buf, full, sr_out, format="WAV", subtype="PCM_16")
        return buf.getvalue()
    finally:
        try:
            os.unlink(tmp_wav.name)
        except OSError:
            pass


def synth_preview(opt, voice_id: str, text: str) -> bytes:
    """Synthesize 1 đoạn ngắn với voice_id → WAV bytes 24kHz PCM_16.

    Dùng TTS singleton hiện hành. Lock-aware: chạy trong thread executor để
    không block event loop. Có thể block tới vài giây trong khi TTS singleton
    đang xử lý câu khác (do _infer_lock).
    """
    meta = _load_meta(voice_id)
    if meta is None:
        raise KeyError(f"voice_id '{voice_id}' không tồn tại")
    ref_wav, ref_txt_path = get_ref_paths(voice_id)
    if not os.path.exists(ref_wav):
        raise FileNotFoundError(f"ref.wav missing: {voice_id}")
    ref_text = ""
    if os.path.exists(ref_txt_path):
        with open(ref_txt_path, "r", encoding="utf-8") as f:
            ref_text = f.read().strip()

    text = (text or "").strip()
    if not text:
        text = "Xin chào, đây là giọng nói được nhân bản từ thư viện."
    if len(text) > 300:
        text = text[:300]

    # Lấy TTS plugin từ session 0 (đã load model)
    from server.session_manager import session_manager
    avatar = session_manager.get_session("0")
    if avatar is None or not hasattr(avatar, "tts"):
        raise RuntimeError("TTS chưa init — session 0 chưa sẵn sàng")
    tts = avatar.tts

    if not hasattr(tts, "_stream_one"):
        raise RuntimeError(
            f"TTS plugin {type(tts).__name__} chưa hỗ trợ preview "
            "(không có _stream_one). Dùng vieneu hoặc vieneu_http."
        )

    chunks: list[np.ndarray] = []
    for pcm_24k in tts._stream_one(text, "", os.path.abspath(ref_wav), ref_text):
        if pcm_24k is None:
            continue
        arr = np.asarray(pcm_24k, dtype=np.float32)
        if arr.size:
            chunks.append(arr)

    if not chunks:
        raise RuntimeError("TTS không trả audio (model có thể chưa load hoặc lỗi)")

    full = np.concatenate(chunks)
    buf = BytesIO()
    sr = getattr(tts, "SR_NATIVE", REF_SR)
    sf.write(buf, full, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()
