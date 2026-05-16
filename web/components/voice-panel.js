// <voice-panel> — Voice library: record mic / upload WAV → save → activate → preview.
// Endpoint: GET /studio/voices, POST /studio/voices/upload, DELETE /studio/voices/{id},
// POST /studio/voices/activate, POST /studio/voices/preview, GET /studio/voices/{id}/ref

import { LiveElement } from "./shared/element.js";
import { api, escapeHtml, escapeAttr } from "./shared/api.js";
import { toast } from "./shared/toast.js";

class VoicePanel extends LiveElement {
  constructor() {
    super();
    this._recorder = null;
    this._chunks = [];
    this._stream = null;
    this._blob = null;
    this._recordingStart = 0;
    this._timerHandle = null;
    this._activeId = null;
    this._previewAudio = null;
  }

  render() {
    this.innerHTML = `
      <div class="panel-head">
        <div class="title-block">
          <h2>Thư viện giọng nói</h2>
          <div class="subtitle">
            Clone giọng cho VieNeu TTS — thu 3-10s mẫu giọng + transcript → lưu thành voice. Active 1 voice để brain dùng.
          </div>
        </div>
      </div>

      <!-- Section 1: Tạo voice mới -->
      <div class="card">
        <h3>+ Thêm giọng mới</h3>
        <form id="voice-form">
          <div class="grid-2">
            <label class="span-2">Tên giọng (để dễ nhận biết)
              <input type="text" name="name" required placeholder="vd: Linh — nữ Sài Gòn, ngọt" />
            </label>

            <div class="span-2">
              <div class="recorder">
                <div class="rec-controls">
                  <button type="button" id="btn-rec-start" class="btn-primary">● Bắt đầu thu</button>
                  <button type="button" id="btn-rec-stop" class="btn-stop" hidden>■ Dừng</button>
                  <span id="rec-timer" class="timer muted">00:00</span>
                  <span class="muted span-spacer">hoặc</span>
                  <label class="upload-btn btn-secondary">
                    ⇡ Upload WAV
                    <input type="file" id="file-input" accept="audio/wav,audio/x-wav,audio/wave" hidden />
                  </label>
                </div>
                <p class="hint">Cần 3-30s, một giọng rõ, không nhạc nền. Tốt nhất 5-10s, đọc đúng câu transcript bên dưới.</p>
                <div id="audio-preview" hidden>
                  <audio id="rec-audio" controls style="width:100%"></audio>
                  <div class="audio-meta" id="audio-meta"></div>
                </div>
              </div>
            </div>

            <label class="span-2">Transcript (chữ khớp y với audio đã thu)
              <textarea name="text" rows="3" required
                placeholder="vd: Xin chào các bạn, mình là Linh, hôm nay shop có rất nhiều ưu đãi cho mọi người."></textarea>
            </label>

            <details class="span-2 adv-row">
              <summary class="muted">Tùy chỉnh ID</summary>
              <label>ID (a-z, 0-9, _, -, để trống = tự sinh từ tên)
                <input type="text" name="voice_id" pattern="[a-z0-9_\\-]{2,40}" />
              </label>
            </details>
          </div>

          <details class="adv-row" id="clone-test-row">
            <summary class="muted">🎧 Nghe thử trước khi lưu (text mẫu test clone)</summary>
            <label>Text mẫu (sẽ đọc bằng giọng vừa thu)
              <textarea name="sample_text" rows="2"
                placeholder="Xin chào, đây là giọng nói được nhân bản, mọi người nghe thử có giống không nha."></textarea>
            </label>
            <div class="btn-row">
              <button type="button" class="btn-secondary" id="btn-test-clone">🔊 Nghe thử clone</button>
              <span class="muted" id="clone-hint">Synthesize ~2-5s — chưa lưu vào library</span>
            </div>
            <div id="clone-status" class="status"></div>
            <audio id="clone-audio" controls style="width:100%;margin-top:8px" hidden></audio>
          </details>

          <div class="btn-row">
            <button type="submit" class="btn-primary" id="btn-save">💾 Lưu giọng</button>
            <button type="reset" class="btn-secondary">Reset</button>
          </div>
          <div id="save-status" class="status"></div>
        </form>
      </div>

      <!-- Section 2: Thư viện -->
      <div class="card">
        <h3>📚 Thư viện</h3>
        <div id="voices-list">
          <div class="empty-state">Chưa có giọng nào — thu hoặc upload bên trên.</div>
        </div>
      </div>

      <!-- Section 3: Preview -->
      <div class="card">
        <h3>🔊 Nghe thử voice active</h3>
        <form id="preview-form">
          <label>Text test (≤ 300 ký tự)
            <textarea name="text" rows="2"
              placeholder="Xin chào, đây là giọng cloned của shop. Hôm nay có deal cực sốc nha!"></textarea>
          </label>
          <div class="btn-row">
            <button type="submit" class="btn-primary" id="btn-preview">▶ Synthesize + nghe</button>
            <span class="muted" id="preview-hint">Cần ít nhất 1 voice Active</span>
          </div>
          <div id="preview-status" class="status"></div>
          <audio id="preview-audio" controls style="width:100%;margin-top:8px" hidden></audio>
        </form>
      </div>
    `;
  }

