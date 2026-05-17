// <live-panel> — Tab Live tối giản: KPI quan trọng + preview avatar + feed
// comments + catalog. Công cụ test (chat thẳng, manual comment) đẩy vào
// section collapsible ở cuối — không che giao diện live thực tế.

import { LiveElement } from "./shared/element.js";
import { api, escapeHtml, escapeAttr, getSessionId } from "./shared/api.js";
import { toast } from "./shared/toast.js";

// KPI strip — chuẩn cho sales livestream.
// Order đặt ngay sau viewers/comments để nổi bật (revenue signal).
const KPIS = [
  { id: "viewers",   label: "Người xem",  symbol: "👁",  hi: false },
  { id: "comments",  label: "Bình luận",  symbol: "💬", hi: false },
  { id: "likes",     label: "Tim",         symbol: "♥",  hi: false },
  { id: "orders",    label: "Đơn chốt",   symbol: "🛒", hi: true  },
  { id: "subs",      label: "Subscribe",   symbol: "⭐", hi: true  },
  { id: "follows",   label: "Follow",      symbol: "+",  hi: false },
  { id: "shares",    label: "Share",       symbol: "↗",  hi: false },
  { id: "gifts",     label: "Quà",         symbol: "🎁", hi: false },
];

// Map stat key in JSON → KPI cell id
const STAT_KEY = {
  viewers:   "viewer_count",
  comments:  "comments_total",
  likes:     "likes_total",
  shares:    "shares_total",
  follows:   "follows_total",
  gifts:     "gifts_total",
  orders:    "orders_total",
  subs:      "subs_total",
  envelopes: "envelopes_total",
  barrages:  "barrages_total",
};

const AVATAR_HUES = [199, 217, 264, 282, 326, 14, 35, 96, 162, 178];
function hueFor(name) {
  let h = 0;
  for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  return AVATAR_HUES[h % AVATAR_HUES.length];
}
function initialsOf(name) {
  const s = String(name || "?").replace(/[^\p{L}\p{N}\s]/gu, "").trim();
  if (!s) return "?";
  const parts = s.split(/\s+/).filter(Boolean);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}
function extractPrice(text) {
  if (!text) return "";
  const m1 = text.match(/(?:gi[áa]|price)\s*[:：]\s*([^\n,;.]+)/i);
  if (m1) return m1[1].trim().slice(0, 40);
  const m2 = text.match(/\b\d{1,3}(?:[.,]\d{3})+\s*(?:đ|VNĐ|VND|₫)\b/i);
  if (m2) return m2[0].trim();
  const m3 = text.match(/\b\d+(?:[.,]\d+)?\s*(?:k|tri[ệe]u|tỷ|nghìn)\b/i);
  if (m3) return m3[0].trim();
  const m4 = text.match(/\$\s?\d+(?:[.,]\d+)?/);
  if (m4) return m4[0].trim();
  return "";
}
function timeAgo(ts) {
  if (!ts) return "";
  const t = typeof ts === "number" ? ts : Date.parse(ts);
  if (!t) return "";
  const sec = Math.max(1, Math.floor((Date.now() - t) / 1000));
  if (sec < 60)    return sec + "s";
  if (sec < 3600)  return Math.floor(sec / 60) + "m";
  if (sec < 86400) return Math.floor(sec / 3600) + "h";
  return Math.floor(sec / 86400) + "d";
}

class LivePanel extends LiveElement {
  constructor() {
    super();
    this._jsmpeg   = null;
    this._liveWs   = null;
    this._wsClosedByUser     = false;
    this._wsReconnectAttempt = 0;
    this._wsReconnectTimer   = null;
    this._products = [];
    this._currentProductId = null;
    this._timeAgoInterval  = null;
    this._lastStats        = {};
  }

