// <product-panel> — Product CRUD tối giản: chỉ Tên + Mô tả text.
// User paste full text bất kỳ ngành hàng, LLM brain xử lý trực tiếp.

import { LiveElement } from "./shared/element.js";
import { api, escapeHtml, escapeAttr } from "./shared/api.js";
import { toast } from "./shared/toast.js";

class ProductPanel extends LiveElement {
  constructor() {
    super();
    this._editingId = null;
  }

  render() {
    this.innerHTML = `
      <div class="panel-head">
        <div class="title-block">
          <h2>Sản phẩm</h2>
          <div class="subtitle">
            Nhập tên + paste mô tả đầy đủ (giá, màu, size, thành phần, FAQ, USP…). LLM nhận nguyên gốc — quần áo, điện tử, mỹ phẩm, F&amp;B, dịch vụ đều dùng được.
          </div>
        </div>
        <div class="actions">
          <button class="btn-secondary" id="btn-import-json">⇡ Import JSON</button>
          <button id="btn-new-product" class="btn-primary">+ Thêm sản phẩm</button>
          <input type="file" id="import-file" accept=".json,application/json" hidden />
        </div>
      </div>

      <div class="card" style="padding:0">
        <div class="table-wrap">
          <table class="data-table" id="products-table">
            <thead>
              <tr>
                <th style="width:140px">ID</th>
                <th style="width:240px">Tên</th>
                <th>Mô tả</th>
                <th style="width:140px"></th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
        <div id="products-empty" class="empty-state" hidden>
          <span class="empty-icon">📦</span>
          Chưa có sản phẩm nào. Nhấn <b>Thêm sản phẩm</b> hoặc <b>Import JSON</b> để bắt đầu.
        </div>
      </div>

      <div class="modal" id="product-modal" aria-hidden="true">
        <div class="modal-body" role="dialog" aria-modal="true" aria-labelledby="product-modal-title">
          <header>
            <h3 id="product-modal-title">Sản phẩm</h3>
            <button class="modal-close" data-close aria-label="Đóng">&times;</button>
          </header>
          <form id="product-form">
            <label>Tên sản phẩm (để dễ chọn / xem)
              <input type="text" name="name" required placeholder="vd: Áo thun cotton mùa hè 2024" />
            </label>
            <label>Mô tả đầy đủ
              <textarea name="text" rows="14" required
                placeholder="Paste mọi thông tin: giá, màu, size, chất liệu, USP, FAQ, hướng dẫn bảo quản, chính sách đổi trả, mã giảm giá… Càng chi tiết LLM tư vấn càng chuẩn."></textarea>
            </label>
            <details class="adv-row">
              <summary class="muted" style="cursor:pointer;padding:var(--space-2) 0">Tùy chỉnh ID (mặc định tự sinh)</summary>
              <label>ID (a-z, 0-9, _, -, .)
                <input type="text" name="id" pattern="[a-zA-Z0-9_\\-\\.]+" placeholder="để trống = auto" />
              </label>
            </details>
          </form>
          <div class="modal-footer">
            <button type="button" class="btn-secondary" data-close>Hủy</button>
            <button type="submit" form="product-form" class="btn-primary">Lưu</button>
          </div>
        </div>
      </div>
    `;
  }

  bind() {
    this.on("#btn-new-product",  "click",  () => this._openModal(null));
    this.on("#btn-import-json",  "click",  () => this.$("#import-file").click());
    this.on("#import-file",      "change", (e) => this._importJson(e));
    this.on("#products-table",   "click",  (e) => this._onTableClick(e));
    this.on("#product-modal",    "click",  (e) => {
      if (e.target.id === "product-modal" || e.target.dataset.close !== undefined) {
        this.$("#product-modal").classList.remove("open");
      }
    });
    this.on("#product-form", "submit", (e) => this._save(e));
  }

  afterMount() { this.refresh(); }
  onActivate() { this.refresh(); }

