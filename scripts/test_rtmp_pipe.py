#!/usr/bin/env python3
"""Standalone test cho rtmp.py logic — feed synthetic video+audio qua pipe vào
ffmpeg subprocess, verify RTMP push tới MediaMTX.

V2: pre-fill 10s silence + audio write trong thread riêng để không block video.
"""

import os
import subprocess
import sys
import threading
import time

import numpy as np


def stderr_reader(proc):
    for raw in proc.stderr:
        line = raw.decode('utf-8', errors='replace').rstrip()
        if line:
            print(f"[ffmpeg] {line}", flush=True)


def audio_writer(audio_fd: int, sr: int, stop_event: threading.Event):
    """Liên tục đẩy silence vào audio pipe để ffmpeg không stall."""
    chunk_samples = sr // 25  # 40ms chunks = 1 video frame worth
    silence = np.zeros(chunk_samples, dtype=np.float32).tobytes()
    next_t = time.perf_counter()
    written = 0
    while not stop_event.is_set():
        try:
            os.write(audio_fd, silence)
            written += chunk_samples
        except BrokenPipeError:
            print(f"[audio-thread] BrokenPipe after {written} samples", flush=True)
            return
        next_t += chunk_samples / sr
        delay = next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    print(f"[audio-thread] stopped after {written} samples", flush=True)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else 'rtmp://localhost:1935/live/test'
    w, h = 576, 768
    fps = 25
    sr = 16000

    v_r, v_w = os.pipe()
    a_r, a_w = os.pipe()

    try:
        import fcntl
        F_SETPIPE_SZ = 1031
        fcntl.fcntl(v_w, F_SETPIPE_SZ, 1024 * 1024)
        fcntl.fcntl(a_w, F_SETPIPE_SZ, 1024 * 1024)
    except Exception as e:
        print(f"[warn] F_SETPIPE_SZ failed: {e}")

    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'info',
        '-fflags', '+nobuffer',
        '-probesize', '32',
        '-analyzeduration', '0',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps),
        '-thread_queue_size', '1024',
        '-i', f'pipe:{v_r}',
        '-f', 'f32le', '-ar', str(sr), '-ac', '1',
        '-thread_queue_size', '1024',
        '-i', f'pipe:{a_r}',
        '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
        '-pix_fmt', 'yuv420p', '-b:v', '2000000',
        '-g', str(fps * 2),
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100',
        '-f', 'flv', url,
    ]
    print(f"[test] cmd:\n  {' '.join(cmd)}\n", flush=True)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        pass_fds=(v_r, a_r),
    )
    os.close(v_r)
    os.close(a_r)

    threading.Thread(target=stderr_reader, args=(proc,), daemon=True).start()

    # Audio thread liên tục đẩy silence — không bị block bởi video write
    stop_event = threading.Event()
    audio_thread = threading.Thread(
        target=audio_writer, args=(a_w, sr, stop_event), daemon=True
    )
    audio_thread.start()
    print("[test] audio thread started", flush=True)

    # Main thread chỉ ghi video — không bị tranh chấp lock với audio
    start = time.perf_counter()
    for i in range(100):
        if proc.poll() is not None:
            print(f"[test] ffmpeg exited at frame {i}, rc={proc.returncode}", flush=True)
            break
        frame = np.full((h, w, 3), (i * 5) % 255, dtype=np.uint8)
        try:
            os.write(v_w, frame.tobytes())
        except BrokenPipeError:
            print(f"[test] BrokenPipe video frame {i}", flush=True)
            break

        target = start + (i + 1) / fps
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

        if i % 25 == 0 or i == 99:
            print(f"[test] frame {i}, ffmpeg alive={proc.poll() is None}", flush=True)

    elapsed = time.perf_counter() - start
    print(f"[test] done {i+1} frames in {elapsed:.2f}s", flush=True)

    stop_event.set()
    try:
        os.close(v_w)
        os.close(a_w)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
    print(f"[test] ffmpeg rc={proc.returncode}", flush=True)


if __name__ == '__main__':
    main()
