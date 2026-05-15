// Toast notifications — global container injected on first use.

let _container = null;

function ensureContainer() {
  if (_container && document.body.contains(_container)) return _container;
  _container = document.getElementById("toast-container");
  if (!_container) {
    _container = document.createElement("div");
    _container.id = "toast-container";
    document.body.appendChild(_container);
  }
  return _container;
}

export function toast(msg, type = "", durationMs = 3500) {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  ensureContainer().appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity 200ms, transform 200ms";
    el.style.opacity = "0";
    el.style.transform = "translateX(40px)";
    setTimeout(() => el.remove(), 220);
  }, durationMs);
  return el;
}
