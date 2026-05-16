"""Vietnamese-aware sentence splitter — buffer streaming text, yield complete sentences.

Thay thế cho brain/gesture_tagger.py (đã xóa). Gesture system chuyển sang
auto-trigger state machine trong avatars/base_avatar.process_frames, không
còn parse tag từ LLM nữa — splitter này chỉ làm 1 việc: gom chunk LLM thành
câu hoàn chỉnh trước khi đẩy vào TTS.

Dùng:

    splitter = SentenceSplitter()
    async for sentence in splitter.feed_stream(llm_stream):
        avatar_session.put_msg_txt(sentence)
"""

from __future__ import annotations

import re
from typing import AsyncGenerator, AsyncIterator, Optional

# Sentence boundary cho tiếng Việt + particle cuối câu (ạ/nha/nhé/nè)
_SENT_END_RE = re.compile(
    r"[.!?。！？\n]|(?<=\S)(?:\bạ|\bnha|\bnhé|\bnè)(?=[\s,.!?])",
    re.IGNORECASE,
)


class SentenceSplitter:
    """Stateful buffer — split LLM stream theo ranh giới câu tiếng Việt."""

    def __init__(self) -> None:
        self._buffer: str = ""

    def _flush_one(self) -> Optional[str]:
        m = _SENT_END_RE.search(self._buffer)
        if not m:
            return None
        end = m.end()
        sentence = self._buffer[:end].strip()
        self._buffer = self._buffer[end:]
        return sentence or None

    async def feed_stream(
        self, stream: AsyncIterator[str]
    ) -> AsyncGenerator[str, None]:
        """Yield mỗi câu hoàn chỉnh. Cuối stream cũng flush phần buffer còn lại."""
        async for chunk in stream:
            if not chunk:
                continue
            self._buffer += chunk
            while True:
                sent = self._flush_one()
                if sent is None:
                    break
                yield sent

        tail = self._buffer.strip()
        self._buffer = ""
        if tail:
            yield tail
