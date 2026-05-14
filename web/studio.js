// LiveTalking — Trang quản trị SPA
// 5 tab: Live / Sản phẩm / Video / Âm thanh / Cài đặt

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

// ─── Toast ─────────────────────────────────────────────────────────────
function toast(msg, type = "") {
  const t = document.createElement("div");
  t.className = "toast " + type;
  t.textContent = msg;
  $("#toast-container").appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// ─── API helpers ───────────────────────────────────────────────────────
async function api(path, opts = {}) {
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, opts);
  return await r.json();
}

function setStatus(el, msg, type = "") {
  if (!el) return;
  el.textContent = msg;
  el.className = "status" + (type ? " " + type : "");
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
const escapeAttr = (s) => escapeHtml(s).replace(/"/g, "&quot;");

// ─── Tab switching ─────────────────────────────────────────────────────
$$(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    $$(".panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $(`#tab-${t.dataset.tab}`).classList.add("active");
    if (t.dataset.tab === "live") refreshLiveProducts();
    if (t.dataset.tab === "products") refreshProducts();
    if (t.dataset.tab === "video") { refreshAvatars(); refreshJobs(); }
    if (t.dataset.tab === "audio") refreshAvatars();
    if (t.dataset.tab === "config") refreshConfig();
  });
});

// ─── Server status ping ────────────────────────────────────────────────
async function pingServer() {
  try {
    const r = await fetch("/studio/avatars");
    $("#server-status").className = r.ok ? "pill" : "pill off";
  } catch { $("#server-status").className = "pill off"; }
}
setInterval(pingServer, 5000); pingServer();

// ═══════════════════════════════════════════════════════════════════════
//  LIVE TAB — WebRTC viewer + TikTok start/stop + WS feed
// ═══════════════════════════════════════════════════════════════════════
let _pc = null;
let _liveWs = null;

function getSessionId() {
  return ($("#sessionid")?.value || "0").trim() || "0";
}

// ─── WebRTC viewer (port từ client.js cũ) ─────────────────────────────
function rtcNegotiate() {
  _pc.addTransceiver("video", { direction: "recvonly" });
  _pc.addTransceiver("audio", { direction: "recvonly" });
  return _pc.createOffer()
    .then((offer) => _pc.setLocalDescription(offer))
    .then(() => new Promise((resolve) => {
      if (_pc.iceGatheringState === "complete") return resolve();
      const cb = () => {
        if (_pc.iceGatheringState === "complete") {
          _pc.removeEventListener("icegatheringstatechange", cb);
          resolve();
        }
      };
      _pc.addEventListener("icegatheringstatechange", cb);
    }))
    .then(() => fetch("/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: _pc.localDescription.sdp, type: _pc.localDescription.type }),
    }))
    .then((r) => r.json())
    .then((ans) => {
      $("#sessionid").value = ans.sessionid;
      return _pc.setRemoteDescription(ans);
    });
}

async function fetchIceConfig() {
  try {
    const r = await fetch("/ice-config", { cache: "no-store" });
    if (!r.ok) return [];
    const j = await r.json();
    return Array.isArray(j.iceServers) ? j.iceServers : [];
  } catch (_) {
    return [];
  }
}

