###############################################################################
#  RTMP output — push avatar stream qua ffmpeg subprocess
#
#  Trước đây dùng `python_rtmpstream` (C++ pybind11 build từ source) — phức tạp,
#  yêu cầu libavcodec-dev + cmake + version FFmpeg đúng. Refactor sang ffmpeg
#  CLI subprocess (chỉ cần `apt install ffmpeg`).
#
#  Architecture:
#    Python main thread          ffmpeg subprocess
#    ------------------          -----------------
#    push_video_frame() ──▶ video pipe ──▶ -i pipe:N (rgb24 rawvideo)
#                                          ↓
#    push_audio_frame() ──▶ audio queue    ↓
#                          ↓               ↓
#    audio writer thread ──▶ audio pipe ──▶ -i pipe:M (f32le PCM mono)
#    (constant 40ms tick — pop queue or silence)
#                                          ↓ libx264 + aac → flv → RTMP
#
#  Lý do tách audio thread:
#    ffmpeg với 2 raw inputs cần CẢ 2 stream tiến đều. Nếu Python pump video
#    trong loop chính và pad silence in-line cho audio, video write (1.3MB)
#    chặn pipe khi ffmpeg buffer đầy → audio không được pump → ffmpeg stall
#    → deadlock. Audio thread chạy độc lập, tick 40ms (= 1 video frame), pop
#    real audio từ queue (nếu có) hoặc đẩy silence. Không bao giờ stall.
###############################################################################

import os
import subprocess
import threading
import time
import queue
from typing import TYPE_CHECKING, Optional

import cv2
import numpy as np

from streamout.base_output import BaseOutput
from registry import register
from utils.logger import logger

if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar


