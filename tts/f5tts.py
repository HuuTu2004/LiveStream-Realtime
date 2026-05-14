###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
###############################################################################
#
#  F5-TTS Vietnamese plugin — in-process inference với voice cloning
#  Default model: HuggingFace `hynt/F5-TTS-Vietnamese-ViVoice` (community fine-tune)
#

import os
import re
import time
import threading
from typing import Iterator, Optional

import numpy as np
import resampy

from utils.logger import logger
from .base_tts import BaseTTS, State
from registry import register


_SENT_SPLIT = re.compile(r'(?<=[\.\!\?\n。！？])\s+|(?<=[,;:])\s+(?=[A-ZĐÁÀẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÉÈẺẼẸÊỀẾỂỄỆÍÌỈĨỊÓÒỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÚÙỦŨỤƯỪỨỬỮỰÝỲỶỸỴ])')


def _split_sentences(text: str, max_chars: int = 180) -> list[str]:
    """Chia text thành các câu nhỏ để F5-TTS infer từng câu (giảm latency first-chunk).

    - Tách theo punctuation Việt + space.
    - Mỗi câu < max_chars để tránh F5-TTS hỗn loạn ở câu dài.
    - Câu quá dài sẽ tự split theo dấu phẩy.
    """
    text = (text or "").strip()
    if not text:
        return []

    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    out: list[str] = []
    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
            continue
        # câu quá dài: tách thêm theo dấu phẩy
        for sub in re.split(r'(?<=[,，])\s+', p):
            sub = sub.strip()
            if sub:
                out.append(sub if len(sub) <= max_chars else sub[:max_chars])
    return out


class _F5Singleton:
    """Singleton load model F5-TTS — share giữa tất cả session, không reload."""

    _instance: Optional["_F5Singleton"] = None
    _lock = threading.Lock()

    def __init__(self, model_repo: str, vocoder: str, device: str):
        from f5_tts.api import F5TTS

        self.model_repo = model_repo
        self.vocoder = vocoder
        self.device = device

        kwargs = {"device": device}
        # F5TTS chấp nhận model name hoặc full ckpt path; truyền HF repo qua model arg
        if model_repo:
            kwargs["model"] = model_repo
        if vocoder:
            kwargs["vocoder_name"] = vocoder
        self.tts = F5TTS(**kwargs)
        logger.info(f"[F5TTS] Loaded model={model_repo} vocoder={vocoder} device={device}")

    @classmethod
    def get(cls, model_repo: str, vocoder: str, device: str) -> "_F5Singleton":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(model_repo, vocoder, device)
            return cls._instance


