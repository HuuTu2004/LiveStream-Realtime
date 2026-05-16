// <video-panel> — Studio: avatar upload + preprocess/train + gesture pack + jobs.

import { LiveElement } from "./shared/element.js";
import { api, setStatus, escapeHtml, escapeAttr, getSessionId } from "./shared/api.js";
import { toast } from "./shared/toast.js";

class VideoPanel extends LiveElement {
  render() {
    this.innerHTML = `
      <div class="panel-head">
        <div class="title-block">
          <h2>Avatar Studio</h2>
          <div class="subtitle">Quy trình tạo avatar: upload video gốc → preprocess → (musetalk) train → preview. Kèm bộ gesture cử chỉ tự nhiên.</div>
        </div>
      </div>

      <div class="card-grid">
        <div class="card">
          <div class="card-head">
            <div>
              <h3>1 · Upload avatar video</h3>
              <span class="subtitle">MP4 10–60s, mặt rõ, đủ ánh sáng.</span>
            </div>
          </div>
          <form id="avatar-upload-form">
            <div class="grid-2">
              <label>Avatar ID
                <input type="text" name="name" required pattern="[a-zA-Z0-9_\\-]{2,40}" placeholder="vd: linh_v1" />
              </label>
              <label>Video MP4
                <input type="file" name="file" accept="video/*" required />
              </label>
            </div>
            <div class="btn-row">
              <button type="submit" class="btn-primary">Upload</button>
            </div>
          </form>
          <div id="avatar-upload-status" class="status"></div>
        </div>

        <div class="card">
          <div class="card-head">
            <div>
              <h3>2 · Preprocess &amp; Train</h3>
              <span class="subtitle">Chọn model phù hợp: wav2lip nhanh, musetalk chất lượng cao.</span>
            </div>
          </div>
          <form id="avatar-action-form">
            <div class="grid-2">
              <label>Avatar ID
                <select name="avatar_id" id="avatar-select"></select>
              </label>
              <label>Model
                <select name="model">
                  <option value="wav2lip">wav2lip (nhanh, pretrained)</option>
                  <option value="musetalk">musetalk (chất lượng cao)</option>
                  <option value="ultralight">ultralight</option>
                </select>
              </label>
              <label>Epochs (musetalk)
                <input type="number" name="epochs" value="20" min="1" max="200" />
              </label>
            </div>
            <div class="btn-row">
              <button type="button" class="btn-secondary" data-action="preprocess">Preprocess</button>
              <button type="button" class="btn-secondary" data-action="train">Train (musetalk)</button>
              <button type="button" class="btn-primary"   data-action="preview">Preview "Xin chào"</button>
            </div>
          </form>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <h3>3 · Danh sách avatar</h3>
            <span class="subtitle">Trạng thái xử lý của từng avatar.</span>
          </div>
          <div class="actions">
            <button id="refresh-avatars" class="btn-secondary btn-small">↻ Refresh</button>
          </div>
        </div>
        <div class="table-wrap">
          <table class="data-table" id="avatars-table">
            <thead>
              <tr>
                <th style="width:160px">ID</th>
                <th>Images</th>
                <th>Latents</th>
                <th>Face</th>
                <th>Voice</th>
                <th>Gestures</th>
                <th style="width:80px"></th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <h3>4 · Gesture pack (cử chỉ)</h3>
            <span class="subtitle">Upload clip MP4 cho mỗi cử chỉ. Trigger để test nhanh.</span>
          </div>
        </div>
        <form id="gesture-upload-form">
          <div class="grid-2">
            <label>Avatar ID
              <select name="avatar_id" class="avatar-select"></select>
            </label>
            <label>Tên cử chỉ
              <select name="name">
                <option value="wave">wave</option>
                <option value="point">point</option>
                <option value="nod">nod</option>
                <option value="smile">smile</option>
                <option value="count">count</option>
                <option value="show">show</option>
                <option value="idle">idle</option>
                <option value="talk_natural">talk_natural</option>
              </select>
            </label>
            <label class="checkbox-label" style="margin-top:var(--space-3)">
              <input type="checkbox" name="loop" />
              <span>Loop?</span>
            </label>
            <label>Blend frames
              <input type="number" name="blend" value="5" min="1" max="15" />
            </label>
            <label class="span-2">Clip MP4
              <input type="file" name="file" accept="video/*" required />
            </label>
          </div>
          <div class="btn-row">
            <button type="submit" class="btn-primary">Upload Clip</button>
          </div>
        </form>
        <div id="gesture-status" class="status"></div>

        <h4 style="margin-top:var(--space-5)">Test trigger</h4>
        <div class="btn-row" id="gesture-trigger-buttons">
          <button data-g="wave">wave</button>
          <button data-g="point">point</button>
          <button data-g="nod">nod</button>
          <button data-g="smile">smile</button>
          <button data-g="count">count</button>
          <button data-g="show">show</button>
          <button data-g="idle">idle</button>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div>
            <h3>Jobs gần nhất</h3>
            <span class="subtitle">10 job mới nhất — tự động cập nhật.</span>
          </div>
          <div class="actions">
            <button id="refresh-jobs" class="btn-secondary btn-small">↻ Refresh</button>
          </div>
        </div>
        <div id="jobs-list">
          <div class="empty-state"><span class="empty-icon">⚙️</span>Chưa có job nào.</div>
        </div>
      </div>
    `;
  }

