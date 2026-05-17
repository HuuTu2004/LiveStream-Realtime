"""Product catalog tối giản — schema {id, name, text}.

Mỗi product chỉ có 3 field: `id` (unique), `name` (tên hiển thị), `text` (mô tả
đầy đủ dạng text tự do). User paste cả block text vào, LLM brain nhận text
nguyên gốc khi sinh câu nói → tự thích nghi mọi ngành hàng (quần áo, điện tử,
mỹ phẩm, thực phẩm, dịch vụ, BĐS, F&B…) không cần parse/extract trước.

Retrieval đơn giản: token overlap giữa comment và (name + text).
Mã sản phẩm dạng "sp 1", "mã 2", hoặc số đứng 1 mình → match theo index.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

_STOPWORDS = {
    "có", "không", "được", "mình", "bạn", "ơi", "ạ", "nha", "nhé", "thì",
    "và", "của", "là", "cho", "với", "này", "đó", "sao", "vậy", "gi", "gì",
    "cái", "một", "shop", "ko",
}

_CODE_RE = re.compile(
    r'\bmã\s*(\d+)'             # "mã 18"
    r'|\bsp\s*(\d+)'             # "sp 18"
    r'|\bma\s*(\d+)'             # "ma 18" (no diacritic)
    r'|\bm[-_]?(\d{1,3})\b'      # "M50", "M-50", "M_50" (mã viết tắt)
    r'|^(\d{1,2})$',             # comment chỉ có số nguyên ngắn
    re.IGNORECASE,
)


def format_product(p: dict) -> str:
    """Render product cho LLM brain. Trả thẳng text user đã paste, kèm header tên."""
    if not p:
        return "Chưa có sản phẩm cụ thể."
    name = (p.get("name") or "").strip() or "Sản phẩm"
    text = (p.get("text") or "").strip()
    if text:
        return f"Tên: {name}\n{text}"
    # Legacy fallback: nếu data cũ còn các field structured, gom thô lại.
    legacy_lines = [f"Tên: {name}"]
    for k in ("price", "description"):
        v = p.get(k)
        if v:
            legacy_lines.append(f"{k}: {v}")
    return "\n".join(legacy_lines)


def _extract_code_number(comments: List[str]) -> Optional[int]:
    best: Optional[int] = None
    best_priority = 0
    for comment in comments:
        text = comment.strip().lower()
        if ": " in text:
            text = text.split(": ", 1)[1]
        for m in _CODE_RE.finditer(text):
            # 5 groups: mã / sp / ma / m\d / standalone
            num_str = m.group(1) or m.group(2) or m.group(3) or m.group(4) or m.group(5)
            if not num_str:
                continue
            num = int(num_str)
            # Priority: explicit "mã"/"sp"/"ma" > "M50" abbrev > standalone digit
            if m.group(1) or m.group(2) or m.group(3):
                priority = 3
            elif m.group(4):
                priority = 2
            else:
                priority = 1
            if priority > best_priority:
                best_priority = priority
                best = num
    return best


class ProductCatalog:
    """Singleton-per-path catalog với hot-reload (mtime) + CRUD."""

    _instances: dict[str, "ProductCatalog"] = {}
    _lock = threading.Lock()

    @classmethod
    def for_path(cls, json_path: str) -> "ProductCatalog":
        with cls._lock:
            if json_path not in cls._instances:
                cls._instances[json_path] = cls(json_path)
            return cls._instances[json_path]

    def __init__(self, json_path: str = "data/products.json"):
        self.json_path = json_path
        self.products: List[dict] = []
        self._last_mtime = 0.0
        self._current_idx = 0
        self._write_lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if not os.path.exists(self.json_path):
                return
            mtime = os.path.getmtime(self.json_path)
            if mtime <= self._last_mtime and self.products:
                return
            with open(self.json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.products = data.get("products", []) if isinstance(data, dict) else (data or [])
            self._last_mtime = mtime
            log.info("[Catalog] Loaded %d sản phẩm từ %s", len(self.products), self.json_path)
        except Exception as e:
            log.error("[Catalog] Lỗi load: %s", e)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.json_path) or ".", exist_ok=True)
        tmp = self.json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"products": self.products}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.json_path)
        self._last_mtime = os.path.getmtime(self.json_path)

    # ------------------------------------------------------------------
    def get_all_products(self) -> List[dict]:
        self._load()
        return self.products

    def get_by_id(self, pid: str) -> Optional[dict]:
        self._load()
        for p in self.products:
            if str(p.get("id")) == str(pid):
                return p
        return None

    def current_product(self) -> Optional[dict]:
        self._load()
        if not self.products:
            return None
        return self.products[min(self._current_idx, len(self.products) - 1)]

    def set_current_by_id(self, product_id: str) -> bool:
        self._load()
        for i, p in enumerate(self.products):
            if str(p.get("id", "")) == str(product_id):
                self._current_idx = i
                return True
        return False

    def set_current_by_index(self, idx: int) -> bool:
        self._load()
        if 0 <= idx < len(self.products):
            self._current_idx = idx
            return True
        return False

    # ─── CRUD ──────────────────────────────────────────────────────────
    def create(self, product: dict) -> dict:
        with self._write_lock:
            self._load()
            pid = (product.get("id") or "").strip()
            if not pid:
                pid = "p_" + uuid.uuid4().hex[:8]
                product = {**product, "id": pid}
            if self.get_by_id(pid) is not None:
                raise ValueError(f"product id '{pid}' đã tồn tại")
            self.products.append(product)
            self._save()
        return product

    def update(self, pid: str, fields: dict) -> dict:
        with self._write_lock:
            self._load()
            for i, p in enumerate(self.products):
                if str(p.get("id")) == str(pid):
                    new_id = fields.get("id", pid)
                    if str(new_id) != str(pid):
                        if any(str(x.get("id")) == str(new_id) for j, x in enumerate(self.products) if j != i):
                            raise ValueError(f"id '{new_id}' đã tồn tại")
                    merged = {**p, **fields}
                    self.products[i] = merged
                    self._save()
                    return merged
            raise KeyError(f"product id '{pid}' không tồn tại")

    def delete(self, pid: str) -> bool:
        with self._write_lock:
            self._load()
            for i, p in enumerate(self.products):
                if str(p.get("id")) == str(pid):
                    self.products.pop(i)
                    if self._current_idx >= len(self.products):
                        self._current_idx = max(0, len(self.products) - 1)
                    self._save()
                    return True
        return False

    def replace_all(self, products: list) -> int:
        with self._write_lock:
            self.products = list(products or [])
            self._current_idx = 0
            self._save()
            return len(self.products)

    # ─── Retrieval ─────────────────────────────────────────────────────
    def _score(self, p: dict, tokens: set) -> int:
        score = 0
        if str(p.get("id", "")).lower() in tokens:
            score += 10
        for w in str(p.get("name", "")).lower().split():
            if len(w) > 2 and w in tokens:
                score += 3
        for w in str(p.get("text", "")).lower().split():
            if len(w) > 2 and w in tokens:
                score += 1
        return score

    def get_relevant_product(
        self, comments: List[str]
    ) -> Tuple[str, Optional[int]]:
        """Match comments → (product_context_text, idx_0based_hoặc_None).

        Ưu tiên mã số ("mã 2" / "sp 3" / "5"). Không có thì score token overlap
        trên name + text. Tokens loại stopwords thuần Việt + từ <2 ký tự.
        """
        self._load()
        if not self.products:
            return "Chưa có thông tin sản phẩm.", None

        code_num = _extract_code_number(comments)
        if code_num is not None:
            idx = code_num - 1
            if 0 <= idx < len(self.products):
                log.info("[Catalog] Khớp mã %d → %s", code_num, self.products[idx].get("name"))
                return format_product(self.products[idx]), idx

        tokens: set = set()
        for c in comments:
            text = c.split(": ", 1)[-1] if ": " in c else c
            for w in text.lower().split():
                if w not in _STOPWORDS and len(w) > 1:
                    tokens.add(w)

        if not tokens:
            return format_product(self.current_product() or self.products[0]), None

        best_idx = max(range(len(self.products)), key=lambda i: self._score(self.products[i], tokens))
        return format_product(self.products[best_idx]), None
