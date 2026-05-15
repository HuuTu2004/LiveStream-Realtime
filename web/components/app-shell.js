// <app-shell> — topbar, sidebar nav, status indicator, tab routing.

import { LiveElement } from "./shared/element.js";

const TABS = [
  { id: "live",     label: "Live",      icon: "🔴", tag: "live-panel" },
  { id: "products", label: "Sản phẩm",  icon: "📦", tag: "product-panel" },
  { id: "video",    label: "Video",     icon: "🎥", tag: "video-panel" },
  { id: "config",   label: "Cài đặt",   icon: "⚙️", tag: "config-panel" },
];

class AppShell extends LiveElement {
  render() {
    this.innerHTML = `
      <header class="topbar">
        <div class="brand">
          <span class="brand-mark">●</span>
          <h1>LiveTalking <span class="muted">Quản trị bán hàng livestream</span></h1>
        </div>
        <nav class="topbar-actions">
          <span id="server-status" class="pill" title="Server ping">
            <span class="status-dot"></span>
            <span class="server-status-label">Online</span>
          </span>
          <input type="text" id="sessionid" value="0" title="Session ID" />
        </nav>
      </header>
      <div class="layout">
        <aside class="tabs" role="tablist">
          ${TABS.map((t, i) => `
            <button class="tab${i === 0 ? " active" : ""}" data-tab="${t.id}" role="tab"
                    aria-selected="${i === 0 ? "true" : "false"}">
              <span class="tab-icon">${t.icon}</span>
              <span class="tab-label">${t.label}</span>
            </button>`).join("")}
        </aside>
        <main class="content">
          ${TABS.map((t, i) => `
            <section id="tab-${t.id}" class="panel${i === 0 ? " active" : ""}" role="tabpanel">
              <${t.tag}></${t.tag}>
            </section>`).join("")}
        </main>
      </div>
    `;
  }

  bind() {
    this.onAll(".tab", "click", (e) => this._switchTab(e.currentTarget));
  }

  afterMount() {
    this._startPing();
    // Reflect first tab in URL hash (optional deep-linking)
    if (location.hash) {
      const id = location.hash.slice(1);
      const btn = this.querySelector(`[data-tab="${id}"]`);
      if (btn) this._switchTab(btn);
    }
  }

  _switchTab(btn) {
    const id = btn.dataset.tab;
    for (const t of this.$$(".tab")) {
      const active = t === btn;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", active);
    }
    for (const p of this.$$(".panel")) {
      p.classList.toggle("active", p.id === `tab-${id}`);
    }
    location.hash = id;
    // Notify the new panel — let it refresh its data
    const panel = this.$(`#tab-${id} > *`);
    panel?.onActivate?.();
  }

  async _startPing() {
    const pill = this.$("#server-status");
    const label = this.$(".server-status-label");
    const tick = async () => {
      try {
        // /preview-info luôn có sẵn (không phụ thuộc studio_enabled)
        const r = await fetch("/preview-info", { cache: "no-store" });
        pill.classList.toggle("off", !r.ok);
        label.textContent = r.ok ? "Online" : "Offline";
      } catch {
        pill.classList.add("off");
        label.textContent = "Offline";
      }
    };
    tick();
    this._pingInterval = setInterval(tick, 5000);
  }

  beforeUnmount() {
    if (this._pingInterval) clearInterval(this._pingInterval);
  }
}

customElements.define("app-shell", AppShell);
