// <voice-panel> — UI thân thiện non-tech: wizard 3 bước (Thu / Kiểm tra / Lưu).
// Không kỹ thuật: bỏ "ID", "TTS", "transcript", "synthesize". Dùng tiếng Việt
// gần gũi: "ghi âm", "câu đọc", "nghe thử", "đặt tên", "dùng giọng này".

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
    this._cloneAudioUrl = null;
  }

  render() {
    this.innerHTML = `
      <div class="panel-head">
        <div class="title-block">
          <h2>Giọng nói của bạn</h2>
          <div class="subtitle">
            Cho avatar nói bằng giọng của bạn — chỉ cần ghi âm <b>5–10 giây</b>, đặt tên và lưu lại.
          </div>
        </div>
      </div>

      <!-- ────────── WIZARD 3 BƯỚC ────────── -->
      <div class="card voice-wizard">

        <!-- Bước 1: Ghi âm -->
        <div class="wiz-step" id="wiz-step-1">
          <div class="step-head">
            <div class="step-num">1</div>
            <div class="step-title">
              <h3>Ghi âm giọng nói</h3>
              <div class="muted">Đọc một câu rõ ràng khoảng <b>5–10 giây</b>. Phòng yên tĩnh, không nhạc nền.</div>
            </div>
          </div>

          <div class="step-body">
            <div class="rec-row">
              <button type="button" id="btn-rec-start" class="btn-rec-big">
                <span class="rec-dot"></span> Bắt đầu thu
              </button>
              <button type="button" id="btn-rec-stop" class="btn-rec-stop" hidden>
                <span class="stop-square"></span> Dừng
              </button>
              <span id="rec-timer" class="timer-big">00:00</span>
              <span class="rec-or">hoặc</span>
              <label class="upload-link">
                tải lên file âm thanh có sẵn
                <input type="file" id="file-input" accept="audio/wav,audio/x-wav,audio/wave,audio/mpeg,audio/webm,audio/ogg" hidden />
              </label>
            </div>

            <div id="audio-preview" class="audio-preview" hidden>
              <div class="preview-label">📼 Đoạn vừa thu:</div>
              <audio id="rec-audio" controls></audio>
              <div class="preview-meta" id="audio-meta"></div>
            </div>

            <details class="hint-row">
              <summary>💡 Mẹo thu âm</summary>
              <ul>
                <li>Ngồi sát mic (~20cm), không quá gần để tránh tiếng thở</li>
                <li>Đọc tự nhiên như đang nói chuyện, đừng đọc đều đều</li>
                <li>3 câu là vừa: vd <i>"Xin chào các bạn, mình là Linh, hôm nay shop có nhiều ưu đãi cho mọi người."</i></li>
                <li>Audio 5–10 giây cho kết quả tốt nhất</li>
              </ul>
            </details>
          </div>
        </div>

        <!-- Bước 2: Câu đọc -->
        <div class="wiz-step" id="wiz-step-2">
          <div class="step-head">
            <div class="step-num">2</div>
            <div class="step-title">
              <h3>Gõ lại đúng câu vừa đọc</h3>
              <div class="muted">Quan trọng — máy đọc câu này để học giọng. Sai chữ → giọng nhân bản sẽ không chuẩn.</div>
            </div>
          </div>

          <div class="step-body">
            <textarea id="ref-text" rows="3"
              placeholder="Gõ chính xác câu vừa thu ở Bước 1 — kể cả dấu chấm phẩy."></textarea>
          </div>
        </div>

        <!-- Bước 3: Nghe thử + Lưu -->
        <div class="wiz-step" id="wiz-step-3">
          <div class="step-head">
            <div class="step-num">3</div>
            <div class="step-title">
              <h3>Nghe thử rồi lưu</h3>
              <div class="muted">Bấm nghe thử để kiểm tra giọng có giống không. Hợp ý mới lưu.</div>
            </div>
          </div>

          <div class="step-body">
            <div class="try-row">
              <button type="button" id="btn-test-clone" class="btn-try-big">
                🔊 Nghe thử giọng đã clone
              </button>
              <span class="muted" id="clone-status-inline">Chưa thử</span>
            </div>

            <audio id="clone-audio" controls hidden></audio>

            <details class="adv-row">
              <summary class="muted">Đổi câu nghe thử (mặc định: lời chào)</summary>
              <textarea id="sample-text" rows="2"
                placeholder="Xin chào, đây là giọng nói được nhân bản, mọi người nghe thử có giống không nha."></textarea>
            </details>

            <hr class="divider" />

            <label class="name-label">
              ✏️ Đặt tên cho giọng này
              <input type="text" id="voice-name"
                placeholder="vd: Giọng của Linh, Giọng nam HN, Giọng MC trẻ trung..." />
            </label>

            <div class="save-row">
              <button type="button" id="btn-save" class="btn-save-big" disabled>
                💾 Lưu vào bộ sưu tập
              </button>
              <button type="button" id="btn-discard" class="btn-link">↻ Bắt đầu lại</button>
            </div>
            <div id="save-status" class="status"></div>
          </div>
        </div>

      </div>

      <!-- ────────── BỘ SƯU TẬP GIỌNG ────────── -->
      <div class="card voice-library">
        <div class="lib-head">
          <h3>📚 Giọng đã lưu</h3>
          <span class="muted" id="active-pill"></span>
        </div>
        <div id="voices-list">
          <div class="empty-state">
            <span class="empty-icon">🎤</span>
            Chưa có giọng nào. Thu một đoạn ở trên rồi bấm <b>Lưu</b>.
          </div>
        </div>
      </div>

      <!-- ────────── DÙNG THỬ GIỌNG ĐANG ACTIVE ────────── -->
      <div class="card voice-tryout" id="card-tryout" hidden>
        <h3>🎬 Đọc thử bằng giọng đang dùng</h3>
        <textarea id="tryout-text" rows="2"
          placeholder="Gõ text bất kỳ, máy sẽ đọc bằng giọng đang dùng. VD: Hôm nay shop có deal cực sốc, mua 2 tặng 1, freeship toàn quốc!"></textarea>
        <div class="btn-row">
          <button type="button" id="btn-tryout" class="btn-primary">▶ Đọc thử</button>
          <span class="muted" id="tryout-status"></span>
        </div>
        <audio id="tryout-audio" controls hidden></audio>
      </div>
    `;
  }

  bind() {
    this.on("#btn-rec-start", "click", () => this._startRecord());
    this.on("#btn-rec-stop",  "click", () => this._stopRecord());
    this.on("#file-input",    "change", (e) => this._onUpload(e));
    this.on("#btn-test-clone","click", () => this._testClone());
    this.on("#btn-save",      "click", () => this._save());
    this.on("#btn-discard",   "click", () => this._discard());
    this.on("#btn-tryout",    "click", () => this._tryout());
    this.on("#voices-list",   "click", (e) => this._onListClick(e));
    this.on("#voice-name",    "input", () => this._refreshSaveEnabled());
    this.on("#ref-text",      "input", () => this._refreshSaveEnabled());
  }

  afterMount() { this.refresh(); this._refreshSaveEnabled(); }
  onActivate() { this.refresh(); }

  beforeUnmount() {
    this._stopRecord(true);
    if (this._previewAudio)   try { URL.revokeObjectURL(this._previewAudio); } catch {}
    if (this._cloneAudioUrl)  try { URL.revokeObjectURL(this._cloneAudioUrl); } catch {}
    if (this._tryoutAudioUrl) try { URL.revokeObjectURL(this._tryoutAudioUrl); } catch {}
  }

  // ─── Record ────────────────────────────────────────────────────────
  async _startRecord() {
    if (this._recorder) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      toast("Trình duyệt không hỗ trợ ghi âm — hãy tải lên file âm thanh có sẵn", "error");
      return;
    }
    try {
      this._stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      toast("Mic bị chặn. Hãy cho phép trang này dùng mic trong Cài đặt trình duyệt.", "error");
      return;
    }

    let mime = "";
    for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg", "audio/wav"]) {
      if (MediaRecorder.isTypeSupported(m)) { mime = m; break; }
    }
    try {
      this._recorder = new MediaRecorder(this._stream, mime ? { mimeType: mime } : {});
    } catch (e) {
      toast("Không khởi động được mic: " + e.message, "error");
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
      if (sec >= 30) this._stopRecord();
    }, 250);
  }

  _stopRecord(silent = false) {
    if (this._timerHandle) { clearInterval(this._timerHandle); this._timerHandle = null; }
    const rec = this._recorder;
    if (rec && rec.state !== "inactive") { try { rec.stop(); } catch {} }
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
    if (!this._chunks.length) { toast("Không có gì được thu", "error"); return; }
    this._blob = new Blob(this._chunks, { type: mime || "audio/webm" });
    this._chunks = [];
    this._renderAudioPreview(this._blob, (Date.now() - this._recordingStart) / 1000);
    this._refreshSaveEnabled();
  }

  _onUpload(e) {
    const f = e.target.files[0];
    if (!f) return;
    this._blob = f;
    this._renderAudioPreview(f, null);
    this._refreshSaveEnabled();
    e.target.value = "";
  }

  _renderAudioPreview(blob, durationFallback) {
    const url = URL.createObjectURL(blob);
    const audio = this.$("#rec-audio");
    audio.src = url;
    this.$("#audio-preview").hidden = false;

    audio.onloadedmetadata = () => {
      const dur = isFinite(audio.duration) ? audio.duration : durationFallback;
      let warn = "";
      let status = "";
      if (dur != null && dur < 3) {
        warn = '<span class="warn-red">⚠️ Quá ngắn — cần ít nhất 3 giây</span>';
      } else if (dur != null && dur > 30) {
        warn = '<span class="warn-orange">⚠️ Hơi dài — chỉ giữ 30s đầu</span>';
      } else if (dur != null) {
        status = '<span class="ok-green">✓ Độ dài tốt</span>';
      }
      const durStr = dur != null ? `${dur.toFixed(1)} giây` : "?";
      this.$("#audio-meta").innerHTML = `<span>Độ dài: <b>${durStr}</b></span> ${status} ${warn}`;
    };
  }

  _clearAudio() {
    this._blob = null;
    const audio = this.$("#rec-audio");
    if (audio.src) { try { URL.revokeObjectURL(audio.src); } catch {} audio.removeAttribute("src"); }
    this.$("#audio-preview").hidden = true;
    this.$("#audio-meta").textContent = "";
    this.$("#rec-timer").textContent = "00:00";
    const clone = this.$("#clone-audio");
    if (clone?.src) { try { URL.revokeObjectURL(clone.src); } catch {} clone.removeAttribute("src"); clone.hidden = true; }
    this._cloneAudioUrl = null;
    this.$("#clone-status-inline").textContent = "Chưa thử";
    this.$("#clone-status-inline").className = "muted";
    this._setStatus("#save-status", "");
  }

  _discard() {
    if (!confirm("Bỏ tất cả và bắt đầu lại từ Bước 1?")) return;
    this._clearAudio();
    this.$("#ref-text").value = "";
    this.$("#voice-name").value = "";
    this.$("#sample-text").value = "";
    this._refreshSaveEnabled();
  }

  // ─── Test clone (chưa lưu) ─────────────────────────────────────────
  async _testClone() {
    if (!this._blob) { toast("Bước 1: cần thu âm hoặc tải file trước", "error"); return; }
    const refText = this.$("#ref-text").value.trim();
    if (!refText) { toast("Bước 2: cần gõ lại câu vừa đọc", "error"); return; }
    const sampleText = this.$("#sample-text")?.value.trim() ||
      "Xin chào, đây là giọng nói được nhân bản, mọi người nghe thử có giống không nha.";

    const fd = new FormData();
    fd.append("text", refText);
    fd.append("sample_text", sampleText);
    const ext = this._blob.type.includes("webm") ? "webm"
              : this._blob.type.includes("ogg") ? "ogg" : "wav";
    fd.append("file", this._blob, `ref.${ext}`);

    const inline = this.$("#clone-status-inline");
    inline.textContent = "⏳ Đang tạo... (~3-5 giây)";
    inline.className = "info";
    this.$("#btn-test-clone").disabled = true;
    try {
      const r = await fetch("/studio/voices/preview_clone", { method: "POST", body: fd });
      if (!r.ok) {
        let msg = "Tạo thử lỗi";
        try { const j = await r.json(); msg = j.msg || msg; } catch {}
        inline.textContent = "❌ " + msg;
        inline.className = "warn-red";
        return;
      }
      const blob = await r.blob();
      if (this._cloneAudioUrl) try { URL.revokeObjectURL(this._cloneAudioUrl); } catch {}
      this._cloneAudioUrl = URL.createObjectURL(blob);
      const player = this.$("#clone-audio");
      player.src = this._cloneAudioUrl;
      player.hidden = false;
      player.play().catch(() => {});
      inline.textContent = "✓ Nghe xem có ưng không, ưng thì bấm Lưu phía dưới";
      inline.className = "ok-green";
    } catch (e) {
      inline.textContent = "❌ Lỗi: " + e.message;
      inline.className = "warn-red";
    } finally {
      this.$("#btn-test-clone").disabled = false;
    }
  }

  // ─── Save ──────────────────────────────────────────────────────────
  _refreshSaveEnabled() {
    const ok = this._blob
      && this.$("#ref-text")?.value.trim()
      && this.$("#voice-name")?.value.trim();
    const btn = this.$("#btn-save");
    if (btn) btn.disabled = !ok;
  }

  async _save() {
    if (!this._blob) { toast("Bước 1: chưa có ghi âm", "error"); return; }
    const refText = this.$("#ref-text").value.trim();
    const name = this.$("#voice-name").value.trim();
    if (!refText) { toast("Bước 2: chưa gõ câu vừa đọc", "error"); return; }
    if (!name)    { toast("Bước 3: chưa đặt tên cho giọng", "error"); return; }

    const fd = new FormData();
    fd.append("name", name);
    fd.append("text", refText);
    const ext = this._blob.type.includes("webm") ? "webm"
              : this._blob.type.includes("ogg") ? "ogg" : "wav";
    fd.append("file", this._blob, `ref.${ext}`);

    this._setStatus("#save-status", "⏳ Đang lưu...");
    this.$("#btn-save").disabled = true;
    try {
      const r = await fetch("/studio/voices/upload", { method: "POST", body: fd });
      const j = await r.json();
      if (j.code === 0) {
        this._setStatus("#save-status", `✓ Đã lưu giọng "${name}". Bấm "Dùng giọng này" ở dưới để avatar nói bằng giọng vừa lưu.`, "ok");
        toast("Đã lưu giọng nói", "ok");
        // Reset wizard sạch — sẵn sàng thu giọng tiếp theo nếu muốn
        this._clearAudio();
        this.$("#ref-text").value = "";
        this.$("#voice-name").value = "";
        this.refresh();
      } else {
        this._setStatus("#save-status", "❌ " + (j.msg || "Lưu thất bại"), "error");
      }
    } catch (e) {
      this._setStatus("#save-status", "❌ Lỗi: " + e.message, "error");
    } finally {
      this._refreshSaveEnabled();
    }
  }

  // ─── Library list ──────────────────────────────────────────────────
  async refresh() {
    const j = await api("/studio/voices");
    if (j.code !== 0) return;
    this._activeId = j.data.active_id || null;
    const list = j.data.voices || [];
    const root = this.$("#voices-list");
    const activePill = this.$("#active-pill");

    if (!list.length) {
      root.innerHTML = `<div class="empty-state">
        <span class="empty-icon">🎤</span>
        Chưa có giọng nào. Thu một đoạn ở trên rồi bấm <b>Lưu</b>.
      </div>`;
      activePill.textContent = "";
      this.$("#card-tryout").hidden = true;
      return;
    }

    activePill.innerHTML = this._activeId
      ? `<span class="badge active">Đang dùng: ${escapeHtml(list.find(v => v.id === this._activeId)?.name || this._activeId)}</span>`
      : `<span class="muted">Chưa chọn giọng nào</span>`;

    root.innerHTML = list.map((v) => `
      <div class="voice-card ${v.active ? "is-active" : ""}">
        <div class="voice-card-head">
          <div class="voice-name">${escapeHtml(v.name || "—")}</div>
          ${v.active ? '<span class="badge active">Đang dùng</span>' : ""}
        </div>
        <div class="voice-card-meta">
          <span>⏱ ${(v.duration_secs ?? 0).toFixed(1)}s</span>
          ${v.encoded ? '<span class="ok-green">✓ Đã sẵn sàng</span>' : '<span class="muted">⏳ Sẽ chuẩn bị khi dùng</span>'}
        </div>
        <div class="voice-card-actions">
          <audio src="/studio/voices/${encodeURIComponent(v.id)}/ref" controls preload="none"></audio>
          ${v.active
            ? '<span class="muted small">Giọng này đang được avatar dùng</span>'
            : `<button class="btn-primary" data-act="activate" data-id="${escapeAttr(v.id)}">▶ Dùng giọng này</button>`}
          <button class="btn-link btn-danger" data-act="delete" data-id="${escapeAttr(v.id)}">Xóa</button>
        </div>
      </div>
    `).join("");

    this.$("#card-tryout").hidden = !this._activeId;
  }

  async _onListClick(e) {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    if (act === "activate") {
      btn.disabled = true;
      btn.textContent = "⏳ Đang chuyển...";
      const r = await api("/studio/voices/activate", { method: "POST", body: { voice_id: id } });
      if (r.code === 0) {
        toast("✓ Đã chuyển sang giọng mới", "ok");
        this.refresh();
      } else {
        toast(r.msg, "error");
        btn.disabled = false;
        btn.textContent = "▶ Dùng giọng này";
      }
    } else if (act === "delete") {
      if (!confirm(`Xóa giọng này? Không thể khôi phục.`)) return;
      const r = await api(`/studio/voices/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (r.code === 0) { toast("Đã xóa", "ok"); this.refresh(); }
      else toast(r.msg, "error");
    }
  }

  // ─── Tryout (đọc thử text bất kỳ với giọng đang dùng) ─────────────
  async _tryout() {
    if (!this._activeId) { toast("Chưa chọn giọng", "error"); return; }
    const text = this.$("#tryout-text").value.trim() ||
      "Xin chào, hôm nay shop có deal cực sốc, mua 2 tặng 1, freeship toàn quốc!";
    const status = this.$("#tryout-status");
    status.textContent = "⏳ Đang đọc... (~2-5 giây)";
    status.className = "info";
    this.$("#btn-tryout").disabled = true;
    try {
      const r = await fetch("/studio/voices/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice_id: this._activeId, text }),
      });
      if (!r.ok) {
        let msg = "Đọc thử lỗi";
        try { const j = await r.json(); msg = j.msg || msg; } catch {}
        status.textContent = "❌ " + msg;
        status.className = "warn-red";
        return;
      }
      const blob = await r.blob();
      if (this._tryoutAudioUrl) try { URL.revokeObjectURL(this._tryoutAudioUrl); } catch {}
      this._tryoutAudioUrl = URL.createObjectURL(blob);
      const player = this.$("#tryout-audio");
      player.src = this._tryoutAudioUrl;
      player.hidden = false;
      player.play().catch(() => {});
      status.textContent = "✓ Xong";
      status.className = "ok-green";
    } catch (e) {
      status.textContent = "❌ " + e.message;
      status.className = "warn-red";
    } finally {
      this.$("#btn-tryout").disabled = false;
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