async function fetchPreviewInfo() {
  try {
    const r = await fetch("/preview-info", { cache: "no-store" });
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

let _hls = null; // hls.js instance khi browser không native HLS

async function connectHLS(url) {
  const video = $("#video");
  const label = $("#conn-label");
  const dot = $(".status-dot", $("#video-overlay"));
  label.textContent = "Đang tải HLS…";
  dot.classList.add("off");

  // Safari/iOS native HLS
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;
    video.onloadedmetadata = () => { label.textContent = `HLS (delay ~3-5s)`; dot.classList.remove("off"); };
    video.onerror = () => { label.textContent = "HLS lỗi"; dot.classList.add("off"); };
    try { await video.play(); } catch (e) { /* ignore autoplay block */ }
    return;
  }

  // Chrome/Firefox/Edge: dùng hls.js (CDN load lazy)
  if (typeof Hls === "undefined") {
    await new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.16/dist/hls.min.js";
      s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
  }
  if (!Hls.isSupported()) {
    label.textContent = "HLS không hỗ trợ browser";
    toast("Browser không hỗ trợ HLS.js", "error"); return;
  }
  _hls = new Hls({ lowLatencyMode: true, liveSyncDuration: 2 });
  _hls.loadSource(url);
  _hls.attachMedia(video);
  _hls.on(Hls.Events.MANIFEST_PARSED, () => {
    label.textContent = `HLS (delay ~3-5s)`; dot.classList.remove("off");
    video.play().catch(() => {});
  });
  _hls.on(Hls.Events.ERROR, (_evt, data) => {
    if (data.fatal) { label.textContent = "HLS fail: " + data.type; dot.classList.add("off"); }
  });
}

async function connectWebRTC() {
  const config = { sdpSemantics: "unified-plan" };
  const iceServers = await fetchIceConfig();
  if (iceServers.length > 0) config.iceServers = iceServers;
  _pc = new RTCPeerConnection(config);
  _pc.addEventListener("track", (evt) => {
    if (evt.track.kind === "video") $("#video").srcObject = evt.streams[0];
    else $("#audio").srcObject = evt.streams[0];
  });
  _pc.addEventListener("connectionstatechange", () => {
    const s = _pc.connectionState;
    const label = $("#conn-label");
    const dot = $(".status-dot", $("#video-overlay"));
    if (s === "connected") { label.textContent = "WebRTC (<1s)"; dot.classList.remove("off"); }
    else if (s === "connecting") { label.textContent = "Đang kết nối…"; dot.classList.add("off"); }
    else { label.textContent = s; dot.classList.add("off"); }
  });
  await rtcNegotiate();
}

$("#btn-conn").addEventListener("click", async () => {
  try {
    const info = await fetchPreviewInfo();
    const customUrl = ($("#preview-url")?.value || "").trim();
    const transport = info?.transport || "webrtc";

    if (transport === "rtmp" || transport === "rtcpush" || customUrl) {
      // HLS preview qua MediaMTX
      const url = customUrl || info?.hls_url;
      if (!url) {
        toast("Server chưa cấu hình HLS — set HLS URL ở Preview URL field", "error");
        return;
      }
      await connectHLS(url);
    } else {
      // WebRTC realtime
      await connectWebRTC();
    }
    $("#btn-conn").hidden = true;
    $("#btn-disconn").hidden = false;
  } catch (e) {
    toast("Preview error: " + e, "error");
  }
});

$("#btn-disconn").addEventListener("click", () => {
  if (_pc) { _pc.close(); _pc = null; }
  if (_hls) { _hls.destroy(); _hls = null; }
  const video = $("#video");
  video.srcObject = null;
  video.removeAttribute("src");
  video.load();
  $("#audio").srcObject = null;
  $("#btn-conn").hidden = false;
  $("#btn-disconn").hidden = true;
  $("#conn-label").textContent = "Chưa kết nối";
  $(".status-dot", $("#video-overlay")).classList.add("off");
});

window.addEventListener("beforeunload", () => { if (_pc) _pc.close(); });

// ─── Live start/stop (TikTok scraper) ─────────────────────────────────
$("#live-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = {
    sessionid: getSessionId(),
    platform: fd.get("platform"),
    live_id: fd.get("live_id"),
  };
  setStatus($("#live-status"), "Đang khởi động brain + connect TikTok…");
  const j = await api("/live/start", { method: "POST", body: payload });
  if (j.code === 0) {
    setStatus($("#live-status"), `OK — đang cào @${payload.live_id}`, "ok");
    $("#btn-live-start").hidden = true;
    $("#btn-live-stop").hidden = false;
    openLiveFeed();
  } else setStatus($("#live-status"), j.msg, "error");
});

