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
#  VieNeu HTTP-client TTS plugin
#  ─────────────────────────────
#  Production multi-venv architecture:
#
#    venv_vieneu (torch 2.6+, vieneu[gpu], lmdeploy, neucodec PyTorch)
#       └── scripts/vastai/vieneu_server.py  →  127.0.0.1:23334 /infer_stream
#                                ↑
#                                │ HTTP stream (length-prefixed f32le PCM 24kHz)
#                                │
#    venv_talking (torch 2.4, wav2lip, aiohttp)
#       └── tts/vieneu_http.py  (THIS FILE — chỉ requests + numpy + scipy)
#
#  Lợi: vieneu xài torch 2.6+ + codec PyTorch full quality (zero rè/clicks),
#  LiveTalking giữ torch 2.4 cho wav2lip. Zero pip conflict giữa 2 process.
#
#  Wire format (mỗi chunk):
#    [4-byte BE uint32 length][length bytes f32le PCM mono 24kHz]
#    Terminator: length = 0.
#

import os
import re
import struct
import time
import threading
from typing import Iterator, Optional

import numpy as np
import requests
import soxr

from utils.logger import logger
from .base_tts import BaseTTS, State
from registry import register


_SENT_SPLIT = re.compile(
    r'(?<=[\.\!\?\n。！？])\s+|(?<=[,;:])\s+(?=[A-ZĐÁÀẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÉÈẺẼẸÊỀẾỂỄỆÍÌỈĨỊÓÒỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÚÙỦŨỤƯỪỨỬỮỰÝỲỶỸỴ])'
)


def _split_sentences(text: str, max_chars: int = 180) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    out: list[str] = []
    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
            continue
        for sub in re.split(r'(?<=[,，])\s+', p):
            sub = sub.strip()
            if sub:
                out.append(sub if len(sub) <= max_chars else sub[:max_chars])
    return out


