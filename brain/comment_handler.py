"""Comment ingestion + intent classification + batching.

Port từ LiveAI/stream/comment_handler.py. Intent → 6 categories:
  BUY_INTENT, PRICE_ASK, SIZE_ASK, QUESTION, SPAM, GENERAL.
GENERAL chỉ reply 60% để tránh spam như người thật.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# Vietnamese name extraction: giữ chữ Latin + có dấu, bỏ emoji/digit/special.
_NAME_KEEP_RE = re.compile(
    r"[A-Za-zÀ-ỹĐđ\s]+",  # Latin extended + Vietnamese diacritics
)


def clean_username(username: str, fallback: str = "bạn") -> str:
    """Trích tên Việt từ TikTok display name: bỏ emoji/digit/special, giữ chữ.
    Examples:
      'sakura🌸💃' → 'sakura'
      '_29thg11_💜' → 'thg'  (fallback nếu quá ngắn)
      'Mẹ Thóc Gạo@@' → 'Mẹ Thóc Gạo'
      'NCK0911_LV' → 'NCK LV'  → fallback nếu chỉ caps random
    """
    if not username:
        return fallback
    parts = _NAME_KEEP_RE.findall(username)
    name = " ".join(p.strip() for p in parts if p.strip())
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) < 2:
        return fallback
    # Cắt nếu quá dài (> 30 ký tự) — TikTok name có thể chứa quote dài
    if len(name) > 30:
        name = name[:30].rsplit(" ", 1)[0] or name[:30]
    return name

_SPAM_PATTERNS = ("http", "t.me", "zalo.me", "follow mình", "sub kênh", "xem tại")
_BUY_WORDS = ("chốt", "mua", "đặt", "lấy", "order", "ship cho", "lấy 1 cái", "mã")

# Variety pool — randomize vibe + opening style mỗi prompt để tránh AI lặp.
_VIBES = (
    "năng động vui vẻ, dùng emoji nhẹ trong văn nói",
    "thân thiện gần gũi, kiểu chị em tâm tình",
    "tự tin chuyên gia, nhấn vào lợi ích cụ thể",
    "hài hước nhẹ duyên dáng",
    "nhiệt huyết bán hàng, đẩy mạnh CTA chốt đơn",
    "trầm ấm điềm đạm, thuyết phục bằng số liệu thực tế",
)
_OPENERS_AVOID = (
    "Chào bạn", "Chào cả nhà", "Xin chào", "Hello", "Hi các bạn",
    "Linh chào", "Mọi người ơi",
)
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

        self._general_reply_rate = 0.3
        # Adaptive priority: nếu có comment trong active_window_secs qua → live
        # đang sôi nổi, giảm tần suất chào random; nếu vắng → tăng tần suất chào
        # + pitch sản phẩm cho new joiners.
        self._last_comment_at: float = 0.0
        self._active_window_secs: float = 30.0
        # Global greet cooldown — tránh spam khi nhiều người join cùng lúc.
        self._last_greet_at: float = 0.0
        self._greet_cooldown_secs: float = 15.0
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

        # Skip degenerate text only (single char repeat, empty)
        if len(text) < 2 or len(set(t)) < 2:
            return

        # Quick BUY_INTENT detection cho urgent priority — vẫn keyword vì khách
        # đang ready chốt đơn cần reply ngay, không đợi batch.
        intent_quick = classify(text)
        log.info("[Comment] %s [%s]: %s → %s", platform or "?", username, text, intent_quick)
        self._last_comment_at = time.time()
        if self.script_engine:
            self.script_engine.reset_silence()

        # Auto switch product nếu khách nhắc mã/tên (giữ — UX tốt)
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

        clean_name = clean_username(username)

        # BUY_INTENT → speak ngay (urgent, không qua batch)
        if intent_quick == "BUY_INTENT":
            prod_ctx = ""
            if self.catalog:
                ctx, switched = self.catalog.get_relevant_product([f"{clean_name}: {text}"])
                prod_ctx = ctx
                if switched is not None and self._switch_product_cb:
                    self._switch_product_cb(switched)
                    self.catalog.set_current_by_index(switched)
            vibe = random.choice(_VIBES)
            avoid_str = ", ".join(f'"{x}"' for x in _OPENERS_AVOID)
            prompt = (
                f"=== SẢN PHẨM ===\n{prod_ctx}\n"
                f"=== TÌNH HUỐNG ===\nKhách [{clean_name}] muốn mua: \"{text}\"\n"
                f"=== VIBE ===\n{vibe}\n"
                f"=== YÊU CẦU ===\n"
                f"Cảm ơn khách bằng tên, khen họ chọn đúng, hướng dẫn bấm giỏ hàng góc "
                f"trái để chốt đơn. 3-5 câu năng lượng.\n"
                f"TRÁNH mở đầu bằng: {avoid_str}. Vào thẳng nội dung kiểu 'Tuyệt vời "
                f"[tên]', 'Hay quá [tên]', 'OK [tên]', 'Linh chốt cho bạn liền nha'..."
            )
            asyncio.create_task(self._safe_speak(prompt, priority=True))
            return

        # MỌI COMMENT KHÁC → batch để LLM tự lọc rác + group + decide reply
        entry = f"[{clean_name}] {text}"
        async with self._lock:
            self._current_batch.append(entry)

    def _live_is_active(self) -> bool:
        """Live có comment trong active_window_secs gần đây = sôi nổi."""
        return (time.time() - self._last_comment_at) < self._active_window_secs

    def _can_greet(self) -> bool:
        """Global cooldown: tránh spam greet khi spike join."""
        return (time.time() - self._last_greet_at) >= self._greet_cooldown_secs

    async def on_join(self, username: str) -> None:
        # Adaptive: live sôi nổi → bỏ qua chào (không cắt mạch reply comment).
        # Live vắng → chào + pitch sản phẩm để giữ engagement.
        # Global cooldown 15s tránh spam khi spike join.
        if username in self._seen_users:
            return
        if not self._can_greet():
            return
        rate = 0.03 if self._live_is_active() else 0.35
        if random.random() > rate:
            return
        name = clean_username(username)
        vibe = random.choice(_VIBES)
        prompt = (
            f"Live đang vắng. Khách [{name}] vừa vào.\n"
            f"VIBE: {vibe}\n"
            f"Nhắc tên khách 1 lần, MỜI xem sản phẩm shop đang giới thiệu (nhắc "
            f"tên + 1 USP). 2 câu thân thiện hướng CTA.\n"
            f"TRÁNH 'Chào bạn', 'Xin chào', 'Hello' — vào thẳng nội dung kiểu "
            f"'À [tên] tới rồi', '[tên] ơi đang giới thiệu...', 'Hay quá có [tên] "
            f"vào', 'Welcome [tên]'."
        )
        self._last_greet_at = time.time()
        await self._safe_speak(prompt, priority=False)

    async def on_like(self, username: str, count: int) -> None:
        # Sales-focused: bỏ thank-like hoàn toàn — like là passive engagement.
        return

    async def on_follow(self, username: str) -> None:
        # Adaptive: live sôi nổi → 10%; live vắng → 60%.
        # Global cooldown 15s tránh spam.
        if not self._can_greet():
            return
        rate = 0.10 if self._live_is_active() else 0.60
        if random.random() > rate:
            return
        name = clean_username(username)
        vibe = random.choice(_VIBES)
        prompt = (
            f"Khách [{name}] vừa FOLLOW shop.\n"
            f"VIBE: {vibe}\n"
            f"Cảm ơn bằng tên, nhấn shop có ưu đãi riêng cho follower, mời ở lại. "
            f"2 câu nồng nhiệt.\n"
            f"TRÁNH 'Chào bạn', 'Cảm ơn bạn' generic — mở đầu kiểu 'Thank [tên]', "
            f"'[tên] tâm lý ghê', 'Ố [tên] follow rồi nha', 'Chuẩn [tên] luôn'."
        )
        self._last_greet_at = time.time()
        await self._safe_speak(prompt, priority=False)

    async def on_share(self, username: str) -> None:
        prompt = (
            "Trân trọng cảm ơn bạn đã share livestream giúp Linh lan tỏa! "
            "Linh chúc bạn may mắn săn được deal tốt hôm nay nha. 2 câu chân thành."
        )
        await self._safe_speak(prompt, priority=False)

    # ─── Commerce events (đẩy mạnh chốt đơn) ──────────────────────────
    async def on_order(self, username: str, product_name: str = "") -> None:
        """Đơn vừa chốt — social proof CỰC MẠNH cho khán giả đang lưỡng lự."""
        if self.script_engine:
            self.script_engine.reset_silence()
        name = clean_username(username)
        prod_part = f" sản phẩm {product_name}" if product_name else ""
        vibe = random.choice(_VIBES)
        prompt = (
            f"🎉 KHÁCH [{name}] VỪA CHỐT ĐƠN{prod_part}!\n"
            f"VIBE: {vibe}\n"
            f"Cảm ơn bằng tên, khen họ nhanh tay săn đúng deal hot, thúc các bạn "
            f"khác lưỡng lự nhanh tay chốt theo trước khi hết hàng. 3 câu năng "
            f"lượng tạo FOMO mạnh.\n"
            f"TRÁNH 'Cảm ơn bạn' generic — mở đầu kiểu 'Wow [tên] nhanh tay quá', "
            f"'Chốt rồi nha [tên]', 'Tuyệt vời [tên]', 'Xuất sắc [tên]'."
        )
        await self._safe_speak(prompt, priority=True)

    async def on_subscribe(self, username: str, level: int = 1) -> None:
        """Subscriber trả phí — VIP greeting mạnh hơn on_follow."""
        if self.script_engine:
            self.script_engine.reset_silence()
        name = clean_username(username)
        vibe = random.choice(_VIBES)
        prompt = (
            f"🌟 KHÁCH VIP [{name}] subscribe shop level {level}!\n"
            f"VIBE: {vibe}\n"
            f"Cảm ơn chân thành, nhấn họ là khách ruột, hứa hẹn quà/voucher riêng "
            f"cho subscriber. 3 câu trang trọng nhưng không cứng.\n"
            f"TRÁNH 'Cảm ơn bạn' generic — kiểu 'Hú hồn [tên] sub luôn', "
            f"'Trời ơi [tên] VIP rồi', 'Khách ruột của Linh đây rồi'."
        )
        await self._safe_speak(prompt, priority=True)

    async def on_envelope(self, username: str) -> None:
        """Lì xì đỏ — thông báo + boost engagement."""
        if self.script_engine:
            self.script_engine.reset_silence()
        prompt = (
            f"Khách [{username}] vừa gửi lì xì đỏ ngay trong livestream! Cảm ơn họ và "
            f"khuyến khích những bạn khác tham gia để có cơ hội nhận lì xì may mắn. "
            f"2 câu vui tươi."
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

            batch_str = "\n".join(f"  - {c}" for c in batch)
            vibe = random.choice(_VIBES)
            avoid_str = ", ".join(f'"{x}"' for x in _OPENERS_AVOID)
            prompt = (
                f"=== SẢN PHẨM ĐANG BÁN ===\n{prod_ctx}\n"
                f"=== BATCH BÌNH LUẬN ({len(batch)} dòng) ===\n{batch_str}\n"
                f"=== VIBE PHIÊN NÀY ===\n{vibe}\n"
                f"=== HƯỚNG DẪN ===\n"
                f"Bạn là Linh — chuyên gia bán hàng. Phản hồi batch trên với VIBE đã chọn.\n\n"
                f"1. **LọC RÁC** (bỏ qua, KHÔNG nhắc): spam / troll / ký tự vô nghĩa / quảng cáo / "
                f"comment tục tĩu / chính trị / chào nhau riêng giữa khách.\n"
                f"   → Nếu cả batch toàn rác → trả về '' (empty).\n\n"
                f"2. **GỘP TRÙNG**: nhiều bạn hỏi 1 thứ (size/giá/màu/ship/mã) → trả lời CHUNG 1 lần.\n\n"
                f"3. **ƯU TIÊN** (giảm dần):\n"
                f"   a. Khách hỏi mã/code cụ thể → confirm + tư vấn sản phẩm đó\n"
                f"   b. Hỏi giá/size/màu/ship → trả lời từ info sản phẩm trên\n"
                f"   c. So sánh / băn khoăn → giải đáp + push CTA\n"
                f"   d. Khen shop → cảm ơn nhẹ 1 câu, không spam cảm ơn\n"
                f"   e. Comment vu vơ → bỏ qua nếu batch đã đủ nội dung\n\n"
                f"4. **TRÁNH LẶP — đa dạng cách mở đầu**:\n"
                f"   - KHÔNG bắt đầu bằng: {avoid_str}\n"
                f"   - Vào thẳng nội dung. Ví dụ: 'Bạn [tên] hỏi về...', 'Mã 18 đúng rồi nha', "
                f"'Size M phù hợp 50-55kg đó', 'Quần này lưng cao tôn dáng cực kỳ...', "
                f"'À đúng rồi, mặc combo còn được giảm thêm...', 'Bạn ơi để Linh nói cho nghe...'\n"
                f"   - Mỗi lần mở đầu phải KHÁC nhau, không lặp pattern.\n\n"
                f"5. **FORMAT**:\n"
                f"   - 2-5 câu nói trôi chảy, gọi tên khách 1-2 lần\n"
                f"   - KHÔNG liệt kê 'Bình luận 1:', 'Trả lời 2:'\n"
                f"   - Kết tự nhiên bằng CTA hoặc nhấn thêm USP (đa dạng kiểu)\n"
                f"   - Văn nói tự nhiên kiểu chị bán hàng livestream, KHÔNG cứng nhắc\n"
                f"   - Áp dụng VIBE đã chọn ở trên"
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
