"""Generic product catalog với hot-reload + keyword retrieval + CRUD.

Schema linh hoạt — KHÔNG fix cho ngành quần áo. Mỗi product chỉ bắt buộc `id`,
các field khác đều optional. Format generator render mọi field có sẵn để LLM
brain hiểu (quần áo, điện tử, mỹ phẩm, thực phẩm, dịch vụ — đều dùng được).

Schema khuyến nghị:
  {
    "id": "abc-01",                  // required, unique
    "name": "...",                   // tên hiển thị
    "price": "299.000đ",             // string flexible
    "description": "...",            // mô tả dài
    "image_url": "...",              // optional
    "attributes": {                  // dict bất kỳ: color, size, weight, voltage, expiry...
      "Màu": "Đen, Trắng",
      "Kích thước": "S, M, L",
      "Chất liệu": "Cotton 100%"
    },
    "selling_points": ["...","..."], // bullet selling
    "faq": {"câu hỏi": "câu trả lời"}
  }

Backward compat: vẫn đọc được legacy fields `colors`, `sizes`, `material` —
sẽ tự gom vào `attributes` khi render.
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
    r'\bmã\s*(\d+)'
    r'|\bsp\s*(\d+)'
    r'|^(\d{1,2})$',
    re.IGNORECASE,
)


def _coerce_legacy_attrs(p: dict) -> dict:
    """Gom legacy fields (colors/sizes/material) vào attributes nếu chưa có."""
    attrs = dict(p.get("attributes") or {})
    legacy_map = {
        "Màu sắc": p.get("colors"),
        "Kích cỡ": p.get("sizes"),
        "Chất liệu": p.get("material"),
    }
    for k, v in legacy_map.items():
        if v and k not in attrs:
            if isinstance(v, list):
                attrs[k] = ", ".join(str(x) for x in v if x)
            else:
                attrs[k] = str(v)
    return attrs


def format_product(p: dict) -> str:
    """Render mọi field có giá trị thành text block cho LLM. Generic, không fix domain."""
    lines: list[str] = []

    name = p.get("name") or p.get("title") or "—"
    lines.append(f"Tên: {name}")

    if p.get("price"):
        lines.append(f"Giá: {p['price']}")

    if p.get("description"):
        lines.append(f"Mô tả: {p['description']}")

    attrs = _coerce_legacy_attrs(p)
    if attrs:
        lines.append("Thuộc tính:")
        for k, v in attrs.items():
            if v in (None, "", []):
                continue
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x) for x in v)
            lines.append(f"  • {k}: {v}")

    pts = p.get("selling_points") or []
    if pts:
        lines.append("Điểm bán:")
        lines.extend(f"  • {s}" for s in pts if s)

    faq = p.get("faq") or {}
    if faq:
        lines.append("Hỏi & Đáp:")
        for q, a in faq.items():
            lines.append(f"  Hỏi: {q}")
            lines.append(f"  Đáp: {a}")

    return "\n".join(lines)


def _extract_code_number(comments: List[str]) -> Optional[int]:
    best: Optional[int] = None
    best_priority = 0
    for comment in comments:
        text = comment.strip().lower()
        if ": " in text:
            text = text.split(": ", 1)[1]
        for m in _CODE_RE.finditer(text):
            num_str = m.group(1) or m.group(2) or m.group(3)
            num = int(num_str)
            priority = 2 if (m.group(1) or m.group(2)) else 1
            if priority > best_priority:
                best_priority = priority
                best = num
    return best


class ProductCatalog:
    """Singleton-per-path catalog. Hot-reload theo file mtime + CRUD operations."""

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
        """Persist current products list to JSON file."""
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
                    # Cho phép đổi id; check unique nếu đổi
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
        for sp in p.get("selling_points", []) or []:
            for w in str(sp).lower().split():
                if len(w) > 2 and w in tokens:
                    score += 1
        attrs = _coerce_legacy_attrs(p)
        for v in attrs.values():
            text = str(v).lower()
            for w in text.split():
                if len(w) > 2 and w in tokens:
                    score += 1
        for q in (p.get("faq") or {}).keys():
            for w in str(q).lower().split():
                if len(w) > 2 and w in tokens:
                    score += 2
        if p.get("description"):
            for w in str(p["description"]).lower().split():
                if len(w) > 2 and w in tokens:
                    score += 1
        return score

    def get_relevant_product(
        self, comments: List[str]
    ) -> Tuple[str, Optional[int]]:
        """Match comments → (product_context_text, idx_0based_hoặc_None)."""
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