@register("tts", "vieneu_http")
class VieNeuHttpTTS(BaseTTS):
    """HTTP-client TTS — connects to a remote vieneu_server.py in another venv.

    Eliminates pip dep conflict between wav2lip (torch 2.4) and vieneu codec
    (torch 2.6+). Server process owns the GPU model; this plugin just streams
    bytes.
    """

    SR_NATIVE = 24000
    SR_TARGET = 16000

    def __init__(self, opt, parent):
        super().__init__(opt, parent)

        api_base = (getattr(opt, "vieneu_api_base", "") or "").rstrip("/")
        if not api_base:
            host = getattr(opt, "vieneu_http_host", "127.0.0.1")
            port = int(getattr(opt, "vieneu_http_port", 23334) or 23334)
            api_base = f"http://{host}:{port}"
        self._endpoint = f"{api_base}/infer_stream"
        self._health = f"{api_base}/health"

        self._default_voice_id = (getattr(opt, "vieneu_voice_id", "") or "").strip()
        self._default_ref_audio = (getattr(opt, "vieneu_ref_audio", "") or "").strip()
        self._default_ref_text = (getattr(opt, "vieneu_ref_text", "") or "").strip()

        # Fallback legacy REF_FILE
        if not self._default_ref_audio and not self._default_voice_id:
            legacy_ref = getattr(opt, "REF_FILE", "")
            if legacy_ref and os.path.exists(legacy_ref):
                self._default_ref_audio = legacy_ref
                self._default_ref_text = getattr(opt, "REF_TEXT", "") or ""

        self._infer_lock = threading.Lock()
        self._session = requests.Session()
        self._timeout = float(getattr(opt, "vieneu_http_timeout", 120.0) or 120.0)
        # Realtime pacer state: deadline cho put_audio_frame kế tiếp.
        self._pace_deadline: Optional[float] = None
        self._wait_server_ready(timeout=180)
        logger.info(
            f"[VieNeuHTTP] endpoint={self._endpoint} voice={self._default_voice_id or '—'} "
            f"ref={self._default_ref_audio or '—'}"
        )

    # ------------------------------------------------------------------
    def _wait_server_ready(self, timeout: float = 180) -> None:
        deadline = time.time() + timeout
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            try:
                r = self._session.get(self._health, timeout=3)
                if r.status_code == 200:
                    logger.info(f"[VieNeuHTTP] server ready: {r.json()}")
                    return
            except Exception as e:
                last_err = e
            time.sleep(2)
        logger.warning(
            f"[VieNeuHTTP] server health check timeout after {timeout}s "
            f"(last err: {last_err}). Will retry per-request."
        )

    # ------------------------------------------------------------------
    def txt_to_audio(self, msg: tuple[str, dict]):
        # CRITICAL: wrap entire body trong try/except — bất kỳ unhandled exc
        # nào sẽ giết process_tts thread (vì BaseTTS.process_tts không catch).
        # Khi thread chết, mọi /human từ browser sau đó sẽ silent (queue grow,
        # không consumer). Đây là nguyên nhân TTS "đột nhiên im lặng".
        try:
            self._txt_to_audio_impl(msg)
        except Exception as e:
            logger.exception(f"[VieNeuHTTP] FATAL in txt_to_audio: {e}")

    def _txt_to_audio_impl(self, msg: tuple[str, dict]):
        text, textevent = msg
        if not text or not text.strip():
            return

        tts_override = textevent.get("tts", {}) if isinstance(textevent, dict) else {}
        voice_id = tts_override.get("voice_id", self._default_voice_id)
        ref_audio = tts_override.get("ref_file", self._default_ref_audio)
        ref_text = tts_override.get("ref_text", self._default_ref_text)

        # KHÔNG split sentence ở client side — vieneu lib bên server tự handle
        # chunking và streaming liên tục. Split ở client = 0.5-1s TTFB cho mỗi
        # câu = gap nghe "ngắt" giữa các câu trên audio output.
        sentences = [text.strip()]
        if not sentences[0]:
            return

        start = time.perf_counter()
        first_chunk_logged = False
        emit_buf = np.empty(0, dtype=np.float32)
        # Reset pacer deadline mỗi utterance.
        self._pace_deadline = None
        # Dynamic pre-buffer: scale theo độ dài text (Việt ~10 char/sec speech).
        # Vieneu gen ~0.95x realtime → deficit ~0.05 sec/sec. Pre-buffer absorb
        # deficit + chunk arrival jitter. Min 2s (text ngắn), max 6s (text rất dài).
        explicit = getattr(self.opt, "vieneu_http_prebuffer", None)
        if explicit and explicit > 0:
            prebuffer_secs = float(explicit)
        else:
            char_count = sum(len(s) for s in sentences)
            est_audio_secs = char_count / 10.0
            prebuffer_secs = max(2.0, min(6.0, est_audio_secs * 0.05 + 1.5))
        logger.info(f"[VieNeuHTTP] prebuffer target = {prebuffer_secs:.1f}s")
        prebuffer_samples = int(prebuffer_secs * self.SR_TARGET)
        prebuffered = False
        # Stateful streaming resampler — không artifact ở biên chunk.
        rs = soxr.ResampleStream(
            in_rate=self.SR_NATIVE,
            out_rate=self.SR_TARGET,
            num_channels=1,
            dtype="float32",
            quality="HQ",
        )

        for sent_idx, sentence in enumerate(sentences):
            if self.state != State.RUNNING:
                break

            is_last_sentence = (sent_idx == len(sentences) - 1)
            try:
                for pcm_24k in self._stream_one(sentence, voice_id, ref_audio, ref_text):
                    if self.state != State.RUNNING:
                        break
                    if pcm_24k is None or len(pcm_24k) == 0:
                        continue

                    if not first_chunk_logged:
                        logger.info(
                            f"[VieNeuHTTP] Time to first chunk: {time.perf_counter() - start:.2f}s"
                        )
                        first_chunk_logged = True

                    pcm_16k = rs.resample_chunk(pcm_24k, last=False).reshape(-1).astype(np.float32)
                    if pcm_16k.size > 0:
                        emit_buf = np.concatenate([emit_buf, pcm_16k]) if emit_buf.size else pcm_16k
                        # Streaming: push từng chunk. Pre-buffer N giây trước
                        # khi bắt đầu drain — absorb production deficit.
                        if not prebuffered:
                            if emit_buf.size >= prebuffer_samples:
                                prebuffered = True
                                logger.info(f"[VieNeuHTTP] prebuffer filled {emit_buf.size/self.SR_TARGET:.2f}s @ {time.perf_counter()-start:.2f}s wallclock")
                                emit_buf = self._drain_buffer(emit_buf, text, textevent, first_chunk_logged)
                        else:
                            emit_buf = self._drain_buffer(emit_buf, text, textevent, first_chunk_logged)
            except Exception as e:
                logger.exception(f"[VieNeuHTTP] stream error on sentence {sentence[:40]!r}: {e}")
                continue

            if is_last_sentence and self.state == State.RUNNING:
                # Text ngắn — drain ngay phần đã tích (không đợi đầy prebuffer)
                if not prebuffered and emit_buf.size > 0:
                    prebuffered = True
                    logger.info(f"[VieNeuHTTP] short utterance — flush {emit_buf.size/self.SR_TARGET:.2f}s")
                    emit_buf = self._drain_buffer(emit_buf, text, textevent, True)
                try:
                    final_16k = rs.resample_chunk(
                        np.empty(0, dtype=np.float32), last=True
                    ).reshape(-1).astype(np.float32)
                    if final_16k.size > 0:
                        emit_buf = np.concatenate([emit_buf, final_16k]) if emit_buf.size else final_16k
                except Exception as e:
                    logger.warning(f"[VieNeuHTTP] resampler flush error: {e}")
                if emit_buf.size > 0:
                    pad = (self.chunk - emit_buf.size % self.chunk) % self.chunk
                    if pad > 0:
                        emit_buf = np.concatenate([emit_buf, np.zeros(pad, dtype=np.float32)])
                    emit_buf = self._drain_buffer(emit_buf, text, textevent, True)
                eventpoint = {"status": "end", "text": text}
                if textevent:
                    eventpoint.update(textevent)
                self.parent.put_audio_frame(np.zeros(self.chunk, dtype=np.float32), eventpoint)

        logger.info(
            f"[VieNeuHTTP] Total stream: {time.perf_counter() - start:.2f}s "
            f"for {len(sentences)} sentence(s)"
        )

    # ------------------------------------------------------------------
    def _stream_one(self, text: str, voice_id: str, ref_audio: str, ref_text: str) -> Iterator[np.ndarray]:
        """POST /infer_stream → yield np.float32 mono 24kHz chunks."""
        payload = {"text": text}
        if voice_id:
            payload["voice_id"] = voice_id
        elif ref_audio and os.path.exists(ref_audio):
            payload["ref_audio"] = ref_audio
            if ref_text:
                payload["ref_text"] = ref_text

        with self._infer_lock:
            try:
                resp = self._session.post(
                    self._endpoint,
                    json=payload,
                    stream=True,
                    timeout=(5.0, self._timeout),
                )
            except requests.exceptions.RequestException as e:
                logger.error(f"[VieNeuHTTP] POST failed: {e}")
                return

            if resp.status_code != 200:
                try:
                    err = resp.json()
                except Exception:
                    err = resp.text[:200]
                logger.error(f"[VieNeuHTTP] server returned {resp.status_code}: {err}")
                resp.close()
                return

            try:
                yield from self._parse_stream(resp)
            finally:
                resp.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_stream(resp: requests.Response) -> Iterator[np.ndarray]:
        """Parse wire format: [4-byte BE length][length bytes f32le PCM]. len=0 → EOF."""
        raw = resp.raw  # urllib3 HTTPResponse — read exact byte counts

        def _read_exact(n: int) -> bytes:
            out = bytearray()
            while len(out) < n:
                chunk = raw.read(n - len(out))
                if not chunk:
                    return bytes(out)
                out.extend(chunk)
            return bytes(out)

        while True:
            hdr = _read_exact(4)
            if len(hdr) < 4:
                return
            (length,) = struct.unpack(">I", hdr)
            if length == 0:
                return
            payload = _read_exact(length)
            if len(payload) < length:
                logger.warning(f"[VieNeuHTTP] short read: got {len(payload)} of {length} bytes")
                return
            yield np.frombuffer(payload, dtype="<f4").copy()

    # ------------------------------------------------------------------
    def _drain_buffer(self, buf: np.ndarray, text: str, textevent: dict, started: bool) -> np.ndarray:
        # Push burst (KHÔNG pace). Render loop của BaseAvatar tự sleep khi
        # output.get_buffer_size() >= 5 → cho asr.queue thời gian fill trước
        # khi ASR drain. Pacer 20ms cũ gây queue depth dao động 0-1, ASR
        # run_step luôn timeout 10ms → silence frame 50% interleave.
        idx = 0
        while buf.size - idx >= self.chunk and self.state == State.RUNNING:
            eventpoint = {}
            if started and idx == 0 and textevent:
                eventpoint = {"status": "start", "text": text}
                eventpoint.update(textevent or {})
            self.parent.put_audio_frame(buf[idx : idx + self.chunk], eventpoint)
            idx += self.chunk
        return buf[idx:] if idx > 0 else buf
