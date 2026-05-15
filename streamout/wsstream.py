###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  Licensed under the Apache License, Version 2.0
###############################################################################
#
#  WSStream output — MPEG-TS over WebSocket (JSMpeg-compatible).
#
#  Mục đích: realtime low-latency (~100-200ms) qua TCP single-port — bypass
#  WebRTC NAT/UDP issues trên Vast.AI mà không cần TURN server.
#
#  Pipeline:
#    push_video_frame(rgb)  ─▶ TCP video sock ─┐
#    push_audio_frame(pcm)  ─▶ TCP audio sock ─┴─▶ ffmpeg (mpeg1+mp2 → mpegts)
#                                                              │
#                                                              ▼
#                                                       stdout reader thread
#                                                              │
#                                                              ▼
#                                                  broadcast WS clients
#                                                  (JSMpeg in browser)
#
#  Cross-platform: dùng TCP loopback sockets (127.0.0.1:0 ephemeral) thay vì
#  os.pipe()+pass_fds — chạy được cả Windows lẫn Linux.
#
#  Codec: MPEG-1 video + MP2 audio bắt buộc cho JSMpeg (lib chỉ decode 2 codec
#  này). Bandwidth ~1.5-2.5 Mbps @ 576x768/25fps (cao hơn H.264 ~30% nhưng
#  software decode trong browser → universal compat, zero infra).
#
###############################################################################

import os
import socket
import subprocess
import threading
import time
import queue
from collections import deque
from typing import TYPE_CHECKING, Optional, Set, Callable

import cv2
import numpy as np

from streamout.base_output import BaseOutput
from registry import register
from utils.logger import logger

if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar


