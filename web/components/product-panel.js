// <product-panel> — Product CRUD (table + modal form).

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
        <h2>Sản phẩm</h2>
        <div class="btn-row">
          <button id="btn-new-product" class="btn-primary">+ Thêm sản phẩm</button>
          <button class="btn-secondary" id="btn-import-json">Import JSON</button>
          <input type="file" id="import-file" accept=".json,application/json" hidden />
        </div>
      </div>
      <p class="hint">CRUD đầy đủ cho bất kỳ mặt hàng nào (quần áo, điện tử, mỹ phẩm, thực phẩm, dịch vụ). Schema linh hoạt — chỉ <code>id</code> bắt buộc.</p>

      <table class="data-table" id="products-table">
        <thead><tr><th>ID</th><th>Tên</th><th>Giá</th><th>Mô tả</th><th></th></tr></thead>
        <tbody></tbody>
      </table>

      <div class="modal" id="product-modal">
        <div class="modal-body">
          <header>
            <h3 id="product-modal-title">Sản phẩm</h3>
            <button class="modal-close" data-close>&times;</button>
          </header>
          <form id="product-form">
            <div class="grid-2">
              <label>ID (bắt buộc, unique)
                <input type="text" name="id" required pattern="[a-zA-Z0-9_\\-\\.]+" />
              </label>
              <label>Tên
                <input type="text" name="name" />
              </label>
              <label>Giá
                <input type="text" name="price" placeholder="vd: 299.000đ / $50 / liên hệ" />
              </label>
              <label>Danh mục
                <input type="text" name="category" placeholder="vd: thời trang, điện tử…" />
              </label>
              <label class="span-2">Mô tả
                <textarea name="description" rows="3"></textarea>
              </label>
              <label class="span-2">Ảnh URL hoặc đường dẫn
                <input type="text" name="image_url" placeholder="https://… hoặc data/images/x.jpg" />
              </label>
            </div>

            <h4>Thuộc tính tùy chỉnh <button type="button" class="btn-small" id="attr-add">+</button></h4>
            <div id="attrs-editor" class="kv-editor"></div>

            <h4>Điểm bán (mỗi dòng 1 ý) <button type="button" class="btn-small" id="sp-add">+</button></h4>
            <div id="sp-editor" class="list-editor"></div>

            <h4>FAQ (Hỏi → Đáp) <button type="button" class="btn-small" id="faq-add">+</button></h4>
            <div id="faq-editor" class="kv-editor"></div>

            <div class="modal-footer">
              <button type="submit">Lưu</button>
              <button type="button" class="btn-secondary" data-close>Hủy</button>
            </div>
          </form>
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
    this.on("#attr-add", "click", () => this._addKv(this.$("#attrs-editor"), "", ""));
    this.on("#sp-add",   "click", () => this._addList(this.$("#sp-editor"), ""));
    this.on("#faq-add",  "click", () => this._addKv(this.$("#faq-editor"), "", ""));
    this.on("#product-form", "submit", (e) => this._save(e));
  }

  afterMount() {
    this.refresh();
  }

  onActivate() {
    this.refresh();
  }

  async refresh() {
    const j = await api("/studio/products");
    const tb = this.$("#products-table tbody");
    tb.innerHTML = "";
    if (j.code !== 0) return;
    for (const p of j.data.products || []) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><code>${escapeHtml(p.id || "")}</code></td>
        <td>${escapeHtml(p.name || "—")}</td>
        <td>${escapeHtml(p.price || "")}</td>
        <td>${escapeHtml((p.description || "").substring(0, 80))}${(p.description || "").length > 80 ? "…" : ""}</td>
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
    this.$("#attrs-editor").innerHTML = "";
    this.$("#sp-editor").innerHTML = "";
    this.$("#faq-editor").innerHTML = "";

    let p = {};
    if (pid) {
      const j = await api("/studio/products");
      if (j.code === 0) p = (j.data.products || []).find((x) => String(x.id) === String(pid)) || {};
      this.$("#product-modal-title").textContent = `Sửa: ${pid}`;
    } else {
      this.$("#product-modal-title").textContent = "Sản phẩm mới";
    }

    for (const k of ["id", "name", "price", "description", "image_url", "category"]) {
      if (form[k]) form[k].value = p[k] || "";
    }
    const attrs = { ...(p.attributes || {}) };
    if (p.colors)   attrs["Màu sắc"]   = Array.isArray(p.colors)   ? p.colors.join(", ")   : p.colors;
    if (p.sizes)    attrs["Kích cỡ"]   = Array.isArray(p.sizes)    ? p.sizes.join(", ")    : p.sizes;
    if (p.material) attrs["Chất liệu"] = p.material;
    for (const [k, v] of Object.entries(attrs)) {
      this._addKv(this.$("#attrs-editor"), k, Array.isArray(v) ? v.join(", ") : v);
    }
    for (const s of p.selling_points || []) this._addList(this.$("#sp-editor"), s);
    for (const [q, a] of Object.entries(p.faq || {})) this._addKv(this.$("#faq-editor"), q, a);

    this.$("#product-modal").classList.add("open");
  }

  _addKv(root, k, v) {
    const row = document.createElement("div");
    row.className = "kv-row";
    row.innerHTML = `
      <input type="text" placeholder="key" value="${escapeAttr(k)}" />
      <input type="text" placeholder="value" value="${escapeAttr(v)}" />
      <button type="button">×</button>`;
    row.querySelector("button").onclick = () => row.remove();
    root.appendChild(row);
  }
  _addList(root, v) {
    const row = document.createElement("div");
    row.className = "list-row";
    row.innerHTML = `<input type="text" value="${escapeAttr(v)}" /><button type="button">×</button>`;
    row.querySelector("button").onclick = () => row.remove();
    root.appendChild(row);
  }

  async _save(e) {
    e.preventDefault();
    const form = e.target;
    const payload = {
      id: form.id.value.trim(),
      name: form.name.value.trim(),
      price: form.price.value.trim(),
      description: form.description.value.trim(),
      image_url: form.image_url.value.trim(),
      category: form.category.value.trim(),
      attributes: {},
      selling_points: [],
      faq: {},
    };
    for (const row of this.$$(".kv-row", this.$("#attrs-editor"))) {
      const [k, v] = row.querySelectorAll("input");
      if (k.value.trim()) payload.attributes[k.value.trim()] = v.value.trim();
    }
    for (const row of this.$$(".list-row", this.$("#sp-editor"))) {
      const v = row.querySelector("input").value.trim();
      if (v) payload.selling_points.push(v);
    }
    for (const row of this.$$(".kv-row", this.$("#faq-editor"))) {
      const [k, v] = row.querySelectorAll("input");
      if (k.value.trim()) payload.faq[k.value.trim()] = v.value.trim();
    }

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
      // Notify live-panel to refresh its product dropdown
      document.querySelector("live-panel")?.refreshProducts?.();
    } else toast(r.msg, "error");
  }

  // Replace inherited $$ for scoped queries
  $$(s, root) { return Array.from((root || this).querySelectorAll(s)); }
}

customElements.define("product-panel", ProductPanel);