$("#btn-live-stop").addEventListener("click", async () => {
  const j = await api("/live/stop", { method: "POST", body: { sessionid: getSessionId() } });
  if (j.code === 0) {
    setStatus($("#live-status"), "Đã dừng", "ok");
    $("#btn-live-start").hidden = false;
    $("#btn-live-stop").hidden = true;
    closeLiveFeed();
  } else toast(j.msg, "error");
});

// ─── Live WS feed ─────────────────────────────────────────────────────
function openLiveFeed() {
  closeLiveFeed();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  _liveWs = new WebSocket(`${proto}://${location.host}/live/feed?sessionid=${encodeURIComponent(getSessionId())}`);
  _liveWs.onmessage = (ev) => {
    try {
      const m = JSON.parse(ev.data);
      if (m.event === "state") renderLiveState(m.data);
      else if (m.event === "comments") prependComments(m.data);
    } catch {}
  };
  _liveWs.onclose = () => { _liveWs = null; };
}
function closeLiveFeed() {
  if (_liveWs) { try { _liveWs.close(); } catch {} _liveWs = null; }
}

function renderLiveState(st) {
  if (!st) return;
  const ps = st.platform_stats || {};
  const brain = st.brain || {};
  $("#stat-viewers").textContent = ps.viewer_count ?? "—";
  $("#stat-comments").textContent = ps.comments_total ?? 0;
  $("#stat-likes").textContent = ps.likes_total ?? 0;
  $("#stat-shares").textContent = ps.shares_total ?? 0;
  $("#stat-follows").textContent = ps.follows_total ?? 0;
  $("#stat-gifts").textContent = ps.gifts_total ?? 0;
  $("#stat-stage").textContent = brain.stage || "—";
  $("#stat-minutes").textContent = brain.stream_minutes ?? 0;
  $("#comments-count").textContent = ps.comments_total ? `${ps.comments_total} tổng` : "";

  // Current product
  const p = brain.current_product;
  const prodEl = $("#current-product");
  if (p && p.id) {
    prodEl.innerHTML = `
      <div class="p-row"><span class="label">ID</span><code>${escapeHtml(p.id)}</code></div>
      <div class="p-row"><span class="label">Tên</span><span class="p-name">${escapeHtml(p.name || "—")}</span></div>
      <div class="p-row"><span class="label">Giá</span><span class="p-price">${escapeHtml(p.price || "")}</span></div>`;
  } else {
    prodEl.innerHTML = `<div class="empty-state">Chưa có sản phẩm hiện hành</div>`;
  }

  // Reflect server-side running state vào UI buttons
  if (st.running && $("#btn-live-stop").hidden) {
    $("#btn-live-start").hidden = true;
    $("#btn-live-stop").hidden = false;
  } else if (!st.running && !$("#btn-live-stop").hidden) {
    $("#btn-live-start").hidden = false;
    $("#btn-live-stop").hidden = true;
  }
}

function prependComments(items) {
  if (!items || !items.length) return;
  const feed = $("#comments-feed");
  // Clear empty-state nếu có
  if ($(".empty-state", feed)) feed.innerHTML = "";
  for (const c of items) {
    const div = document.createElement("div");
    div.className = "comment-row" + (c.type === "gift" ? " gift" : "");
    div.innerHTML = `<span class="u">${escapeHtml(c.username)}:</span><span class="t">${escapeHtml(c.text)}</span>`;
    feed.insertBefore(div, feed.firstChild);
  }
  // Trim max 100
  while (feed.children.length > 100) feed.removeChild(feed.lastChild);
}

// Manual comment vào brain
$("#live-manual-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const j = await api("/live/comment", {
    method: "POST",
    body: { sessionid: getSessionId(), username: fd.get("username"), text: fd.get("text") },
  });
  if (j.code === 0) { toast("Đã gửi", "ok"); e.target.text.value = ""; }
  else toast(j.msg, "error");
});

