#!/usr/bin/env python3
"""Simplest possible test: video-only RTMP push qua stdin.

So với test_rtmp_pipe.py: bỏ audio (single input), dùng stdin (pipe:0)
thay vì pass_fds. Loại biến số cho ffmpeg 2-input mux logic.
"""

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

    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'info',
        '-fflags', '+nobuffer',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps),
        '-i', 'pipe:0',
        '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
        '-pix_fmt', 'yuv420p', '-b:v', '2000000',
        '-g', str(fps * 2),
        '-an',  # NO audio
        '-f', 'flv', url,
    ]
    print(f"[test] cmd:\n  {' '.join(cmd)}\n", flush=True)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    threading.Thread(target=stderr_reader, args=(proc,), daemon=True).start()

    start = time.perf_counter()
    for i in range(100):
        if proc.poll() is not None:
            print(f"[test] ffmpeg exited early at frame {i}, rc={proc.returncode}", flush=True)
            break
        frame = np.full((h, w, 3), (i * 5) % 255, dtype=np.uint8)
        try:
            proc.stdin.write(frame.tobytes())
            proc.stdin.flush()
        except BrokenPipeError:
            print(f"[test] BrokenPipe at frame {i}", flush=True)
            break

        target = start + (i + 1) / fps
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

        if i % 25 == 0 or i == 99:
            print(f"[test] frame {i} written, ffmpeg alive={proc.poll() is None}", flush=True)

    print(f"[test] done {i+1} frames in {time.perf_counter()-start:.2f}s", flush=True)
    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
    print(f"[test] ffmpeg rc={proc.returncode}", flush=True)


if __name__ == '__main__':
    main()
