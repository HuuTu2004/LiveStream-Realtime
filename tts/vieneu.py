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
#  VieNeu-TTS plugin — Vietnamese realtime TTS cho livestream sales.
#
#  Modes (chọn qua --vieneu_mode):
#
#    gpu       — KHUYẾN NGHỊ cho production GPU. Plugin tự spawn `lmdeploy
#                serve api_server` local background, init Vieneu remote
#                client connect 127.0.0.1:VIENEU_PORT. Throughput tối đa nhờ
#                TurboMind (FlashAttention + tensor parallel + paged KV cache).
#    standard  — GGUF + ONNX local. Real-time CPU OR GPU thông thường,
#                ko tận dụng được TurboMind. Quality cao nhất.
#    turbo     — 0.3B variant, 2x faster CPU/GPU. Dùng khi resource hạn chế.
#    remote    — Connect tới lmdeploy server đã chạy sẵn ở --vieneu_api_base.
#
#  Voice cloning: 3-5s ref WAV. Standard mode cần ref_text, turbo không cần.
#  Output: 24 kHz mono → plugin resample 16 kHz cho LiveTalking pipeline.
#

import atexit
import os
import re
import subprocess
import sys
import threading
import time
from io import BytesIO
from typing import Optional

import numpy as np
import resampy
import soundfile as sf

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


# ─── LMDeploy GPU server manager (singleton-spawned subprocess) ───────

