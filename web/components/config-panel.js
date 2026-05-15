// <config-panel> — dynamic settings form (brain/llm/tts/avatar/server/studio).

import { LiveElement } from "./shared/element.js";
import { api, escapeHtml, escapeAttr } from "./shared/api.js";
import { toast } from "./shared/toast.js";

class ConfigPanel extends LiveElement {
  constructor() {
    super();
    this._schema = {};
    this._current = {};
  }

  render() {
    this.innerHTML = `
      <h2>Cài đặt hệ thống</h2>
      <p class="hint">Toàn bộ tham số có thể chỉnh tại đây. Field có <span class="badge restart">RESTART</span> cần khởi động lại server.</p>

      <div class="card"><h3>🧠 Bộ não bán hàng</h3><form data-config-form data-group="brain"></form></div>
      <div class="card"><h3>💬 LLM</h3><form data-config-form data-group="llm"></form></div>
      <div class="card"><h3>🎙️ TTS / Voice</h3><form data-config-form data-group="tts"></form></div>
      <div class="card"><h3>🎥 Avatar</h3><form data-config-form data-group="avatar"></form></div>
      <div class="card"><h3>🌐 Server / Transport</h3><form data-config-form data-group="server"></form></div>
      <div class="card"><h3>🛠️ Studio</h3><form data-config-form data-group="studio"></form></div>

      <div class="card">
        <h3>Raw JSON</h3>
        <pre id="config-raw"></pre>
      </div>
    `;
  }

  afterMount() {
    this.refresh();
  }

  onActivate() {
    this.refresh();
  }

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

  /** Public — used by audio-panel to render tts group inline */
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
          <option value="true" ${value ? "selected" : ""}>Bật</option>
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
          ${escapeHtml(key)}${badge}${secret}
          <span class="field-desc">${escapeHtml(meta.description || "")}</span>
        </label>
        <div>${inputHtml}</div>`;
      form.appendChild(row);
    }
    if (!form.querySelector(".form-actions")) {
      const actions = document.createElement("div");
      actions.className = "form-actions";
      actions.innerHTML = `<button type="submit">Lưu nhóm này</button>`;
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
