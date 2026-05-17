"""Sales Script Engine — 8-stage cycle + silence triggers + random events.

Port từ LiveAI/stream/script_engine.py. Khác biệt: speak_fn là async callable
nhận (prompt: str, priority: bool = False) — không phụ thuộc TTS engine cụ thể.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

SALES_CYCLE = [
    {
        "stage": "CHÀO HỎI",
        "prompt": (
            "Chào mừng những vị khách quý đang ghé thăm livestream của Linh! Hãy gửi lời chào "
            "hoặc comment tỉnh thành bạn đang xem để Linh gửi lời chúc sức khỏe nha. "
            "Đừng quên thả tim và share livestream để nhận ngay mã giảm giá bí mật vào cuối buổi. "
            "Nói khoảng 4 câu đầy năng lượng và thân thiện."
        ),
    },
    {
        "stage": "NỖI ĐAU",
        "prompt": (
            "Chia sẻ sâu sắc về những vấn đề khách thường gặp: mua hàng online không giống hình, "
            "vải mặc nóng, hay sợ nhất là sai kích cỡ không đổi trả được. Đồng cảm với khách hàng "
            "và khẳng định Linh ở đây để giúp bạn giải quyết triệt để nỗi lo đó. 4 câu thấu hiểu."
        ),
    },
    {
        "stage": "GIẢI PHÁP",
        "prompt": (
            "Giới thiệu sản phẩm hôm nay như một giải pháp hoàn hảo nhất. Phân tích tại sao sản phẩm "
            "này lại khắc phục được mọi nhược điểm của các loại hàng thông thường khác trên thị trường. "
            "Nhấn mạnh vào sự tâm huyết của shop khi chọn lựa sản phẩm này. 4-5 câu chuyên nghiệp."
        ),
    },
    {
        "stage": "GIÁ TRỊ",
        "prompt": (
            "Đi sâu vào giá trị thực tế: cảm giác thoải mái khi mặc, độ bền vượt trội theo thời gian, "
            "và sự tự tin mà khách hàng sẽ có được khi sở hữu nó. Dùng mô hình FAB để thuyết phục khách. "
            "5 câu giàu hình ảnh và thuyết phục."
        ),
    },
    {
        "stage": "XÃ HỘI CHỨNG",
        "prompt": (
            "Kể về những phản hồi tích cực từ những khách hàng đã mua trước đó. Đặc biệt là những "
            "câu chuyện khách quay lại mua thêm cho người thân vì quá hài lòng với chất lượng. "
            "Khẳng định uy tín của shop qua hàng ngàn đơn hàng đã gửi đi. 4 câu tạo niềm tin tuyệt đối."
        ),
    },
    {
        "stage": "ƯU ĐÃI",
        "prompt": (
            "Công bố mức giá 'Độc quyền' chỉ dành riêng cho phiên livestream này. So sánh với giá niêm yết "
            "để khách thấy đây là cơ hội đầu tư cho bản thân cực kỳ tiết kiệm. Kèm theo voucher hoặc quà tặng "
            "hấp dẫn nếu chốt đơn ngay bây giờ. 4 câu hào hứng và lôi cuốn."
        ),
    },
    {
        "stage": "KHAN HIẾM",
        "prompt": (
            "Cảnh báo số lượng trong kho chỉ còn tính bằng đầu ngón tay cho mỗi size/màu. Nhấn mạnh "
            "rằng lô hàng sau sẽ không còn giá này hoặc phải đợi rất lâu mới có hàng lại. "
            "Thúc giục những ai còn đang cân nhắc hãy quyết định ngay kẻo hối tiếc. 4 câu khẩn cấp."
        ),
    },
    {
        "stage": "CHỐT ĐƠN",
        "prompt": (
            "Hướng dẫn khách hàng chốt đơn chuyên nghiệp: bấm trực tiếp vào biểu tượng giỏ hàng ở góc dưới "
            "bên trái màn hình. Tuyệt đối KHÔNG yêu cầu khách hàng để lại số điện thoại hay thông tin cá nhân. "
            "Cam kết kiểm hàng thoải mái trước khi thanh toán và chế độ bảo hành 1 đổi 1 tận tâm từ shop. "
            "4 câu dứt khoát và đáng tin cậy."
        ),
    },
]

_RANDOM_EVENTS = [
    {
        "name": "flash_sale",
        "interval": (900, 1800),
        "prompts": [
            "Thôi shop giảm thêm 30k cho 5 đơn tới trong 3 phút! Ai muốn thì comment SIZE xuống ngay!",
            "Flash deal nhanh nha — giảm thêm 20k cho ai chốt trong 2 phút tới, comment CHỐT đi!",
            "Nè nè, shop vừa quyết định tặng freeship cho 10 đơn tiếp theo — ai nhanh tay thì comment ngay nha!",
        ],
    },
    {
        "name": "stock_warning",
        "interval": (1200, 2400),
        "prompts": [
            "Ơ shop vừa check kho... màu này size M còn 3 cái thôi á, ai muốn nhanh lên nha!",
            "Check lại thấy size L đang cạn nhanh rồi, ai cần thì chốt trước kẻo hết nha.",
            "Màu trắng hết trước rồi đó, chỉ còn màu tối thôi — ai thích màu trắng thì nhanh nha!",
        ],
    },
    {
        "name": "order_bump",
        "interval": (600, 1200),
        "prompts": [
            "Vừa có bạn chốt đơn rồi đó! Ai đang cân nhắc thì nhanh lên, đang bay nè.",
            "Shop vừa nhận thêm mấy đơn, cảm ơn mọi người nha! Ai chưa chốt thì mau lên đi.",
            "Ôi vừa có 2 bạn chốt liền một lúc luôn! Bạn nào đang cân nhắc thì đừng chần chừ nha.",
        ],
    },
    {
        "name": "engagement",
        "interval": (300, 600),
        "prompts": [
            "Mà này bạn ơi, ai đang xem từ tỉnh nào comment xuống cho Linh biết với nha!",
            "Hỏi nhanh nha: mọi người thường hay mặc màu tối hay màu sáng? Comment xuống đi!",
            "Ai đang xem mà chưa follow thì follow nha — live sau Linh có deal khủng cho người follow đó!",
            "Share livestream này giúp Linh với nha, ai share được Linh tặng thêm quà cuối buổi nha!",
            "Mọi người đang xem từ điện thoại hay máy tính vậy? Comment xuống cho Linh biết nha haha.",
        ],
    },
]

_VIEWER_MILESTONES = {
    50:  "Ôi 50 người rồi! Cảm ơn mọi người nha — ai chưa follow thì follow ủng hộ Linh với!",
    100: "Wow 100 người đang xem! Shop tặng voucher 20k cho 3 bạn comment nhanh nhất ngay bây giờ nha!",
    200: "200 người rồi!! Ôi trời, cảm ơn mọi người quá luôn — Linh hạnh phúc lắm á, yêu mọi người!",
    500: "500 người!!! Linh không ngờ luôn, cảm ơn mọi người share giúp nha, love all mọi người!",
}

_TIME_MILESTONES = [
    (1800, "Ủa mới đó mà đã được 30 phút rồi! Cảm ơn mọi người đồng hành nha, vẫn còn nhiều thứ hay lắm đó."),
    (3600, "1 tiếng rồi! Cảm ơn mọi người nha — ai vào sau thì kéo lên xem từ đầu, còn nhiều deal lắm đó."),
    (5400, "Tiếng rưỡi rồi luôn! Linh không ngờ mọi người kiên trì vậy, cảm ơn nhiều lắm, yêu mọi người!"),
]


SpeakFn = Callable[..., Awaitable[None]]
IdleFn = Callable[[], bool]


class ScriptEngine:
    """Drives stage prompts + random events.

    Có 2 chế độ (chọn qua flag continuous):
      - continuous=True (default): idle-driven — poll is_idle_fn() mỗi
        idle_poll_secs, vừa idle là fire stage/event ngay. Avatar nói liên tục.
      - continuous=False: legacy silence-driven — đợi silence > threshold mới
        fire. Để dành cho usecase muốn có gap dài giữa các đoạn.
    """

    def __init__(
        self,
        silence_timeout: int = 30,
        continuous: bool = True,
        idle_poll_secs: float = 0.5,
        random_event_chance: float = 0.25,
    ):
        self._speak_fn: Optional[SpeakFn] = None
        self._idle_fn: Optional[IdleFn] = None
        self._stage_cb: Optional[Callable[[str], None]] = None
        self._silence_timeout = max(10, silence_timeout)
        self._continuous = bool(continuous)
        self._idle_poll_secs = max(0.1, float(idle_poll_secs))
        self._random_event_chance = max(0.0, min(1.0, float(random_event_chance)))
        self._last_interaction = 0.0
        self._stage_idx = 0
        self._start_time = 0.0
        self._event_next: dict[str, float] = {}
        self._viewer_count = 0
        self._viewer_milestones_hit: set[int] = set()
        self._time_milestones_done: set[int] = set()
        self._silence_streak = 0
        self._next_silence_threshold = self._new_silence_threshold()
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        # Paused = host pause live (TikTok LivePauseEvent). Engine vẫn chạy
        # loop nhưng SKIP fire prompts để tránh nói khi host đi ra ngoài.
        self._paused = False

    def _new_silence_threshold(self) -> float:
        return self._silence_timeout * random.uniform(0.65, 1.35)

    def set_speaker(self, fn: SpeakFn) -> None:
        self._speak_fn = fn

    def set_idle_fn(self, fn: IdleFn) -> None:
        """Inject từ BrainManager — return True khi avatar không nói VÀ
        speak_lock không bị giữ. Chỉ dùng ở continuous mode."""
        self._idle_fn = fn

    def set_stage_callback(self, cb: Callable[[str], None]) -> None:
        self._stage_cb = cb

    def current_stage(self) -> str:
        return SALES_CYCLE[self._stage_idx]["stage"]

    def reset_silence(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        self._last_interaction = loop.time()
        self._silence_streak = 0
        self._next_silence_threshold = self._new_silence_threshold()

    def update_viewer_count(self, count: int) -> None:
        self._viewer_count = count

    def _advance_stage(self) -> str:
        self._stage_idx = (self._stage_idx + 1) % len(SALES_CYCLE)
        stage_name = SALES_CYCLE[self._stage_idx]["stage"]
        if self._stage_cb:
            try:
                self._stage_cb(stage_name)
            except Exception:
                log.exception("[ScriptEngine] stage_cb error")
        return stage_name

    def _schedule_events(self, now: float) -> None:
        for ev in _RANDOM_EVENTS:
            lo, hi = ev["interval"]
            self._event_next[ev["name"]] = now + random.randint(lo, hi)

    # ------------------------------------------------------------------
    async def _check_viewer_milestones(self) -> bool:
        for milestone, msg in _VIEWER_MILESTONES.items():
            if self._viewer_count >= milestone and milestone not in self._viewer_milestones_hit:
                self._viewer_milestones_hit.add(milestone)
                log.info("[ScriptEngine] Cột mốc %d người xem", milestone)
                await self._safe_speak(msg, priority=False)
                return True
        return False

    async def _check_time_milestones(self, elapsed: float) -> bool:
        for threshold, msg in _TIME_MILESTONES:
            if elapsed >= threshold and threshold not in self._time_milestones_done:
                self._time_milestones_done.add(threshold)
                log.info("[ScriptEngine] Cột mốc %ds", threshold)
                await self._safe_speak(msg, priority=False)
                return True
        return False

    async def _check_random_events(self, now: float) -> bool:
        for ev in _RANDOM_EVENTS:
            if now >= self._event_next.get(ev["name"], float("inf")):
                msg = random.choice(ev["prompts"])
                log.info("[ScriptEngine] Sự kiện: %s", ev["name"])
                await self._safe_speak(msg, priority=False)
                lo, hi = ev["interval"]
                self._event_next[ev["name"]] = now + random.randint(lo, hi)
                return True
        return False

    async def _fire_stage_prompt(self, reason: str = "idle") -> None:
        """Fire stage prompt hiện tại + advance index.

        reason='silence' giữ logic legacy: silence kéo dài 4 lần liên tiếp →
        nhảy sang ƯU ĐÃI để cứu chuyển đổi. continuous mode dùng reason='idle'
        và bỏ qua silence_streak heuristic vì idle là trạng thái bình thường.
        """
        if reason == "silence":
            self._silence_streak += 1
            if self._silence_streak >= 4 and self._stage_idx < 5:
                self._stage_idx = 5
                log.info("[ScriptEngine] Im lặng kéo dài → nhảy sang ƯU ĐÃI")

        entry = SALES_CYCLE[self._stage_idx]
        log.info("[ScriptEngine] %s → '%s'",
                 "Khoảng lặng" if reason == "silence" else "Idle", entry["stage"])
        await self._safe_speak(entry["prompt"], priority=False)
        self._advance_stage()

        try:
            loop = asyncio.get_running_loop()
            self._last_interaction = loop.time()
        except RuntimeError:
            pass
        self._next_silence_threshold = self._new_silence_threshold()

    # Backward-compat alias: code/test cũ vẫn có thể gọi _fire_silence.
    async def _fire_silence(self) -> None:
        await self._fire_stage_prompt(reason="silence")

    async def _safe_speak(self, prompt: str, priority: bool = False) -> None:
        if not self._speak_fn:
            return
        try:
            await self._speak_fn(prompt, priority=priority)
        except Exception:
            log.exception("[ScriptEngine] speak_fn error")

    # ------------------------------------------------------------------
    def start(self) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self._stopped = False
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def pause(self) -> None:
        """Tạm dừng fire prompts (loop vẫn chạy, comment vẫn được xử lý qua
        speak_fn priority). Dùng khi host pause TikTok live."""
        self._paused = True

    def resume(self) -> None:
        """Bật lại — fire prompts như bình thường."""
        self._paused = False
        # Reset timer để khi resume engine không fire dồn dập do silence quá dài
        try:
            loop = asyncio.get_running_loop()
            self._last_interaction = loop.time()
        except RuntimeError:
            pass

    def is_paused(self) -> bool:
        return self._paused

    async def run(self) -> None:
        assert self._speak_fn, "Phải gọi set_speaker() trước"
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._last_interaction = now
        self._start_time = now
        self._schedule_events(now)

        if self._stage_cb:
            try:
                self._stage_cb(self.current_stage())
            except Exception:
                pass

        if self._continuous:
            if self._idle_fn is None:
                log.warning("[ScriptEngine] continuous=True nhưng chưa set_idle_fn() — "
                            "fallback silence-driven")
                await self._run_silence_driven(loop)
            else:
                log.info("[ScriptEngine] Bắt đầu (continuous) — poll mỗi %.2fs",
                         self._idle_poll_secs)
                await self._run_idle_driven(loop)
        else:
            log.info("[ScriptEngine] Bắt đầu (silence-driven) — im lặng %ds sẽ tự nói",
                     self._silence_timeout)
            await self._run_silence_driven(loop)

    async def _run_idle_driven(self, loop: asyncio.AbstractEventLoop) -> None:
        """Continuous mode: vừa idle là fire câu kế.

        Trật tự mỗi vòng:
          1. Avatar đang nói / speak_lock đang giữ → skip vòng này.
          2. Viewer/time milestone chưa fire → fire.
          3. Xác suất random_event_chance → fire random event nếu đến lịch.
          4. Còn lại → fire stage prompt theo SALES_CYCLE.

        Quan trọng: _safe_speak là `await`, KHÔNG `create_task`. Do
        BrainManager._speak_lock serialize, await speak xong là biết TTS đã
        nhận full sentence; vòng sau idle_fn() trả True chỉ khi avatar phát
        hết audio thật sự — không spam câu mới chồng lên câu cũ.
        """
        while not self._stopped:
            try:
                await asyncio.sleep(self._idle_poll_secs)
            except asyncio.CancelledError:
                break

            # Host pause → skip fire prompts. Comment vẫn được handle qua
            # speak_fn priority do CommentHandler quản lý độc lập.
            if self._paused:
                continue

            try:
                if self._idle_fn is None or not self._idle_fn():
                    continue

                now = loop.time()
                elapsed = now - self._start_time

                if await self._check_viewer_milestones():
                    self.reset_silence()
                    continue
                if await self._check_time_milestones(elapsed):
                    self.reset_silence()
                    continue
                if (self._random_event_chance > 0
                        and random.random() < self._random_event_chance
                        and await self._check_random_events(now)):
                    self.reset_silence()
                    continue
                await self._fire_stage_prompt(reason="idle")
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("[ScriptEngine] idle loop error")

    async def _run_silence_driven(self, loop: asyncio.AbstractEventLoop) -> None:
        """Legacy mode: check mỗi 5s, fire khi silence > threshold."""
        while not self._stopped:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            if self._paused:
                continue
            now = loop.time()
            elapsed = now - self._start_time
            silence = now - self._last_interaction

            try:
                if await self._check_viewer_milestones():
                    self.reset_silence()
                    continue
                if await self._check_time_milestones(elapsed):
                    self.reset_silence()
                    continue
                if silence < self._next_silence_threshold:
                    await self._check_random_events(now)
                    continue
                await self._fire_stage_prompt(reason="silence")
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("[ScriptEngine] loop iteration error")
