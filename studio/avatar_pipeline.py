"""Avatar pipeline: upload → preprocess (musetalk|wav2lip) → train (musetalk) → preview.

Preprocess + train chạy trong subprocess để không chiếm GPU của server inference.
Subprocess emit JSON-line progress trên stdout: `{"progress":0.4, "loss":0.01, "msg":"..."}`
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Optional

from .job_registry import get_registry

log = logging.getLogger(__name__)


def _avatar_dir(avatar_id: str) -> str:
    return os.path.join("data", "avatars", avatar_id)


def list_avatars() -> list[dict]:
    base = os.path.join("data", "avatars")
    if not os.path.isdir(base):
        return []
    out = []
    for entry in sorted(os.listdir(base)):
        adir = os.path.join(base, entry)
        if not os.path.isdir(adir):
            continue
        out.append({
            "id": entry,
            "name": entry,
            "has_full_imgs": os.path.isdir(os.path.join(adir, "full_imgs")),
            "has_latents": os.path.exists(os.path.join(adir, "latents.pt")),
            "has_coords": os.path.exists(os.path.join(adir, "coords.pkl")),
            "has_mask": os.path.isdir(os.path.join(adir, "mask")),
            "has_face_imgs": os.path.isdir(os.path.join(adir, "face_imgs")),
            "has_voice": os.path.exists(os.path.join(adir, "voice", "ref.wav")),
            "has_gestures": os.path.exists(os.path.join(adir, "gestures.json")),
        })
    return out


async def save_uploaded_video(file_bytes: bytes, filename: str, avatar_id: str, workdir: str) -> str:
    raw_dir = os.path.join(workdir, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    safe_name = avatar_id + os.path.splitext(filename)[1]
    out_path = os.path.join(raw_dir, safe_name)
    with open(out_path, "wb") as f:
        f.write(file_bytes)
    # Đảm bảo thư mục avatar cũng tồn tại để các bước sau ghi vào
    os.makedirs(_avatar_dir(avatar_id), exist_ok=True)
    return out_path


# ─── Subprocess job runner ─────────────────────────────────────────────

async def _run_subprocess_job(jid: str, args: list[str], cwd: Optional[str] = None) -> int:
    """Spawn subprocess, parse JSON-line trên stdout làm progress event.

    Trả về returncode. Update job state qua registry.
    """
    reg = get_registry()
    await reg.update(jid, state="running", progress=0.0)
    log.info("[Studio] starting job %s: %s", jid, " ".join(args))

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    reg.attach_proc(jid, proc)

    try:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                continue
            if not line:
                continue
            reg.log(jid, line)
            # Try parse JSON progress
            if line.startswith("{") and line.endswith("}"):
                try:
                    msg = json.loads(line)
                    update_fields = {}
                    if "progress" in msg:
                        update_fields["progress"] = float(msg["progress"])
                    if "msg" in msg or "loss" in msg or "eta_s" in msg or "step" in msg:
                        update_fields["meta"] = {
                            **(reg.get(jid).meta if reg.get(jid) else {}),
                            **{k: msg[k] for k in ("msg", "loss", "eta_s", "step", "epoch", "total") if k in msg},
                        }
                    if update_fields:
                        await reg.update(jid, **update_fields)
                except Exception:
                    pass
        rc = await proc.wait()
    except asyncio.CancelledError:
        proc.terminate()
        await reg.update(jid, state="cancelled", error="cancelled")
        raise

    if rc == 0:
        await reg.update(jid, state="done", progress=1.0)
    else:
        await reg.update(jid, state="failed", error=f"exit code {rc}")
    return rc


# ─── Public API ────────────────────────────────────────────────────────

async def preprocess(avatar_id: str, model: str, raw_video: str, workdir: str) -> str:
    reg = get_registry()
    job = reg.create("avatar_preprocess", meta={"avatar_id": avatar_id, "model": model, "video": raw_video})
    args = [
        sys.executable, "-u", "-m", "studio.workers.preprocess_avatar",
        "--avatar_id", avatar_id,
        "--model", model,
        "--video", raw_video,
    ]
    asyncio.create_task(_run_subprocess_job(job.id, args))
    return job.id


async def train(avatar_id: str, model: str, epochs: int, workdir: str) -> str:
    if model != "musetalk":
        raise ValueError(f"Training chỉ hỗ trợ musetalk (model={model!r})")
    reg = get_registry()
    job = reg.create("avatar_train", meta={"avatar_id": avatar_id, "model": model, "epochs": epochs})
    args = [
        sys.executable, "-u", "-m", "studio.workers.train_musetalk",
        "--avatar_id", avatar_id,
        "--epochs", str(epochs),
    ]
    asyncio.create_task(_run_subprocess_job(job.id, args))
    return job.id


def delete_avatar(avatar_id: str) -> bool:
    adir = _avatar_dir(avatar_id)
    if not os.path.isdir(adir):
        return False
    try:
        shutil.rmtree(adir)
        return True
    except Exception:
        log.exception("delete_avatar")
        return False