  render() {
    this.innerHTML = `
      <div class="panel-head">
        <div class="title-block">
          <h2>Phiên livestream</h2>
          <div class="subtitle">Preview avatar · scrape TikTok realtime · điều khiển pitch sản phẩm.</div>
        </div>
        <div class="actions">
          <span id="pause-pill" class="pill warn" hidden>
            <span>⏸ Host paused</span>
          </span>
          <span id="live-state-pill" class="pill off">
            <span class="status-dot"></span>
            <span>Chưa live</span>
          </span>
        </div>
      </div>

      <!-- KPI strip -->
      <div class="kpi-strip">
        ${KPIS.map((k) => `
          <div class="kpi${k.hi ? " kpi-highlight" : ""}" data-kpi="${k.id}">
            <div class="kpi-label">
              <span class="kpi-icon">${k.symbol}</span>
              <span>${k.label}</span>
            </div>
            <div class="kpi-value" id="stat-${k.id}">—</div>
          </div>`).join("")}
      </div>

      <!-- Video preview -->
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
          <button id="btn-popout"  class="btn-secondary" title="Mở cửa sổ riêng cho OBS Window Capture">⧉ Mở cho OBS</button>
        </div>
      </div>

      <!-- Main grid: comments feed | side panel -->
      <div class="live-grid">
        <div class="live-main">
          <div class="card comments-console">
            <div class="card-head">
              <div>
                <h3>Bình luận trực tiếp</h3>
                <span class="subtitle" id="comments-count">Đợi dữ liệu từ phiên live…</span>
              </div>
              <div class="actions">
                <button id="btn-clear-comments" class="btn-ghost btn-small" title="Xóa hiển thị (giữ trên server)">Clear</button>
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

        <div class="live-side">
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
            <span class="pill" id="ws-indicator" style="margin-left:auto">
              <span class="status-dot"></span>
              <span>Đang kết nối…</span>
            </span>
          </div>

          <div class="card on-air-card" id="on-air-card">
            <div class="on-air-head">
              <span class="label">On Air</span>
              <span class="hint">AI đang pitch sản phẩm này</span>
            </div>
            <div id="on-air-body">
              <div class="on-air-empty">
                <span class="empty-icon">📦</span>
                Chưa chọn sản phẩm — AI sẽ pitch chung khi vào phiên.
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Catalog -->
      <div class="card catalog-card">
        <div class="card-head">
          <div>
            <h3>Catalog sản phẩm</h3>
            <span class="subtitle">Click thẻ để chuyển sản phẩm on-air.</span>
          </div>
          <div class="actions">
            <button id="btn-refresh-catalog" class="btn-secondary btn-small">↻ Refresh</button>
          </div>
        </div>
        <div class="catalog-grid" id="catalog-grid"></div>
      </div>

      <!-- Live control -->
      <div class="card control-card">
        <div class="card-head">
          <div>
            <h3>Điều khiển phiên</h3>
            <span class="subtitle">Kết nối TikTok scraper.</span>
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
            <button type="submit" class="btn-primary" id="btn-live-start">▶ Bắt đầu Live</button>
            <button type="button" class="btn-stop"    id="btn-live-stop" hidden>■ Dừng Live</button>
          </div>
        </form>
        <div id="live-status" class="status"></div>
      </div>

      
    `;
  }

  bind() {
    this.on("#btn-conn",            "click",  () => this._connect());
    this.on("#btn-disconn",         "click",  () => this._disconnect());
    this.on("#btn-popout",          "click",  () => this._popoutForOBS());
    this.on("#speak-form",          "submit", (e) => this._speak(e));
    this.on("#speak-stop",          "click",  () => this._stopSpeak());
    this.on("#live-form",           "submit", (e) => this._startLive(e));
    this.on("#btn-live-stop",       "click",  () => this._stopLive());
    this.on("#live-manual-form",    "submit", (e) => this._sendManual(e));
    this.on("#btn-refresh-catalog", "click",  () => this.refreshProducts());
    this.on("#btn-clear-comments",  "click",  () => this._clearComments());
    this.on("#catalog-grid",        "click",  (e) => this._onCatalogClick(e));
    window.addEventListener("beforeunload", () => this._disconnect());
  }

  async afterMount() {
    await this.refreshProducts();
    this._openWs();
    const j = await api(`/live/state?sessionid=${encodeURIComponent(getSessionId())}`);
    if (j.code === 0 && j.data.running) this._setRunning(true);
    this._timeAgoInterval = setInterval(() => this._refreshTimeAgo(), 15000);
  }

  onActivate() { this.refreshProducts(); }