  async refresh() {
    const j = await api("/studio/products");
    const tb = this.$("#products-table tbody");
    const empty = this.$("#products-empty");
    const table = this.$("#products-table");
    tb.innerHTML = "";
    if (j.code !== 0) return;
    const list = j.data.products || [];
    if (!list.length) {
      empty.hidden = false;
      table.style.display = "none";
      return;
    }
    empty.hidden = true;
    table.style.display = "";
    for (const p of list) {
      const text = p.text || p.description || "";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><code>${escapeHtml(p.id || "")}</code></td>
        <td>${escapeHtml(p.name || "—")}</td>
        <td class="subtle">${escapeHtml(text.substring(0, 140))}${text.length > 140 ? "…" : ""}</td>
        <td class="actions">
          <button class="btn-small" data-edit="${escapeAttr(p.id)}">Sửa</button>
          <button class="btn-small btn-danger" data-del="${escapeAttr(p.id)}">Xóa</button>
        </td>`;
      tb.appendChild(tr);
    }
  }

  async _onTableClick(e) {
    if (e.target.dataset.edit !== undefined) {
      this._openModal(e.target.dataset.edit);
    } else if (e.target.dataset.del !== undefined) {
      const id = e.target.dataset.del;
      if (!confirm(`Xóa sản phẩm "${id}"?`)) return;
      const r = await api(`/studio/products/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (r.code === 0) { toast("Đã xóa", "ok"); this.refresh(); }
      else toast(r.msg, "error");
    }
  }

  async _importJson(e) {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    const r = await fetch("/studio/products/upload", { method: "POST", body: fd });
    const j = await r.json();
    if (j.code === 0) { toast(`Import ${j.data.count} sản phẩm`, "ok"); this.refresh(); }
    else toast(j.msg, "error");
    e.target.value = "";
  }

  async _openModal(pid) {
    this._editingId = pid;
    const form = this.$("#product-form");
    form.reset();

    let p = {};
    if (pid) {
      const j = await api("/studio/products");
      if (j.code === 0) p = (j.data.products || []).find((x) => String(x.id) === String(pid)) || {};
      this.$("#product-modal-title").textContent = `Sửa: ${pid}`;
    } else {
      this.$("#product-modal-title").textContent = "Sản phẩm mới";
    }

    if (form.id)   form.id.value   = p.id   || "";
    if (form.name) form.name.value = p.name || "";
    if (form.text) form.text.value = p.text || this._legacyToText(p);

    this.$("#product-modal").classList.add("open");
  }

  /** Chuyển product schema cũ → text block để user edit tiếp. Chỉ chạy khi mở
   *  sản phẩm có sẵn từ data cũ. New product luôn dùng `text` thẳng. */
  _legacyToText(p) {
    const parts = [];
    if (p.price)       parts.push(`Giá: ${p.price}`);
    if (p.category)    parts.push(`Danh mục: ${p.category}`);
    if (p.description) parts.push(p.description);
    const attrs = p.attributes || {};
    if (p.colors)   attrs["Màu sắc"]   = Array.isArray(p.colors)   ? p.colors.join(", ")   : p.colors;
    if (p.sizes)    attrs["Kích cỡ"]   = Array.isArray(p.sizes)    ? p.sizes.join(", ")    : p.sizes;
    if (p.material) attrs["Chất liệu"] = p.material;
    for (const [k, v] of Object.entries(attrs)) {
      parts.push(`${k}: ${Array.isArray(v) ? v.join(", ") : v}`);
    }
    if ((p.selling_points || []).length) {
      parts.push("Điểm bán:");
      for (const sp of p.selling_points) parts.push(`- ${sp}`);
    }
    if (p.faq && Object.keys(p.faq).length) {
      parts.push("FAQ:");
      for (const [q, a] of Object.entries(p.faq)) parts.push(`Hỏi: ${q}\nĐáp: ${a}`);
    }
    return parts.join("\n");
  }

  async _save(e) {
    e.preventDefault();
    const form = e.target;
    const payload = {
      id:   form.id.value.trim(),
      name: form.name.value.trim(),
      text: form.text.value.trim(),
    };
    if (!payload.name) { toast("Cần nhập Tên", "error"); return; }
    if (!payload.text) { toast("Cần nhập Mô tả", "error"); return; }

    let r;
    if (this._editingId !== null) {
      r = await api(`/studio/products/${encodeURIComponent(this._editingId)}`, { method: "PUT", body: payload });
    } else {
      r = await api("/studio/products", { method: "POST", body: payload });
    }
    if (r.code === 0) {
      toast("Đã lưu", "ok");
      this.$("#product-modal").classList.remove("open");
      this.refresh();
      document.querySelector("live-panel")?.refreshProducts?.();
    } else toast(r.msg, "error");
  }
}

customElements.define("product-panel", ProductPanel);
