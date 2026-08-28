/**
 * Minimal DOM helpers. No dependencies.
 *
 * `el()` builds elements with attribute/event handling and safe text
 * insertion (string children become textContent, never innerHTML), which
 * keeps user/backend data injection-safe.
 */

const EVENT_ATTRS = new Set([
  "onclick", "onchange", "oninput", "onkeydown", "onkeyup",
  "onfocus", "onblur", "onopen", "onclose", "ontoggle",
  "onsubmit",
]);

/** Attributes that must be assigned as properties. */
const PROPERTY_ATTRS = new Set([
  "value", "for", "colspan", "rowspan", "maxlength", "min", "max", "step",
  "placeholder", "name", "checked", "disabled", "hidden", "open", "multiple",
  "autofocus", "tabindex", "readonly", "selected", "autofocus",
]);

/**
 * Create an element.
 * @template {keyof HTMLElementTagNameMap} T
 * @param {T} tag
 * @param {Record<string, any>} [attrs] — keys: class, style, dataset, onX events,
 *   or any HTML attribute.
 * @param {(Node | string)[]} children
 * @returns {HTMLElementTagNameMap[T]}
 */
export function el(tag, attrs = {}, ...children) {
  const node = /** @type {HTMLElementTagNameMap[T]} */ (document.createElement(tag));
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (EVENT_ATTRS.has(key)) node.addEventListener(key.slice(2), value);
    else if (PROPERTY_ATTRS.has(key)) node[key] = value;
    else node.setAttribute(key, value);
  }
  append(node, ...children);
  return node;
}

/**
 * Append children, converting strings to text nodes (never parsed as HTML).
 * @param {Node} node
 * @param {(Node | string)[]} children
 */
export function append(node, ...children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/**
 * Replace a node's children.
 * @param {Node} node
 * @param {(Node | string)[]} children
 */
export function fill(node, ...children) {
  node.replaceChildren();
  append(node, ...children);
}

/** Clear a node. @param {Node} node */
export function clear(node) {
  node.replaceChildren();
}

/**
 * Format seconds as m:ss (or h:mm:ss when longer).
 * @param {number} seconds
 * @returns {string}
 */
export function fmtDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  // Round to whole seconds up front: decomposing first and rounding the
  // seconds component afterwards can overflow it (59.9s → "0:60").
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

/**
 * Format gigabytes.
 * @param {number | null | undefined} gb
 * @returns {string}
 */
export function fmtGb(gb) {
  if (gb === null || gb === undefined || !Number.isFinite(gb)) return "n/a";
  return `${gb.toFixed(1)} GiB`;
}

/**
 * Format an ISO timestamp for display.
 * @param {string | null | undefined} iso
 * @returns {string}
 */
export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/**
 * Shorten a UUID for compact display.
 * @param {string | null | undefined} id
 * @returns {string}
 */
export function shortId(id) {
  if (!id) return "—";
  return id.length > 13 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

/**
 * Clamp a number into [lo, hi].
 * @param {number} v @param {number} lo @param {number} hi
 * @returns {number}
 */
export function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}