@register("streamout", "wsstream")
class WSStreamOutput(BaseOutput):
    """MPEG-TS over WebSocket — realtime playback bằng JSMpeg."""

    def __init__(self, opt=None, parent: Optional['BaseAvatar'] = None, **kwargs):
        super().__init__(opt, parent)
        self.fps = getattr(opt, 'fps', 25)
        # Bitrate tunable qua env (mpeg1 cần bitrate cao hơn h264 ~30%)
        self.video_bitrate = int(os.environ.get('WSSTREAM_VBITRATE', '1500000'))
        self.audio_bitrate = int(os.environ.get('WSSTREAM_ABITRATE', '128000'))
        self.sample_rate = 16000
        if parent and hasattr(parent, 'sample_rate'):
            self.sample_rate = parent.sample_rate

        self.width = getattr(opt, 'W', 450)
        self.height = getattr(opt, 'H', 450)

        self._proc: Optional[subprocess.Popen] = None
        self._video_sock: Optional[socket.socket] = None
        self._audio_sock: Optional[socket.socket] = None
        self._stop = threading.Event()

        # Audio sync — 1 chunk per video frame (640 samples @ 16kHz/25fps)
        self._chunk_samples = self.sample_rate // self.fps
        self._audio_buf = np.empty(0, dtype=np.float32)
        self._silence_bytes = np.zeros(self._chunk_samples, dtype=np.float32).tobytes()
        self._audio_queue: queue.Queue = queue.Queue()

        # WS clients — set of callbacks (each writes to 1 WebSocket).
        # Callbacks run on the ffmpeg reader thread → must schedule onto asyncio loop.
        self._clients: Set[Callable[[bytes], None]] = set()
        self._clients_lock = threading.Lock()

        # Header cache — late-joining clients need MPEG-TS PAT/PMT + 1 keyframe.
        # 64 chunks × 4 KB = 256 KB → đủ ~1s context @ 2 Mbps.
        self._header_buf: deque = deque(maxlen=64)

        # Stats
        self._framecount = 0
        self._starttime = 0.0
        self._totalframe = 0

    # ─── Lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        self._audio_queue = queue.Queue()
        self._stop.clear()
        # ffmpeg spawn deferred to first push_video_frame (cần biết W×H thật)

    @staticmethod
    def _pick_free_port() -> int:
        """Bind ephemeral port rồi đóng — OS sẽ cấp port khác sau đó."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _spawn_ffmpeg(self, h: int, w: int) -> None:
        # ─── Architecture: video qua STDIN, audio qua TCP listen ──────────
        # stdin pipe luôn "ready" từ lúc Popen — ffmpeg không cần đợi connect.
        # → mở input audio TCP ngay sau khi parse args → Python connect được.
        # (Pattern 2-TCP-listen bị deadlock vì ffmpeg đợi video data flow xong
        # mới mở audio listener, mà Python chưa push frame vì đang connect audio.)
        a_port = self._pick_free_port()

        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'warning',
            '-fflags', '+nobuffer', '-flags', 'low_delay',
            # Skip stream probing — bắt buộc khi mix stdin + TCP listen, ko skip
            # thì ffmpeg probe rawvideo stdin trước, đọc 5MB → block tới khi
            # Python push frame → audio TCP listener không bao giờ mở.
            '-probesize', '32', '-analyzeduration', '0',
            # ─── INPUT ORDER MATTERS ────────────────────────────────────────
            # Audio TCP listen TRƯỚC — ffmpeg mở listen socket ngay → Python
            # connect thread connect được. Sau đó ffmpeg mới mở stdin (pipe:0
            # luôn ready). Đảo lại (stdin trước) → chicken-and-egg deadlock.
            #
            # Audio input — f32le PCM mono qua TCP listen
            '-f', 'f32le', '-ar', str(self.sample_rate), '-ac', '1',
            '-thread_queue_size', '512',
            '-listen_timeout', '20000',
            '-i', f'tcp://127.0.0.1:{a_port}?listen=1',
            # Video input — raw RGB24 từ stdin
            '-f', 'rawvideo', '-pix_fmt', 'rgb24',
            '-s', f'{w}x{h}', '-r', str(self.fps),
            '-thread_queue_size', '512',
            '-i', 'pipe:0',
            # Encode: MPEG-1 video + MP2 audio (JSMpeg-compatible)
            '-c:v', 'mpeg1video',
            '-b:v', str(self.video_bitrate),
            '-bf', '0',                          # no B-frames → low latency
            '-g', str(self.fps),                 # keyframe mỗi 1s
            '-c:a', 'mp2',
            '-b:a', str(self.audio_bitrate),
            '-ar', '44100',                      # MP2 chuẩn 44.1kHz
            # Mux ra MPEG-TS, push stdout
            '-f', 'mpegts',
            '-muxdelay', '0.001', '-muxpreload', '0.001',
            '-',
        ]
        logger.info(f"[WSStream] spawning ffmpeg ({w}x{h} @ {self.fps}fps, "
                    f"v={self.video_bitrate // 1000}k, a={self.audio_bitrate // 1000}k, "
                    f"video=stdin audio=tcp:{a_port})")

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Video "socket" = stdin pipe (BufferedWriter)
        self._video_sock = self._proc.stdin  # type: ignore[assignment]

        # Start stderr reader NGAY để thấy ffmpeg log + tránh pipe-full block
        threading.Thread(target=self._read_stderr, daemon=True, name='ws-stderr').start()

        # ─── Connect audio TCP với retry loop ───────────────────────────
        # QUAN TRỌNG: start audio connect thread NGAY (trước khi ghi gì vào
        # stdin). Lý do: RGB frame 576x768 = 1.3MB, stdin pipe buffer chỉ 64KB
        # → write block tới khi ffmpeg drain. ffmpeg không drain video tới khi
        # audio input mở xong → deadlock. Audio connect chạy song song unblock.
        def _connect_with_retry(port: int, name: str, max_wait: float = 20.0) -> Optional[socket.socket]:
            deadline = time.time() + max_wait
            last_err: Optional[Exception] = None
            while time.time() < deadline:
                if self._proc and self._proc.poll() is not None:
                    logger.error(f"[WSStream] ffmpeg died trước khi connect được {name}")
                    return None
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(2)
                    s.connect(('127.0.0.1', port))
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    s.settimeout(None)
                    return s
                except (ConnectionRefusedError, OSError) as e:
                    last_err = e
                    time.sleep(0.15)
            logger.error(f"[WSStream] connect {name} (port {port}) timed out: {last_err}")
            return None

        def _connect_audio():
            a = _connect_with_retry(a_port, 'audio', max_wait=20.0)
            if a is None:
                return
            self._audio_sock = a
            logger.info("[WSStream] connected to ffmpeg (video=stdin, audio=tcp)")
            # KHÔNG pre-fill silence — sẽ đẩy audio stream PTS lệch so với
            # video PTS → lip-sync lệch chính xác = số giây prefill.
            # Audio sẽ tự được fill silence chunk-by-chunk khi queue empty
            # trong _write_audio_chunk (mỗi chunk 40ms cùng video frame).

        threading.Thread(target=_connect_audio, daemon=True, name='ws-connect-audio').start()

        # Đợi audio socket ready (max 25s)
        for _ in range(250):
            if self._audio_sock:
                break
            time.sleep(0.1)
        if self._audio_sock is None:
            logger.error("[WSStream] không connect được audio TCP sau 25s")
            return

        # stdout reader → broadcast mpegts chunks tới WS clients
        # (stderr reader đã start ở trên — không duplicate)
        threading.Thread(target=self._read_stdout, daemon=True, name='ws-stdout').start()

        self._starttime = time.perf_counter()
        self._totalframe = 0

    # ─── Threads ───────────────────────────────────────────────────────────
    def _read_stdout(self) -> None:
        """Đọc MPEG-TS chunks từ ffmpeg stdout, broadcast tới mọi WS client."""
        proc = self._proc
        if not proc or not proc.stdout:
            return
        while not self._stop.is_set():
            try:
                chunk = proc.stdout.read(4096)
            except Exception:
                break
            if not chunk:
                break
            # Cache cho client mới join (PAT/PMT + recent keyframe context)
            self._header_buf.append(chunk)
            # Broadcast
            with self._clients_lock:
                clients = list(self._clients)
            dead = []
            for cb in clients:
                try:
                    cb(chunk)
                except Exception:
                    dead.append(cb)
            if dead:
                with self._clients_lock:
                    for cb in dead:
                        self._clients.discard(cb)
        logger.info("[WSStream] stdout reader exited")

    def _read_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        for raw in proc.stderr:
            try:
                line = raw.decode('utf-8', errors='replace').rstrip()
            except Exception:
                continue
            if not line:
                continue
            low = line.lower()
            if 'error' in low or 'failed' in low or 'broken' in low:
                logger.error(f"[ffmpeg-ws] {line}")
            elif 'input #' in low or 'output #' in low or 'stream #' in low or 'press [q]' in low:
                logger.info(f"[ffmpeg-ws] {line}")
            else:
                # Tạm bật INFO để debug — sau khi stable có thể hạ về DEBUG
                logger.info(f"[ffmpeg-ws] {line}")

    # ─── BaseOutput contract ───────────────────────────────────────────────
    def push_video_frame(self, frame) -> None:
        if not isinstance(frame, np.ndarray):
            return
        if self._proc is None:
            self.height, self.width = frame.shape[:2]
            self._spawn_ffmpeg(self.height, self.width)
        if self._video_sock is None:
            return  # stdin chưa sẵn (proc died early)

        # Upstream pipeline = BGR (OpenCV) → ffmpeg muốn RGB24
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        try:
            # self._video_sock = self._proc.stdin (BufferedWriter)
            self._video_sock.write(rgb.tobytes())
        except (BrokenPipeError, OSError, ConnectionResetError):
            logger.error("[WSStream] video stdin broken — ffmpeg died?")
            self._cleanup_proc()
            return

        # Audio sync: 1 chunk per video frame → tránh drift
        self._write_audio_chunk()

        # Frame pacing
        delay = self._starttime + self._totalframe / self.fps - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        self._totalframe += 1

        self._framecount += 1
        if self._framecount % 250 == 0:
            elapsed = max(time.perf_counter() - self._starttime, 1e-3)
            with self._clients_lock:
                nc = len(self._clients)
            logger.info(f"[WSStream] {nc} client(s) | avg fps={self._totalframe / elapsed:.2f}")

    def _write_audio_chunk(self) -> None:
        if self._audio_sock is None:
            return
        # Drain audio queue
        while self._audio_buf.size < self._chunk_samples:
            try:
                data = self._audio_queue.get_nowait()
            except queue.Empty:
                break
            if data.size > 0:
                self._audio_buf = np.concatenate([self._audio_buf, data])

        if self._audio_buf.size >= self._chunk_samples:
            payload = self._audio_buf[:self._chunk_samples].tobytes()
            self._audio_buf = self._audio_buf[self._chunk_samples:]
        else:
            payload = self._silence_bytes
        try:
            self._audio_sock.sendall(payload)
        except (BrokenPipeError, OSError, ConnectionResetError):
            pass

    def push_audio_frame(self, frame, eventpoint=None) -> None:
        if not isinstance(frame, np.ndarray):
            return
        if frame.dtype == np.int16:
            frame = frame.astype(np.float32) / 32767.0
        elif frame.dtype != np.float32:
            frame = frame.astype(np.float32)
        self._audio_queue.put(frame)
        if self.parent and eventpoint:
            self.parent.notify(eventpoint)

    # ─── WS client management (gọi từ aiohttp WS route handler) ────────────
    def register_client(self, send_callback: Callable[[bytes], None]) -> None:
        """Add WS client. send_callback(bytes) sẽ được gọi từ ffmpeg reader thread."""
        with self._clients_lock:
            self._clients.add(send_callback)
            total = len(self._clients)
        logger.info(f"[WSStream] client connected ({total} total)")
        # Flush cached header chunks ngay để decoder bootstrap PAT/PMT/keyframe
        for chunk in list(self._header_buf):
            try:
                send_callback(chunk)
            except Exception:
                with self._clients_lock:
                    self._clients.discard(send_callback)
                break

    def unregister_client(self, send_callback: Callable[[bytes], None]) -> None:
        with self._clients_lock:
            self._clients.discard(send_callback)
            total = len(self._clients)
        logger.info(f"[WSStream] client disconnected ({total} left)")

    # ─── Cleanup ───────────────────────────────────────────────────────────
    def _cleanup_proc(self) -> None:
        self._stop.set()
        # Video = stdin pipe; Audio = TCP socket
        if self._video_sock is not None:
            try: self._video_sock.close()
            except Exception: pass
            self._video_sock = None
        if self._audio_sock is not None:
            try: self._audio_sock.close()
            except Exception: pass
            self._audio_sock = None
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try: self._proc.kill()
                except Exception: pass
            self._proc = None

    def stop(self) -> None:
        self._cleanup_proc()
        logger.info("[WSStream] output stopped")