class _LMDeployServer:
    """Spawn `lmdeploy serve api_server` local, share giữa tất cả session.

    Tự shutdown khi Python exit (atexit hook).
    """

    _instance: Optional["_LMDeployServer"] = None
    _lock = threading.Lock()

    def __init__(self, model_name: str, port: int, tp: int):
        self.model_name = model_name
        self.port = port
        self.tp = tp
        self.api_base = f"http://127.0.0.1:{port}/v1"
        self._proc: Optional[subprocess.Popen] = None
        self._log_thread: Optional[threading.Thread] = None
        self._start()
        atexit.register(self.shutdown)

    @classmethod
    def get(cls, model_name: str, port: int, tp: int) -> "_LMDeployServer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(model_name, port, tp)
            else:
                # Đã có instance — re-use bất kể args khác (1 server / process)
                logger.info(f"[LMDeploy] re-use existing server at {cls._instance.api_base}")
            return cls._instance

    def _start(self) -> None:
        # Nếu port đã có process khác listen (vd: user start trước), skip spawn
        if self._port_in_use():
            logger.info(f"[LMDeploy] port {self.port} đã có server → skip spawn, dùng external")
            return

        cmd = [
            sys.executable, "-m", "lmdeploy",
            "serve", "api_server",
            self.model_name,
            "--server-port", str(self.port),
            "--server-name", "127.0.0.1",
            "--tp", str(self.tp),
        ]
        logger.info(f"[LMDeploy] spawning: {' '.join(cmd)}")

        env = os.environ.copy()
        env.setdefault("CUDA_VISIBLE_DEVICES", "0")

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            bufsize=1,
            universal_newlines=True,
        )
        # Forward stdout sang logger để debug
        self._log_thread = threading.Thread(target=self._pipe_logs, daemon=True)
        self._log_thread.start()
        # Đợi server ready (max 180s — model load có thể chậm)
        if not self._wait_ready(timeout=180):
            raise RuntimeError("LMDeploy server không sẵn sàng sau 180s")
        logger.info(f"[LMDeploy] ready at {self.api_base}")

    def _pipe_logs(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            line = line.rstrip()
            if line:
                logger.debug(f"[lmdeploy] {line}")

    def _port_in_use(self) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", self.port))
                return True
            except (ConnectionRefusedError, socket.timeout, OSError):
                return False

    def _wait_ready(self, timeout: float = 180) -> bool:
        import urllib.request
        import urllib.error
        deadline = time.time() + timeout
        url = f"{self.api_base}/models"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return True
            except (urllib.error.URLError, ConnectionResetError, OSError):
                pass
            if self._proc is not None and self._proc.poll() is not None:
                logger.error(f"[LMDeploy] subprocess died early (exit code {self._proc.returncode})")
                return False
            time.sleep(2)
        return False

    def shutdown(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            logger.info("[LMDeploy] shutting down server...")
            try:
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None


# ─── VieNeu model singleton ───────────────────────────────────────────

class _VieNeuSingleton:
    """Singleton — share model giữa tất cả session."""

    _instance: Optional["_VieNeuSingleton"] = None
    _lock = threading.Lock()

    def __init__(self, mode: str, emotion: str, api_base: str, model_name: str):
        from vieneu import Vieneu  # lazy import

        self.mode = mode
        kwargs: dict = {}
        if mode == "turbo":
            # CPU GGUF or CPU+GPU offload via llama-cpp-python n_gpu_layers
            kwargs["mode"] = "turbo"
        elif mode == "turbo_gpu":
            # Native torch+transformers GPU — nhanh hơn turbo (gguf) khi có GPU mạnh
            kwargs["mode"] = "turbo_gpu"
            kwargs["device"] = "cuda"
        elif mode in ("remote", "gpu"):
            # gpu = remote + auto-spawn lmdeploy (đã xử lý trước khi gọi class này)
            kwargs["mode"] = "remote"
            kwargs["api_base"] = api_base
            kwargs["model_name"] = model_name
            if emotion:
                kwargs["emotion"] = emotion
        else:
            # standard — default GGUF+ONNX local (auto offload nếu llama-cpp build CUDA)
            if emotion:
                kwargs["emotion"] = emotion

        self.tts = Vieneu(**kwargs)
        logger.info(f"[VieNeu] Loaded mode={mode} emotion={emotion or '—'}")
        self._voice_cache: dict = {}

    @classmethod
    def get(cls, mode: str, emotion: str, api_base: str, model_name: str) -> "_VieNeuSingleton":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(mode, emotion, api_base, model_name)
            return cls._instance

    def encode_reference(self, ref_audio: str):
        if ref_audio not in self._voice_cache:
            self._voice_cache[ref_audio] = self.tts.encode_reference(ref_audio)
        return self._voice_cache[ref_audio]

    def get_preset_voice(self, voice_id: str):
        cache_key = f"preset:{voice_id}"
        if cache_key not in self._voice_cache:
            self._voice_cache[cache_key] = self.tts.get_preset_voice(voice_id)
        return self._voice_cache[cache_key]


@register("tts", "vieneu")
class VieNeuTTS(BaseTTS):
    """VieNeu-TTS plugin — realtime Vietnamese TTS cho livestream sales."""

    SR_NATIVE = 24000
    SR_TARGET = 16000

    def __init__(self, opt, parent):
        super().__init__(opt, parent)

        self.mode = getattr(opt, "vieneu_mode", "gpu")
        self.emotion = getattr(opt, "vieneu_emotion", "natural")
        model_name = getattr(opt, "vieneu_model_name", "pnnbao-ump/VieNeu-TTS-v2")
        api_base = getattr(opt, "vieneu_api_base", "") or ""
        port = int(getattr(opt, "vieneu_port", 23333) or 23333)
        tp = int(getattr(opt, "vieneu_tp", 1) or 1)

        # GPU mode: auto-spawn lmdeploy server local
        if self.mode == "gpu" and not api_base:
            server = _LMDeployServer.get(model_name, port, tp)
            api_base = server.api_base
            logger.info(f"[VieNeu] GPU mode — connect tới lmdeploy {api_base}")
        elif self.mode == "remote" and not api_base:
            api_base = f"http://127.0.0.1:{port}/v1"
            logger.warning(f"[VieNeu] remote mode nhưng api_base trống → fallback {api_base}")

        self._singleton = _VieNeuSingleton.get(self.mode, self.emotion, api_base, model_name)

        self._default_voice_id = (getattr(opt, "vieneu_voice_id", "") or "").strip()
        self._default_ref_audio = (getattr(opt, "vieneu_ref_audio", "") or "").strip()
        self._default_ref_text = (getattr(opt, "vieneu_ref_text", "") or "").strip()

        # Fallback ref từ legacy REF_FILE (compat với code cũ)
        if not self._default_ref_audio and not self._default_voice_id:
            legacy_ref = getattr(opt, "REF_FILE", "")
            if legacy_ref and os.path.exists(legacy_ref):
                self._default_ref_audio = legacy_ref
                self._default_ref_text = getattr(opt, "REF_TEXT", "") or ""
                logger.info(f"[VieNeu] Fallback dùng REF_FILE: {legacy_ref}")

        self._infer_lock = threading.Lock()
        logger.info(
            f"[VieNeu] init mode={self.mode} voice_id={self._default_voice_id or '—'} "
            f"ref={self._default_ref_audio or '—'}"
        )

    # ------------------------------------------------------------------
    def txt_to_audio(self, msg: tuple[str, dict]):
        text, textevent = msg
        if not text or not text.strip():
            return

        tts_override = textevent.get("tts", {}) if isinstance(textevent, dict) else {}
        voice_id = tts_override.get("voice_id", self._default_voice_id)
        ref_audio = tts_override.get("ref_file", self._default_ref_audio)
        ref_text = tts_override.get("ref_text", self._default_ref_text)

        sentences = _split_sentences(text)
        if not sentences:
            return

        start = time.perf_counter()
        first_sentence = True

        for sent_idx, sentence in enumerate(sentences):
            if self.state != State.RUNNING:
                break
            try:
                audio = self._infer_one(sentence, voice_id, ref_audio, ref_text)
            except Exception as e:
                logger.exception(f"[VieNeu] infer error on sentence: {sentence[:40]!r}: {e}")
                continue
            if audio is None or len(audio) == 0:
                continue

            if first_sentence:
                logger.info(f"[VieNeu] Time to first chunk: {time.perf_counter()-start:.2f}s")

            self._emit_chunks(
                pcm_24k=audio,
                text=text,
                textevent=textevent,
                is_first_sentence=first_sentence,
                is_last_sentence=(sent_idx == len(sentences) - 1),
            )
            first_sentence = False

        logger.info(
            f"[VieNeu] Total: {time.perf_counter()-start:.2f}s for {len(sentences)} sentence(s)"
        )

    # ------------------------------------------------------------------
    def _infer_one(self, text: str, voice_id: str, ref_audio: str, ref_text: str) -> np.ndarray:
        with self._infer_lock:
            kwargs = {"text": text}
            if voice_id:
                kwargs["voice"] = self._singleton.get_preset_voice(voice_id)
            elif ref_audio and os.path.exists(ref_audio):
                # Turbo mode (và gpu/remote chạy turbo backend) thường không cần ref_text
                if self.mode in ("turbo",):
                    kwargs["voice"] = self._singleton.encode_reference(ref_audio)
                else:
                    kwargs["ref_audio"] = ref_audio
                    if ref_text:
                        kwargs["ref_text"] = ref_text
            audio = self._singleton.tts.infer(**kwargs)
        return self._to_float32_mono(audio)

    @staticmethod
    def _to_float32_mono(audio) -> np.ndarray:
        if audio is None:
            return np.array([], dtype=np.float32)
        if isinstance(audio, (bytes, bytearray)):
            data, sr = sf.read(BytesIO(audio), dtype="float32")
            audio = data
        elif hasattr(audio, "cpu"):
            audio = audio.cpu().numpy()
        arr = np.asarray(audio, dtype=np.float32)
        if arr.ndim > 1:
            arr = arr[:, 0] if arr.shape[1] < arr.shape[0] else arr[0, :]
        return arr

    # ------------------------------------------------------------------
    def _emit_chunks(
        self,
        pcm_24k: np.ndarray,
        text: str,
        textevent: dict,
        is_first_sentence: bool,
        is_last_sentence: bool,
    ) -> None:
        stream = resampy.resample(pcm_24k, sr_orig=self.SR_NATIVE, sr_new=self.SR_TARGET)
        streamlen = stream.shape[0]
        idx = 0
        first_chunk = True

        while streamlen >= self.chunk and self.state == State.RUNNING:
            eventpoint = {}
            if first_chunk and is_first_sentence:
                eventpoint = {"status": "start", "text": text}
            if first_chunk and textevent:
                eventpoint.update(textevent)
            self.parent.put_audio_frame(stream[idx : idx + self.chunk], eventpoint)
            streamlen -= self.chunk
            idx += self.chunk
            first_chunk = False

        if is_last_sentence and self.state == State.RUNNING:
            eventpoint = {"status": "end", "text": text}
            eventpoint.update(textevent or {})
            self.parent.put_audio_frame(np.zeros(self.chunk, dtype=np.float32), eventpoint)
