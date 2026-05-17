"""Patched FaceParsing — bypasses 79999_iter.pth (Google Drive blocked).
Generates a TIGHT elliptical alpha mask covering only the mouth+chin region.

Replaces the bisenet-based FaceParsing — skips ML segmentation, uses
geometric mask focused on mouth/chin (excludes neck/shoulders to avoid
shaking at the mask boundary).
"""
import os
import numpy as np
import cv2
from PIL import Image


class FaceParsing:
    def __init__(self, left_cheek_width=90, right_cheek_width=90, **kwargs):
        self.left_cheek_width = left_cheek_width
        self.right_cheek_width = right_cheek_width

    def __call__(self, image, size=(512, 512), mode="raw"):
        if isinstance(image, str):
            image = Image.open(image)
        if isinstance(image, np.ndarray):
            image = Image.fromarray(image)
        w, h = image.size
        # TIGHT mask — chỉ vùng MIỆNG + CẰM (không bao cổ).
        # Trong expanded crop (expand=1.5), face_box ở giữa, mouth ≈ y=0.62-0.78.
        # cy=0.65, ay=0.15 → ellipse bottom = 0.80h (chin) — EXCLUDE neck (>0.83h).
        # ax=0.32 → width = 64% crop, vừa khít mouth+chin contour.
        mask = np.zeros((h, w), dtype=np.uint8)
        cx, cy = w // 2, int(h * 0.65)
        ax, ay = int(w * 0.32), int(h * 0.15)
        cv2.ellipse(mask, (cx, cy), (ax, ay), 0, 0, 360, 255, -1)
        # Feather nhẹ — đủ smooth seam, không bleed quá rộng.
        blur_k = max(11, (min(w, h) // 10) | 1)
        mask = cv2.GaussianBlur(mask, (blur_k, blur_k), 0)
        return Image.fromarray(mask)