// Product switch trên Live tab
async function refreshLiveProducts() {
  const j = await api("/studio/products");
  if (j.code !== 0) return;
  const sel = $("#live-product-select");
  sel.innerHTML = (j.data.products || []).map((p) =>
    `<option value="${escapeAttr(p.id)}">${escapeHtml(p.id)} — ${escapeHtml(p.name || "")}</option>`).join("");
}

$("#live-product-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pid = e.target.product_id.value;
  const j = await api("/live/product/switch", {
    method: "POST",
    body: { sessionid: getSessionId(), product_id: pid },
  });
  if (j.code === 0) toast("Đã chuyển sản phẩm", "ok"); else toast(j.msg, "error");
});

// Khi load page, check state nếu live đang chạy → reopen feed
(async () => {
  const j = await api(`/live/state?sessionid=${encodeURIComponent(getSessionId())}`);
  if (j.code === 0 && j.data.running) {
    $("#btn-live-start").hidden = true;
    $("#btn-live-stop").hidden = false;
    openLiveFeed();
  }
})();

// ═══════════════════════════════════════════════════════════════════════
//  SẢN PHẨM (CRUD)
// ═══════════════════════════════════════════════════════════════════════
let _editingProductId = null;

async function refreshProducts() {
  const j = await api("/studio/products");
  const tb = $("#products-table tbody");
  tb.innerHTML = "";
  if (j.code !== 0) return;
  for (const p of j.data.products || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(p.id || "")}</code></td>
      <td>${escapeHtml(p.name || "—")}</td>
      <td>${escapeHtml(p.price || "")}</td>
      <td>${escapeHtml((p.description || "").substring(0, 70))}${(p.description || "").length > 70 ? "…" : ""}</td>
      <td class="actions">
        <button class="btn-small" data-edit="${escapeAttr(p.id)}">Sửa</button>
        <button class="btn-small btn-danger" data-del="${escapeAttr(p.id)}">Xóa</button>
      </td>`;
    tb.appendChild(tr);
  }
}

$("#products-table").addEventListener("click", async (e) => {
  if (e.target.dataset.edit !== undefined) openProductModal(e.target.dataset.edit);
  else if (e.target.dataset.del !== undefined) {
    const id = e.target.dataset.del;
    if (!confirm(`Xóa sản phẩm "${id}"?`)) return;
    const r = await api(`/studio/products/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (r.code === 0) { toast("Đã xóa", "ok"); refreshProducts(); }
    else toast(r.msg, "error");
  }
});

$("#btn-new-product").addEventListener("click", () => openProductModal(null));

$("#btn-import-json").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const r = await fetch("/studio/products/upload", { method: "POST", body: fd });
  const j = await r.json();
  if (j.code === 0) { toast(`Import ${j.data.count} sản phẩm`, "ok"); refreshProducts(); }
  else toast(j.msg, "error");
  e.target.value = "";
});

async function openProductModal(pid) {
  _editingProductId = pid;
  const form = $("#product-form");
  form.reset();
  $("#attrs-editor").innerHTML = "";
  $("#sp-editor").innerHTML = "";
  $("#faq-editor").innerHTML = "";

  let p = {};
  if (pid) {
    const j = await api("/studio/products");
    if (j.code === 0) p = (j.data.products || []).find((x) => String(x.id) === String(pid)) || {};
    $("#product-modal-title").textContent = `Sửa: ${pid}`;
  } else {
    $("#product-modal-title").textContent = "Sản phẩm mới";
  }

  for (const k of ["id", "name", "price", "description", "image_url", "category"]) {
    if (form[k]) form[k].value = p[k] || "";
  }
  const attrs = { ...(p.attributes || {}) };
  if (p.colors) attrs["Màu sắc"] = Array.isArray(p.colors) ? p.colors.join(", ") : p.colors;
  if (p.sizes) attrs["Kích cỡ"] = Array.isArray(p.sizes) ? p.sizes.join(", ") : p.sizes;
  if (p.material) attrs["Chất liệu"] = p.material;
  for (const [k, v] of Object.entries(attrs)) {
    addKvRow($("#attrs-editor"), k, Array.isArray(v) ? v.join(", ") : v);
  }
  for (const s of p.selling_points || []) addListRow($("#sp-editor"), s);
  for (const [q, a] of Object.entries(p.faq || {})) addKvRow($("#faq-editor"), q, a);

  $("#product-modal").classList.add("open");
}

