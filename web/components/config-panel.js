// <config-panel> — dynamic settings form (brain/llm/tts/avatar/server/studio).

import { LiveElement } from "./shared/element.js";
import { api, escapeHtml, escapeAttr } from "./shared/api.js";
import { toast } from "./shared/toast.js";

const GROUPS = [
  { id: "brain",  title: "Brain bán hàng",   subtitle: "Pitch, FAQ retrieval, session tuning." },
  { id: "llm",    title: "LLM",              subtitle: "Provider, model, temperature, max tokens." },
  { id: "tts",    title: "TTS / Voice",      subtitle: "Engine, voice id, tốc độ, độ ổn định." },
  { id: "avatar", title: "Avatar",           subtitle: "Model, batch, smoothing, gesture priority." },
  { id: "server", title: "Server / Transport", subtitle: "WSStream, virtualcam, ports." },
  { id: "studio", title: "Studio",           subtitle: "Đường dẫn data, jobs, training defaults." },
];

class ConfigPanel extends LiveElement {
  constructor() {
    super();
    this._schema = {};
    this._current = {};
  }

  render() {
    this.innerHTML = `
      <div class="panel-head">
        <div class="title-block">
          <h2>Cài đặt hệ thống</h2>
          <div class="subtitle">
            Toàn bộ tham số có thể chỉnh tại đây. Field có <span class="badge restart">RESTART</span> cần khởi động lại server;
            <span class="badge dynamic">DYNAMIC</span> áp dụng ngay; <span class="badge secret">SECRET</span> được mã hóa.
          </div>
        </div>
      </div>

      ${GROUPS.map((g) => `
        <div class="card">
          <div class="card-head">
            <div>
              <h3>${g.title}</h3>
              <span class="subtitle">${g.subtitle}</span>
            </div>
          </div>
          <form data-config-form data-group="${g.id}"></form>
        </div>`).join("")}

      <div class="card">
        <div class="card-head">
          <div>
            <h3>Raw JSON</h3>
            <span class="subtitle">Snapshot toàn bộ config hiện tại.</span>
          </div>
          <div class="actions">
            <button id="copy-raw" class="btn-secondary btn-small">⧉ Copy</button>
          </div>
        </div>
        <pre id="config-raw"></pre>
      </div>
    `;
  }

  bind() {
    this.on("#copy-raw", "click", async () => {
      try {
        await navigator.clipboard.writeText(this.$("#config-raw").textContent);
        toast("Đã copy JSON", "ok");
      } catch {
        toast("Copy thất bại", "error");
      }
    });
  }

  afterMount() { this.refresh(); }
  onActivate() { this.refresh(); }

  async refresh() {
    const j = await api("/config");
    if (j.code !== 0) { toast(j.msg, "error"); return; }
    this._schema = j.data.schema;
    this._current = j.data.current;
    this.$("#config-raw").textContent = JSON.stringify(this._current, null, 2);
    for (const form of this.querySelectorAll("[data-config-form]")) {
      this.renderGroupInto(form, form.dataset.group);
    }
  }

  /** Public — used by other panels to render a group inline */
  async renderGroupInto(form, group) {
    if (!form) return;
    if (!this._schema || !Object.keys(this._schema).length) {
      const j = await api("/config");
      if (j.code !== 0) return;
      this._schema = j.data.schema;
      this._current = j.data.current;
    }
    form.innerHTML = "";
    const fields = Object.entries(this._schema).filter(([_, m]) => m.group === group);
    if (!fields.length) {
      form.innerHTML = `<div class="empty-state" style="margin:0"><span class="empty-icon">⚙️</span>Nhóm này chưa có field.</div>`;
      return;
    }
    for (const [key, meta] of fields) {
      const value = this._current[key];
      const row = document.createElement("div");
      row.className = "field-row";
      const id = `cfg-${group}-${key}`;
      const badge = meta.restart
        ? '<span class="badge restart">RESTART</span>'
        : '<span class="badge dynamic">DYNAMIC</span>';
      const secret = meta.secret ? '<span class="badge secret">SECRET</span>' : "";
      let inputHtml = "";
      if (meta.choices) {
        inputHtml = `<select id="${id}" name="${key}">${meta.choices.map((c) =>
          `<option value="${escapeAttr(c)}" ${String(value) === c ? "selected" : ""}>${escapeHtml(c)}</option>`).join("")}</select>`;
      } else if (meta.type === "bool") {
        inputHtml = `<select id="${id}" name="${key}">
          <option value="true"  ${value ? "selected" : ""}>Bật</option>
          <option value="false" ${!value ? "selected" : ""}>Tắt</option>
        </select>`;
      } else if (meta.type === "int" || meta.type === "float") {
        inputHtml = `<input id="${id}" name="${key}" type="number" ${meta.type === "float" ? 'step="any"' : ""} value="${escapeAttr(value ?? "")}" />`;
      } else if (meta.secret) {
        inputHtml = `<input id="${id}" name="${key}" type="password" value="${escapeAttr(value ?? "")}" placeholder="*** giữ nguyên ***" />`;
      } else {
        inputHtml = `<input id="${id}" name="${key}" type="text" value="${escapeAttr(value ?? "")}" />`;
      }
      row.innerHTML = `
        <label for="${id}" class="field-label">
          <span class="field-key">${escapeHtml(key)}</span>${badge}${secret}
          <span class="field-desc">${escapeHtml(meta.description || "")}</span>
        </label>
        <div>${inputHtml}</div>`;
      form.appendChild(row);
    }
    if (!form.querySelector(".form-actions")) {
      const actions = document.createElement("div");
      actions.className = "form-actions";
      actions.innerHTML = `<button type="submit" class="btn-primary">Lưu nhóm này</button>`;
      form.appendChild(actions);
    }
    form.onsubmit = async (e) => {
      e.preventDefault();
      const payload = {};
      for (const [key, meta] of fields) {
        const el = form[key];
        if (!el) continue;
        let v = el.value;
        if (meta.type === "bool")       v = v === "true";
        else if (meta.type === "int")   v = parseInt(v) || 0;
        else if (meta.type === "float") v = parseFloat(v) || 0;
        if (meta.secret && (v === "***" || v === "")) continue;
        payload[key] = v;
      }
      const j = await api("/config", { method: "POST", body: payload });
      if (j.code === 0) {
        const applied = (j.data.applied || []).length;
        const restartReq = j.data.restart_required || [];
        let msg = `Đã lưu ${applied} field`;
        if (j.data.brain_restarted) msg += " (brain restart)";
        if (restartReq.length) msg += `. Cần restart server cho: ${restartReq.join(", ")}`;
        toast(msg, "ok");
        this.refresh();
      } else toast(j.msg, "error");
    };
  }
}

customElements.define("config-panel", ConfigPanel);
