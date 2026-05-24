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
        # Optional shop guarantee custom text (override default trust hooks)
        self.llm.shop_guarantee = getattr(opt, "shop_guarantee", "") or ""

        # Script engine (8-stage + silence + random events)
        self.script_engine = ScriptEngine(
            silence_timeout=getattr(opt, "silence_gap_secs", 30),
            continuous=getattr(opt, "continuous_talk", True),
            idle_poll_secs=getattr(opt, "idle_poll_secs", 0.1),
            random_event_chance=getattr(opt, "random_event_chance", 0.25),
        )
        self.script_engine.set_speaker(self.speak)
        self.script_engine.set_idle_fn(self.is_idle)

        # Comment handler (intent + batching)
        self.comments = CommentHandler(
            catalog=self.catalog,
            script_engine=self.script_engine,
            batch_window_secs=getattr(opt, "comment_batch_secs", 3.0),
        )
        # Adaptive thresholds (overridable via CLI/env)
        self.comments._active_window_secs = float(getattr(opt, "live_active_window_secs", 30.0))
        self.comments._greet_cooldown_secs = float(getattr(opt, "greet_cooldown_secs", 15.0))
        self.comments.set_speaker(self.speak)

        # State
        self._stream_start = time.monotonic()
        self._priority_count = 0  # đếm các speak priority đang chạy
        self._lock = asyncio.Lock()
        # Interrupt support: track current running speak để priority có thể cancel
        self._current_speak_task = None
        self._current_is_priority = False
        # Serialize tất cả speak() — comment priority và stage prompt cùng dùng
        # 1 lock → FIFO, không bao giờ overlap LLM stream / TTS push.
        self._speak_lock = asyncio.Lock()
        # Time-based buffer estimator: tránh khựng bằng cách fire câu kế khi
        # estimated remaining buffer < (safety_margin + LLM_duration_EMA).
        # Mỗi speak() cộng dồn estimated audio duration vào _buffer_drain_at
        # và cập nhật LLM duration EMA — system tự thích nghi tốc độ LLM.
        self._target_buffer_secs = float(getattr(opt, "target_buffer_secs", 1.5))
        self._tts_chars_per_sec = max(4.0, float(getattr(opt, "tts_chars_per_sec", 14.0)))
        self._buffer_drain_at = 0.0  # monotonic time khi buffer ước tính cạn
        # LLM stream duration EMA — khởi tạo bằng prior 2s (typical), update
        # sau mỗi speak() bằng EMA α=0.3 → adaptive với LLM thực tế. Là phần
        # SỐNG CÒN của 'không khựng': fire câu kế trước khi cạn ÍT NHẤT bằng
        # thời gian LLM mất để sinh + push toàn bộ câu mới.
        self._llm_duration_ema = float(getattr(opt, "llm_duration_init", 2.0))
        self._llm_ema_alpha = 0.3
        # Hard timeout cho mỗi speak() — phòng LLM server treo (network hỏng,
        # vLLM OOM, Ollama lock). Quá timeout → cancel, release lock, brain
        # tiếp tục stage kế. Không có timeout = brain chết im lặng vĩnh viễn.
        self._speak_timeout = float(getattr(opt, "speak_timeout_secs", 30.0))
        # Silent-correction: nếu avatar thực sự silent kéo dài (estimate sai),
        # đồng bộ _buffer_drain_at về now để không bao giờ kẹt.
        self._silent_polls = 0
        self._silent_sync_polls = max(2, int(getattr(opt, "silent_sync_polls", 5)))
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
    def is_idle(self) -> bool:
        """True khi ScriptEngine nên fire câu kế để giữ buffer đầy.

        Time-based: ước tính `remaining = _buffer_drain_at - now` (giây audio
        còn trong queue). Trả True khi remaining < target_buffer → fire LLM
        câu kế ngay, kịp đẩy text vào TTS trước khi audio queue cạn.

        Đảm bảo KHÔNG khựng: target_buffer chọn > LLM TTFT điển hình. Mặc định
        3s = đủ cover LLM 1-3s. Khi avatar phát đến điểm còn 3s reserve,
        ScriptEngine fire → LLM 2s xong → câu mới vào TTS khi reserve còn 1s
        → avatar nuốt tiếp không cảm giác gián đoạn.

        Silent-correction: nếu avatar im lặng thực tế kéo dài hơn estimate
        (TTS chậm hơn ước tính), đồng bộ _buffer_drain_at = now để giải kẹt.
        """
        if self._speak_lock.locked():
            return False

        try:
            speaking = self.avatar_session.is_speaking()
        except Exception:
            log.debug("[Brain] is_speaking() probe failed", exc_info=True)
            speaking = False

        now = time.monotonic()

        if speaking:
            self._silent_polls = 0
        else:
            self._silent_polls += 1
            if self._silent_polls >= self._silent_sync_polls:
                # Avatar silent thực sự lâu hơn estimate → sync về now.
                # Không trừ về 0 hẳn để vẫn còn 1 chút momentum cho fire kế.
                self._buffer_drain_at = min(self._buffer_drain_at, now)

        remaining = self._buffer_drain_at - now
        # Threshold = safety_margin + LLM duration EMA → fire đủ sớm để câu
        # kế xong LLM TRƯỚC khi buffer cạn. Cộng EMA mới là điểm cốt lõi để
        # không khựng khi LLM chậm.
        threshold = self._target_buffer_secs + self._llm_duration_ema
        return remaining < threshold

    # ------------------------------------------------------------------
    async def speak(self, prompt: str, priority: bool = False) -> None:
        """Đẩy 1 prompt qua LLM → SentenceSplitter → TTS → avatar.

        priority=True (comment/order/subscribe): INTERRUPT auto-pitch hiện
        tại → cancel running speak + flush TTS queue + take over ngay. Đây
        là yếu tố then chốt cho UX bán hàng live — khách comment phải reply
        trong giây chứ không xếp hàng 1-4 phút sau auto-pitch.

        priority=False (stage prompt): chờ lock FIFO bình thường.
        """
        if not self._running:
            log.warning("[Brain] speak() called while not running")
            return

        if priority:
            self._priority_count += 1
            # Interrupt path: nếu đang speak non-priority → cancel + flush audio queue
            cur = getattr(self, "_current_speak_task", None)
            cur_is_priority = getattr(self, "_current_is_priority", False)
            if cur is not None and not cur.done() and not cur_is_priority:
                log.info("[Brain] priority interrupting non-priority speak")
                cur.cancel()
                try:
                    self.avatar_session.flush_talk()
                except Exception:
                    log.exception("[Brain] flush_talk on interrupt")
                # Reset buffer accounting — TTS queue đã drop, đừng giữ ước tính cũ
                self._buffer_drain_at = time.monotonic()
        try:
            async with self._speak_lock:
                # Re-check sau khi acquire lock — có thể stop() đã gọi trong
                # lúc chờ lock.
                if not self._running:
                    return
                llm_start = time.monotonic()
                product = self.catalog.current_product()
                splitter = SentenceSplitter()
                had_output = False
                captured: list[str] = []

                async def _drain_stream() -> None:
                    nonlocal had_output
                    stream = self.llm.stream(
                        prompt,
                        product=product,
                        stream_minutes=self.stream_minutes,
                    )
                    async for sent in splitter.feed_stream(stream):
                        if not sent:
                            continue
                        had_output = True
                        captured.append(sent)
                        self.avatar_session.put_msg_txt(sent)

                # Track current task để priority có thể cancel
                self._current_speak_task = asyncio.current_task()
                self._current_is_priority = priority
                try:
                    await asyncio.wait_for(_drain_stream(), timeout=self._speak_timeout)
                except asyncio.TimeoutError:
                    log.warning("[Brain] speak() timeout %.1fs — LLM treo, "
                                "release lock và bỏ qua. Stage kế sẽ fire.",
                                self._speak_timeout)
                except asyncio.CancelledError:
                    log.info("[Brain] speak() cancelled by priority interrupt")
                    raise
                    # captured/had_output có thể đã partial — vẫn account vào
                    # buffer nếu có để tránh fire chồng ngay sau timeout.
                if had_output:
                    self._last_text = " ".join(captured)
                    now = time.monotonic()
                    # Update LLM duration EMA (adaptive — tự điều chỉnh theo
                    # LLM thực tế trong session). is_idle threshold dùng EMA
                    # này để fire câu kế đủ sớm tránh khựng.
                    llm_dur = now - llm_start
                    self._llm_duration_ema = (
                        (1 - self._llm_ema_alpha) * self._llm_duration_ema
                        + self._llm_ema_alpha * llm_dur
                    )
                    # Time-based buffer accounting: ước tính tổng số giây audio
                    # vừa push qua TTS. _buffer_drain_at giữ thời điểm queue
                    # ước tính cạn — max(now, prev) đảm bảo monotonic kể cả
                    # khi estimate trước đã hết hạn.
                    total_chars = sum(len(s) for s in captured)
                    duration = total_chars / self._tts_chars_per_sec
                    self._buffer_drain_at = max(now, self._buffer_drain_at) + duration
                    remaining = self._buffer_drain_at - now
                    log.debug("[Brain] spoke (%d sent, %d chars, +%.1fs audio, "
                              "buffer=%.1fs, llm_ema=%.1fs, priority=%s): %s",
                              len(captured), total_chars, duration, remaining,
                              self._llm_duration_ema, priority, self._last_text[:80])
                else:
                    log.warning("[Brain] empty LLM output for prompt: %s", prompt[:60])
        except asyncio.CancelledError:
            pass  # bị priority cancel — normal flow
        except Exception:
            log.exception("[Brain] speak error")
        finally:
            self._current_speak_task = None
            self._current_is_priority = False
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

    async def on_order(self, username: str, product_name: str = "") -> None:
        """Đơn hàng vừa chốt từ TikTok Shop — social proof realtime mạnh nhất."""
        if self._running and hasattr(self.comments, "on_order"):
            await self.comments.on_order(username, product_name)

    async def on_subscribe(self, username: str, level: int = 1) -> None:
        """Subscriber trả phí — VIP greeting (cao hơn follow)."""
        if self._running and hasattr(self.comments, "on_subscribe"):
            await self.comments.on_subscribe(username, level)

    async def on_envelope(self, username: str) -> None:
        """Lì xì đỏ — thông báo nhanh để boost engagement."""
        if self._running and hasattr(self.comments, "on_envelope"):
            await self.comments.on_envelope(username)

    async def pause(self) -> None:
        """Host pause live → brain im lặng tới khi resume.

        Không stop hoàn toàn (giữ state, comment vẫn được nhận). Chỉ ngăn
        ScriptEngine fire stage prompts để tránh nói khi host đi ra ngoài.
        """
        async with self._lock:
            if hasattr(self.script_engine, "pause"):
                self.script_engine.pause()
                log.info("[Brain] Paused (host paused live)")

    async def resume(self) -> None:
        """Host resume live → brain nói tiếp."""
        async with self._lock:
            if hasattr(self.script_engine, "resume"):
                self.script_engine.resume()
                log.info("[Brain] Resumed (host resumed live)")

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
