"""JobRegistry — quản lý training/preprocess jobs với async progress stream.

Mỗi job có:
- id (uuid)
- kind (avatar_preprocess / avatar_train / voice_validate / gesture_extract)
- state: pending|running|done|failed|cancelled
- progress: 0.0-1.0
- meta: dict (loss, eta_s, message...)
- log_tail: deque[str] — N log line gần nhất
- subscribers: asyncio.Queue list — push progress events realtime
- proc: subprocess.Popen | None — process gốc (cho heavy jobs)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass
class JobState:
    id: str
    kind: str
    state: str = "pending"  # pending | running | done | failed | cancelled
    progress: float = 0.0
    meta: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class JobRegistry:
    """In-memory registry, persist snapshot ra disk khi update để survive restart."""

    def __init__(self, workdir: str = "data/uploads"):
        self.workdir = workdir
        self.jobs_dir = os.path.join(workdir, "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        self._jobs: dict[str, JobState] = {}
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._log_tails: dict[str, deque] = {}
        self._procs: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    def create(self, kind: str, meta: Optional[dict] = None) -> JobState:
        jid = uuid.uuid4().hex[:12]
        job = JobState(id=jid, kind=kind, meta=meta or {})
        self._jobs[jid] = job
        self._log_tails[jid] = deque(maxlen=500)
        self._subs[jid] = []
        self._persist(job)
        return job

    def get(self, jid: str) -> Optional[JobState]:
        return self._jobs.get(jid)

    def list(self) -> list[dict]:
        return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda x: -x.created_at)]

    # ------------------------------------------------------------------
    def _persist(self, job: JobState) -> None:
        try:
            with open(os.path.join(self.jobs_dir, f"{job.id}.json"), "w", encoding="utf-8") as f:
                json.dump(job.to_dict(), f, ensure_ascii=False)
        except Exception:
            log.exception("[Job] persist failed: %s", job.id)

    async def update(self, jid: str, **fields) -> None:
        job = self._jobs.get(jid)
        if not job:
            return
        for k, v in fields.items():
            if hasattr(job, k):
                setattr(job, k, v)
            else:
                job.meta[k] = v
        job.updated_at = time.time()
        self._persist(job)
        await self._broadcast(jid, {"event": "update", "data": job.to_dict()})

    def log(self, jid: str, line: str) -> None:
        tail = self._log_tails.get(jid)
        if tail is not None:
            tail.append(line)
        # Broadcast log line (best-effort, không await)
        for q in self._subs.get(jid, []):
            try:
                q.put_nowait({"event": "log", "line": line})
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    async def subscribe(self, jid: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        async with self._lock:
            self._subs.setdefault(jid, []).append(q)
        # Replay state + tail
        job = self._jobs.get(jid)
        if job:
            await q.put({"event": "update", "data": job.to_dict()})
        for line in list(self._log_tails.get(jid, [])):
            await q.put({"event": "log", "line": line})
        return q

    async def unsubscribe(self, jid: str, q: asyncio.Queue) -> None:
        async with self._lock:
            if jid in self._subs and q in self._subs[jid]:
                self._subs[jid].remove(q)

    async def _broadcast(self, jid: str, event: dict) -> None:
        for q in list(self._subs.get(jid, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    def attach_proc(self, jid: str, proc) -> None:
        self._procs[jid] = proc

    def cancel(self, jid: str) -> bool:
        proc = self._procs.get(jid)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                return True
            except Exception:
                pass
        return False


# ─── Global singleton ──────────────────────────────────────────────────
_registry: Optional[JobRegistry] = None


def init_registry(workdir: str) -> JobRegistry:
    global _registry
    if _registry is None:
        _registry = JobRegistry(workdir=workdir)
    return _registry


def get_registry() -> JobRegistry:
    if _registry is None:
        return init_registry("data/uploads")
    return _registry
