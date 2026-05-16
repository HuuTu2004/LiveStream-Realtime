// <app-shell> — sidebar nav (grouped), topbar with breadcrumb + status,
// responsive drawer for tablet/mobile.

import { LiveElement } from "./shared/element.js";

const NAV_SECTIONS = [
  {
    title: "Vận hành",
    items: [
      { id: "live",     label: "Live",      tag: "live-panel",    icon: "live"    },
    ],
  },
  {
    title: "Nội dung",
    items: [
      { id: "products", label: "Sản phẩm",  tag: "product-panel", icon: "box"     },
      { id: "video",    label: "Avatar",    tag: "video-panel",   icon: "camera"  },
    ],
  },
  {
    title: "Hệ thống",
    items: [
      { id: "config",   label: "Cài đặt",   tag: "config-panel",  icon: "gear"    },
    ],
  },
];

const ICONS = {
  live: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3" fill="currentColor"/><path d="M5 12a7 7 0 0114 0M3 12a9 9 0 0118 0"/></svg>`,
  box:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8L12 3 3 8v8l9 5 9-5V8z"/><path d="M3 8l9 5 9-5M12 13v9"/></svg>`,
  camera: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="14" height="12" rx="2"/><path d="M22 8l-6 4 6 4V8z"/></svg>`,
  gear: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 01-4 0v-.1a1.7 1.7 0 00-1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 010-4h.1a1.7 1.7 0 001.5-1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3h.1a1.7 1.7 0 001-1.5V3a2 2 0 014 0v.1a1.7 1.7 0 001 1.5h.1a1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8v.1a1.7 1.7 0 001.5 1H21a2 2 0 010 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg>`,
  menu: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>`,
};

class AppShell extends LiveElement {
  render() {
    const allItems = NAV_SECTIONS.flatMap((s) => s.items);
    const first = allItems[0];

    this.innerHTML = `
      <aside class="sidebar" role="navigation" aria-label="Điều hướng chính">
        <div class="sidebar-brand">
          <div class="logo" aria-hidden="true"></div>
          <div>
            <div class="name">LiveTalking</div>
            <div class="tag">Sales Console</div>
          </div>
        </div>
        <nav class="sidebar-nav">
          ${NAV_SECTIONS.map((sec) => `
            <div>
              <div class="nav-section-title">${sec.title}</div>
              <div class="nav-section" role="tablist">
                ${sec.items.map((t, i) => `
                  <button class="tab${t.id === first.id ? " active" : ""}"
                          data-tab="${t.id}"
                          data-label="${t.label}"
                          role="tab"
                          aria-selected="${t.id === first.id ? "true" : "false"}">
                    <span class="tab-icon">${ICONS[t.icon] || ""}</span>
                    <span class="tab-label">${t.label}</span>
                  </button>`).join("")}
              </div>
            </div>`).join("")}
        </nav>
        <div class="sidebar-footer">
          <span>v1.0</span>
          <span class="env-badge" id="env-badge">dev</span>
        </div>
      </aside>

      <header class="topbar">
        <button class="icon-btn menu-toggle" id="menu-toggle"
                aria-label="Mở menu" aria-expanded="false">
          ${ICONS.menu}
        </button>
        <div class="brand-mobile">LiveTalking</div>
        <div class="crumbs">
          <span>Console</span>
          <span class="sep">/</span>
          <span class="current" id="crumb-current">${first.label}</span>
        </div>
        <div class="topbar-actions">
          <span id="server-status" class="pill success" title="Server ping">
            <span class="status-dot"></span>
            <span class="server-status-label">Online</span>
          </span>
          <div class="session-field" title="Session ID hiện hành">
            <label for="sessionid">Session</label>
            <input type="text" id="sessionid" value="0" maxlength="3" />
          </div>
        </div>
      </header>

      <main class="content">
        <div class="content-inner">
          ${allItems.map((t) => `
            <section id="tab-${t.id}" class="panel${t.id === first.id ? " active" : ""}"
                     role="tabpanel" aria-labelledby="tab-btn-${t.id}">
              <${t.tag}></${t.tag}>
            </section>`).join("")}
        </div>
      </main>

      <div class="drawer-backdrop" id="drawer-backdrop"></div>
    `;
  }

  bind() {
    this.onAll(".tab", "click", (e) => this._switchTab(e.currentTarget));
    this.on("#menu-toggle",     "click", () => this._toggleDrawer());
    this.on("#drawer-backdrop", "click", () => this._closeDrawer());

    // Close drawer on ESC
    this._escHandler = (e) => { if (e.key === "Escape") this._closeDrawer(); };
    document.addEventListener("keydown", this._escHandler);
  }

  afterMount() {
    this._startPing();
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
    this.$("#crumb-current").textContent = btn.dataset.label || id;
    location.hash = id;
    this._closeDrawer();

    const panel = this.$(`#tab-${id} > *`);
    panel?.onActivate?.();
  }

  _toggleDrawer() {
    const open = this.getAttribute("data-drawer-open") === "true";
    this.setAttribute("data-drawer-open", open ? "false" : "true");
    this.$("#menu-toggle")?.setAttribute("aria-expanded", open ? "false" : "true");
  }
  _closeDrawer() {
    this.setAttribute("data-drawer-open", "false");
    this.$("#menu-toggle")?.setAttribute("aria-expanded", "false");
  }

  async _startPing() {
    const pill  = this.$("#server-status");
    const label = this.$(".server-status-label");
    const tick = async () => {
      try {
        const r = await fetch("/preview-info", { cache: "no-store" });
        if (r.ok) {
          pill.classList.remove("off", "danger");
          pill.classList.add("success");
          label.textContent = "Online";
        } else {
          pill.classList.remove("success");
          pill.classList.add("off", "danger");
          label.textContent = "Offline";
        }
      } catch {
        pill.classList.remove("success");
        pill.classList.add("off", "danger");
        label.textContent = "Offline";
      }
    };
    tick();
    this._pingInterval = setInterval(tick, 5000);
  }

  beforeUnmount() {
    if (this._pingInterval) clearInterval(this._pingInterval);
    if (this._escHandler)   document.removeEventListener("keydown", this._escHandler);
  }
}

customElements.define("app-shell", AppShell);