$$("[data-close]").forEach((b) => b.addEventListener("click", () => $("#product-modal").classList.remove("open")));
$("#product-modal").addEventListener("click", (e) => {
  if (e.target.id === "product-modal") $("#product-modal").classList.remove("open");
});

$("#attr-add").addEventListener("click", () => addKvRow($("#attrs-editor"), "", ""));
$("#sp-add").addEventListener("click", () => addListRow($("#sp-editor"), ""));
$("#faq-add").addEventListener("click", () => addKvRow($("#faq-editor"), "", ""));

function addKvRow(root, k, v) {
  const row = document.createElement("div");
  row.className = "kv-row";
  row.innerHTML = `
    <input type="text" placeholder="key" value="${escapeAttr(k)}" />
    <input type="text" placeholder="value" value="${escapeAttr(v)}" />
    <button type="button">×</button>`;
  row.querySelector("button").onclick = () => row.remove();
  root.appendChild(row);
}
function addListRow(root, v) {
  const row = document.createElement("div");
  row.className = "list-row";
  row.innerHTML = `<input type="text" value="${escapeAttr(v)}" /><button type="button">×</button>`;
  row.querySelector("button").onclick = () => row.remove();
  root.appendChild(row);
}

$("#product-form").addEventListener("submit", async (e) => {
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
  for (const row of $$(".kv-row", $("#attrs-editor"))) {
    const [k, v] = $$("input", row);
    if (k.value.trim()) payload.attributes[k.value.trim()] = v.value.trim();
  }
  for (const row of $$(".list-row", $("#sp-editor"))) {
    const v = $("input", row).value.trim();
    if (v) payload.selling_points.push(v);
  }
  for (const row of $$(".kv-row", $("#faq-editor"))) {
    const [k, v] = $$("input", row);
    if (k.value.trim()) payload.faq[k.value.trim()] = v.value.trim();
  }

  let r;
  if (_editingProductId !== null) {
    r = await api(`/studio/products/${encodeURIComponent(_editingProductId)}`, { method: "PUT", body: payload });
  } else {
    r = await api("/studio/products", { method: "POST", body: payload });
  }
  if (r.code === 0) { toast("Đã lưu", "ok"); $("#product-modal").classList.remove("open"); refreshProducts(); refreshLiveProducts(); }
  else toast(r.msg, "error");
});

// ═══════════════════════════════════════════════════════════════════════
//  VIDEO
// ═══════════════════════════════════════════════════════════════════════
$("#avatar-upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  setStatus($("#avatar-upload-status"), "Đang upload…");
  const r = await fetch("/studio/avatar/upload", { method: "POST", body: fd });
  const j = await r.json();
  if (j.code === 0) { setStatus($("#avatar-upload-status"), `OK: ${j.data.avatar_id} (${j.data.size_bytes} bytes)`, "ok"); refreshAvatars(); }
  else setStatus($("#avatar-upload-status"), j.msg, "error");
});

