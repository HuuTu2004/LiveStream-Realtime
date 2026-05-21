"""Fix musetalk latents.pt to use 8-channel concat (masked + ref) via vae.get_latents_for_unet.

Run on instance:
  python -m scripts.vastai.fix_musetalk_latents --avatar_id mau
"""
from __future__ import annotations

import argparse
import glob
import os
import pickle
import sys

import cv2
import numpy as np
import torch
from tqdm import tqdm

from avatars.musetalk.models.vae import VAE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar_id", required=True)
    ap.add_argument("--extra_margin", type=int, default=10, help="v15 y2 extra margin")
    args = ap.parse_args()

    avatar_dir = os.path.join("data", "avatars", args.avatar_id)
    full_dir = os.path.join(avatar_dir, "full_imgs")
    coords_path = os.path.join(avatar_dir, "coords.pkl")
    mask_dir = os.path.join(avatar_dir, "mask")
    latents_path = os.path.join(avatar_dir, "latents.pt")
    mask_coords_path = os.path.join(avatar_dir, "mask_coords.pkl")

    if not os.path.exists(coords_path):
        print(f"[ERR] missing {coords_path} — run preprocess_avatar first", file=sys.stderr)
        sys.exit(1)

    with open(coords_path, "rb") as f:
        coords = pickle.load(f)

    img_files = sorted(glob.glob(os.path.join(full_dir, "*.png")))
    print(f"[fix] {len(img_files)} frames, {len(coords)} bboxes")

    os.makedirs(mask_dir, exist_ok=True)
    print("[fix] loading VAE (sd-vae)...")
    vae = VAE(model_path="./models/sd-vae", use_float16=False)
    vae.vae = vae.vae.half().to(vae.device)
    vae._use_float16 = True

    latents_cycle = []
    mask_coords_cycle = []

    for idx, (img_path, bbox) in enumerate(tqdm(zip(img_files, coords), total=len(img_files), desc="VAE 8-ch encode")):
        frame = cv2.imread(img_path)
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = bbox
        # v15 extra margin
        y2 = min(y2 + args.extra_margin, h)
        coords[idx] = [x1, y1, x2, y2]

        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        resized = cv2.resize(crop, (256, 256), interpolation=cv2.INTER_LANCZOS4)

        with torch.no_grad():
            latent = vae.get_latents_for_unet(resized)  # [1, 8, 32, 32] on cuda
        latents_cycle.append(latent)  # keep on GPU — pipeline expects device match w/ UNet

        # Dummy white mask (full crop region) — face-parse-bisent would be nicer but optional
        mask = np.full((y2 - y1, x2 - x1), 255, dtype=np.uint8)
        mask_filename = os.path.join(mask_dir, f"{idx:08d}.png")
        cv2.imwrite(mask_filename, mask)
        mask_coords_cycle.append([x1, y1, x2, y2])

    torch.save(latents_cycle, latents_path)
    with open(mask_coords_path, "wb") as f:
        pickle.dump(mask_coords_cycle, f)
    with open(coords_path, "wb") as f:
        pickle.dump(coords, f)

    print(f"[fix] saved latents ({len(latents_cycle)}) → {latents_path}")
    print(f"[fix] saved mask images → {mask_dir}")
    print(f"[fix] saved mask_coords → {mask_coords_path}")
    print(f"[fix] each latent shape: {latents_cycle[0].shape if latents_cycle else 'EMPTY'}")


if __name__ == "__main__":
    main()