@register("tts", "f5tts")
class F5TTSVietnamese(BaseTTS):
    """F5-TTS plugin tối ưu cho realtime livestream tiếng Việt.

    Streaming chiến thuật: F5-TTS native sinh full audio mỗi câu. Plugin split
    text theo punctuation Việt trước, infer từng câu, push chunk 320 samples
    (20ms @16kHz) ngay khi câu đầu xong → perceived first-chunk latency ~1-1.5s.

    Voice cloning: cần 1 ref WAV 5-15s + ref text. Set qua:
      --f5_ref_audio data/avatars/X/voice/ref.wav
      --f5_ref_text  "Xin chào tôi là Linh"
    Hoặc override per-request qua textevent['tts']['ref_file'] / ['ref_text'].
    """

    SR_NATIVE = 24000  # F5-TTS native output
    SR_TARGET = 16000  # LiveTalking pipeline

    def __init__(self, opt, parent):
        super().__init__(opt, parent)
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model_repo = getattr(opt, "f5_model_path", "") or "hf://hynt/F5-TTS-Vietnamese-ViVoice"
        vocoder = getattr(opt, "f5_vocoder", "") or "vocos"

        self._singleton = _F5Singleton.get(model_repo, vocoder, device)

        # Reference audio + text
        self._default_ref_audio = getattr(opt, "f5_ref_audio", "") or getattr(opt, "REF_FILE", "")
        self._default_ref_text = getattr(opt, "f5_ref_text", "") or (getattr(opt, "REF_TEXT", "") or "")

        if not self._default_ref_audio or not os.path.exists(self._default_ref_audio):
            logger.warning(
                f"[F5TTS] Reference audio not found: '{self._default_ref_audio}'. "
                "Voice cloning sẽ fallback random voice. Set --f5_ref_audio để dùng cloned voice."
            )

        # Inference lock — F5-TTS không thread-safe khi share trong process
        self._infer_lock = threading.Lock()

    # ------------------------------------------------------------------
    def txt_to_audio(self, msg: tuple[str, dict]):
        text, textevent = msg
        if not text or not text.strip():
            return

        ref_audio = textevent.get("tts", {}).get("ref_file", self._default_ref_audio)
        ref_text = textevent.get("tts", {}).get("ref_text", self._default_ref_text)

        if not ref_audio or not os.path.exists(ref_audio):
            logger.error(f"[F5TTS] Missing ref_audio: {ref_audio}")
            return

        sentences = _split_sentences(text)
        if not sentences:
            return

        start = time.perf_counter()
        first_sentence = True

        for sent_idx, sentence in enumerate(sentences):
            if self.state != State.RUNNING:
                break

            try:
                pcm_24k = self._infer_one(ref_audio, ref_text, sentence)
            except Exception as e:
                logger.exception(f"[F5TTS] Inference error on sentence: {sentence[:40]!r}: {e}")
                continue

            if pcm_24k is None or len(pcm_24k) == 0:
                continue

            if first_sentence:
                logger.info(f"[F5TTS] Time to first chunk: {time.perf_counter()-start:.2f}s")

            self._emit_chunks(
                pcm_24k=pcm_24k,
                text=text,
                textevent=textevent,
                is_first_sentence=first_sentence,
                is_last_sentence=(sent_idx == len(sentences) - 1),
            )
            first_sentence = False

        logger.info(f"[F5TTS] Total synthesis: {time.perf_counter()-start:.2f}s for {len(sentences)} sentence(s)")

    # ------------------------------------------------------------------
    def _infer_one(self, ref_audio: str, ref_text: str, gen_text: str) -> np.ndarray:
        """Gọi F5-TTS infer cho 1 câu. Trả về float32 mono 24kHz."""
        with self._infer_lock:
            wav, sr, _ = self._singleton.tts.infer(
                ref_file=ref_audio,
                ref_text=ref_text or "",
                gen_text=gen_text,
                show_info=lambda *_: None,
                progress=None,
                remove_silence=False,
                seed=-1,
            )
        if isinstance(wav, np.ndarray):
            audio = wav.astype(np.float32, copy=False)
        else:
            import torch
            if hasattr(wav, "cpu"):
                wav = wav.cpu().numpy()
            audio = np.asarray(wav, dtype=np.float32)

        if audio.ndim > 1:
            audio = audio[:, 0] if audio.shape[1] < audio.shape[0] else audio[0, :]

        # F5-TTS thường trả về 24kHz, nhưng vẫn check
        if sr != self.SR_NATIVE:
            audio = resampy.resample(audio, sr_orig=sr, sr_new=self.SR_NATIVE)
        return audio

    # ------------------------------------------------------------------
    def _emit_chunks(
        self,
        pcm_24k: np.ndarray,
        text: str,
        textevent: dict,
        is_first_sentence: bool,
        is_last_sentence: bool,
    ) -> None:
        """Resample 24k→16k, chia chunk 320 samples, push vào avatar audio pipeline."""
        stream = resampy.resample(pcm_24k, sr_orig=self.SR_NATIVE, sr_new=self.SR_TARGET)
        streamlen = stream.shape[0]
        idx = 0
        first_chunk = True

        while streamlen >= self.chunk and self.state == State.RUNNING:
            eventpoint = {}
            # 'start' chỉ ở chunk đầu của câu ĐẦU TIÊN
            if first_chunk and is_first_sentence:
                eventpoint = {"status": "start", "text": text}
            elif first_chunk:
                # chunk đầu của câu sau (không phải câu đầu): gắn gesture event nếu có
                pass

            # gesture/tts piggyback — chỉ ở chunk đầu mỗi câu
            if first_chunk and textevent:
                # truyền nguyên textevent (chứa 'gesture', 'tts', etc.) để renderer pickup
                eventpoint.update(textevent)

            self.parent.put_audio_frame(stream[idx : idx + self.chunk], eventpoint)
            streamlen -= self.chunk
            idx += self.chunk
            first_chunk = False

        # Chunk cuối của câu cuối: pad zeros + 'end' marker
        if is_last_sentence and self.state == State.RUNNING:
            eventpoint = {"status": "end", "text": text}
            eventpoint.update(textevent or {})
            self.parent.put_audio_frame(
                np.zeros(self.chunk, dtype=np.float32), eventpoint
            )
