"""LiveManager — orchestrator cho 1 phiên livestream bán hàng.

Trách nhiệm:
- Khởi động/tắt platform listener (TikTok / Facebook / YouTube)
- Khởi động/tắt BrainManager
- Aggregate stats từ listener + brain để UI hiển thị 1 chỗ
- Buffer comment realtime cho UI feed

1 LiveSession <=> 1 avatar_session <=> 1 BrainManager <=> 1 platform listener.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .brain_manager import BrainManager
    from avatars.base_avatar import BaseAvatar

log = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = ("tiktok",)

# Persistent live state — survives app.py restart.
_STATE_DIR = Path("data/uploads")
_STATE_FILE = _STATE_DIR / "live_state.json"


def _save_state_disk(state: dict) -> None:
    """Atomic write live state (running session map) to disk."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception:
        log.exception("[Live] save state disk")


def _load_state_disk() -> dict:
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("[Live] load state disk")
        return {}


class LiveManager:
    """Orchestrator cho 1 sessionid."""

    def __init__(self, opt, avatar_session: "BaseAvatar"):
        self.opt = opt
        self.avatar_session = avatar_session
        self.sessionid = getattr(avatar_session, "sessionid", "default")
        self._brain: Optional["BrainManager"] = None
        self._listener = None
        self._platform: str = ""
        self._live_id: str = ""
        self._running = False
        self._lock = asyncio.Lock()
        # Realtime pubsub — each subscriber owns an asyncio.Queue. Listener
        # callback pushes events here; WS handlers fan out to clients.
        self._subscribers: set[asyncio.Queue] = set()

    # ------------------------------------------------------------------
    async def start(self, platform: str, live_id: str) -> dict:
        """Bắt đầu live: spawn brain + platform listener."""
        async with self._lock:
            if self._running:
                return {"error": "đã đang chạy, hãy /live/stop trước"}
            if platform not in SUPPORTED_PLATFORMS:
                return {"error": f"platform '{platform}' chưa hỗ trợ. Choices: {SUPPORTED_PLATFORMS}"}

            # 1) Brain
            from .brain_manager import get_or_create_brain
            self._brain = await get_or_create_brain(self.opt, self.avatar_session)
            await self._brain.start()

            # 2) Platform listener
            if platform == "tiktok":
                from .platforms.tiktok import TikTokListener
                self._listener = TikTokListener(self._brain, live_id)
                # Wire realtime push: listener publishes events into our bus,
                # WS handlers consume from it.
                self._listener.on_event = self.publish
                await self._listener.start()

            self._platform = platform
            self._live_id = live_id
            self._running = True
            log.info("[Live] started session=%s platform=%s live_id=%s",
                     self.sessionid, platform, live_id)
            # Persist session map to disk so app restart can auto-resume.
            self._persist()
            return {"ok": True, "state": self.state()}

    async def stop(self) -> dict:
        async with self._lock:
            if not self._running:
                return {"ok": True, "state": self.state()}
            if self._listener is not None:
                try:
                    await self._listener.stop()
                except Exception:
                    log.exception("[Live] listener stop")
                self._listener = None
            if self._brain is not None:
                try:
                    await self._brain.stop()
                except Exception:
                    log.exception("[Live] brain stop")
            self._running = False
            log.info("[Live] stopped session=%s", self.sessionid)
            self._persist()
            return {"ok": True, "state": self.state()}

    # ------------------------------------------------------------------
    def state(self) -> dict:
        out = {
            "sessionid": self.sessionid,
            "running": self._running,
            "platform": self._platform,
            "live_id": self._live_id,
        }
        if self._listener is not None:
            out["platform_stats"] = self._listener.stats()
        if self._brain is not None:
            out["brain"] = self._brain.state()
        return out

    def recent_comments(self, limit: int = 50) -> list:
        if self._listener is None:
            return []
        return self._listener.recent_comments(limit)

    # ─── Pass-through tới brain (UI có thể gửi comment thủ công khi không có platform) ──
    async def feed_comment_manual(self, username: str, text: str) -> None:
        if self._brain is not None:
            await self._brain.feed_comment(username, text, platform="manual")

    def switch_product(self, product_id: str = "", index: int = -1) -> bool:
        if self._brain is None:
            return False
        ok = self._brain.switch_product(product_id=product_id, index=index)
        if ok:
            # Push state immediately so subscribers see the new on-air
            # without waiting for the 1s snapshot tick.
            self.publish({"event": "state", "data": self.state()})
        return ok

    # ─── Pubsub (push to WS subscribers) ───────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        """Subscribe to realtime events. Caller owns the queue and must
        call unsubscribe() (typically in a finally block)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _persist(self) -> None:
        """Save running session map (sessionid → platform + live_id) to disk."""
        state = _load_state_disk()
        if self._running:
            state[self.sessionid] = {
                "platform": self._platform,
                "live_id": self._live_id,
            }
        else:
            state.pop(self.sessionid, None)
        _save_state_disk(state)

    def publish(self, event: dict) -> None:
        """Non-blocking fan-out. If a subscriber's queue is full (slow client),
        drop the oldest event instead of blocking — better to lose one frame
        than to back-pressure the listener and stall comment ingestion."""
        if not self._subscribers:
            return
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait(event)
                except Exception:
                    log.debug("[Live] failed to publish into subscriber queue")


# ─── Global registry ──────────────────────────────────────────────────
_lives: dict[str, LiveManager] = {}
_lock = asyncio.Lock()


async def get_or_create_live(opt, avatar_session: "BaseAvatar") -> LiveManager:
    sid = getattr(avatar_session, "sessionid", "default")
    async with _lock:
        if sid not in _lives:
            _lives[sid] = LiveManager(opt, avatar_session)
        return _lives[sid]


def get_live(sessionid: str) -> Optional[LiveManager]:
    return _lives.get(sessionid)


async def remove_live(sessionid: str) -> None:
    async with _lock:
        live = _lives.pop(sessionid, None)
    if live is not None:
        await live.stop()


async def auto_resume(opt, session_lookup) -> None:
    """Restore running sessions from disk after app restart.
    Call once at app boot. `session_lookup(sid)` should return a BaseAvatar
    (the active session for that sessionid) — typically session_manager.get_session.
    """
    state = _load_state_disk()
    if not state:
        return
    log.info("[Live] auto_resume: %d session(s) on disk", len(state))
    for sid, info in state.items():
        platform = info.get("platform") or "tiktok"
        live_id = (info.get("live_id") or "").strip()
        if not live_id:
            continue
        avatar = session_lookup(sid) if session_lookup else None
        if avatar is None:
            log.warning("[Live] auto_resume: sessionid=%s không có avatar — bỏ qua", sid)
            continue
        try:
            live = await get_or_create_live(opt, avatar)
            result = await live.start(platform, live_id)
            if "error" in result:
                log.warning("[Live] auto_resume %s failed: %s", sid, result["error"])
            else:
                log.info("[Live] auto_resume OK: sid=%s @%s", sid, live_id)
        except Exception:
            log.exception("[Live] auto_resume sid=%s", sid)
