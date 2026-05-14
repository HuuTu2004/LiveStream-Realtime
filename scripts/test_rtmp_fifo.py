#!/usr/bin/env python3
"""Test: video stdin + audio named FIFO. Cleaner pattern hơn pass_fds."""

import os
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np


def stderr_reader(proc):
    for raw in proc.stderr:
        line = raw.decode('utf-8', errors='replace').rstrip()
        if line:
            print(f"[ffmpeg] {line}", flush=True)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else 'rtmp://localhost:1935/live/test_fifo'
    w, h = 576, 768
    fps = 25
    sr = 16000

    # Tạo named FIFO cho audio
    fifo_path = tempfile.mktemp(suffix='.fifo')
    os.mkfifo(fifo_path)
    print(f"[test] audio FIFO: {fifo_path}", flush=True)

    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'info',
        '-fflags', '+nobuffer',
        '-probesize', '32',
        '-analyzeduration', '0',
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps),
        '-thread_queue_size', '1024',
        '-i', 'pipe:0',  # video qua stdin
        '-f', 'f32le', '-ar', str(sr), '-ac', '1',
        '-thread_queue_size', '1024',
        '-i', fifo_path,  # audio qua named FIFO
        '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
        '-pix_fmt', 'yuv420p', '-b:v', '2000000',
        '-g', str(fps * 2),
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100',
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

    # Mở FIFO write — blocking until ffmpeg open read
    print("[test] opening FIFO for write (blocks until ffmpeg reader ready)...", flush=True)
    audio_fd = os.open(fifo_path, os.O_WRONLY)
    print(f"[test] FIFO open fd={audio_fd}", flush=True)

    # Pre-fill 0.5s silence
    prefill = np.zeros(sr // 2, dtype=np.float32)
    os.write(audio_fd, prefill.tobytes())
    audio_samples = len(prefill)
    print(f"[test] prefilled {audio_samples} silence samples", flush=True)

    start = time.perf_counter()
    for i in range(100):
        if proc.poll() is not None:
            print(f"[test] ffmpeg exited at frame {i}, rc={proc.returncode}", flush=True)
            break
        frame = np.full((h, w, 3), (i * 5) % 255, dtype=np.uint8)
        try:
            proc.stdin.write(frame.tobytes())
            proc.stdin.flush()
        except BrokenPipeError:
            print(f"[test] BrokenPipe video frame {i}", flush=True)
            break

        # Audio sync pad
        expected = int((i + 1) * sr / fps)
        pad = expected - audio_samples
        if pad > 0:
            silence = np.zeros(pad, dtype=np.float32)
            try:
                os.write(audio_fd, silence.tobytes())
                audio_samples += pad
            except BrokenPipeError:
                print(f"[test] BrokenPipe audio frame {i}", flush=True)
                break

        target = start + (i + 1) / fps
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

        if i % 25 == 0 or i == 99:
            print(f"[test] frame {i}, audio_samples={audio_samples}", flush=True)

    elapsed = time.perf_counter() - start
    print(f"[test] done {i+1} frames in {elapsed:.2f}s", flush=True)

    try:
        proc.stdin.close()
        os.close(audio_fd)
    except OSError:
        pass

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
    print(f"[test] ffmpeg rc={proc.returncode}", flush=True)

    try:
        os.unlink(fifo_path)
    except OSError:
        pass


if __name__ == '__main__':
    main()