  bind() {
    this.on("#avatar-upload-form",  "submit", (e) => this._upload(e));
    this.on("#avatar-action-form",  "click",  (e) => this._action(e));
    this.on("#avatars-table",       "click",  (e) => this._tableClick(e));
    this.on("#refresh-avatars",     "click",  () => this.refreshAvatars());
    this.on("#refresh-jobs",        "click",  () => this.refreshJobs());
    this.on("#gesture-upload-form", "submit", (e) => this._uploadGesture(e));
    this.on("#gesture-trigger-buttons", "click", (e) => this._triggerGesture(e));
  }

  afterMount() {
    this.refreshAvatars();
    this.refreshJobs();
    this._jobsInterval = setInterval(() => {
      if (this.closest(".panel")?.classList.contains("active")) this.refreshJobs();
    }, 4000);
  }

  onActivate() {
    this.refreshAvatars();
    this.refreshJobs();
  }

  beforeUnmount() {
    if (this._jobsInterval) clearInterval(this._jobsInterval);
  }

  async _upload(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    setStatus(this.$("#avatar-upload-status"), "Đang upload…");
    const r = await fetch("/studio/avatar/upload", { method: "POST", body: fd });
    const j = await r.json();
    if (j.code === 0) {
      setStatus(this.$("#avatar-upload-status"), `OK: ${j.data.avatar_id} (${(j.data.size_bytes / 1024 / 1024).toFixed(2)} MB)`, "ok");
      this.refreshAvatars();
    } else setStatus(this.$("#avatar-upload-status"), j.msg, "error");
  }

  async _action(e) {
    if (e.target.tagName !== "BUTTON") return;
    const action = e.target.dataset.action;
    if (!action) return;
    const f = e.target.form;
    const payload = { avatar_id: f.avatar_id.value, model: f.model.value };
    let j;
    if (action === "preprocess") {
      j = await api("/studio/avatar/preprocess", { method: "POST", body: payload });
    } else if (action === "train") {
      if (payload.model !== "musetalk") { toast("Train chỉ cho musetalk", "error"); return; }
      j = await api("/studio/avatar/train", { method: "POST", body: { ...payload, epochs: +f.epochs.value } });
    } else if (action === "preview") {
      j = await api("/studio/avatar/preview", {
        method: "POST",
        body: { avatar_id: payload.avatar_id, text: "Xin chào, tôi là Linh" },
      });
    }
    if (j && j.code === 0) {
      toast(`Job ${j.data.job_id} chạy`, "ok");
      this._trackJob(j.data.job_id, this.$("#avatar-upload-status"));
      this.refreshJobs();
    } else if (j) toast(j.msg, "error");
  }

