"""Parse gesture tags từ LLM stream và emit gesture event đồng bộ với câu.

Logic:
- Pattern: `[wave|point|nod|smile|count|show|idle]`.
- Tag được "bám" vào câu kế tiếp — gesture event sẽ gắn vào câu đó.
- Tag bị strip khỏi text trước khi feed vào TTS.

Cách dùng:

    tagger = GestureTagger()
    async for clean_chunk, gesture in tagger.feed_stream(llm_stream):
        # clean_chunk: text đã strip tag; gesture: dict|None gắn vào câu tiếp theo
        ...

Hoặc dùng dạng buffer câu-hoàn-chỉnh:

    sentences = tagger.collect_sentences(llm_stream)
    # → list[(clean_sentence, gesture_dict_or_empty)]
"""

from __future__ import annotations

import re
from typing import AsyncGenerator, AsyncIterator, Optional

VALID_GESTURES = {"wave", "point", "nod", "smile", "count", "show", "idle"}

_TAG_RE = re.compile(r"\[(" + "|".join(VALID_GESTURES) + r")\]", re.IGNORECASE)

# Sentence boundary cho tiếng Việt + particle cuối câu
_SENT_END_RE = re.compile(r"[.!?。！？\n]|(?<=\S)(?:\bạ|\bnha|\bnhé|\bnè)(?=[\s,.!?])", re.IGNORECASE)


def strip_tags(text: str) -> str:
    """Strip toàn bộ gesture tag — dùng khi cần văn bản sạch tuyệt đối."""
    return _TAG_RE.sub("", text).strip()


def extract_first_gesture(text: str) -> tuple[str, Optional[str]]:
    """Trả về (clean_text, first_gesture_name_or_None)."""
    m = _TAG_RE.search(text)
    if not m:
        return text, None
    gesture = m.group(1).lower()
    clean = _TAG_RE.sub("", text).strip()
    return clean, gesture


class GestureTagger:
    """Stateful parser — buffer text từ LLM, split theo câu, attach pending gesture."""

    def __init__(self):
        self._buffer = ""
        self._pending_gesture: Optional[str] = None

    # ------------------------------------------------------------------
    def _extract_pending(self, text: str) -> str:
        """Strip tag đầu tiên ra khỏi text, lưu pending_gesture."""
        m = _TAG_RE.search(text)
        if not m:
            return text
        if self._pending_gesture is None:
            self._pending_gesture = m.group(1).lower()
        return _TAG_RE.sub("", text, count=1) + ("" if not _TAG_RE.search(_TAG_RE.sub("", text, count=1)) else "")

    def _strip_all_tags_keep_first(self, text: str) -> str:
        """Strip tất cả tag; gesture đầu lưu vào pending."""
        m = _TAG_RE.search(text)
        if m and self._pending_gesture is None:
            self._pending_gesture = m.group(1).lower()
        return _TAG_RE.sub("", text)

    # ------------------------------------------------------------------
    def _flush_sentence(self) -> Optional[tuple[str, dict]]:
        """Tìm câu hoàn chỉnh trong buffer. Trả về (clean_text, info_dict) hoặc None."""
        m = _SENT_END_RE.search(self._buffer)
        if not m:
            return None
        end = m.end()
        sentence = self._buffer[:end]
        self._buffer = self._buffer[end:]

        sentence = self._strip_all_tags_keep_first(sentence).strip()
        if not sentence:
            return None

        info = {}
        if self._pending_gesture:
            info["gesture"] = self._pending_gesture
            self._pending_gesture = None
        return sentence, info

    # ------------------------------------------------------------------
    async def feed_stream(
        self, stream: AsyncIterator[str]
    ) -> AsyncGenerator[tuple[str, dict], None]:
        """Async iterator: yield (clean_sentence, {'gesture': name}) khi có câu hoàn chỉnh.

        Cuối stream sẽ flush phần buffer còn lại làm 1 câu.
        """
        async for chunk in stream:
            if not chunk:
                continue
            self._buffer += chunk
            # Có thể có nhiều câu trong buffer → loop flush
            while True:
                out = self._flush_sentence()
                if out is None:
                    break
                yield out

        # Flush phần còn lại
        if self._buffer.strip():
            tail = self._strip_all_tags_keep_first(self._buffer).strip()
            self._buffer = ""
            if tail:
                info = {}
                if self._pending_gesture:
                    info["gesture"] = self._pending_gesture
                    self._pending_gesture = None
                yield tail, info
