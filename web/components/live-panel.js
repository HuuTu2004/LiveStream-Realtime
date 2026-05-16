// <live-panel> — Live tab: WSStream video (JSMpeg) + chat speak + comments + control + stats + product.

import { LiveElement } from "./shared/element.js";
import { api, escapeHtml, escapeAttr, getSessionId } from "./shared/api.js";
import { toast } from "./shared/toast.js";

const KPIS = [
  { id: "viewers",  label: "Người xem",   icon: "viewers",  symbol: "👁" },
  { id: "comments", label: "Bình luận",   icon: "comments", symbol: "💬" },
  { id: "likes",    label: "Tim",         icon: "likes",    symbol: "♥" },
  { id: "shares",   label: "Share",       icon: "shares",   symbol: "↗" },
  { id: "follows",  label: "Follow mới",  icon: "follows",  symbol: "+" },
  { id: "gifts",    label: "Quà",         icon: "gifts",    symbol: "🎁" },
];

class LivePanel extends LiveElement {
  constructor() {
    super();
    this._jsmpeg = null;
    this._liveWs = null;
  }

  render() {
    this.innerHTML = `
      <div class="panel-head">
        <div class="title-block">
          <h2>Phiên livestream</h2>
          <div class="subtitle">Preview avatar, điều khiển scrape TikTok và theo dõi tương tác theo thời gian thực.</div>
        </div>
        <div class="actions">
          <span id="live-state-pill" class="pill off"><span class="status-dot"></span><span>Chưa live</span></span>
        </div>
      </div>

      <!-- KPI strip -->
      <div class="kpi-strip">
        ${KPIS.map((k) => `
          <div class="kpi" data-kpi="${k.id}">
            <div class="kpi-label">
              <span class="kpi-icon ${k.icon}">${k.symbol}</span>
              <span>${k.label}</span>
            </div>
            <div class="kpi-value" id="stat-${k.id}">—</div>
            <div class="kpi-meta" id="meta-${k.id}">&nbsp;</div>
          </div>`).join("")}
      </div>

      <div class="live-grid">
        <!-- Left column: video + chat + comments -->
        <div class="live-main">
          <div class="card video-card">
            <div class="video-wrap">
              <canvas id="ws-canvas"></canvas>
              <div class="video-overlay">
                <span class="status-dot off"></span>
                <span id="conn-label">Chưa kết nối</span>
              </div>
              <div class="video-tag">wsstream · ~150ms</div>
            </div>
            <div class="video-controls">
              <button id="btn-conn"    class="btn-primary">▶ Kết nối preview</button>
              <button id="btn-disconn" class="btn-secondary" hidden>■ Ngắt</button>
              <button id="btn-popout"  class="btn-secondary" title="Mở cửa sổ riêng để OBS Window Capture">⧉ Mở cho OBS</button>
              <span class="meta">MPEG-TS / JSMpeg</span>
            </div>
          </div>

          <div class="card speak-card">
            <div class="card-head">
              <div>
                <h3>Chat thẳng với avatar</h3>
                <span class="subtitle">Bỏ qua brain — đẩy text trực tiếp vào TTS queue.</span>
              </div>
            </div>
            <form id="speak-form">
              <textarea name="text" rows="2" required
                placeholder="Nhập text avatar sẽ nói (vd: Xin chào các bạn, em là Linh...)"></textarea>
              <div class="btn-row">
                <button type="submit" class="btn-primary">▶ Avatar nói</button>
                <button type="button" id="speak-stop" class="btn-secondary">■ Dừng</button>
              </div>
            </form>
            <div id="speak-status" class="status"></div>
          </div>

          <div class="card">
            <div class="card-head">
              <div>
                <h3>Bình luận trực tiếp</h3>
                <span class="subtitle" id="comments-count">Đợi dữ liệu từ phiên live…</span>
              </div>
            </div>
            <div id="comments-feed" class="comments-feed">
              <div class="empty-state">
                <span class="empty-icon">💬</span>
                Chưa có bình luận. Bắt đầu Live để cào TikTok.
              </div>
            </div>
          </div>
        </div>

        <!-- Right column: control + brain + product + manual -->
        <div class="live-side">
          <div class="card control-card">
            <div class="card-head">
              <div>
                <h3>Điều khiển phiên</h3>
                <span class="subtitle">Kết nối scraper và khởi động brain bán hàng.</span>
              </div>
            </div>
            <form id="live-form">
              <div class="form-row">
                <label>Platform
                  <select name="platform">
                    <option value="tiktok">TikTok</option>
                  </select>
                </label>
                <label>Live ID (@username)
                  <input type="text" name="live_id" placeholder="@tenchannel hoặc tenchannel" required />
                </label>
              </div>
              <div class="btn-row">
                <button type="submit"  class="btn-primary" id="btn-live-start">▶ Bắt đầu Live</button>
                <button type="button"  class="btn-stop"    id="btn-live-stop" hidden>■ Dừng Live</button>
              </div>
            </form>
            <div id="live-status" class="status"></div>
          </div>

          <div class="card">
            <div class="card-head">
              <div>
                <h3>Trạng thái brain</h3>
                <span class="subtitle">Giai đoạn pitch và thời lượng phiên.</span>
              </div>
            </div>
            <div class="brain-strip">
              <div class="item">
                <span class="item-label">Stage</span>
                <span class="item-value" id="stat-stage">—</span>
              </div>
              <span class="sep"></span>
              <div class="item">
                <span class="item-label">Phút live</span>
                <span class="item-value"><span id="stat-minutes">0</span>'</span>
              </div>
            </div>
          </div>

          <div class="card product-card">
            <div class="card-head">
              <div>
                <h3>Sản phẩm đang bán</h3>
                <span class="subtitle">Đang được brain pitch trực tiếp.</span>
              </div>
            </div>
            <div id="current-product">
              <div class="empty-state">
                <span class="empty-icon">📦</span>
                Chưa có sản phẩm hiện hành.
              </div>
            </div>
            <h4>Chuyển sản phẩm</h4>
            <form id="live-product-form">
              <select name="product_id" id="live-product-select"></select>
              <button type="submit" class="btn-secondary">Switch</button>
            </form>
          </div>

          <div class="card manual-comment-card">
            <div class="card-head">
              <div>
                <h3>Gửi comment thủ công</h3>
                <span class="subtitle">Inject vào brain để test luồng xử lý.</span>
              </div>
            </div>
            <form id="live-manual-form">
              <div class="grid-2">
                <input type="text" name="username" placeholder="Username" value="Khách lạ" />
                <input type="text" name="text" placeholder="Nội dung bình luận…" required class="span-2" />
              </div>
              <button type="submit" class="btn-secondary btn-block">Gửi vào brain</button>
            </form>
          </div>
        </div>
      </div>
    `;
  }

