"""LLM client cho sales brain — async streaming, sliding window history.

Hỗ trợ cả OpenAI thật (api.openai.com) lẫn OpenAI-compatible endpoint
(Ollama, vLLM, LM Studio, Vast.AI vLLM). Mặc định dùng OpenAI cho chất lượng
sales tiếng Việt; có thể override qua --llm_url + --llm_model + --llm_api_key.

Mỗi LLMClient instance giữ AsyncOpenAI riêng → connection pool tách biệt per
session, tránh thrashing khi multi-session cùng spam LLM. 429 detection có
exponential backoff độc lập per instance, không block instance khác.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import Counter, deque
from typing import Any, AsyncGenerator, Optional

from .prompts.linh_vi import SYSTEM_PROMPT, STYLE_VARIANTS, DRIFT_HINTS
from .product_catalog import format_product

log = logging.getLogger(__name__)

_MAX_HISTORY_PAIRS = 4   # 4 cặp Q/A gần nhất
_OPENER_HISTORY = 10     # nhớ 10 response gần nhất để detect lặp opener
_OPENER_MAX_CHARS = 40   # opener = clause đầu, max 40 ký tự


def _build_product_context(product: dict) -> str:
    """Generic — render bất kỳ schema sản phẩm nào (quần áo, điện tử, mỹ phẩm...)."""
    if not product:
        return "Chưa có sản phẩm cụ thể."
    return format_product(product)


def _extract_opener(text: str) -> str:
    """Lấy clause đầu (tới dấu ngắt) hoặc 6 từ đầu. Dùng để detect lặp."""
    text = (text or "").strip()
    if not text:
        return ""
    # Tìm dấu ngắt câu đầu tiên
    for marker in ".!?,\n":
        idx = text.find(marker)
        if 0 < idx <= _OPENER_MAX_CHARS:
            return text[:idx].strip().lower()
    # Fallback: 6 từ đầu, max 40 ký tự
    words = text.split()[:6]
    out = " ".join(words).strip().lower()
    return out[:_OPENER_MAX_CHARS]


def _is_rate_limit_error(err: BaseException) -> bool:
    """Detect 429 / rate limit từ multiple SDK error shapes."""
    s = str(err).lower()
    if "429" in s or "rate limit" in s or "too many requests" in s:
        return True
    # openai SDK exposes .status_code on some exception types
    code = getattr(err, "status_code", None) or getattr(err, "code", None)
    return code == 429 or str(code) == "429"


class LLMClient:
    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        temperature: float = 0.9,
        max_tokens: int = 500,
    ):
        self.base_url = base_url
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._history: list[dict] = []
        # Per-instance AsyncOpenAI — KHÔNG share giữa các session. Lazy init.
        # Mỗi session có connection pool riêng → 1 session bị rate-limit không
        # block session khác.
        self._client: Optional[Any] = None
        # Opener tracking — detect lặp pattern qua nhiều response. Inject
        # avoid block động vào system prompt khi phát hiện ≥2 response cùng
        # opener trong 10 lần gần nhất.
        self._recent_openers: deque[str] = deque(maxlen=_OPENER_HISTORY)
        # 429 backoff state (per-instance, không global).
        self._backoff_secs: float = 0.0
        self._rate_limit_until: float = 0.0  # monotonic timestamp

    # ------------------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            kwargs: dict = {"api_key": self.api_key or "none"}
            if self.base_url and self.base_url not in (
                "https://api.openai.com/v1",
                "https://api.openai.com",
            ):
                kwargs["base_url"] = self.base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    def reset_history(self) -> None:
        self._history.clear()
        self._recent_openers.clear()

    def _trim_history(self) -> None:
        if len(self._history) > _MAX_HISTORY_PAIRS * 2:
            self._history = self._history[-(_MAX_HISTORY_PAIRS * 2):]

    def _dynamic_avoid_block(self) -> str:
        """Nếu 1 opener xuất hiện ≥2 lần trong 10 response gần nhất → ban explicit."""
        if len(self._recent_openers) < 3:
            return ""
        counts = Counter(op for op in self._recent_openers if op)
        banned = [op for op, c in counts.items() if c >= 2]
        if not banned:
            return ""
        avoid_str = "; ".join(f'"{op}"' for op in banned[:6])
        return (
            "\n\n━━━ TUYỆT ĐỐI KHÔNG MỞ ĐẦU BẰNG ━━━\n"
            f"Bạn vừa lặp: {avoid_str}\n"
            "Lần này PHẢI mở đầu KHÁC HẲN — đừng dùng cùng 3-5 từ đầu của response gần nhất."
        )

    # ------------------------------------------------------------------
    async def stream(
        self,
        user_msg: str,
        product: Optional[dict] = None,
        stream_minutes: int = 0,
    ) -> AsyncGenerator[str, None]:
        """Async generator yield token text. Tự cập nhật history khi kết thúc."""

        # Per-instance 429 backoff. Nếu instance này đang trong backoff window
        # → sleep tới hết. Instance khác không bị block.
        now = time.monotonic()
        if now < self._rate_limit_until:
            wait = self._rate_limit_until - now
            log.warning("[LLMClient] rate-limited, wait %.1fs trước khi gọi tiếp", wait)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                return

        drift_hint = ""
        for threshold in sorted(DRIFT_HINTS, reverse=True):
            if stream_minutes >= threshold:
                drift_hint = DRIFT_HINTS[threshold]
                break

        style_hint = "\n\n[BIẾN THỂ PHONG CÁCH NGẪU NHIÊN: " + random.choice(STYLE_VARIANTS) + "]"
        # Custom shop guarantee override (nếu user set --shop_guarantee)
        custom_guarantee = getattr(self, "shop_guarantee", "") or ""
        guarantee_block = ""
        if custom_guarantee.strip():
            guarantee_block = (
                f"\n\n━━━ CHÍNH SÁCH SHOP RIÊNG (ưu tiên cao hơn default) ━━━\n"
                f"{custom_guarantee}\n"
                f"Lồng ghép TỰ NHIÊN vào câu nói khi khách lưỡng lự / hỏi giá / so sánh.\n"
            )
        avoid_block = self._dynamic_avoid_block()
        system = (
            SYSTEM_PROMPT + drift_hint + style_hint + guarantee_block + avoid_block +
            "\n\nproduct_info:\n" + _build_product_context(product)
        )

        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(self._history[-_MAX_HISTORY_PAIRS * 2:])
        messages.append({"role": "user", "content": user_msg})

        client = self._get_client()

        try:
            stream = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                frequency_penalty=0.6,
                presence_penalty=0.5,
                stream=True,
            )
        except Exception as e:
            if _is_rate_limit_error(e):
                # Exponential backoff: 2 → 4 → 8 → 16 → 30s, cap 30.
                self._backoff_secs = min(max(self._backoff_secs * 2, 2.0), 30.0)
                self._rate_limit_until = time.monotonic() + self._backoff_secs
                log.warning(
                    "[LLMClient] 429 detected, backoff %.1fs (model=%s)",
                    self._backoff_secs, self.model,
                )
            else:
                self._backoff_secs = 0.0
            log.exception("[LLMClient] stream init failed: %s", e)
            return

        # Success path — reset backoff.
        self._backoff_secs = 0.0
        self._rate_limit_until = 0.0

        captured: list[str] = []
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    captured.append(delta)
                    yield delta
        except Exception as e:
            if _is_rate_limit_error(e):
                self._backoff_secs = min(max(self._backoff_secs * 2, 2.0), 30.0)
                self._rate_limit_until = time.monotonic() + self._backoff_secs
                log.warning("[LLMClient] 429 mid-stream, backoff %.1fs", self._backoff_secs)
            log.exception("[LLMClient] stream iter error: %s", e)
        finally:
            full = "".join(captured)
            if full.strip():
                self._history.append({"role": "user", "content": user_msg})
                self._history.append({"role": "assistant", "content": full})
                self._trim_history()
                self._recent_openers.append(_extract_opener(full))