$("#avatar-action-form").addEventListener("click", async (e) => {
  if (e.target.tagName !== "BUTTON") return;
  const action = e.target.dataset.action;
  if (!action) return;
  const f = e.target.form;
  const payload = { avatar_id: f.avatar_id.value, model: f.model.value };
  let j;
  if (action === "preprocess") j = await api("/studio/avatar/preprocess", { method: "POST", body: payload });
  else if (action === "train") {
    if (payload.model !== "musetalk") { toast("Train chỉ cho musetalk", "error"); return; }
    j = await api("/studio/avatar/train", { method: "POST", body: { ...payload, epochs: +f.epochs.value } });
  } else if (action === "preview") {
    j = await api("/studio/avatar/preview", { method: "POST", body: { avatar_id: payload.avatar_id, text: "Xin chào, tôi là Linh" } });
  }
  if (j && j.code === 0) {
    toast(`Job ${j.data.job_id} chạy`, "ok");
    trackJob(j.data.job_id, $("#avatar-upload-status"));
    refreshJobs();
  } else if (j) toast(j.msg, "error");
});

function trackJob(jobId, statusEl) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/studio/job/${jobId}/ws`);
  ws.onmessage = (ev) => {
    try {
      const m = JSON.parse(ev.data);
      if (m.event === "update") {
        const d = m.data;
        const pct = Math.round((d.progress || 0) * 100);
        const cls = d.state === "done" ? "ok" : d.state === "failed" ? "error" : "";
        setStatus(statusEl, `[${d.state}] ${pct}% — ${d.meta?.msg || ""}`, cls);
        refreshJobs();
      }
    } catch {}
  };
}

async function refreshAvatars() {
  const j = await api("/studio/avatars");
  if (j.code !== 0) return;
  const list = j.data.avatars || [];
  const tb = $("#avatars-table tbody");
  tb.innerHTML = "";
  for (const a of list) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(a.id)}</code></td>
      <td class="${a.has_full_imgs ? "yes" : "no"}">${a.has_full_imgs ? "✓" : "—"}</td>
      <td class="${a.has_latents ? "yes" : "no"}">${a.has_latents ? "✓" : "—"}</td>
      <td class="${a.has_face_imgs ? "yes" : "no"}">${a.has_face_imgs ? "✓" : "—"}</td>
      <td class="${a.has_voice ? "yes" : "no"}">${a.has_voice ? "✓" : "—"}</td>
      <td class="${a.has_gestures ? "yes" : "no"}">${a.has_gestures ? "✓" : "—"}</td>
      <td class="actions"><button class="btn-small btn-danger" data-del="${escapeAttr(a.id)}">Xóa</button></td>`;
    tb.appendChild(tr);
  }
  $$(".avatar-select, #avatar-select").forEach((sel) => {
    const cur = sel.value;
    sel.innerHTML = list.map((a) => `<option value="${escapeAttr(a.id)}">${escapeHtml(a.id)}</option>`).join("");
    if (list.find((a) => a.id === cur)) sel.value = cur;
  });
}

$("#avatars-table").addEventListener("click", async (e) => {
  if (e.target.dataset.del !== undefined) {
    if (!confirm(`Xóa avatar "${e.target.dataset.del}"?`)) return;
    const r = await api("/studio/avatar/delete", { method: "POST", body: { avatar_id: e.target.dataset.del } });
    if (r.code === 0) { toast("Đã xóa", "ok"); refreshAvatars(); }
    else toast(r.msg, "error");
  }
});

$("#refresh-avatars").addEventListener("click", refreshAvatars);

async function refreshJobs() {
  const j = await api("/studio/jobs");
  if (j.code !== 0) return;
  const wrap = $("#jobs-list");
  wrap.innerHTML = "";
  for (const job of (j.data.jobs || []).slice(0, 10)) {
    const div = document.createElement("div");
    div.className = "job " + (job.state || "");
    const pct = Math.round((job.progress || 0) * 100);
    div.innerHTML = `
      <div class="head"><span>${escapeHtml(job.id)} · ${escapeHtml(job.kind)}</span><span>${escapeHtml(job.state)}</span></div>
      <div class="bar"><div style="width:${pct}%"></div></div>
      <div class="meta">${escapeHtml(job.meta?.msg || "")} ${job.meta?.loss !== undefined ? "loss=" + job.meta.loss : ""} ${escapeHtml(job.error || "")}</div>`;
    wrap.appendChild(div);
  }
}
$("#refresh-jobs").addEventListener("click", refreshJobs);
setInterval(() => { if ($("#tab-video").classList.contains("active")) refreshJobs(); }, 4000);

