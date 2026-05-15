// Base class for LiveTalking custom elements.
// Renders into light DOM (no Shadow DOM) so global styles + querySelector work.

export class LiveElement extends HTMLElement {
  constructor() {
    super();
    this._handlers = []; // [el, event, handler] for cleanup
  }

  connectedCallback() {
    if (this._rendered) return;
    this._rendered = true;
    this.render();
    this.bind();
    this.afterMount?.();
  }

  disconnectedCallback() {
    for (const [el, ev, h] of this._handlers) el.removeEventListener(ev, h);
    this._handlers = [];
    this.beforeUnmount?.();
  }

  /** Override in subclasses — set innerHTML from a template */
  render() {}

  /** Override in subclasses — attach event handlers using on() */
  bind() {}

  /** Tracked listener — automatically removed on disconnect */
  on(selector, event, handler) {
    const el = typeof selector === "string" ? this.querySelector(selector) : selector;
    if (!el) return;
    el.addEventListener(event, handler);
    this._handlers.push([el, event, handler]);
  }

  /** Tracked listener on multiple elements (e.g. all buttons in a row) */
  onAll(selector, event, handler) {
    for (const el of this.querySelectorAll(selector)) {
      el.addEventListener(event, handler);
      this._handlers.push([el, event, handler]);
    }
  }

  /** Helper — local query (scoped to this component) */
  $ (s)  { return this.querySelector(s); }
  $$(s)  { return Array.from(this.querySelectorAll(s)); }
}
