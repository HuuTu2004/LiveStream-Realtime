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
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .brain_manager import BrainManager
    from avatars.base_avatar import BaseAvatar

log = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = ("tiktok",)


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