  _trackJob(jobId, statusEl) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/studio/job/${jobId}/ws`);
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.event === "update") {
          const d = m.data;
          const pct = Math.round((d.progress || 0) * 100);
          const cls = d.state === "done" ? "ok" : d.state === "failed" ? "error" : "";
          setStatus(statusEl, `[${d.state}] ${pct}% — ${d.meta?.msg || ""}`, cls);
          this.refreshJobs();
        }
      } catch {}
    };
  }

  async refreshAvatars() {
    const j = await api("/studio/avatars");
    if (j.code !== 0) return;
    const list = j.data.avatars || [];
    const tb = this.$("#avatars-table tbody");
    tb.innerHTML = "";
    if (!list.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="7"><div class="empty-state" style="margin:var(--space-2)">
        <span class="empty-icon">🎭</span>Chưa có avatar nào — upload bên trên.</div></td>`;
      tb.appendChild(tr);
    } else {
      for (const a of list) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><code>${escapeHtml(a.id)}</code></td>
          ${this._capCell(a.has_full_imgs)}
          ${this._capCell(a.has_latents)}
          ${this._capCell(a.has_face_imgs)}
          ${this._capCell(a.has_voice)}
          ${this._capCell(a.has_gestures)}
          <td class="actions"><button class="btn-small btn-danger" data-del="${escapeAttr(a.id)}">Xóa</button></td>`;
        tb.appendChild(tr);
      }
    }
    for (const sel of this.querySelectorAll(".avatar-select, #avatar-select")) {
      const cur = sel.value;
      sel.innerHTML = list.map((a) => `<option value="${escapeAttr(a.id)}">${escapeHtml(a.id)}</option>`).join("");
      if (list.find((a) => a.id === cur)) sel.value = cur;
    }
  }

  _capCell(hasIt) {
    return hasIt
      ? `<td><span class="pill success" style="padding:1px 8px;font-size:10px">✓</span></td>`
      : `<td><span class="pill" style="padding:1px 8px;font-size:10px;color:var(--text-disabled)">—</span></td>`;
  }

  async _tableClick(e) {
    if (e.target.dataset.del !== undefined) {
      if (!confirm(`Xóa avatar "${e.target.dataset.del}"?`)) return;
      const r = await api("/studio/avatar/delete", { method: "POST", body: { avatar_id: e.target.dataset.del } });
      if (r.code === 0) { toast("Đã xóa", "ok"); this.refreshAvatars(); }
      else toast(r.msg, "error");
    }
  }

  async refreshJobs() {
    const j = await api("/studio/jobs");
    if (j.code !== 0) return;
    const wrap = this.$("#jobs-list");
    const jobs = (j.data.jobs || []).slice(0, 10);
    if (!jobs.length) {
      wrap.innerHTML = `<div class="empty-state"><span class="empty-icon">⚙️</span>Chưa có job nào.</div>`;
      return;
    }
    wrap.innerHTML = "";
    for (const job of jobs) {
      const div = document.createElement("div");
      div.className = "job " + (job.state || "");
      const pct = Math.round((job.progress || 0) * 100);
      const loss = job.meta?.loss !== undefined ? ` · loss=${job.meta.loss}` : "";
      const errSpan = job.error ? `<span class="error"> · ${escapeHtml(job.error)}</span>` : "";
      div.innerHTML = `
        <div class="head">
          <span><span class="id">${escapeHtml(job.id)}</span><span class="kind">${escapeHtml(job.kind || "")}</span></span>
          <span class="state-pill">${escapeHtml(job.state || "")}</span>
        </div>
        <div class="bar"><div style="width:${pct}%"></div></div>
        <div class="meta">${escapeHtml(job.meta?.msg || "")}${loss}${errSpan} · ${pct}%</div>`;
      wrap.appendChild(div);
    }
  }

  async _uploadGesture(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    setStatus(this.$("#gesture-status"), "Đang extract frames…");
    const r = await fetch("/studio/gesture/upload", { method: "POST", body: fd });
    const j = await r.json();
    if (j.code === 0) {
      setStatus(this.$("#gesture-status"), `OK ${j.data.gesture}: ${j.data.frames} frames`, "ok");
      this.refreshAvatars();
    } else setStatus(this.$("#gesture-status"), j.msg, "error");
  }

  async _triggerGesture(e) {
    if (e.target.tagName !== "BUTTON") return;
    const name = e.target.dataset.g;
    const j = await api("/set_gesture", { method: "POST", body: { sessionid: getSessionId(), name } });
    if (j.code !== 0) toast(j.msg, "error"); else toast(`Trigger ${name}`, "ok");
  }
}

customElements.define("video-panel", VideoPanel);