  bind() {
    this.on("#btn-rec-start", "click", () => this._startRecord());
    this.on("#btn-rec-stop",  "click", () => this._stopRecord());
    this.on("#file-input",    "change", (e) => this._onUpload(e));
    this.on("#btn-test-clone","click", () => this._testClone());
    this.on("#voice-form",    "submit", (e) => this._save(e));
    this.on("#voice-form",    "reset",  () => this._clearAudio());
    this.on("#preview-form",  "submit", (e) => this._preview(e));
    this.on("#voices-list",   "click",  (e) => this._onListClick(e));
  }

  afterMount() { this.refresh(); }
  onActivate() { this.refresh(); }

  beforeUnmount() {
    this._stopRecord(true);
    if (this._previewAudio) {
      try { URL.revokeObjectURL(this._previewAudio); } catch {}
    }
  }

  // ─── Record qua MediaRecorder ─────────────────────────────────────
  async _startRecord() {
    if (this._recorder) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      toast("Browser không hỗ trợ ghi âm. Hãy upload file WAV.", "error");
      return;
    }
    try {
      this._stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      toast("Mic bị từ chối: " + e.message, "error");
      return;
    }

    // Ưu tiên mime audio/webm hoặc audio/ogg — sẽ convert WAV server-side?
    // Đơn giản hơn: record WebM, browser hỗ trợ rộng. Server bound bằng
    // soundfile (ffmpeg/libsndfile) → WAV/WebM/Ogg đều decode được.
    let mime = "";
    for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg", "audio/wav"]) {
      if (MediaRecorder.isTypeSupported(m)) { mime = m; break; }
    }
    try {
      this._recorder = new MediaRecorder(this._stream, mime ? { mimeType: mime } : {});
    } catch (e) {
      toast("MediaRecorder lỗi: " + e.message, "error");
      this._cleanupStream();
      return;
    }
    this._chunks = [];
    this._recorder.ondataavailable = (ev) => { if (ev.data.size > 0) this._chunks.push(ev.data); };
    this._recorder.onstop = () => this._onRecordStopped(mime);
    this._recorder.start(250);

    this._recordingStart = Date.now();
    this.$("#btn-rec-start").hidden = true;
    this.$("#btn-rec-stop").hidden = false;
    this._timerHandle = setInterval(() => {
      const sec = Math.floor((Date.now() - this._recordingStart) / 1000);
      const mm = String(Math.floor(sec / 60)).padStart(2, "0");
      const ss = String(sec % 60).padStart(2, "0");
      this.$("#rec-timer").textContent = `${mm}:${ss}`;
      // Auto stop sau 30s
      if (sec >= 30) this._stopRecord();
    }, 250);
  }

  _stopRecord(silent = false) {
    if (this._timerHandle) { clearInterval(this._timerHandle); this._timerHandle = null; }
    const rec = this._recorder;
    if (rec && rec.state !== "inactive") {
      try { rec.stop(); } catch {}
    }
    this._recorder = null;
    this._cleanupStream();
    this.$("#btn-rec-start").hidden = false;
    this.$("#btn-rec-stop").hidden = true;
    if (silent) this._clearAudio();
  }

  _cleanupStream() {
    if (this._stream) {
      for (const t of this._stream.getTracks()) try { t.stop(); } catch {}
      this._stream = null;
    }
  }

  _onRecordStopped(mime) {
    if (!this._chunks.length) {
      toast("Không có audio thu được", "error");
      return;
    }
    this._blob = new Blob(this._chunks, { type: mime || "audio/webm" });
    this._chunks = [];
    this._renderAudioPreview(this._blob, (Date.now() - this._recordingStart) / 1000);
  }

  _onUpload(e) {
    const f = e.target.files[0];
    if (!f) return;
    this._blob = f;
    // Tạm thời để duration unknown — sẽ tính bằng <audio> metadata
    this._renderAudioPreview(f, null);
    e.target.value = "";
  }

  _renderAudioPreview(blob, durationFallback) {
    const url = URL.createObjectURL(blob);
    const audio = this.$("#rec-audio");
    audio.src = url;
    this.$("#audio-preview").hidden = false;

    audio.onloadedmetadata = () => {
      const dur = isFinite(audio.duration) ? audio.duration : durationFallback;
      const sizeKb = (blob.size / 1024).toFixed(1);
      let warn = "";
      if (dur != null && dur < 3) warn = ' <span class="danger">QUÁ NGẮN (cần ≥3s)</span>';
      else if (dur != null && dur > 30) warn = ' <span class="warning">SẼ BỊ CẮT (>30s)</span>';
      this.$("#audio-meta").innerHTML = `
        <span>Thời lượng: <b>${dur != null ? dur.toFixed(1) + "s" : "?"}</b></span>
        <span>Size: <b>${sizeKb} KB</b></span>
        <span>Type: <code>${escapeHtml(blob.type || "?")}</code></span>${warn}`;
    };
  }

  _clearAudio() {
    this._blob = null;
    const audio = this.$("#rec-audio");
    if (audio.src) {
      try { URL.revokeObjectURL(audio.src); } catch {}
      audio.removeAttribute("src");
    }
    this.$("#audio-preview").hidden = true;
    this.$("#audio-meta").textContent = "";
    this.$("#rec-timer").textContent = "00:00";
  }

  // ─── Test clone (synth WITHOUT saving) ─────────────────────────────
  async _testClone() {
    if (!this._blob) {
      toast("Cần thu hoặc upload audio trước", "error");
      return;
    }
    const form = this.$("#voice-form");
    const refText = form.text.value.trim();
    if (!refText) {
      toast("Cần nhập Transcript khớp với audio", "error");
      return;
    }
    const sampleText = form.sample_text?.value.trim() ||
      "Xin chào, đây là giọng nói được nhân bản, mọi người nghe thử có giống không nha.";

    const fd = new FormData();
    fd.append("text", refText);
    fd.append("sample_text", sampleText);
    const ext = (this._blob.type.includes("webm") ? "webm"
              : this._blob.type.includes("ogg") ? "ogg" : "wav");
    fd.append("file", this._blob, `ref.${ext}`);

    this._setStatus("#clone-status", "Đang synthesize... (chưa lưu)");
    this.$("#btn-test-clone").disabled = true;
    try {
      const r = await fetch("/studio/voices/preview_clone", { method: "POST", body: fd });
      if (!r.ok) {
        let msg = "Test fail";
        try { const j = await r.json(); msg = j.msg || msg; } catch {}
        this._setStatus("#clone-status", msg, "error");
        return;
      }
      const blob = await r.blob();
      if (this._cloneAudioUrl) try { URL.revokeObjectURL(this._cloneAudioUrl); } catch {}
      this._cloneAudioUrl = URL.createObjectURL(blob);
      const player = this.$("#clone-audio");
      player.src = this._cloneAudioUrl;
      player.hidden = false;
      player.play().catch(() => {});
      this._setStatus("#clone-status",
        `OK (${(blob.size / 1024).toFixed(1)} KB) — nếu ưng, bấm Lưu`, "ok");
    } catch (e) {
      this._setStatus("#clone-status", "Lỗi: " + e.message, "error");
    } finally {
      this.$("#btn-test-clone").disabled = false;
    }
  }

  // ─── Save ──────────────────────────────────────────────────────────
  async _save(e) {
    e.preventDefault();
    if (!this._blob) {
      toast("Cần thu hoặc upload audio trước", "error");
      return;
    }
    const form = e.target;
    const name = form.name.value.trim();
    const text = form.text.value.trim();
    const voiceId = form.voice_id?.value.trim() || "";
    if (!name || !text) {
      toast("Cần nhập đủ Tên và Transcript", "error");
      return;
    }

    const fd = new FormData();
    fd.append("name", name);
    fd.append("text", text);
    if (voiceId) fd.append("voice_id", voiceId);
    // Đặt tên file phù hợp đuôi để server detect được dạng
    const ext = (this._blob.type.includes("webm") ? "webm"
              : this._blob.type.includes("ogg") ? "ogg" : "wav");
    fd.append("file", this._blob, `ref.${ext}`);

    this._setStatus("#save-status", "Đang upload + validate...");
    this.$("#btn-save").disabled = true;
    try {
      const r = await fetch("/studio/voices/upload", { method: "POST", body: fd });
      const j = await r.json();
      if (j.code === 0) {
        this._setStatus("#save-status", `Đã lưu voice "${j.data.id}" (${j.data.duration_secs}s)`, "ok");
        toast("Đã lưu giọng", "ok");
        form.reset();
        this._clearAudio();
        this.refresh();
      } else {
        this._setStatus("#save-status", j.msg, "error");
      }
    } catch (e) {
      this._setStatus("#save-status", "Lỗi mạng: " + e.message, "error");
    } finally {
      this.$("#btn-save").disabled = false;
    }
  }

  // ─── List + actions ────────────────────────────────────────────────
  async refresh() {
    const j = await api("/studio/voices");
    if (j.code !== 0) return;
    this._activeId = j.data.active_id || null;
    const list = j.data.voices || [];
    const root = this.$("#voices-list");
    if (!list.length) {
      root.innerHTML = `<div class="empty-state">Chưa có giọng nào — thu hoặc upload bên trên.</div>`;
      this.$("#preview-hint").textContent = "Cần ít nhất 1 voice Active";
      return;
    }

    root.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Tên</th>
            <th>Thời lượng</th>
            <th>Transcript</th>
            <th>Active</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${list.map((v) => `
            <tr>
              <td><code>${escapeHtml(v.id)}</code></td>
              <td>${escapeHtml(v.name || "—")}</td>
              <td class="muted">${(v.duration_secs ?? 0).toFixed(1)}s</td>
              <td class="muted">${(v.transcript_chars ?? 0)} ký tự</td>
              <td>${v.active ? '<span class="badge active">ACTIVE</span>' : ""}</td>
              <td class="actions">
                <audio src="/studio/voices/${encodeURIComponent(v.id)}/ref" controls preload="none" style="height:28px"></audio>
                ${v.active ? "" : `<button class="btn-small btn-primary" data-act="activate" data-id="${escapeAttr(v.id)}">Active</button>`}
                <button class="btn-small btn-danger" data-act="delete" data-id="${escapeAttr(v.id)}">Xóa</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;

    const active = list.find((v) => v.active);
    this.$("#preview-hint").textContent = active
      ? `Active: ${active.name || active.id}`
      : "Chưa Active voice nào";
  }

  async _onListClick(e) {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;

    if (act === "activate") {
      const r = await api("/studio/voices/activate", { method: "POST", body: { voice_id: id } });
      if (r.code === 0) {
        toast(`Active: ${id}` + (r.data.brain_restarted ? " (brain restart)" : ""), "ok");
        this.refresh();
      } else toast(r.msg, "error");
    } else if (act === "delete") {
      if (!confirm(`Xóa giọng "${id}"?`)) return;
      const r = await api(`/studio/voices/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (r.code === 0) { toast("Đã xóa", "ok"); this.refresh(); }
      else toast(r.msg, "error");
    }
  }

  // ─── Preview synthesize ────────────────────────────────────────────
  async _preview(e) {
    e.preventDefault();
    if (!this._activeId) {
      toast("Cần Active 1 voice trước khi preview", "error");
      return;
    }
    const text = e.target.text.value.trim() ||
      "Xin chào, đây là giọng nói được nhân bản từ thư viện.";
    this._setStatus("#preview-status", "Đang synthesize... (có thể mất 2-5s)");
    this.$("#btn-preview").disabled = true;
    try {
      const r = await fetch("/studio/voices/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice_id: this._activeId, text }),
      });
      if (!r.ok) {
        let msg = "Synth fail";
        try { const j = await r.json(); msg = j.msg || msg; } catch {}
        this._setStatus("#preview-status", msg, "error");
        return;
      }
      const blob = await r.blob();
      if (this._previewAudio) try { URL.revokeObjectURL(this._previewAudio); } catch {}
      this._previewAudio = URL.createObjectURL(blob);
      const player = this.$("#preview-audio");
      player.src = this._previewAudio;
      player.hidden = false;
      player.play().catch(() => {});
      this._setStatus("#preview-status", `OK (${(blob.size / 1024).toFixed(1)} KB)`, "ok");
    } catch (e) {
      this._setStatus("#preview-status", "Lỗi: " + e.message, "error");
    } finally {
      this.$("#btn-preview").disabled = false;
    }
  }

  _setStatus(sel, msg, type = "") {
    const el = this.$(sel);
    if (!el) return;
    el.textContent = msg;
    el.className = "status" + (type ? " " + type : "");
  }
}

customElements.define("voice-panel", VoicePanel);