  // ─── WSStream preview (JSMpeg) ──────────────────────────────────────
  async _connect() {
    try {
      let info = null;
      try {
        const r = await fetch("/preview-info", { cache: "no-store" });
        if (r.ok) info = await r.json();
      } catch {}

      if (info?.transport === "virtualcam") {
        toast("Server dùng virtualcam — xem qua OBS, không có preview browser.", "error");
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
    if (typeof JSMpeg === "undefined") { toast("JSMpeg CDN load fail", "error"); return; }

    this._jsmpeg = new JSMpeg.Player(url, {
      canvas, autoplay: true, audio: true,
      videoBufferSize: 1024 * 1024,
      audioBufferSize: 256 * 1024,
      onSourceEstablished: () => { label.textContent = "Đang phát"; dot.classList.remove("off"); },
      onSourceCompleted:   () => { label.textContent = "Stream kết thúc"; dot.classList.add("off"); },
      onError: (e) => { label.textContent = "Lỗi stream"; dot.classList.add("off"); toast("WSStream error: " + e, "error"); },
    });
  }

  _popoutForOBS() {
    const session = encodeURIComponent(getSessionId());
    const url = `preview.html?session=${session}&fit=contain`;
    const w = 1280, h = 720;
    const left = Math.max(0, (screen.availWidth  - w) / 2 | 0);
    const top  = Math.max(0, (screen.availHeight - h) / 2 | 0);
    const features = [
      `width=${w}`, `height=${h}`, `left=${left}`, `top=${top}`,
      "menubar=no", "toolbar=no", "location=no", "status=no", "scrollbars=no", "resizable=yes",
    ].join(",");
    const win = window.open(url, `livetalking_obs_${session}`, features);
    if (!win) { toast("Trình duyệt chặn popup — cho phép popup cho domain này.", "error"); return; }
    win.focus();
    // Mute main browser audio (popup là source cho OBS) — tránh duplicate âm thanh.
    const mute = (m) => { try { if (this._jsmpeg?.audioOut) { this._jsmpeg.audioOut.volume = m ? 0 : 1; } } catch {} };
    mute(true);
    const watch = setInterval(() => {
      if (win.closed) { clearInterval(watch); mute(false); toast("Cửa sổ OBS đóng — bật lại audio chính.", "ok"); }
    }, 1000);
    toast("Đã mở cửa sổ preview · Audio chính đã mute. Thêm Window Capture trong OBS.", "ok");
  }

  _disconnect() {
    if (this._jsmpeg) { try { this._jsmpeg.destroy(); } catch {} this._jsmpeg = null; }
    const canvas = this.$("#ws-canvas");
    if (canvas) { const ctx = canvas.getContext("2d"); ctx?.clearRect(0, 0, canvas.width, canvas.height); }
    const btnConn = this.$("#btn-conn"); if (btnConn) btnConn.hidden = false;
    const btnDis  = this.$("#btn-disconn"); if (btnDis) btnDis.hidden = true;
    const label   = this.$("#conn-label"); if (label) label.textContent = "Chưa kết nối";
    this.querySelector(".video-overlay .status-dot")?.classList.add("off");
  }

  // ─── Direct speak (debug only) ────────────────────────────────────
  async _speak(e) {
    e.preventDefault();
    const text = e.target.text.value.trim();
    if (!text) return;
    this._setStatus("#speak-status", "Đang đẩy vào TTS…");
    const j = await api("/human", { method: "POST", body: { type: "echo", text, sessionid: getSessionId() } });
    if (j.code === 0) this._setStatus("#speak-status", "OK", "ok");
    else this._setStatus("#speak-status", j.msg, "error");
  }
  async _stopSpeak() {
    await api("/interrupt_talk", { method: "POST", body: { sessionid: getSessionId() } });
    this._setStatus("#speak-status", "Đã ngắt", "warn");
  }

  // ─── Live control ────────────────────────────────────────────────
  async _startLive(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      sessionid: getSessionId(),
      platform: fd.get("platform"),
      live_id:  fd.get("live_id"),
    };
    this._setStatus("#live-status", "Đang kết nối...");
    const j = await api("/live/start", { method: "POST", body: payload });
    if (j.code === 0) {
      this._setStatus("#live-status", `Đang cào @${payload.live_id}`, "ok");
      this._setRunning(true);
    } else this._setStatus("#live-status", j.msg, "error");
  }
  async _stopLive() {
    const j = await api("/live/stop", { method: "POST", body: { sessionid: getSessionId() } });
    if (j.code === 0) { this._setStatus("#live-status", "Đã dừng", "warn"); this._setRunning(false); }
    else toast(j.msg, "error");
  }

  _setRunning(running) {
    this.$("#btn-live-start").hidden = running;
    this.$("#btn-live-stop").hidden  = !running;
    const pill = this.$("#live-state-pill");
    if (pill) {
      pill.classList.toggle("off",     !running);
      pill.classList.toggle("danger",  !running);
      pill.classList.toggle("success",  running);
      pill.querySelector("span:last-child").textContent = running ? "Đang live" : "Chưa live";
    }
  }

  _setPaused(paused) {
    const pill = this.$("#pause-pill");
    if (pill) pill.hidden = !paused;
  }

  // ─── Live WS ─────────────────────────────────────────────────────
  _openWs() { this._wsClosedByUser = false; this._connectWs(); }

  _connectWs() {
    if (this._wsReconnectTimer) { clearTimeout(this._wsReconnectTimer); this._wsReconnectTimer = null; }
    if (this._liveWs && this._liveWs.readyState <= 1) { try { this._liveWs.close(); } catch {} }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/live/feed?sessionid=${encodeURIComponent(getSessionId())}`;
    const ws = new WebSocket(url);
    this._liveWs = ws;
    this._setWsIndicator("connecting");

    ws.onopen = () => { this._wsReconnectAttempt = 0; this._setWsIndicator("open"); };
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if      (m.event === "state")    this._renderState(m.data);
        else if (m.event === "stat")     this._renderStat(m.data);
        else if (m.event === "comments") this._prependComments(m.data);
      } catch {}
    };
    ws.onclose = () => {
      this._liveWs = null;
      if (this._wsClosedByUser) { this._setWsIndicator("closed"); return; }
      const delay = Math.min(15000, 1000 * Math.pow(2, this._wsReconnectAttempt));
      this._wsReconnectAttempt++;
      this._setWsIndicator("reconnect", Math.round(delay / 1000));
      this._wsReconnectTimer = setTimeout(() => this._connectWs(), delay);
    };
    ws.onerror = () => this._setWsIndicator("reconnect");
  }

  _closeWs() {
    this._wsClosedByUser = true;
    if (this._wsReconnectTimer) { clearTimeout(this._wsReconnectTimer); this._wsReconnectTimer = null; }
    if (this._liveWs) { try { this._liveWs.close(); } catch {} this._liveWs = null; }
    this._setWsIndicator("closed");
  }

  _setWsIndicator(state, secs) {
    const el = this.$("#ws-indicator");
    if (!el) return;
    const label = el.querySelector("span:last-child");
    if (state === "open")        { el.className = "pill success"; label.textContent = "Realtime"; }
    else if (state === "connecting") { el.className = "pill";        label.textContent = "Đang kết nối…"; }
    else if (state === "reconnect")  { el.className = "pill warn";   label.textContent = secs ? `Reconnect ${secs}s…` : "Reconnecting…"; }
    else                          { el.className = "pill off";    label.textContent = "Offline"; }
  }

  // ─── State render ────────────────────────────────────────────────
  _renderState(st) {
    if (!st) return;
    const ps = st.platform_stats || {};
    const brain = st.brain || {};
    this._lastStats = { ...ps };
    this._renderKpis(ps);

    const total = (ps.comments_total || 0) + (ps.gifts_total || 0) + (ps.orders_total || 0);
    this.$("#comments-count").textContent = total > 0
      ? `${total.toLocaleString("vi-VN")} sự kiện · ${(ps.orders_total||0)} đơn · ${(ps.gifts_total||0)} quà`
      : "Đợi dữ liệu từ phiên live…";

    this.$("#stat-stage").textContent  = brain.stage || "—";
    this.$("#stat-minutes").textContent = brain.stream_minutes ?? 0;

    const p = brain.current_product;
    const newId = p?.id || null;
    if (newId !== this._currentProductId) {
      this._currentProductId = newId;
      this._renderCatalogActiveState();
    }
    this._renderOnAir(p);
    this._setRunning(!!st.running);
    this._setPaused(!!ps.paused);

    // Restore "Đang cào @username" status + form input after F5 / WS reconnect.
    if (st.running && st.live_id) {
      this._setStatus("#live-status", `Đang cào @${st.live_id}`, "ok");
      const idInput = this.querySelector("input[name='live_id']");
      if (idInput && !idInput.value) idInput.value = st.live_id;
      const platSel = this.querySelector("select[name='platform']");
      if (platSel && st.platform) platSel.value = st.platform;
    } else if (!st.running && !this._userJustStopped) {
      const sv = this.$("#live-status");
      if (sv && !sv.textContent) this._setStatus("#live-status", "Chưa cào", "warn");
    }
  }

  _renderStat(stats) {
    if (!stats) return;
    this._lastStats = { ...this._lastStats, ...stats };
    this._renderKpis(this._lastStats);
    if (typeof stats.paused === "boolean") this._setPaused(stats.paused);
  }

  _renderKpis(ps) {
    const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("vi-VN"));
    for (const k of KPIS) {
      const el = this.$(`#stat-${k.id}`);
      if (!el) continue;
      el.textContent = fmt(ps[STAT_KEY[k.id]] ?? 0);
    }
  }

