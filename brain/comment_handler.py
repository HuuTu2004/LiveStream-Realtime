"""Comment ingestion + intent classification + batching.

Port từ LiveAI/stream/comment_handler.py. Intent → 6 categories:
  BUY_INTENT, PRICE_ASK, SIZE_ASK, QUESTION, SPAM, GENERAL.
GENERAL chỉ reply 60% để tránh spam như người thật.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

_SPAM_PATTERNS = ("http", "t.me", "zalo.me", "follow mình", "sub kênh", "xem tại")
_BUY_WORDS = ("chốt", "mua", "đặt", "lấy", "order", "ship cho", "lấy 1 cái", "mã")
_PRICE_WORDS = ("giá", "bao nhiêu", "nhiêu tiền", "nhiêu ạ", "rẻ không", "đắt không")
_SIZE_WORDS = ("size", "kg", "cao", "nặng", "mặc được không", "vừa không", "số mấy")
_ASK_WORDS = (
    "?", "không", "có không", "chất", "vải", "ship", "đổi", "trả", "như thế nào",
    "được không", "dày", "mỏng", "dài", "rộng", "co rút", "xù lông",
)

_JOIN_GREETINGS = [
    "Chào bạn mới vào nha! Đang xem hàng gì cứ hỏi Linh nè.",
    "Ơ có bạn nè! Chào bạn, shop đang có deal hot lắm đó.",
    "Welcome nha bạn! Bạn đến đúng lúc rồi — đang giới thiệu sản phẩm hot nè.",
    "Chào bạn! Xem có thích không thì hỏi Linh nha, tư vấn miễn phí luôn đó hehe.",
]


def classify(text: str) -> str:
    t = text.lower()
    if any(p in t for p in _SPAM_PATTERNS):
        return "SPAM"
    if any(w in t for w in _BUY_WORDS):
        return "BUY_INTENT"
    if any(w in t for w in _PRICE_WORDS):
        return "PRICE_ASK"
    if any(w in t for w in _SIZE_WORDS):
        return "SIZE_ASK"
    if any(w in t for w in _ASK_WORDS):
        return "QUESTION"
    return "GENERAL"


SpeakFn = Callable[..., Awaitable[None]]
SwitchProductFn = Callable[[int], None]


class CommentHandler:
    """Async comment processor with dedup, intent classify, batch flush."""

    def __init__(
        self,
        catalog,
        script_engine=None,
        batch_window_secs: float = 3.0,
        seen_history: int = 2000,
    ):
        self.catalog = catalog
        self.script_engine = script_engine
        self._speak_fn: Optional[SpeakFn] = None
        self._switch_product_cb: Optional[SwitchProductFn] = None
        # asyncio.sleep accept float — cho phép sub-second batch nếu cần.
        self._batch_window = max(0.5, float(batch_window_secs))

        self._current_batch: list[str] = []
        self._lock = asyncio.Lock()
        self._seen_users: dict[str, float] = {}
        self._processed: set[tuple[str, str]] = set()
        self._processed_q: deque[tuple[str, str]] = deque(maxlen=seen_history)

        self._general_reply_rate = 0.6
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    # ------------------------------------------------------------------
    def set_speaker(self, fn: SpeakFn) -> None:
        self._speak_fn = fn

    def set_switch_product_callback(self, cb: SwitchProductFn) -> None:
        self._switch_product_cb = cb

    # ------------------------------------------------------------------
    async def on_comment(
        self,
        username: str,
        text: str,
        has_icon: bool = False,
        platform: str = "",
    ) -> None:
        text = (text or "").strip()
        username = (username or "bạn").strip()
        if not text:
            return

        comment_id = (username, text)
        if comment_id in self._processed:
            return
        if len(self._processed_q) >= (self._processed_q.maxlen or 0):
            oldest = self._processed_q.popleft()
            self._processed.discard(oldest)
        self._processed.add(comment_id)
        self._processed_q.append(comment_id)

        if has_icon:
            username = "bạn icon"

        t = text.lower()

        # Lọc spam ngắn / repeated char
        if len(text) < 3:
            return
        if len(set(t)) < 3 and len(text) > 10:
            return

        intent = classify(text)
        if intent == "SPAM":
            return

        buy_kw = ("giá", "mã", "mua", "xem", "size", "nhiu", "bao tiền", "chốt")
        if len(text) < 6 and intent != "BUY_INTENT" and not any(k in t for k in buy_kw):
            return

        log.info("[Comment] %s [%s]: %s → %s", platform or "?", username, text, intent)
        if self.script_engine:
            self.script_engine.reset_silence()

        # Auto switch product nếu khách nhắc mã/tên
        if self._switch_product_cb and self.catalog:
            try:
                for i, p in enumerate(self.catalog.get_all_products()):
                    p_name = (p.get("name") or "").lower()
                    p_id = str(p.get("id") or "").lower()
                    if (p_name and p_name in t) or (p_id and p_id in t):
                        log.info("[Comment] Khớp SP: %s → switch index %d", p_name, i)
                        self._switch_product_cb(i)
                        self.catalog.set_current_by_index(i)
                        break
            except Exception:
                log.exception("[Comment] product match error")

        self._seen_users[username] = time.time()

        # BUY_INTENT → speak ngay, priority
        if intent == "BUY_INTENT":
            prod_ctx = ""
            if self.catalog:
                ctx, switched = self.catalog.get_relevant_product([f"{username}: {text}"])
                prod_ctx = ctx
                if switched is not None and self._switch_product_cb:
                    self._switch_product_cb(switched)
                    self.catalog.set_current_by_index(switched)

            prompt = (
                f"=== SẢN PHẨM ĐANG BÁN ===\n{prod_ctx}\n"
                f"=== TÌNH HUỐNG ===\n"
                f"Một bạn vừa comment muốn mua: \"{text}\"\n"
                f"=== YÊU CẦU ===\n"
                f"Phản hồi chuyên nghiệp: bắt đầu bằng việc gọi tên khách ([{username}]) "
                f"nhưng hãy tự động ĐOÁN và trích xuất tên tiếng Việt từ username "
                f"(bỏ các số và ký tự đặc biệt). Khen họ đã chọn đúng sản phẩm hot. "
                f"Hướng dẫn chi tiết: bấm vào biểu tượng giỏ hàng ở góc trái màn hình để chốt đơn. "
                f"4-5 câu đầy năng lượng, trôi chảy, KHÔNG liệt kê!"
            )
            asyncio.create_task(self._safe_speak(prompt, priority=True))
            return

        # SIZE_ASK chưa có số đo: 40% xác suất hỏi ngược
        if intent == "SIZE_ASK":
            has_measurements = (
                any(w in t for w in ("kg", "cân", "cao", "nặng", "cm"))
                or any(str(i) in text for i in range(40, 130))
            )
            if not has_measurements and random.random() < 0.4:
                prompt = (
                    f"Một bạn đang quan tâm về size nhưng chưa có số đo (comment: \"{text}\"). "
                    f"Hãy khéo léo mời bạn chia sẻ chiều cao, cân nặng để Linh tư vấn chuẩn. "
                    f"Dùng xưng hô 'bạn - Linh'. Nói 2 câu thật duyên dáng."
                )
                asyncio.create_task(self._safe_speak(prompt, priority=False))
                return

        # GENERAL: chỉ reply 60%
        if intent == "GENERAL" and random.random() > self._general_reply_rate:
            return

        # Còn lại: gom batch
        entry = f"[{username}] hỏi: {text}"
        async with self._lock:
            self._current_batch.append(entry)

    async def on_join(self, username: str) -> None:
        if username in self._seen_users:
            return
        if random.random() > 0.4:
            return
        greeting = random.choice(_JOIN_GREETINGS)
        await self._safe_speak(greeting, priority=False)

    async def on_like(self, username: str, count: int) -> None:
        if count and count % 10 == 0:
            prompt = (
                "Cảm ơn cả nhà thả tim nha! Ai thương Linh thì thả tim thật mạnh để Linh có "
                "động lực tung thêm deal hot nha. 2 câu năng lượng."
            )
            await self._safe_speak(prompt, priority=False)

    async def on_follow(self, username: str) -> None:
        prompt = (
            "Chào mừng người bạn mới gia nhập đại gia đình shop! Cảm ơn bạn đã follow, "
            "ở lại xem live Linh có nhiều ưu đãi dành riêng cho follower đó. 2 câu nồng nhiệt."
        )
        await self._safe_speak(prompt, priority=False)

    async def on_share(self, username: str) -> None:
        prompt = (
            "Trân trọng cảm ơn bạn đã share livestream giúp Linh lan tỏa! "
            "Linh chúc bạn may mắn săn được deal tốt hôm nay nha. 2 câu chân thành."
        )
        await self._safe_speak(prompt, priority=False)

    # ------------------------------------------------------------------
    async def _safe_speak(self, prompt: str, priority: bool = False) -> None:
        if not self._speak_fn:
            return
        try:
            await self._speak_fn(prompt, priority=priority)
        except Exception:
            log.exception("[Comment] speak_fn error")

    async def batch_loop(self) -> None:
        assert self._speak_fn, "Phải gọi set_speaker() trước"
        while not self._stopped:
            try:
                await asyncio.sleep(self._batch_window)
            except asyncio.CancelledError:
                break

            batch: list[str] = []
            async with self._lock:
                if self._current_batch:
                    batch = self._current_batch[:]
                    self._current_batch.clear()
            if not batch:
                continue

            log.info("[Comment] Flush batch %d", len(batch))
            prod_ctx = "Chưa có sản phẩm cụ thể."
            if self.catalog:
                try:
                    prod_ctx, switched = self.catalog.get_relevant_product(batch)
                    if switched is not None and self._switch_product_cb:
                        self._switch_product_cb(switched)
                        self.catalog.set_current_by_index(switched)
                except Exception:
                    log.exception("[Comment] catalog match error")

            size_count = sum(1 for c in batch if any(w in c.lower() for w in _SIZE_WORDS))
            price_count = sum(1 for c in batch if any(w in c.lower() for w in _PRICE_WORDS))
            note = ""
            if size_count >= 2:
                note += f"(Lưu ý: {size_count} bạn hỏi size — trả lời chung 1 lần) "
            if price_count >= 2:
                note += f"({price_count} bạn hỏi giá — nhắc rõ giá live) "

            batch_str = "\n".join(f"  - {c}" for c in batch)
            prompt = (
                f"=== SẢN PHẨM ĐANG BÁN ===\n{prod_ctx}\n"
                f"=== BÌNH LUẬN KHÁCH ({len(batch)} người) ===\n{batch_str}\n"
                f"=== YÊU CẦU ===\n"
                f"{note}"
                f"Trả lời các bình luận với tư cách Linh - Chuyên gia bán hàng. "
                f"Tự động ĐOÁN tên tiếng Việt từ username để xưng hô (bỏ số/ký tự đặc biệt). "
                f"Tư vấn sâu vào lợi ích, giải đáp thắc mắc, kết thúc bằng CTA. "
                f"TUYỆT ĐỐI KHÔNG liệt kê 'Bình luận 1:', 'Bình luận 2:'. Gộp lại thành "
                f"MỘT đoạn nói chuyện trôi chảy duy nhất. 5-7 câu đủ chiều sâu."
            )
            await self._safe_speak(prompt, priority=True)

    # ------------------------------------------------------------------
    def start(self) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self._stopped = False
        self._task = asyncio.create_task(self.batch_loop())
        return self._task

    async def stop(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
