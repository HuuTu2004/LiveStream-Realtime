###############################################################################
#  RTMP output — push avatar stream qua ffmpeg subprocess
#
#  Trước đây dùng `python_rtmpstream` (C++ pybind11 build từ source) — phức tạp,
#  yêu cầu libavcodec-dev + cmake + version FFmpeg đúng. Refactor sang ffmpeg
#  CLI subprocess (chỉ cần `apt install ffmpeg`).
#
#  Architecture:
#    Python                                  ffmpeg subprocess
#    ------                                  -----------------
#    push_video_frame() → write to pipe ──▶ -i pipe:N (rgb24 rawvideo)
#    push_audio_frame() → write to pipe ──▶ -i pipe:M (f32le PCM mono)
#                                            ↓
#                                            libx264 + aac → flv → RTMP server
#
#  Sử dụng `pass_fds` để pass UNIX pipe file descriptors cho ffmpeg.
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
    """RTMP push qua ffmpeg subprocess (no C++ extension build needed)."""

    def __init__(self, opt=None, parent: Optional['BaseAvatar'] = None, **kwargs):
        super().__init__(opt, parent)
        self.push_url = getattr(opt, 'push_url', 'rtmp://localhost/live/livestream')
        self.fps = getattr(opt, 'fps', 25)
        self.bitrate = getattr(opt, 'bitrate', 2_000_000)  # 2 Mbps mặc định
        self.sample_rate = getattr(opt, 'sample_rate', 16000)
        if parent and hasattr(parent, 'sample_rate'):
            self.sample_rate = parent.sample_rate

        self._proc: Optional[subprocess.Popen] = None
        self._video_fd_w: Optional[int] = None
        self._audio_fd_w: Optional[int] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self.width = getattr(opt, 'W', 450)
        self.height = getattr(opt, 'H', 450)

        # Audio/video sync — đếm để pad silence khi avatar idle
        self._video_frame_count = 0
        self._audio_samples_written = 0

        # FPS stats
        self.framecount = 0
        self.lasttime = time.perf_counter()
        self.totaltime = 0.0

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Streamer khởi tạo lazily khi frame đầu tiên đến — biết width/height."""
        self._audio_queue: queue.Queue = queue.Queue()
        self._quit_event = False

    def _spawn_ffmpeg(self, h: int, w: int) -> None:
        # Tạo 2 OS pipes — video + audio
        v_r, v_w = os.pipe()
        a_r, a_w = os.pipe()

        # Tăng pipe buffer lên 1MB để tránh block trên video frame lớn
        try:
            import fcntl
            F_SETPIPE_SZ = 1031  # Linux F_SETPIPE_SZ
            fcntl.fcntl(v_w, F_SETPIPE_SZ, 1024 * 1024)
            fcntl.fcntl(a_w, F_SETPIPE_SZ, 1024 * 1024)
        except Exception as e:
            logger.debug(f"[RTMP] F_SETPIPE_SZ failed: {e}")

        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'info',
            # Low-latency flags + skip metadata probe để start nhanh
            '-fflags', '+nobuffer+flush_packets',
            '-flags', 'low_delay',
            '-probesize', '32',
            '-analyzeduration', '0',
            # Video input (rgb24 raw, được Python feed qua v_r)
            '-f', 'rawvideo',
            '-pix_fmt', 'rgb24',
            '-s', f'{w}x{h}',
            '-r', str(self.fps),
            '-thread_queue_size', '1024',
            '-i', f'pipe:{v_r}',
            # Audio input (float32 little-endian mono, từ a_r)
            '-f', 'f32le',
            '-ar', str(self.sample_rate),
            '-ac', '1',
            '-thread_queue_size', '1024',
            '-i', f'pipe:{a_r}',
            # Video encode: x264 zerolatency cho real-time
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-pix_fmt', 'yuv420p',
            '-b:v', str(self.bitrate),
            '-maxrate', str(self.bitrate),
            '-bufsize', str(self.bitrate),
            '-g', str(self.fps * 2),  # keyframe mỗi 2s
            # Audio encode: AAC 128kbps, resample 44.1kHz (RTMP/FLV standard)
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-flush_packets', '1',
            # Output FLV qua RTMP
            '-f', 'flv',
            self.push_url,
        ]

        logger.info(f"[RTMP] spawning ffmpeg → {self.push_url} ({w}x{h} @ {self.fps}fps, audio {self.sample_rate}Hz)")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(v_r, a_r),
        )
        # Parent đóng read-ends (ffmpeg child đã inherit)
        os.close(v_r)
        os.close(a_r)
        self._video_fd_w = v_w
        self._audio_fd_w = a_w

        # Stderr reader thread — forward ffmpeg log
        self._stderr_thread = threading.Thread(target=self._pipe_stderr, daemon=True)
        self._stderr_thread.start()

        # Pre-fill 0.5s silence vào audio pipe để ffmpeg probe stream nhanh
        # (ffmpeg parse raw f32le mới open được input #1 → cần data sẵn)
        prefill_samples = self.sample_rate // 2  # 500ms
        prefill = np.zeros(prefill_samples, dtype=np.float32)
        try:
            os.write(self._audio_fd_w, prefill.tobytes())
            self._audio_samples_written = prefill_samples
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
            if 'error' in low or 'failed' in low or 'broken' in low or 'closed' in low:
                logger.error(f"[ffmpeg] {line}")
            elif any(k in low for k in ('warning', 'connection', 'rtmp', 'input #', 'stream #', 'output #', 'press [q]', 'fps=')):
                logger.info(f"[ffmpeg] {line}")
            else:
                logger.debug(f"[ffmpeg] {line}")

    # ------------------------------------------------------------------
    def push_video_frame(self, frame) -> None:
        if not isinstance(frame, np.ndarray):
            return
        with self._lock:
            if self._proc is None:
                # Lazy init dựa trên kích thước frame đầu tiên
                self.height, self.width = frame.shape[:2]
                self._spawn_ffmpeg(self.height, self.width)
                # Flush audio đã buffer trước khi video đầu tới
                while not self._audio_queue.empty():
                    buf = self._audio_queue.get()
                    try:
                        os.write(self._audio_fd_w, buf.tobytes())
                        self._audio_samples_written += len(buf)
                    except OSError:
                        break

            # Pipeline upstream dùng BGR (OpenCV), ffmpeg input là RGB
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            try:
                os.write(self._video_fd_w, rgb.tobytes())
            except BrokenPipeError:
                logger.error("[RTMP] ffmpeg pipe broken — subprocess died")
                self._cleanup_proc()
                return
            self._video_frame_count += 1

            # Sync audio: pad silence nếu audio fell behind expected video time
            # ffmpeg với 2 raw inputs cần cả 2 stream tiến đều — không có audio
            # thì mux pipeline đứng. Khi avatar idle (no TTS), audio không đến →
            # ta tự pad silence ở đây.
            expected_samples = int(self._video_frame_count * self.sample_rate / self.fps)
            samples_to_pad = expected_samples - self._audio_samples_written
            if samples_to_pad > 0 and self._audio_fd_w is not None:
                silence = np.zeros(samples_to_pad, dtype=np.float32)
                try:
                    os.write(self._audio_fd_w, silence.tobytes())
                    self._audio_samples_written += samples_to_pad
                except (BrokenPipeError, OSError):
                    pass

        # Frame pacing — giữ đúng fps
        delay = self._starttime + self._totalframe / self.fps - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        self._totalframe += 1

        # FPS stats log mỗi 100 frame
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
        # Upstream emit int16; ffmpeg f32le cần float32 [-1, 1]
        if frame.dtype == np.int16:
            frame = frame.astype(np.float32) / 32767.0
        elif frame.dtype != np.float32:
            frame = frame.astype(np.float32)

        if self._proc is None:
            # Video chưa khởi → buffer audio cho tới khi frame đầu tới
            self._audio_queue.put(frame)
            return

        with self._lock:
            try:
                os.write(self._audio_fd_w, frame.tobytes())
                self._audio_samples_written += len(frame)
            except BrokenPipeError:
                return

        if self.parent and eventpoint:
            self.parent.notify(eventpoint)

    # ------------------------------------------------------------------
    def _cleanup_proc(self) -> None:
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
        self._quit_event = True
        with self._lock:
            self._cleanup_proc()
        logger.info("[RTMP] output stopped")