  _renderOnAir(currentMini) {
    const body = this.$("#on-air-body");
    if (!currentMini || !currentMini.id) {
      body.innerHTML = `
        <div class="on-air-empty">
          <span class="empty-icon">📦</span>
          Chưa chọn sản phẩm — AI sẽ pitch chung khi vào phiên.
        </div>`;
      return;
    }
    const full = this._products.find((x) => String(x.id) === String(currentMini.id)) || {};
    const name  = currentMini.name || full.name || currentMini.id;
    const price = currentMini.price || extractPrice(full.text) || "";
    const desc  = (full.text || full.description || "").trim();
    const idx   = this._products.findIndex((x) => String(x.id) === String(currentMini.id));

    body.innerHTML = `
      <div class="on-air-body">
        <div class="info">
          <div class="name">${escapeHtml(name)}</div>
          <div class="id-row">
            <code>${escapeHtml(currentMini.id)}</code>
            ${idx >= 0 ? `<span>· #${idx + 1}</span>` : ""}
            ${price ? `<span class="price-chip">${escapeHtml(price)}</span>` : ""}
          </div>
          <div class="desc">${escapeHtml((desc || "Chưa có mô tả.").slice(0, 200))}</div>
        </div>
        <div class="actions">
          <button type="button" class="btn-ghost btn-small" data-action="next">→ Sản phẩm kế</button>
        </div>
      </div>
    `;
    body.querySelectorAll("button[data-action]").forEach((b) =>
      b.addEventListener("click", (e) => this._onOnAirAction(e.currentTarget))
    );
  }

  _onOnAirAction(btn) {
    if (btn.dataset.action !== "next") return;
    if (!this._products.length) return;
    const idx = this._products.findIndex((x) => String(x.id) === String(this._currentProductId));
    const next = this._products[(idx + 1) % this._products.length];
    this._switchTo(next.id);
  }

  // ─── Comments feed ───────────────────────────────────────────────
  _prependComments(items) {
    if (!items || !items.length) return;
    const feed = this.$("#comments-feed");
    if (feed.querySelector(".empty-state")) feed.innerHTML = "";
    // Auto-scroll only if user is near the top (don't yank reading position)
    const stickTop = feed.scrollTop < 80;
    for (const c of items) {
      const node = this._renderComment(c);
      feed.insertBefore(node, feed.firstChild);
    }
    while (feed.children.length > 200) feed.removeChild(feed.lastChild);
    if (stickTop) {
      requestAnimationFrame(() => feed.scrollTo({ top: 0, behavior: "smooth" }));
    }
  }

  _renderComment(c) {
    const div = document.createElement("div");
    const cls = ["comment-row"];
    const type = c.type || "comment";
    if (type !== "comment") cls.push(type);  // gift / order / subscribe / envelope / barrage
    div.className = cls.join(" ");
    const hue = hueFor(c.username);
    const initials = initialsOf(c.username);
    const ts = c.ts || c.timestamp || Date.now();
    div.dataset.ts = ts;

    // Icon nhỏ trước tên cho event đặc biệt
    const typeIcon = {
      comment:   "",
      gift:      "🎁",
      order:     "🛒",
      subscribe: "⭐",
      envelope:  "🧧",
      barrage:   "⚡",
    }[type] || "";

    div.innerHTML = `
      <span class="avatar-circle" style="background:hsl(${hue}, 60%, 48%)" aria-hidden="true">${escapeHtml(initials)}</span>
      <div class="comment-body">
        <span class="u">${typeIcon ? `<span class="type-icon">${typeIcon}</span>` : ""}${escapeHtml(c.username || "")}</span>
        <span class="t">${escapeHtml(c.text || "")}</span>
      </div>
      <span class="comment-time" data-ts="${ts}">${timeAgo(ts)}</span>
    `;
    return div;
  }

  _refreshTimeAgo() {
    for (const el of this.querySelectorAll(".comment-time[data-ts]")) {
      el.textContent = timeAgo(+el.dataset.ts);
    }
  }

  _clearComments() {
    this.$("#comments-feed").innerHTML = `
      <div class="empty-state">
        <span class="empty-icon">💬</span>
        Đã xoá hiển thị. Comment mới sẽ tiếp tục đổ về.
      </div>`;
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

  // ─── Catalog ────────────────────────────────────────────────────
  async refreshProducts() {
    const j = await api("/studio/products");
    if (j.code !== 0) return;
    this._products = j.data.products || [];
    this._renderCatalog();
  }

  _renderCatalog() {
    const grid = this.$("#catalog-grid");
    if (!grid) return;
    if (!this._products.length) {
      grid.innerHTML = `
        <div class="catalog-empty">
          <span class="empty-state" style="margin:0">
            <span class="empty-icon">📦</span>
            Chưa có sản phẩm — vào tab <b>Sản phẩm</b> tạo thẻ mới.
          </span>
        </div>`;
      return;
    }
    grid.innerHTML = this._products.map((p, i) => {
      const active = String(p.id) === String(this._currentProductId);
      const text  = p.text || p.description || "";
      const price = extractPrice(text);
      return `
        <button type="button" class="product-tile${active ? " active" : ""}" data-id="${escapeAttr(p.id)}">
          <div class="tile-head">
            <span class="index-badge">#${i + 1}</span>
            <span class="on-air-flag">On Air</span>
          </div>
          <div class="tile-name">${escapeHtml(p.name || p.id)}</div>
          ${price ? `<div class="tile-price">${escapeHtml(price)}</div>` : ""}
          <div class="tile-desc">${escapeHtml(text.slice(0, 140))}</div>
        </button>`;
    }).join("");
  }

  _renderCatalogActiveState() {
    for (const tile of this.querySelectorAll(".product-tile")) {
      tile.classList.toggle("active", String(tile.dataset.id) === String(this._currentProductId));
    }
  }

  _onCatalogClick(e) {
    const tile = e.target.closest(".product-tile");
    if (!tile) return;
    const id = tile.dataset.id;
    if (!id) return;
    if (String(id) === String(this._currentProductId)) { toast("Sản phẩm này đang on-air", "warn"); return; }
    this._switchTo(id);
  }

  async _switchTo(productId) {
    this._currentProductId = productId;
    this._renderCatalogActiveState();
    const local = this._products.find((x) => String(x.id) === String(productId));
    if (local) this._renderOnAir({ id: local.id, name: local.name, price: extractPrice(local.text || "") });
    const j = await api("/live/product/switch", {
      method: "POST",
      body: { sessionid: getSessionId(), product_id: productId },
    });
    if (j.code === 0) {
      const name = (local && local.name) || productId;
      toast(`AI sẽ pitch "${name}" từ câu kế`, "ok");
    } else toast(j.msg || "Không chuyển được sản phẩm", "error");
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
    if (this._timeAgoInterval) clearInterval(this._timeAgoInterval);
  }
}

customElements.define("live-panel", LivePanel);