  bind() {
    this.on("#btn-conn",          "click",  () => this._connect());
    this.on("#btn-disconn",       "click",  () => this._disconnect());
    this.on("#btn-popout",        "click",  () => this._popoutForOBS());
    this.on("#speak-form",        "submit", (e) => this._speak(e));
    this.on("#speak-stop",        "click",  () => this._stopSpeak());
    this.on("#live-form",         "submit", (e) => this._startLive(e));
    this.on("#btn-live-stop",     "click",  () => this._stopLive());
    this.on("#live-manual-form",  "submit", (e) => this._sendManual(e));
    this.on("#live-product-form", "submit", (e) => this._switchProduct(e));
    window.addEventListener("beforeunload", () => this._disconnect());
  }

  async afterMount() {
    this.refreshProducts();
    const j = await api(`/live/state?sessionid=${encodeURIComponent(getSessionId())}`);
    if (j.code === 0 && j.data.running) {
      this._setRunning(true);
      this._openWs();
    }
  }

  onActivate() {
    this.refreshProducts();
  }

  // ─── WSStream preview (JSMpeg) ──────────────────────────────────────
  async _connect() {
    try {
      let info = null;
      try {
        const r = await fetch("/preview-info", { cache: "no-store" });
        if (r.ok) info = await r.json();
      } catch {}

      if (info?.transport === "virtualcam") {
        toast("Server đang dùng virtualcam — xem bằng OBS, không có preview trong browser.", "error");
        return;
      }

      const wsUrl = info?.ws_url
        || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/wsstream/0`;
      await this._connectWSStream(wsUrl);
      this.$("#btn-conn").hidden = true;
      this.$("#btn-disconn").hidden = false;
    } catch (e) {
      toast("Preview error: " + e, "error");
    }
  }

  async _connectWSStream(url) {
    const canvas = this.$("#ws-canvas");
    const label  = this.$("#conn-label");
    const dot    = this.querySelector(".video-overlay .status-dot");
    label.textContent = "Đang load JSMpeg…";
    dot.classList.add("off");

    if (typeof JSMpeg === "undefined") {
      await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = "https://cdn.jsdelivr.net/gh/phoboslab/jsmpeg@master/jsmpeg.min.js";
        s.onload = resolve; s.onerror = reject;
        document.head.appendChild(s);
      });
    }
    if (typeof JSMpeg === "undefined") {
      toast("JSMpeg CDN load fail", "error"); return;
    }

    this._jsmpeg = new JSMpeg.Player(url, {
      canvas,
      autoplay: true,
      audio: true,
      videoBufferSize: 1024 * 1024,
      audioBufferSize: 256 * 1024,
      onSourceEstablished: () => {
        label.textContent = "Đang phát";
        dot.classList.remove("off");
      },
      onSourceCompleted: () => {
        label.textContent = "Stream kết thúc";
        dot.classList.add("off");
      },
      onError: (e) => {
        label.textContent = "Lỗi stream";
        dot.classList.add("off");
        toast("WSStream error: " + e, "error");
      },
    });
  }

  _popoutForOBS() {
    // Open a chromeless window targeted at /preview.html so OBS can grab it as
    // a Window Capture source. 1280x720 default; user can resize before adding to OBS.
    const session = encodeURIComponent(getSessionId());
    const url = `preview.html?session=${session}&fit=contain`;
    const w = 1280, h = 720;
    const left = Math.max(0, (screen.availWidth  - w) / 2 | 0);
    const top  = Math.max(0, (screen.availHeight - h) / 2 | 0);
    const features = [
      `width=${w}`, `height=${h}`, `left=${left}`, `top=${top}`,
      "menubar=no", "toolbar=no", "location=no", "status=no",
      "scrollbars=no", "resizable=yes",
    ].join(",");
    const win = window.open(url, `livetalking_obs_${session}`, features);
    if (!win) {
      toast("Trình duyệt chặn popup — cho phép popup cho domain này rồi thử lại.", "error");
      return;
    }
    win.focus();
    toast("Đã mở cửa sổ preview · Thêm OBS → Window Capture chọn nó.", "ok");
  }

  _disconnect() {
    if (this._jsmpeg) { try { this._jsmpeg.destroy(); } catch {} this._jsmpeg = null; }
    const canvas = this.$("#ws-canvas");
    if (canvas) {
      const ctx = canvas.getContext("2d");
      ctx?.clearRect(0, 0, canvas.width, canvas.height);
    }
    const btnConn = this.$("#btn-conn"); if (btnConn) btnConn.hidden = false;
    const btnDis  = this.$("#btn-disconn"); if (btnDis) btnDis.hidden = true;
    const label   = this.$("#conn-label"); if (label) label.textContent = "Chưa kết nối";
    this.querySelector(".video-overlay .status-dot")?.classList.add("off");
  }

  // ─── Direct speak ────────────────────────────────────────────────
  async _speak(e) {
    e.preventDefault();
    const text = e.target.text.value.trim();
    if (!text) return;
    this._setStatus("#speak-status", "Đang đẩy vào TTS queue…");
    const j = await api("/human", {
      method: "POST",
      body: { type: "echo", text, sessionid: getSessionId() },
    });
    if (j.code === 0) this._setStatus("#speak-status", "OK — kiểm tra avatar đang nói", "ok");
    else this._setStatus("#speak-status", j.msg, "error");
  }

  async _stopSpeak() {
    await api("/interrupt_talk", { method: "POST", body: { sessionid: getSessionId() } });
    this._setStatus("#speak-status", "Đã ngắt", "warn");
  }

  // ─── TikTok scraper control ──────────────────────────────────────
  async _startLive(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      sessionid: getSessionId(),
      platform: fd.get("platform"),
      live_id:  fd.get("live_id"),
    };
    this._setStatus("#live-status", "Đang khởi động brain + connect TikTok…");
    const j = await api("/live/start", { method: "POST", body: payload });
    if (j.code === 0) {
      this._setStatus("#live-status", `OK — đang cào @${payload.live_id}`, "ok");
      this._setRunning(true);
      this._openWs();
    } else this._setStatus("#live-status", j.msg, "error");
  }

  async _stopLive() {
    const j = await api("/live/stop", { method: "POST", body: { sessionid: getSessionId() } });
    if (j.code === 0) {
      this._setStatus("#live-status", "Đã dừng", "warn");
      this._setRunning(false);
      this._closeWs();
    } else toast(j.msg, "error");
  }

  _setRunning(running) {
    this.$("#btn-live-start").hidden = running;
    this.$("#btn-live-stop").hidden = !running;
    const pill = this.$("#live-state-pill");
    if (pill) {
      pill.classList.toggle("off", !running);
      pill.classList.toggle("danger", !running);
      pill.classList.toggle("success", running);
      pill.querySelector("span:last-child").textContent = running ? "Đang live" : "Chưa live";
    }
  }

  _openWs() {
    this._closeWs();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this._liveWs = new WebSocket(`${proto}://${location.host}/live/feed?sessionid=${encodeURIComponent(getSessionId())}`);
    this._liveWs.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.event === "state")    this._renderState(m.data);
        else if (m.event === "comments") this._prependComments(m.data);
      } catch {}
    };
    this._liveWs.onclose = () => { this._liveWs = null; };
  }
  _closeWs() {
    if (this._liveWs) { try { this._liveWs.close(); } catch {} this._liveWs = null; }
  }

  _renderState(st) {
    if (!st) return;
    const ps = st.platform_stats || {};
    const brain = st.brain || {};
    const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("vi-VN"));
    const map = {
      "#stat-viewers":  fmt(ps.viewer_count),
      "#stat-comments": fmt(ps.comments_total ?? 0),
      "#stat-likes":    fmt(ps.likes_total ?? 0),
      "#stat-shares":   fmt(ps.shares_total ?? 0),
      "#stat-follows":  fmt(ps.follows_total ?? 0),
      "#stat-gifts":    fmt(ps.gifts_total ?? 0),
      "#stat-stage":    brain.stage || "—",
      "#stat-minutes":  brain.stream_minutes ?? 0,
    };
    for (const [s, v] of Object.entries(map)) {
      const el = this.$(s);
      if (el) el.textContent = v;
    }
    this.$("#comments-count").textContent = ps.comments_total
      ? `${fmt(ps.comments_total)} tổng`
      : "Đợi dữ liệu từ phiên live…";

    const p = brain.current_product;
    const prodEl = this.$("#current-product");
    if (p && p.id) {
      prodEl.innerHTML = `
        <div class="product-block">
          <div class="p-row"><span class="label">ID</span><code>${escapeHtml(p.id)}</code></div>
          <div class="p-row"><span class="label">Tên</span><span class="p-name">${escapeHtml(p.name || "—")}</span></div>
          <div class="p-row"><span class="label">Giá</span><span class="p-price">${escapeHtml(p.price || "—")}</span></div>
        </div>`;
    } else {
      prodEl.innerHTML = `<div class="empty-state"><span class="empty-icon">📦</span>Chưa có sản phẩm hiện hành.</div>`;
    }
    this._setRunning(!!st.running);
  }

  _prependComments(items) {
    if (!items || !items.length) return;
    const feed = this.$("#comments-feed");
    if (feed.querySelector(".empty-state")) feed.innerHTML = "";
    for (const c of items) {
      const div = document.createElement("div");
      div.className = "comment-row" + (c.type === "gift" ? " gift" : "") + (c.type === "like" ? " like" : "");
      div.innerHTML = `<span class="u">${escapeHtml(c.username)}</span><span class="t">${escapeHtml(c.text)}</span>`;
      feed.insertBefore(div, feed.firstChild);
    }
    while (feed.children.length > 120) feed.removeChild(feed.lastChild);
  }

  async _sendManual(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const j = await api("/live/comment", {
      method: "POST",
      body: { sessionid: getSessionId(), username: fd.get("username"), text: fd.get("text") },
    });
    if (j.code === 0) { toast("Đã gửi", "ok"); e.target.text.value = ""; }
    else toast(j.msg, "error");
  }

  async _switchProduct(e) {
    e.preventDefault();
    const pid = e.target.product_id.value;
    const j = await api("/live/product/switch", {
      method: "POST",
      body: { sessionid: getSessionId(), product_id: pid },
    });
    if (j.code === 0) toast("Đã chuyển sản phẩm", "ok"); else toast(j.msg, "error");
  }

  async refreshProducts() {
    const j = await api("/studio/products");
    if (j.code !== 0) return;
    const sel = this.$("#live-product-select");
    if (!sel) return;
    sel.innerHTML = (j.data.products || []).map((p) =>
      `<option value="${escapeAttr(p.id)}">${escapeHtml(p.id)} — ${escapeHtml(p.name || "")}</option>`
    ).join("");
  }

  _setStatus(sel, msg, type = "") {
    const el = this.$(sel);
    if (!el) return;
    el.textContent = msg;
    el.className = "status" + (type ? " " + type : "");
  }

  beforeUnmount() {
    this._disconnect();
    this._closeWs();
  }
}

customElements.define("live-panel", LivePanel);
