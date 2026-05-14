#!/usr/bin/env python3
"""Standalone test cho rtmp.py logic — feed synthetic video+audio qua pipe vào
ffmpeg subprocess, verify RTMP push tới MediaMTX.

Chạy:
    cd /workspace/LiveTalking
    python scripts/test_rtmp_pipe.py rtmp://localhost:1935/live/test

Nếu MediaMTX hiển thị "is publishing to path 'live/test'" + stay online → OK.
Nếu close: EOF nhanh → cấu hình ffmpeg/pipe có vấn đề.
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


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else 'rtmp://localhost:1935/live/test'
    w, h = 576, 768
    fps = 25
    sr = 16000

    # 2 OS pipes
    v_r, v_w = os.pipe()
    a_r, a_w = os.pipe()

    # 1MB pipe buffers
    try:
        import fcntl
        F_SETPIPE_SZ = 1031
        fcntl.fcntl(v_w, F_SETPIPE_SZ, 1024 * 1024)
        fcntl.fcntl(a_w, F_SETPIPE_SZ, 1024 * 1024)
    except Exception as e:
        print(f"[warn] F_SETPIPE_SZ failed: {e}")

    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'info',
        '-fflags', '+nobuffer+flush_packets',
        '-flags', 'low_delay',
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
        '-flush_packets', '1',
        '-f', 'flv', url,
    ]
    print(f"[test] cmd:\n  {' '.join(cmd)}\n")

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

    # Pre-fill 0.5s silence
    prefill = np.zeros(sr // 2, dtype=np.float32)
    os.write(a_w, prefill.tobytes())
    print(f"[test] prefilled {len(prefill)} silence samples")
    audio_samples_written = len(prefill)

    # Generate 100 synthetic frames (4s @ 25fps)
    samples_per_frame = sr // fps  # 640
    start = time.perf_counter()
    for i in range(100):
        if proc.poll() is not None:
            print(f"[test] ffmpeg exited early at frame {i}, returncode={proc.returncode}")
            break
        # Synthetic frame: gradient based on i
        frame = np.full((h, w, 3), i * 2 % 255, dtype=np.uint8)
        try:
            os.write(v_w, frame.tobytes())
        except BrokenPipeError:
            print(f"[test] BrokenPipeError on video at frame {i}")
            break

        # Audio sync: pad silence to match video time
        expected = int((i + 1) * sr / fps)
        pad = expected - audio_samples_written
        if pad > 0:
            silence = np.zeros(pad, dtype=np.float32)
            try:
                os.write(a_w, silence.tobytes())
                audio_samples_written += pad
            except BrokenPipeError:
                print(f"[test] BrokenPipeError on audio at frame {i}")
                break

        # Frame pacing
        target_time = start + (i + 1) / fps
        delay = target_time - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

        if i % 25 == 0:
            print(f"[test] frame {i}, ffmpeg alive={proc.poll() is None}")

    elapsed = time.perf_counter() - start
    print(f"\n[test] done. {i+1} frames in {elapsed:.2f}s ({(i+1)/elapsed:.1f} fps)")

    # Close pipes + wait
    try:
        os.close(v_w)
        os.close(a_w)
    except OSError:
        pass

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=5)

    print(f"[test] ffmpeg final returncode={proc.returncode}")


if __name__ == '__main__':
    main()
