"""LLM client cho sales brain — async streaming, sliding window history.

Hỗ trợ cả OpenAI thật (api.openai.com) lẫn OpenAI-compatible endpoint
(Ollama, vLLM, LM Studio, Vast.AI vLLM). Mặc định dùng OpenAI cho chất lượng
sales tiếng Việt; có thể override qua --llm_url + --llm_model + --llm_api_key.
"""

from __future__ import annotations

import json
import logging
import os
import random
from typing import AsyncGenerator, Optional

from .prompts.linh_vi import SYSTEM_PROMPT, STYLE_VARIANTS, DRIFT_HINTS
from .product_catalog import format_product

log = logging.getLogger(__name__)

_MAX_HISTORY_PAIRS = 4  # 4 cặp Q/A gần nhất

_clients: dict = {}


def _get_client(base_url: str, api_key: str):
    # Lazy import — cho phép brain module load được kể cả khi openai chưa cài
    from openai import AsyncOpenAI
    key = (base_url or "", api_key or "")
    if key not in _clients:
        kwargs: dict = {"api_key": api_key or "none"}
        if base_url and base_url not in ("https://api.openai.com/v1", "https://api.openai.com"):
            kwargs["base_url"] = base_url
        _clients[key] = AsyncOpenAI(**kwargs)
    return _clients[key]


def _build_product_context(product: dict) -> str:
    """Generic — render bất kỳ schema sản phẩm nào (quần áo, điện tử, mỹ phẩm...)."""
    if not product:
        return "Chưa có sản phẩm cụ thể."
    return format_product(product)


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

    # ------------------------------------------------------------------
    def reset_history(self) -> None:
        self._history.clear()

    def _trim_history(self) -> None:
        if len(self._history) > _MAX_HISTORY_PAIRS * 2:
            self._history = self._history[-(_MAX_HISTORY_PAIRS * 2):]

    # ------------------------------------------------------------------
    async def stream(
        self,
        user_msg: str,
        product: Optional[dict] = None,
        stream_minutes: int = 0,
    ) -> AsyncGenerator[str, None]:
        """Async generator yield token text. Tự cập nhật history khi kết thúc."""

        drift_hint = ""
        for threshold in sorted(DRIFT_HINTS, reverse=True):
            if stream_minutes >= threshold:
                drift_hint = DRIFT_HINTS[threshold]
                break

        style_hint = "\n\n[BIẾN THỂ PHONG CÁCH NGẪU NHIÊN: " + random.choice(STYLE_VARIANTS) + "]"
        system = (
            SYSTEM_PROMPT + drift_hint + style_hint +
            "\n\nproduct_info:\n" + _build_product_context(product)
        )

        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(self._history[-_MAX_HISTORY_PAIRS * 2:])
        messages.append({"role": "user", "content": user_msg})

        client = _get_client(self.base_url, self.api_key)

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
            log.exception("[LLMClient] stream init failed: %s", e)
            return

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
            log.exception("[LLMClient] stream iter error: %s", e)
        finally:
            full = "".join(captured)
            if full.strip():
                self._history.append({"role": "user", "content": user_msg})
                self._history.append({"role": "assistant", "content": full})
                self._trim_history()
