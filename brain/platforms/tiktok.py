"""TikTok Live scraper — dùng thư viện `TikTokLive` (như LiveAI).

Connect tới TikTok live qua @username hoặc room_id, listen các event:
- Comment → brain.feed_comment
- Like → brain.on_like
- Join → brain.on_join
- Follow → brain.on_follow
- Share → brain.on_share
- Gift → brain.on_like (treat as engagement)
- ViewerUpdate → brain.set_viewer_count

Reconnect tự động khi disconnect (TikTokLive lib handle phần lớn).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..brain_manager import BrainManager


def _normalize_unique_id(s: str) -> str:
    """Chuẩn hóa input: bỏ '@', bỏ URL prefix, trả về unique_id thuần."""
    s = (s or "").strip()
    if not s:
        return ""
    # Strip URL form: https://www.tiktok.com/@user/live → user
    if "tiktok.com/@" in s:
        s = s.split("tiktok.com/@", 1)[1].split("/", 1)[0]
    s = s.lstrip("@").strip()
    return s


class TikTokListener:
    """Listener cho 1 phiên TikTok Live. Connect async + push event vào brain."""

    def __init__(self, brain: "BrainManager", live_id: str):
        self.brain = brain
        self.live_id = _normalize_unique_id(live_id)
        self._client = None
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._stats = {
            "platform": "tiktok",
            "live_id": self.live_id,
            "connected": False,
            "viewer_count": 0,
            "comments_total": 0,
            "likes_total": 0,
            "joins_total": 0,
            "follows_total": 0,
            "shares_total": 0,
            "gifts_total": 0,
            "last_error": "",
        }
        # Buffer comment gần đây cho UI hiển thị
        self._recent_comments: list[dict] = []
        self._max_recent = 100

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return dict(self._stats)

    def recent_comments(self, limit: int = 50) -> list[dict]:
        return self._recent_comments[-limit:][::-1]  # newest first

    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            log.warning("[TikTok] already running for %s", self.live_id)
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                log.exception("[TikTok] disconnect error")
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._stats["connected"] = False

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        try:
            # Lazy import — chỉ require khi user thực sự dùng TikTok
            from TikTokLive import TikTokLiveClient
            from TikTokLive.events import (
                ConnectEvent, DisconnectEvent, CommentEvent, LikeEvent,
                JoinEvent, FollowEvent, ShareEvent, GiftEvent,
                LiveEndEvent, RoomUserSeqEvent,
            )
        except ImportError:
            self._stats["last_error"] = "TikTokLive chưa được cài. pip install TikTokLive"
            log.error("[TikTok] %s", self._stats["last_error"])
            return

        if not self.live_id:
            self._stats["last_error"] = "live_id rỗng"
            return

        self._client = TikTokLiveClient(unique_id=self.live_id)

        # ─── Event handlers ───────────────────────────────────────────
        @self._client.on(ConnectEvent)
        async def on_connect(event: "ConnectEvent"):
            self._stats["connected"] = True
            self._stats["last_error"] = ""
            log.info("[TikTok] connected to @%s", self.live_id)

        @self._client.on(DisconnectEvent)
        async def on_disconnect(event: "DisconnectEvent"):
            self._stats["connected"] = False
            log.info("[TikTok] disconnected from @%s", self.live_id)

        @self._client.on(LiveEndEvent)
        async def on_live_end(event: "LiveEndEvent"):
            self._stats["connected"] = False
            self._stats["last_error"] = "Live đã kết thúc"
            log.info("[TikTok] live ended @%s", self.live_id)

        @self._client.on(CommentEvent)
        async def on_comment(event: "CommentEvent"):
            try:
                user = self._extract_username(event.user)
                text = event.comment or ""
                self._stats["comments_total"] += 1
                rec = {
                    "type": "comment",
                    "username": user,
                    "text": text,
                    "ts": asyncio.get_event_loop().time(),
                }
                self._buffer_comment(rec)
                await self.brain.feed_comment(user, text, platform="tiktok")
            except Exception:
                log.exception("[TikTok] CommentEvent")

        @self._client.on(LikeEvent)
        async def on_like(event: "LikeEvent"):
            try:
                user = self._extract_username(event.user)
                count = int(getattr(event, "count", 1) or 1)
                self._stats["likes_total"] += count
                await self.brain.on_like(user, count)
            except Exception:
                log.exception("[TikTok] LikeEvent")

        @self._client.on(JoinEvent)
        async def on_join(event: "JoinEvent"):
            try:
                user = self._extract_username(event.user)
                self._stats["joins_total"] += 1
                await self.brain.on_join(user)
            except Exception:
                log.exception("[TikTok] JoinEvent")

        @self._client.on(FollowEvent)
        async def on_follow(event: "FollowEvent"):
            try:
                user = self._extract_username(event.user)
                self._stats["follows_total"] += 1
                await self.brain.on_follow(user)
            except Exception:
                log.exception("[TikTok] FollowEvent")

        @self._client.on(ShareEvent)
        async def on_share(event: "ShareEvent"):
            try:
                user = self._extract_username(event.user)
                self._stats["shares_total"] += 1
                await self.brain.on_share(user)
            except Exception:
                log.exception("[TikTok] ShareEvent")

        @self._client.on(GiftEvent)
        async def on_gift(event: "GiftEvent"):
            try:
                user = self._extract_username(event.user)
                gift_name = getattr(getattr(event, "gift", None), "name", "gift") or "gift"
                self._stats["gifts_total"] += 1
                rec = {
                    "type": "gift",
                    "username": user,
                    "text": f"đã tặng {gift_name}",
                    "ts": asyncio.get_event_loop().time(),
                }
                self._buffer_comment(rec)
                # Treat gift as boosted engagement
                await self.brain.on_like(user, 10)
            except Exception:
                log.exception("[TikTok] GiftEvent")

        @self._client.on(RoomUserSeqEvent)
        async def on_viewer(event: "RoomUserSeqEvent"):
            try:
                # `total` thường là viewer count
                count = int(getattr(event, "total", 0) or 0)
                if count > 0:
                    self._stats["viewer_count"] = count
                    self.brain.set_viewer_count(count)
            except Exception:
                log.exception("[TikTok] RoomUserSeqEvent")

        # ─── Connect loop with retry ──────────────────────────────────
        backoff = 5
        while not self._stopped:
            try:
                log.info("[TikTok] connecting to @%s ...", self.live_id)
                await self._client.start()
                # Khi start() return (live end hoặc disconnect), reconnect
                if self._stopped:
                    break
                log.info("[TikTok] client.start() returned, retry in %ds", backoff)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats["last_error"] = str(e)
                log.exception("[TikTok] connect error: %s", e)

            if self._stopped:
                break
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, 60)

        self._stats["connected"] = False
        log.info("[TikTok] listener stopped for @%s", self.live_id)

    # ------------------------------------------------------------------
    def _buffer_comment(self, rec: dict) -> None:
        self._recent_comments.append(rec)
        if len(self._recent_comments) > self._max_recent:
            self._recent_comments = self._recent_comments[-self._max_recent:]

    @staticmethod
    def _extract_username(user) -> str:
        """TikTokLive user object → display name string."""
        if user is None:
            return "bạn"
        for attr in ("nickname", "unique_id", "display_id"):
            v = getattr(user, attr, None)
            if v:
                return str(v)
        return "bạn"
