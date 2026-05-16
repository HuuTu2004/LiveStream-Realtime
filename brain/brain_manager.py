"""BrainManager — lifecycle wrapper kết nối Brain vào BaseAvatar.

Flow:
1. BrainManager khởi tạo trong main asyncio loop (KHÔNG trong render thread).
2. start() → spawn ScriptEngine + CommentHandler async tasks.
3. speak_fn → gọi LLM stream → SentenceSplitter gom thành câu → put_msg_txt
   vào TTS. Gesture không còn đi qua LLM tag — base_avatar tự auto-trigger
   gesture ngẫu nhiên khi đang nói (xem avatars/base_avatar.process_frames).
4. stop() → cancel tasks, không destroy avatar_session.

Thread-safety: BrainManager sống trong asyncio loop của aiohttp app.
avatar_session.put_msg_txt() là thread-safe (đẩy vào queue).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

from .llm_client import LLMClient
from .product_catalog import ProductCatalog
from .script_engine import ScriptEngine
from .comment_handler import CommentHandler
from .sentence_splitter import SentenceSplitter

if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar

log = logging.getLogger(__name__)


class BrainManager:
    """Một BrainManager / 1 avatar session."""

    def __init__(self, opt, avatar_session: "BaseAvatar"):
        self.opt = opt
        self.avatar_session = avatar_session
        self.sessionid = getattr(avatar_session, "sessionid", "default")

        # Product catalog (singleton per path → hot reload)
        products_path = getattr(opt, "products_path", "data/products.json")
        self.catalog = ProductCatalog.for_path(products_path)

        # LLM client
        self.llm = LLMClient(
            base_url=getattr(opt, "llm_url", ""),
            api_key=getattr(opt, "llm_api_key", ""),
            model=getattr(opt, "llm_model", "gpt-4o-mini"),
        )

        # Script engine (8-stage + silence + random events)
        self.script_engine = ScriptEngine(
            silence_timeout=getattr(opt, "silence_gap_secs", 30)
        )
        self.script_engine.set_speaker(self.speak)

        # Comment handler (intent + batching)
        self.comments = CommentHandler(
            catalog=self.catalog,
            script_engine=self.script_engine,
        )
        self.comments.set_speaker(self.speak)

        # State
        self._stream_start = time.monotonic()
        self._priority_count = 0  # đếm các speak priority đang chạy
        self._lock = asyncio.Lock()
        self._running = False
        self._current_stage = self.script_engine.current_stage()
        self._last_text: str = ""

        # Switch product callback hook (UI dashboard tự subscribe nếu cần)
        self._switch_product_listeners: list = []
        self.comments.set_switch_product_callback(self._on_switch_product_idx)

    # ------------------------------------------------------------------
    @property
    def stream_minutes(self) -> int:
        return int((time.monotonic() - self._stream_start) / 60)

    # ------------------------------------------------------------------
    def add_switch_product_listener(self, cb) -> None:
        self._switch_product_listeners.append(cb)

    def _on_switch_product_idx(self, idx: int) -> None:
        for cb in self._switch_product_listeners:
            try:
                cb(idx)
            except Exception:
                log.exception("[Brain] switch listener error")

    # ------------------------------------------------------------------
    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._running = True
            self._stream_start = time.monotonic()
            self.script_engine.set_stage_callback(self._stage_changed)
            self.script_engine.start()
            self.comments.start()
            log.info("[Brain] Started sessionid=%s", self.sessionid)

    async def stop(self) -> None:
        async with self._lock:
            if not self._running:
                return
            self._running = False
            await self.script_engine.stop()
            await self.comments.stop()
            log.info("[Brain] Stopped sessionid=%s", self.sessionid)

    def _stage_changed(self, stage: str) -> None:
        self._current_stage = stage
        log.info("[Brain] Stage → %s", stage)

    # ------------------------------------------------------------------
    async def speak(self, prompt: str, priority: bool = False) -> None:
        """Đẩy 1 prompt qua LLM → gesture-tagger → TTS → avatar.

        Khi priority=True → gắn cờ để các non-priority speak khác lùi lại
        (hiện tại chỉ ghi log; backpressure thực tế nằm ở TTS queue).
        """
        if not self._running:
            log.warning("[Brain] speak() called while not running")
            return

        product = self.catalog.current_product()
        if priority:
            self._priority_count += 1
        try:
            stream = self.llm.stream(
                prompt,
                product=product,
                stream_minutes=self.stream_minutes,
            )
            splitter = SentenceSplitter()
            had_output = False
            captured: list[str] = []
            async for sent in splitter.feed_stream(stream):
                if not sent:
                    continue
                had_output = True
                captured.append(sent)
                self.avatar_session.put_msg_txt(sent)
            if had_output:
                self._last_text = " ".join(captured)
                log.debug("[Brain] spoke (%d sent): %s", len(captured), self._last_text[:80])
            else:
                log.warning("[Brain] empty LLM output for prompt: %s", prompt[:60])
        except Exception:
            log.exception("[Brain] speak error")
        finally:
            if priority:
                self._priority_count = max(0, self._priority_count - 1)

    # ------------------------------------------------------------------
    async def feed_comment(
        self,
        username: str,
        text: str,
        has_icon: bool = False,
        platform: str = "",
    ) -> None:
        if not self._running:
            return
        await self.comments.on_comment(username, text, has_icon=has_icon, platform=platform)

    async def on_join(self, username: str) -> None:
        if self._running:
            await self.comments.on_join(username)

    async def on_like(self, username: str, count: int) -> None:
        if self._running:
            await self.comments.on_like(username, count)

    async def on_follow(self, username: str) -> None:
        if self._running:
            await self.comments.on_follow(username)

    async def on_share(self, username: str) -> None:
        if self._running:
            await self.comments.on_share(username)

    def set_viewer_count(self, count: int) -> None:
        self.script_engine.update_viewer_count(int(count))

    def switch_product(self, product_id: str = "", index: int = -1) -> bool:
        if product_id:
            ok = self.catalog.set_current_by_id(product_id)
            if ok:
                # broadcast tới listeners
                for i, p in enumerate(self.catalog.get_all_products()):
                    if str(p.get("id")) == str(product_id):
                        self._on_switch_product_idx(i)
                        break
            return ok
        if index >= 0:
            ok = self.catalog.set_current_by_index(index)
            if ok:
                self._on_switch_product_idx(index)
            return ok
        return False

    # ------------------------------------------------------------------
    def state(self) -> dict:
        cur = self.catalog.current_product()
        return {
            "sessionid": self.sessionid,
            "running": self._running,
            "stage": self._current_stage,
            "stream_minutes": self.stream_minutes,
            "viewer_count": self.script_engine._viewer_count,
            "current_product": {
                "id": cur.get("id") if cur else None,
                "name": cur.get("name") if cur else None,
                "price": cur.get("price") if cur else None,
            } if cur else None,
            "last_text": self._last_text[:200],
        }


# ─── Global registry per sessionid ─────────────────────────────────────
_brains: dict[str, BrainManager] = {}
_brains_lock = asyncio.Lock()


async def get_or_create_brain(opt, avatar_session: "BaseAvatar") -> BrainManager:
    sid = getattr(avatar_session, "sessionid", "default")
    async with _brains_lock:
        if sid not in _brains:
            _brains[sid] = BrainManager(opt, avatar_session)
        return _brains[sid]


def get_brain(sessionid: str) -> Optional[BrainManager]:
    return _brains.get(sessionid)


async def remove_brain(sessionid: str) -> None:
    async with _brains_lock:
        brain = _brains.pop(sessionid, None)
    if brain:
        await brain.stop()
