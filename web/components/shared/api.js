// Shared API helpers — unified fetch + DOM utilities + error handling.

export const $  = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

export async function api(path, opts = {}) {
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, opts);
  return await r.json();
}

export function setStatus(el, msg, type = "") {
  if (!el) return;
  el.textContent = msg;
  el.className = "status" + (type ? " " + type : "");
}

export function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
export const escapeAttr = (s) => escapeHtml(s).replace(/"/g, "&quot;");

export function getSessionId() {
  return ($("#sessionid")?.value || "0").trim() || "0";
}
