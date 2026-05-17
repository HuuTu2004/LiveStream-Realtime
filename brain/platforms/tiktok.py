"""TikTok Live scraper — wire TikTokLive lib → BrainManager.

Event coverage (đầy đủ cho sales):
  Core engagement:
    Comment → feed_comment (LLM reply)
    Like / Digg → on_like (milestone reply)
    Join → on_join (greeting random 40%)
    Follow → on_follow (thank)
    Share → on_share (thank)
    Gift → on_like ×10 + comment feed entry
    RoomUserSeq → set_viewer_count (milestones)
  Commerce (TikTok Shop):
    VideoLiveGoodsOrder → on_order (REAL social proof — đơn vừa chốt!)
    Subscribe → on_subscribe (VIP greeting, mạnh hơn follow)
    Envelope → on_envelope (lì xì — thông báo + boost engagement)
  Barrage (paid):
    Barrage → priority feed_comment (cao hơn comment thường)
  Host control:
    LivePause / LiveUnpause → brain.pause / brain.resume
  Connection:
    Connect, Disconnect, LiveEnd

Reconnect tự động exp-backoff 5→60s.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
            "paused": False,             # host bấm pause live
            "viewer_count": 0,
            "comments_total": 0,
            "likes_total": 0,
            "joins_total": 0,
            "follows_total": 0,
            "shares_total": 0,
            "gifts_total": 0,
            "orders_total": 0,           # đơn từ TikTok Shop ngay trong live
            "subs_total": 0,             # subscriber trả phí
            "envelopes_total": 0,        # phong bao đỏ
            "barrages_total": 0,         # comment chữ chạy trả phí
            "last_order_at": 0,
            "last_error": "",
        }
        # Buffer comment gần đây cho UI hiển thị
        self._recent_comments: list[dict] = []
        self._max_recent = 100
        # Realtime hook — set by LiveManager. Signature: (event_dict) -> None
        # Called synchronously from event handlers, must be non-blocking.
        self.on_event = None

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
            # Optional events — có từ TikTokLive 6.x. Wrap try để lib version
            # cũ không crash.
            _opt = {}
            for name in (
                "VideoLiveGoodsOrderEvent", "SubscribeEvent", "EnvelopeEvent",
                "BarrageEvent", "LivePauseEvent", "LiveUnpauseEvent",
            ):
                try:
                    _opt[name] = __import__(
                        "TikTokLive.events", fromlist=[name]
                    ).__dict__.get(name)
                except Exception:
                    _opt[name] = None
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
                    "ts": int(time.time() * 1000),
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
                self._emit_stat()
                await self.brain.on_like(user, count)
            except Exception:
                log.exception("[TikTok] LikeEvent")

        @self._client.on(JoinEvent)
        async def on_join(event: "JoinEvent"):
            try:
                user = self._extract_username(event.user)
                self._stats["joins_total"] += 1
                self._emit_stat()
                await self.brain.on_join(user)
            except Exception:
                log.exception("[TikTok] JoinEvent")

        @self._client.on(FollowEvent)
        async def on_follow(event: "FollowEvent"):
            try:
                user = self._extract_username(event.user)
                self._stats["follows_total"] += 1
                self._emit_stat()
                await self.brain.on_follow(user)
            except Exception:
                log.exception("[TikTok] FollowEvent")

        @self._client.on(ShareEvent)
        async def on_share(event: "ShareEvent"):
            try:
                user = self._extract_username(event.user)
                self._stats["shares_total"] += 1
                self._emit_stat()
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
                    "ts": int(time.time() * 1000),
                }
                self._buffer_comment(rec)   # _buffer_comment emits "comments" event
                self._emit_stat()            # gift counter delta
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
                    self._emit_stat()
                    self.brain.set_viewer_count(count)
            except Exception:
                log.exception("[TikTok] RoomUserSeqEvent")

        # ─── COMMERCE: đơn hàng phát sinh trong live (TikTok Shop) ────
        if _opt.get("VideoLiveGoodsOrderEvent"):
            @self._client.on(_opt["VideoLiveGoodsOrderEvent"])
            async def on_order(event):
                try:
                    user = self._extract_username(getattr(event, "user", None))
                    product_name = (
                        getattr(event, "product_name", "")
                        or getattr(event, "title", "")
                        or "sản phẩm"
                    )
                    self._stats["orders_total"] += 1
                    self._stats["last_order_at"] = int(time.time() * 1000)
                    rec = {
                        "type": "order",
                        "username": user,
                        "text": f"vừa chốt đơn: {product_name}",
                        "ts": int(time.time() * 1000),
                    }
                    self._buffer_comment(rec)
                    self._emit_stat()
                    if hasattr(self.brain, "on_order"):
                        await self.brain.on_order(user, product_name)
                except Exception:
                    log.exception("[TikTok] VideoLiveGoodsOrderEvent")

        # ─── SUBSCRIBE: subscriber trả phí (VIP) ──────────────────────
        if _opt.get("SubscribeEvent"):
            @self._client.on(_opt["SubscribeEvent"])
            async def on_subscribe(event):
                try:
                    user = self._extract_username(getattr(event, "user", None))
                    level = int(getattr(event, "sub_level", 1) or 1)
                    self._stats["subs_total"] += 1
                    rec = {
                        "type": "subscribe",
                        "username": user,
                        "text": f"vừa subscribe (level {level})",
                        "ts": int(time.time() * 1000),
                    }
                    self._buffer_comment(rec)
                    self._emit_stat()
                    if hasattr(self.brain, "on_subscribe"):
                        await self.brain.on_subscribe(user, level)
                except Exception:
                    log.exception("[TikTok] SubscribeEvent")

        # ─── ENVELOPE: lì xì đỏ (engagement booster) ──────────────────
        if _opt.get("EnvelopeEvent"):
            @self._client.on(_opt["EnvelopeEvent"])
            async def on_envelope(event):
                try:
                    user = self._extract_username(getattr(event, "user", None))
                    self._stats["envelopes_total"] += 1
                    rec = {
                        "type": "envelope",
                        "username": user,
                        "text": "vừa gửi lì xì đỏ",
                        "ts": int(time.time() * 1000),
                    }
                    self._buffer_comment(rec)
                    self._emit_stat()
                    if hasattr(self.brain, "on_envelope"):
                        await self.brain.on_envelope(user)
                except Exception:
                    log.exception("[TikTok] EnvelopeEvent")

        # ─── BARRAGE: comment chạy chữ trả phí (priority) ─────────────
        if _opt.get("BarrageEvent"):
            @self._client.on(_opt["BarrageEvent"])
            async def on_barrage(event):
                try:
                    user = self._extract_username(getattr(event, "user", None))
                    text = (getattr(event, "comment", "") or
                            getattr(event, "content", "") or "").strip()
                    if not text:
                        return
                    self._stats["barrages_total"] += 1
                    rec = {
                        "type": "barrage",
                        "username": user,
                        "text": f"⚡ {text}",
                        "ts": int(time.time() * 1000),
                    }
                    self._buffer_comment(rec)
                    self._emit_stat()
                    # Barrage là comment trả phí → đẩy thẳng vào brain.
                    # Brain CommentHandler tự xử intent (rất khả năng BUY).
                    await self.brain.feed_comment(user, text, platform="tiktok", has_icon=True)
                except Exception:
                    log.exception("[TikTok] BarrageEvent")

        # ─── HOST CONTROL: pause / resume ─────────────────────────────
        if _opt.get("LivePauseEvent"):
            @self._client.on(_opt["LivePauseEvent"])
            async def on_pause(event):
                try:
                    self._stats["paused"] = True
                    self._emit_stat()
                    log.info("[TikTok] Host paused live @%s", self.live_id)
                    if hasattr(self.brain, "pause"):
                        await self.brain.pause()
                except Exception:
                    log.exception("[TikTok] LivePauseEvent")

        if _opt.get("LiveUnpauseEvent"):
            @self._client.on(_opt["LiveUnpauseEvent"])
            async def on_unpause(event):
                try:
                    self._stats["paused"] = False
                    self._emit_stat()
                    log.info("[TikTok] Host resumed live @%s", self.live_id)
                    if hasattr(self.brain, "resume"):
                        await self.brain.resume()
                except Exception:
                    log.exception("[TikTok] LiveUnpauseEvent")

        # ─── Connect loop with retry ──────────────────────────────────
        # TikTokLive 6.5+: client.start() raises AlreadyConnectedError if called twice.
        # Always disconnect() first to reset state — safe to call on never-connected client.
        backoff = 5
        while not self._stopped:
            try:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                log.info("[TikTok] connecting to @%s ...", self.live_id)
                await self._client.start()
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
        # Push to live subscribers — UI sees the comment within ms instead
        # of waiting for the 1s state snapshot tick.
        self._emit({"event": "comments", "data": [rec]})

    def _emit(self, event: dict) -> None:
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(event)
        except Exception:
            log.exception("[TikTok] on_event callback failed")

    def _emit_stat(self) -> None:
        """Push the lightweight stats delta — used after counter-only events
        (like/join/follow/share/viewer) so the KPI strip updates instantly."""
        self._emit({"event": "stat", "data": dict(self._stats)})

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
