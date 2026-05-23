"""Preprocess a single video file into a MuseTalk avatar directory.

Uses the wav2lip-bundled S3FD face detector (no mmpose dependency).
Outputs:
  data/avatars/{avatar_id}/full_imgs/*.png   (raw frames)
  data/avatars/{avatar_id}/coords.pkl         (bbox per frame)
  data/avatars/{avatar_id}/avator_info.json   (metadata)

After this, run:
  python -m scripts.vastai.fix_musetalk_latents --avatar_id {avatar_id}
to fill in latents.pt + mask/ + mask_coords.pkl.

Usage:
  python -m scripts.vastai.preprocess_mau_avatar \\
      --video data/uploads/mau.mp4 --avatar_id mau
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

import cv2
import numpy as np
import torch
from tqdm import tqdm

from avatars.wav2lip import face_detection


def video2imgs(vid_path: str, save_path: str) -> int:
    cap = cv2.VideoCapture(vid_path)
    count = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        cv2.imwrite(f"{save_path}/{count:08d}.png", frame)
        count += 1
    cap.release()
    return count


def get_smoothened_boxes(boxes: np.ndarray, T: int = 5) -> np.ndarray:
    out = boxes.copy().astype(np.float64)
    n = len(boxes)
    for i in range(n):
        end = min(i + T, n)
        window = boxes[i:end] if end - i == T else boxes[max(0, n - T):n]
        out[i] = np.mean(window, axis=0)
    return out.astype(np.int64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, help="Input video file")
    ap.add_argument("--avatar_id", required=True)
    ap.add_argument("--pads", nargs=4, type=int, default=[0, 10, 0, 0],
                    help="Pad top bottom left right around bbox")
    ap.add_argument("--batch_size", type=int, default=16)
    ap.add_argument("--no_smooth", action="store_true")
    args = ap.parse_args()

    avatar_dir = os.path.join("data", "avatars", args.avatar_id)
    full_dir = os.path.join(avatar_dir, "full_imgs")
    os.makedirs(full_dir, exist_ok=True)

    print(f"[prep] extract frames {args.video} → {full_dir}")
    n_frames = video2imgs(args.video, full_dir)
    if n_frames == 0:
        print(f"[ERR] no frames extracted from {args.video}", file=sys.stderr)
        return 1
    print(f"[prep] extracted {n_frames} frames")

    img_files = sorted([os.path.join(full_dir, f) for f in os.listdir(full_dir) if f.endswith(".png")])
    images = [cv2.imread(p) for p in tqdm(img_files, desc="read frames")]

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[prep] face detection on {device} (S3FD)")
    detector = face_detection.FaceAlignment(
        face_detection.LandmarksType._2D, flip_input=False, device=device
    )

    bsz = args.batch_size
    preds = []
    while True:
        try:
            for i in tqdm(range(0, len(images), bsz), desc=f"face detect (bs={bsz})"):
                batch = np.array(images[i:i + bsz])
                preds.extend(detector.get_detections_for_batch(batch))
            break
        except RuntimeError as e:
            if bsz == 1:
                raise RuntimeError(f"OOM at batch_size=1: {e}")
            bsz //= 2
            preds = []
            print(f"[prep] OOM → retry batch_size={bsz}")

    pady1, pady2, padx1, padx2 = args.pads
    boxes = []
    missing = 0
    for rect, img in zip(preds, images):
        if rect is None:
            missing += 1
            boxes.append([0, 0, 0, 0])
            continue
        h, w = img.shape[:2]
        x1, y1, x2, y2 = rect
        y1 = max(0, y1 - pady1)
        y2 = min(h, y2 + pady2)
        x1 = max(0, x1 - padx1)
        x2 = min(w, x2 + padx2)
        boxes.append([x1, y1, x2, y2])

    if missing > 0:
        print(f"[WARN] {missing}/{len(images)} frames có không face detected")

    boxes_arr = np.array(boxes, dtype=np.int64)
    if not args.no_smooth and len(boxes_arr) >= 5:
        boxes_arr = get_smoothened_boxes(boxes_arr, T=5)

    coords = [tuple(b.tolist()) for b in boxes_arr]
    coords_path = os.path.join(avatar_dir, "coords.pkl")
    with open(coords_path, "wb") as f:
        pickle.dump(coords, f)
    print(f"[prep] saved {len(coords)} coords → {coords_path}")

    info_path = os.path.join(avatar_dir, "avator_info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({
            "avatar_id": args.avatar_id,
            "video_path": args.video,
            "bbox_shift": 0,
            "frames": n_frames,
        }, f, ensure_ascii=False, indent=2)
    print(f"[prep] saved metadata → {info_path}")

    del detector
    torch.cuda.empty_cache() if torch.cuda.is_available() else None

    print("[prep] DONE. Next:")
    print(f"  python -m scripts.vastai.fix_musetalk_latents --avatar_id {args.avatar_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
