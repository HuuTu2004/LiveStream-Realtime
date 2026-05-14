"""Preprocess avatar video → frames + landmarks + (musetalk) latents.

CLI:
  python -m studio.workers.preprocess_avatar --avatar_id NAME --model musetalk --video data/uploads/raw/NAME.mp4

Emit JSON progress lines:
  {"progress": 0.4, "msg": "extracting frames", "step": 120, "total": 300}

Cấu trúc output:
  data/avatars/{avatar_id}/
    full_imgs/00000000.png ...       # tất cả model dùng
    coords.pkl                        # bbox landmarks
    face_imgs/00000000.png ...       # wav2lip: cropped face 256x256
    latents.pt + mask/ + mask_coords.pkl  # musetalk only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import pickle
import subprocess

import cv2
import numpy as np


def emit(progress: float, msg: str = "", **kw):
    payload = {"progress": float(max(0.0, min(1.0, progress))), "msg": msg}
    payload.update(kw)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def extract_frames(video: str, out_dir: str, fps: int = 25) -> int:
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", video,
        "-vf", f"fps={fps}",
        "-q:v", "2",
        os.path.join(out_dir, "%08d.png"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[:400]}")
    return len([f for f in os.listdir(out_dir) if f.endswith(".png")])


def detect_faces_and_save_coords(frames_dir: str, coords_path: str) -> list:
    """Dùng mediapipe để detect face bbox → coords.pkl format compatible với LiveTalking."""
    try:
        import mediapipe as mp
    except ImportError:
        emit(0.5, "mediapipe không có — bbox sẽ là full frame")
        files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
        coords = []
        for f in files:
            img = cv2.imread(os.path.join(frames_dir, f))
            h, w = img.shape[:2]
            coords.append([0, 0, w, h])
        with open(coords_path, "wb") as fp:
            pickle.dump(coords, fp)
        return coords

    mp_face = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.5)
    files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
    coords = []
    last_box = None
    for i, f in enumerate(files):
        img = cv2.imread(os.path.join(frames_dir, f))
        h, w = img.shape[:2]
        res = mp_face.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        if res.detections:
            d = res.detections[0]
            rb = d.location_data.relative_bounding_box
            x1 = max(0, int(rb.xmin * w))
            y1 = max(0, int(rb.ymin * h))
            x2 = min(w, int((rb.xmin + rb.width) * w))
            y2 = min(h, int((rb.ymin + rb.height) * h))
            # mở rộng 1.3x cho phù hợp lipsync
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            half = int(max(x2 - x1, y2 - y1) * 0.65)
            x1 = max(0, cx - half)
            y1 = max(0, cy - half)
            x2 = min(w, cx + half)
            y2 = min(h, cy + half)
            last_box = [x1, y1, x2, y2]
        if last_box is None:
            last_box = [0, 0, w, h]
        coords.append(last_box)
        if (i + 1) % 25 == 0 or (i + 1) == len(files):
            emit(0.5 + 0.3 * (i + 1) / len(files), "detecting face", step=i + 1, total=len(files))
    with open(coords_path, "wb") as fp:
        pickle.dump(coords, fp)
    return coords


def make_face_crops(frames_dir: str, coords: list, out_dir: str, size: int = 256) -> int:
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
    for i, (f, box) in enumerate(zip(files, coords)):
        img = cv2.imread(os.path.join(frames_dir, f))
        x1, y1, x2, y2 = box
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        crop = cv2.resize(crop, (size, size))
        cv2.imwrite(os.path.join(out_dir, f), crop)
        if (i + 1) % 25 == 0:
            emit(0.8 + 0.15 * (i + 1) / len(files), "cropping faces", step=i + 1, total=len(files))
    return len(files)


def musetalk_prepare_latents(frames_dir: str, coords: list, avatar_dir: str) -> None:
    """MuseTalk-specific: encode VAE latents + tạo mask cycle."""
    emit(0.85, "preparing musetalk latents (VAE encode)")
    try:
        # Lazy import — chỉ khi cần MuseTalk
        from avatars.musetalk.utils.utils import load_all_model
        import torch

        vae, _, _ = load_all_model()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        vae.vae = vae.vae.half().to(device)

        files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
        latents = []
        mask_dir = os.path.join(avatar_dir, "mask")
        os.makedirs(mask_dir, exist_ok=True)
        mask_coords = []

        for i, (f, box) in enumerate(zip(files, coords)):
            img = cv2.imread(os.path.join(frames_dir, f))
            x1, y1, x2, y2 = box
            face = img[y1:y2, x1:x2]
            if face.size == 0:
                continue
            face_resized = cv2.resize(face, (256, 256))
            # Encode → latent
            face_rgb = cv2.cvtColor(face_resized, cv2.COLOR_BGR2RGB)
            face_t = torch.from_numpy(face_rgb).permute(2, 0, 1).unsqueeze(0).float() / 127.5 - 1
            face_t = face_t.half().to(device)
            with torch.no_grad():
                latent = vae.vae.encode(face_t).latent_dist.sample() * 0.18215
            latents.append(latent.cpu())
            # Lưu mask đơn giản (full white face)
            mask = np.full((face.shape[0], face.shape[1]), 255, dtype=np.uint8)
            cv2.imwrite(os.path.join(mask_dir, f), mask)
            mask_coords.append([x1, y1, x2, y2])
            if (i + 1) % 25 == 0:
                emit(0.85 + 0.13 * (i + 1) / len(files), "vae encoding", step=i + 1, total=len(files))

        torch.save(latents, os.path.join(avatar_dir, "latents.pt"))
        with open(os.path.join(avatar_dir, "mask_coords.pkl"), "wb") as fp:
            pickle.dump(mask_coords, fp)
    except Exception as e:
        emit(0.85, f"musetalk prep error (sẽ dùng frame-only mode): {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar_id", required=True)
    ap.add_argument("--model", required=True, choices=["musetalk", "wav2lip", "ultralight"])
    ap.add_argument("--video", required=True)
    ap.add_argument("--fps", type=int, default=25)
    args = ap.parse_args()

    if not os.path.exists(args.video):
        emit(0.0, f"video not found: {args.video}")
        sys.exit(1)

    avatar_dir = os.path.join("data", "avatars", args.avatar_id)
    os.makedirs(avatar_dir, exist_ok=True)

    emit(0.05, "extracting frames")
    full_dir = os.path.join(avatar_dir, "full_imgs")
    n_frames = extract_frames(args.video, full_dir, fps=args.fps)
    emit(0.4, f"extracted {n_frames} frames", total_frames=n_frames)

    emit(0.45, "detecting faces")
    coords = detect_faces_and_save_coords(full_dir, os.path.join(avatar_dir, "coords.pkl"))

    if args.model in ("wav2lip", "ultralight"):
        face_dir = os.path.join(avatar_dir, "face_imgs")
        make_face_crops(full_dir, coords, face_dir, size=256 if args.model == "wav2lip" else 160)

    if args.model == "musetalk":
        musetalk_prepare_latents(full_dir, coords, avatar_dir)

    # Lưu avatar_info
    with open(os.path.join(avatar_dir, "avator_info.json"), "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "fps": args.fps, "frames": n_frames}, f)

    emit(1.0, f"done: {args.avatar_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        emit(0.0, f"FAILED: {e}")
        raise