@register("streamout", "rtmp")
class RTMPOutput(BaseOutput):
    """RTMP push qua ffmpeg subprocess + audio thread tránh deadlock."""

    def __init__(self, opt=None, parent: Optional['BaseAvatar'] = None, **kwargs):
        super().__init__(opt, parent)
        self.push_url = getattr(opt, 'push_url', 'rtmp://localhost/live/livestream')
        self.fps = getattr(opt, 'fps', 25)
        self.bitrate = getattr(opt, 'bitrate', 2_000_000)
        self.sample_rate = getattr(opt, 'sample_rate', 16000)
        if parent and hasattr(parent, 'sample_rate'):
            self.sample_rate = parent.sample_rate

        self._proc: Optional[subprocess.Popen] = None
        self._video_fd_w: Optional[int] = None
        self._audio_fd_w: Optional[int] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self.width = getattr(opt, 'W', 450)
        self.height = getattr(opt, 'H', 450)

        # Audio buffer + sync — single-thread architecture:
        # per video frame, drain audio queue và write 1 chunk audio đồng bộ.
        self._chunk_samples = self.sample_rate // self.fps  # 640 @ 16kHz/25fps
        self._audio_buf = np.empty(0, dtype=np.float32)
        self._silence_chunk_bytes = np.zeros(self._chunk_samples, dtype=np.float32).tobytes()

        # FPS stats
        self.framecount = 0
        self.lasttime = time.perf_counter()
        self.totaltime = 0.0

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Streamer khởi tạo lazily khi frame đầu tiên đến — biết width/height."""
        self._audio_queue: queue.Queue = queue.Queue()
        self._stop_event.clear()

    def _spawn_ffmpeg(self, h: int, w: int) -> None:
        v_r, v_w = os.pipe()
        a_r, a_w = os.pipe()

        # Tăng pipe buffer lên 1MB (Linux only) để tránh block frame lớn
        try:
            import fcntl
            F_SETPIPE_SZ = 1031
            fcntl.fcntl(v_w, F_SETPIPE_SZ, 1024 * 1024)
            fcntl.fcntl(a_w, F_SETPIPE_SZ, 1024 * 1024)
        except Exception as e:
            logger.debug(f"[RTMP] F_SETPIPE_SZ failed: {e}")

        # Detect NVENC availability — probe ffmpeg thật (Vast container có
        # nvidia-smi nhưng KHÔNG có libnvidia-encode, nvenc fail runtime).
        encoder_env = os.environ.get('RTMP_ENCODER', 'auto').lower()
        use_nvenc = False
        if encoder_env in ('auto', 'nvenc'):
            try:
                test = subprocess.run(
                    ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                     '-f', 'lavfi', '-i', 'nullsrc=s=64x64', '-frames:v', '1',
                     '-c:v', 'h264_nvenc', '-f', 'null', '-'],
                    capture_output=True, timeout=5,
                )
                use_nvenc = (test.returncode == 0)
                if not use_nvenc:
                    logger.info(f"[RTMP] nvenc probe fail: {test.stderr.decode()[:200]}")
            except Exception as e:
                logger.info(f"[RTMP] nvenc probe error: {e}")
        if use_nvenc:
            video_codec = [
                '-c:v', 'h264_nvenc',
                '-preset', 'p1',           # p1=fastest, p7=slowest+best
                '-tune', 'll',             # low-latency
                '-rc', 'cbr',
                '-b:v', str(self.bitrate),
                '-maxrate', str(self.bitrate),
                '-bufsize', str(self.bitrate),
                '-zerolatency', '1',
                '-pix_fmt', 'yuv420p',
                '-g', str(self.fps * 2),
            ]
            encoder_name = 'h264_nvenc (GPU)'
        else:
            video_codec = [
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-b:v', str(self.bitrate),
                '-g', str(self.fps * 2),
            ]
            encoder_name = 'libx264 (CPU)'

        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'info',
            '-fflags', '+nobuffer',
            '-probesize', '32',
            '-analyzeduration', '0',
            '-f', 'rawvideo',
            '-pix_fmt', 'rgb24',
            '-s', f'{w}x{h}',
            '-r', str(self.fps),
            '-thread_queue_size', '1024',
            '-i', f'pipe:{v_r}',
            '-f', 'f32le',
            '-ar', str(self.sample_rate),
            '-ac', '1',
            '-thread_queue_size', '1024',
            '-i', f'pipe:{a_r}',
            *video_codec,
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-f', 'flv',
            self.push_url,
        ]
        logger.info(f"[RTMP] encoder = {encoder_name}")

        logger.info(f"[RTMP] spawning ffmpeg → {self.push_url} ({w}x{h} @ {self.fps}fps)")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(v_r, a_r),
        )
        os.close(v_r)
        os.close(a_r)
        self._video_fd_w = v_w
        self._audio_fd_w = a_w

        # Stderr reader thread
        self._stderr_thread = threading.Thread(target=self._pipe_stderr, daemon=True)
        self._stderr_thread.start()

        # Pre-fill 5s silence để ffmpeg probe input #1 + giữ audio không stall
        # trong khi avatar pipeline warmup (wav2lip + TTS first inference có thể chậm).
        # Per push_video_frame sau đó write 1 chunk audio để bù lại consumed silence.
        prefill = np.zeros(self.sample_rate * 5, dtype=np.float32)
        try:
            os.write(self._audio_fd_w, prefill.tobytes())
        except OSError as e:
            logger.warning(f"[RTMP] audio prefill failed: {e}")

        self._starttime = time.perf_counter()
        self._totalframe = 0

    def _pipe_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for raw in self._proc.stderr:
            try:
                line = raw.decode('utf-8', errors='replace').rstrip()
            except Exception:
                continue
            if not line:
                continue
            low = line.lower()
            if 'error' in low or 'failed' in low or 'broken' in low:
                logger.error(f"[ffmpeg] {line}")
            elif 'input #' in low or 'output #' in low or 'stream #' in low:
                logger.info(f"[ffmpeg] {line}")
            else:
                logger.debug(f"[ffmpeg] {line}")

    def _write_audio_chunk(self) -> None:
        """Per video frame: drain queue + write exactly chunk_samples to audio pipe.

        Sync với video: gọi 1 lần mỗi push_video_frame để audio & video tiến đều.
        Buffer phần dư trong self._audio_buf (avatar push 320-sample chunks,
        ta cần 640 mỗi frame → cần 2 chunks).
        """
        # Drain queue cho đủ chunk_samples
        while self._audio_buf.size < self._chunk_samples:
            try:
                data = self._audio_queue.get_nowait()
            except queue.Empty:
                break
            if data.size > 0:
                self._audio_buf = np.concatenate([self._audio_buf, data])

        if self._audio_fd_w is None:
            return

        if self._audio_buf.size >= self._chunk_samples:
            payload = self._audio_buf[:self._chunk_samples].tobytes()
            self._audio_buf = self._audio_buf[self._chunk_samples:]
        else:
            # Silence fill cho frame này
            payload = self._silence_chunk_bytes

        try:
            os.write(self._audio_fd_w, payload)
        except (BrokenPipeError, OSError):
            pass

    # ------------------------------------------------------------------
    def push_video_frame(self, frame) -> None:
        if not isinstance(frame, np.ndarray):
            return
        if self._proc is None:
            # Lazy init dựa trên kích thước frame đầu tiên
            self.height, self.width = frame.shape[:2]
            self._spawn_ffmpeg(self.height, self.width)

        # Pipeline upstream dùng BGR (OpenCV) → ffmpeg input là RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        try:
            os.write(self._video_fd_w, rgb.tobytes())
        except BrokenPipeError:
            logger.error("[RTMP] video pipe broken — ffmpeg died")
            self._cleanup_proc()
            return

        # Audio sync: write CHÍNH XÁC 1 chunk audio cho video frame này.
        # → audio & video tiến đều, không drift do thread khác clock.
        self._write_audio_chunk()

        # Frame pacing — giữ đúng fps (tránh push nhanh hơn fps gây buffer bloat)
        delay = self._starttime + self._totalframe / self.fps - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        self._totalframe += 1

        # FPS stats
        self.totaltime += (time.perf_counter() - self.lasttime)
        self.framecount += 1
        self.lasttime = time.perf_counter()
        if self.framecount == 100:
            logger.info(f"[RTMP] actual avg fps = {self.framecount/self.totaltime:.2f}")
            self.framecount = 0
            self.totaltime = 0.0

    def push_audio_frame(self, frame, eventpoint=None) -> None:
        if not isinstance(frame, np.ndarray):
            return
        # Upstream emit int16 [-32767, 32767]; ffmpeg f32le cần float32 [-1, 1]
        if frame.dtype == np.int16:
            frame = frame.astype(np.float32) / 32767.0
        elif frame.dtype != np.float32:
            frame = frame.astype(np.float32)

        # Queue cho audio thread — KHÔNG write trực tiếp (audio thread maintain rate)
        self._audio_queue.put(frame)

        if self.parent and eventpoint:
            self.parent.notify(eventpoint)

    # ------------------------------------------------------------------
    def _cleanup_proc(self) -> None:
        self._stop_event.set()
        if self._video_fd_w is not None:
            try: os.close(self._video_fd_w)
            except OSError: pass
            self._video_fd_w = None
        if self._audio_fd_w is not None:
            try: os.close(self._audio_fd_w)
            except OSError: pass
            self._audio_fd_w = None
        if self._proc:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def stop(self) -> None:
        self._cleanup_proc()
        logger.info("[RTMP] output stopped")
