"""Gesture pipeline: extract MP4 clip → frames PNG + update gestures.json."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger(__name__)


def _manifest_path(avatar_id: str) -> str:
    return os.path.join("data", "avatars", avatar_id, "gestures.json")


def load_manifest(avatar_id: str) -> dict:
    p = _manifest_path(avatar_id)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("[Gesture] read manifest")
        return {}


def save_manifest(avatar_id: str, manifest: dict) -> None:
    p = _manifest_path(avatar_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def extract_clip(
    avatar_id: str,
    mp4_bytes: bytes,
    gesture_name: str,
    fps: int = 25,
    loop: bool = False,
    blend: int = 5,
) -> dict:
    """Extract frames bằng ffmpeg → data/avatars/{id}/gestures/{name}/*.png.

    Cập nhật gestures.json với entry mới.
    """
    if not gesture_name or not gesture_name.isidentifier():
        raise ValueError(f"gesture_name không hợp lệ: {gesture_name!r}")

    out_dir = os.path.join("data", "avatars", avatar_id, "gestures", gesture_name)
    os.makedirs(out_dir, exist_ok=True)
    # Clear cũ nếu có
    for f in os.listdir(out_dir):
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass

    # Lưu MP4 tạm
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(mp4_bytes)
        tmp_path = tmp.name

    try:
        # ffmpeg -i clip.mp4 -vf fps=25 out_dir/%08d.png
        cmd = [
            "ffmpeg", "-y", "-i", tmp_path,
            "-vf", f"fps={fps}",
            "-pix_fmt", "rgb24",
            os.path.join(out_dir, "%08d.png"),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    n_frames = len([f for f in os.listdir(out_dir) if f.endswith(".png")])
    if n_frames == 0:
        raise RuntimeError("Không trích xuất được frame nào từ clip")

    # Update manifest
    manifest = load_manifest(avatar_id)
    manifest[gesture_name] = {
        "frames": n_frames,
        "loop": bool(loop),
        "fps": fps,
        "blend": int(blend),
    }
    save_manifest(avatar_id, manifest)

    return {
        "avatar_id": avatar_id,
        "gesture": gesture_name,
        "frames": n_frames,
        "manifest_entry": manifest[gesture_name],
    }


def delete_gesture(avatar_id: str, gesture_name: str) -> bool:
    gdir = os.path.join("data", "avatars", avatar_id, "gestures", gesture_name)
    if os.path.isdir(gdir):
        shutil.rmtree(gdir, ignore_errors=True)
    manifest = load_manifest(avatar_id)
    if gesture_name in manifest:
        manifest.pop(gesture_name)
        save_manifest(avatar_id, manifest)
        return True
    return False