$("#gesture-upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  setStatus($("#gesture-status"), "Đang extract frames…");
  const r = await fetch("/studio/gesture/upload", { method: "POST", body: fd });
  const j = await r.json();
  if (j.code === 0) { setStatus($("#gesture-status"), `OK ${j.data.gesture}: ${j.data.frames} frames`, "ok"); refreshAvatars(); }
  else setStatus($("#gesture-status"), j.msg, "error");
});

$("#gesture-trigger-buttons").addEventListener("click", async (e) => {
  if (e.target.tagName !== "BUTTON") return;
  const name = e.target.dataset.g;
  const j = await api("/set_gesture", { method: "POST", body: { sessionid: getSessionId(), name } });
  if (j.code !== 0) toast(j.msg, "error"); else toast(`Trigger ${name}`, "ok");
});

// ═══════════════════════════════════════════════════════════════════════
//  ÂM THANH
// ═══════════════════════════════════════════════════════════════════════
$("#voice-upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  setStatus($("#voice-status"), "Đang xử lý…");
  const r = await fetch("/studio/voice/upload", { method: "POST", body: fd });
  const j = await r.json();
  if (j.code === 0) { setStatus($("#voice-status"), `OK — ${j.data.duration_secs.toFixed(1)}s @ ${j.data.sample_rate}Hz`, "ok"); refreshAvatars(); }
  else setStatus($("#voice-status"), j.msg, "error");
});

$("#tts-test-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  setStatus($("#tts-status"), "Đang đẩy vào TTS queue…");
  const j = await api("/human", { method: "POST", body: { type: "echo", text: f.text.value, sessionid: getSessionId() } });
  if (j.code === 0) setStatus($("#tts-status"), "OK — kiểm tra avatar đang nói", "ok");
  else setStatus($("#tts-status"), j.msg, "error");
});
$("#tts-stop").addEventListener("click", async () => {
  await api("/interrupt_talk", { method: "POST", body: { sessionid: getSessionId() } });
  setStatus($("#tts-status"), "Đã ngắt");
});

// ═══════════════════════════════════════════════════════════════════════
//  CÀI ĐẶT (dynamic config)
// ═══════════════════════════════════════════════════════════════════════
let _configSchema = {};
let _configCurrent = {};

async function refreshConfig() {
  const j = await api("/config");
  if (j.code !== 0) { toast(j.msg, "error"); return; }
  _configSchema = j.data.schema;
  _configCurrent = j.data.current;
  $("#config-raw").textContent = JSON.stringify(_configCurrent, null, 2);
  renderConfigForms();
}

function renderConfigForms() {
  $$("[data-config-form]").forEach((form) => {
    const group = form.dataset.group;
    form.innerHTML = "";
    const fields = Object.entries(_configSchema).filter(([_, m]) => m.group === group);
    for (const [key, meta] of fields) {
      const value = _configCurrent[key];
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
        if (meta.type === "bool") v = v === "true";
        else if (meta.type === "int") v = parseInt(v) || 0;
        else if (meta.type === "float") v = parseFloat(v) || 0;
        if (meta.secret && (v === "***" || v === "")) continue;
        payload[key] = v;
      }
      const j = await api("/config", { method: "POST", body: payload });
      if (j.code === 0) {
        const applied = (j.data.applied || []).length;
        const restartReq = (j.data.restart_required || []);
        let msg = `Đã lưu ${applied} field`;
        if (j.data.brain_restarted) msg += ` (brain restart)`;
        if (restartReq.length) msg += `. Cần restart server cho: ${restartReq.join(", ")}`;
        toast(msg, "ok");
        refreshConfig();
      } else toast(j.msg, "error");
    };
  });
}

// ─── Init ──────────────────────────────────────────────────────────────
refreshLiveProducts();
