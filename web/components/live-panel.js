// <live-panel> — Live console: WSStream preview + speak + comments console
// + product catalog gallery (click to switch). The brain reads
// catalog.current_product() on every fire, so switching here makes the
// avatar talk about the new product from the next sentence onward.

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

// Deterministic avatar tint per username — keeps the comment feed lively
// without storing any extra state.
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

// Extract a likely price hint from free-text product description.
// Recognises "Giá: XYZ", "299.000đ", "299k", "$50", "1,5 triệu", "₫299.000".
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
    this._wsClosedByUser  = false;
    this._wsReconnectAttempt = 0;
    this._wsReconnectTimer = null;
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
          <div class="subtitle">Preview avatar · điều khiển scrape TikTok · pitch sản phẩm theo lựa chọn của bạn.</div>
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

      <!-- Video + Comments console -->
      <div class="live-grid">
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
        </div>

        <div class="live-side">
          <div class="card comments-console">
            <div class="card-head">
              <div>
                <h3>Bình luận trực tiếp</h3>
                <span class="subtitle" id="comments-count">Đợi dữ liệu từ phiên live…</span>
              </div>
              <div class="actions">
                <button id="btn-clear-comments" class="btn-ghost btn-small" title="Xóa hiển thị (giữ nguyên trên server)">Clear</button>
              </div>
            </div>
            <div id="comments-feed" class="comments-feed">
              <div class="empty-state">
                <span class="empty-icon">💬</span>
                Chưa có bình luận. Bắt đầu Live để cào TikTok.
              </div>
            </div>
          </div>

          <div class="brain-strip" id="brain-strip">
            <div class="item">
              <span class="item-label">Stage</span>
              <span class="item-value" id="stat-stage">—</span>
            </div>
            <span class="sep"></span>
            <div class="item">
              <span class="item-label">Phút live</span>
              <span class="item-value"><span id="stat-minutes">0</span>'</span>
            </div>
            <span class="sep"></span>
            <div class="item" style="flex:1;min-width:0">
              <span class="item-label">Đang pitch</span>
              <span class="item-value" id="stat-current-name" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">—</span>
            </div>
            <span class="pill" id="ws-indicator" style="margin-left:auto">
              <span class="status-dot"></span>
              <span>Đang kết nối…</span>
            </span>
          </div>
        </div>
      </div>

      <!-- On-Air spotlight -->
      <div class="card on-air-card" id="on-air-card">
        <div class="on-air-head">
          <span class="label">On Air</span>
          <span class="hint">AI đang nói về sản phẩm này · pick một thẻ bên dưới để đổi</span>
        </div>
        <div id="on-air-body">
          <div class="on-air-empty">
            <span class="empty-icon">📦</span>
            Chưa chọn sản phẩm — AI sẽ pitch chung khi vào phiên.
          </div>
        </div>
      </div>

      <!-- Catalog gallery -->
      <div class="card catalog-card">
        <div class="card-head">
          <div>
            <h3>Catalog sản phẩm</h3>
            <span class="subtitle">Click một thẻ → đổi sản phẩm đang bán. Đang ở sản phẩm nào, AI nói về sản phẩm đó.</span>
          </div>
          <div class="actions">
            <button id="btn-refresh-catalog" class="btn-secondary btn-small">↻ Refresh</button>
          </div>
        </div>
        <div class="catalog-grid" id="catalog-grid"></div>
      </div>

      <!-- Bottom controls -->
      <div class="card-grid">
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

        <div class="card manual-comment-card">
          <div class="card-head">
            <div>
              <h3>Gửi comment thủ công</h3>
              <span class="subtitle">Inject comment vào brain — hữu ích để test luồng hỏi đáp.</span>
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
    // Open the feed WS unconditionally — when scraper isn't running yet the
    // server replies with {running: false} and stays open, ready to push
    // events the moment Start Live is clicked. No race between start + WS.
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

  // ─── Live control ────────────────────────────────────────────────
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
      // WS is already open + subscribed — listener publish hook attaches now,
      // events from the scraper will start flowing within ms.
    } else this._setStatus("#live-status", j.msg, "error");
  }

  async _stopLive() {
    const j = await api("/live/stop", { method: "POST", body: { sessionid: getSessionId() } });
    if (j.code === 0) {
      this._setStatus("#live-status", "Đã dừng", "warn");
      this._setRunning(false);
      // Keep WS open so the user can see the running=false state echo and
      // restart without a reconnect round-trip.
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
    this._wsClosedByUser = false;
    this._connectWs();
  }

  _connectWs() {
    if (this._wsReconnectTimer) { clearTimeout(this._wsReconnectTimer); this._wsReconnectTimer = null; }
    if (this._liveWs && this._liveWs.readyState <= 1) {
      try { this._liveWs.close(); } catch {}
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/live/feed?sessionid=${encodeURIComponent(getSessionId())}`;
    const ws = new WebSocket(url);
    this._liveWs = ws;
    this._setWsIndicator("connecting");

    ws.onopen = () => {
      this._wsReconnectAttempt = 0;
      this._setWsIndicator("open");
    };
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
      if (this._wsClosedByUser) {
        this._setWsIndicator("closed");
        return;
      }
      // Exponential backoff: 1, 2, 4, 8, 15 (cap), 15, ...
      const delay = Math.min(15000, 1000 * Math.pow(2, this._wsReconnectAttempt));
      this._wsReconnectAttempt++;
      this._setWsIndicator("reconnect", Math.round(delay / 1000));
      this._wsReconnectTimer = setTimeout(() => this._connectWs(), delay);
    };
    ws.onerror = () => {
      // onclose fires after onerror — let the close handler schedule the
      // reconnect. Just surface visually.
      this._setWsIndicator("reconnect");
    };
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
    if (state === "open") {
      el.className = "pill success";
      el.querySelector("span:last-child").textContent = "Realtime";
    } else if (state === "connecting") {
      el.className = "pill";
      el.querySelector("span:last-child").textContent = "Đang kết nối…";
    } else if (state === "reconnect") {
      el.className = "pill warn";
      el.querySelector("span:last-child").textContent = secs ? `Reconnect ${secs}s…` : "Reconnecting…";
    } else {
      el.className = "pill off";
      el.querySelector("span:last-child").textContent = "Offline";
    }
  }

  // ─── State rendering ─────────────────────────────────────────────
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
    this._lastStats = { ...ps };
    this.$("#comments-count").textContent = ps.comments_total
      ? `${fmt(ps.comments_total)} tổng · ${fmt(ps.gifts_total || 0)} quà`
      : "Đợi dữ liệu từ phiên live…";

    const p = brain.current_product;
    const newId = p?.id || null;
    if (newId !== this._currentProductId) {
      this._currentProductId = newId;
      this._renderCatalogActiveState();
    }
    this._renderOnAir(p);
    this._setRunning(!!st.running);
  }

  _renderStat(stats) {
    // Lightweight delta push (likes/joins/follows/shares/gifts/viewers)
    // — bypasses the full state render so a 50/s like flood doesn't trash
    // the rest of the panel.
    if (!stats) return;
    this._lastStats = { ...this._lastStats, ...stats };
    const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("vi-VN"));
    const map = {
      "#stat-viewers":  fmt(stats.viewer_count ?? this._lastStats.viewer_count),
      "#stat-comments": fmt(stats.comments_total ?? this._lastStats.comments_total ?? 0),
      "#stat-likes":    fmt(stats.likes_total ?? this._lastStats.likes_total ?? 0),
      "#stat-shares":   fmt(stats.shares_total ?? this._lastStats.shares_total ?? 0),
      "#stat-follows":  fmt(stats.follows_total ?? this._lastStats.follows_total ?? 0),
      "#stat-gifts":    fmt(stats.gifts_total ?? this._lastStats.gifts_total ?? 0),
    };
    for (const [s, v] of Object.entries(map)) {
      const el = this.$(s);
      if (el) el.textContent = v;
    }
  }

  _renderOnAir(currentMini) {
    const body = this.$("#on-air-body");
    const nameEl = this.$("#stat-current-name");
    if (!currentMini || !currentMini.id) {
      body.innerHTML = `
        <div class="on-air-empty">
          <span class="empty-icon">📦</span>
          Chưa chọn sản phẩm — AI sẽ pitch chung khi vào phiên.
        </div>`;
      nameEl.textContent = "—";
      return;
    }
    // Resolve full product (catalog has the long text). Mini state from
    // backend only carries id/name/price.
    const full = this._products.find((x) => String(x.id) === String(currentMini.id)) || {};
    const name  = currentMini.name || full.name || currentMini.id;
    const price = currentMini.price || extractPrice(full.text) || "";
    const desc  = (full.text || full.description || "").trim();
    const idx   = this._products.findIndex((x) => String(x.id) === String(currentMini.id));

    nameEl.textContent = name;
    body.innerHTML = `
      <div class="on-air-body">
        <div class="info">
          <div class="name">${escapeHtml(name)}</div>
          <div class="id-row">
            <code>${escapeHtml(currentMini.id)}</code>
            ${idx >= 0 ? `<span>· vị trí #${idx + 1}</span>` : ""}
            ${price ? `<span class="price-chip">${escapeHtml(price)}</span>` : ""}
          </div>
          <div class="desc">${escapeHtml(desc || "Chưa có mô tả chi tiết.")}</div>
        </div>
        <div class="actions">
          <button type="button" class="btn-primary" data-action="intro" data-id="${escapeAttr(currentMini.id)}">
            🎙 Giới thiệu lại ngay
          </button>
          <button type="button" class="btn-secondary" data-action="cta" data-id="${escapeAttr(currentMini.id)}">
            🛒 Chốt đơn / kêu mua
          </button>
          <button type="button" class="btn-ghost" data-action="next">
            → Sản phẩm kế tiếp
          </button>
        </div>
      </div>
    `;
    body.querySelectorAll("button[data-action]").forEach((b) =>
      b.addEventListener("click", (e) => this._onOnAirAction(e.currentTarget))
    );
  }

  async _onOnAirAction(btn) {
    const action = btn.dataset.action;
    if (action === "intro") {
      const p = this._products.find((x) => String(x.id) === String(this._currentProductId));
      if (!p) return;
      const text = `Tiếp theo mình giới thiệu sản phẩm ${p.name || p.id}. Mọi người chú ý phần mô tả nha.`;
      await api("/human", { method: "POST", body: { type: "echo", text, sessionid: getSessionId() } });
      toast("Đã đẩy intro vào TTS", "ok");
    } else if (action === "cta") {
      const p = this._products.find((x) => String(x.id) === String(this._currentProductId));
      if (!p) return;
      const text = `Còn vài suất ưu đãi ${p.name || p.id}, mọi người chốt đơn ngay trong giỏ hàng nha!`;
      await api("/human", { method: "POST", body: { type: "echo", text, sessionid: getSessionId() } });
      toast("Đã đẩy CTA", "ok");
    } else if (action === "next") {
      if (!this._products.length) return;
      const idx = this._products.findIndex((x) => String(x.id) === String(this._currentProductId));
      const next = this._products[(idx + 1) % this._products.length];
      this._switchTo(next.id);
    }
  }

  // ─── Comments feed ───────────────────────────────────────────────
  _prependComments(items) {
    if (!items || !items.length) return;
    const feed = this.$("#comments-feed");
    if (feed.querySelector(".empty-state")) feed.innerHTML = "";
    for (const c of items) {
      const node = this._renderComment(c);
      feed.insertBefore(node, feed.firstChild);
    }
    while (feed.children.length > 200) feed.removeChild(feed.lastChild);
  }

  _renderComment(c) {
    const div = document.createElement("div");
    const cls = ["comment-row"];
    if (c.type === "gift") cls.push("gift");
    else if (c.type === "like") cls.push("like");
    div.className = cls.join(" ");
    const hue = hueFor(c.username);
    const initials = initialsOf(c.username);
    const ts = c.ts || c.timestamp || Date.now();
    div.dataset.ts = ts;
    div.innerHTML = `
      <span class="avatar-circle" style="background:hsl(${hue}, 60%, 48%)" aria-hidden="true">${escapeHtml(initials)}</span>
      <div class="comment-body">
        <span class="u">${escapeHtml(c.username || "")}</span>
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
    const feed = this.$("#comments-feed");
    feed.innerHTML = `
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

  // ─── Catalog (product gallery + switch) ──────────────────────────
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
          <div class="tile-desc">${escapeHtml(text.slice(0, 160))}</div>
          <div class="tile-footer">
            <code>${escapeHtml(p.id)}</code>
            <span class="tile-cta">Đặt làm on-air →</span>
          </div>
        </button>`;
    }).join("");
  }

  _renderCatalogActiveState() {
    for (const tile of this.querySelectorAll(".product-tile")) {
      const isActive = String(tile.dataset.id) === String(this._currentProductId);
      tile.classList.toggle("active", isActive);
    }
  }

  _onCatalogClick(e) {
    const tile = e.target.closest(".product-tile");
    if (!tile) return;
    const id = tile.dataset.id;
    if (!id) return;
    if (String(id) === String(this._currentProductId)) {
      toast("Sản phẩm này đã đang on-air", "warn");
      return;
    }
    this._switchTo(id);
  }

  async _switchTo(productId) {
    // Optimistic UI: mark active immediately so the user gets feedback even
    // before the WS state echoes back.
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
      toast(`Đang chuyển — AI sẽ nói về "${name}" từ câu kế tiếp`, "ok");
    } else {
      toast(j.msg || "Không chuyển được sản phẩm", "error");
      // Roll back optimistic state on next WS tick (it'll re-sync automatically).
    }
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
