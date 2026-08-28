/**
 * Shared UI builders: icons, status badges, toasts, modals (accessible
 * dialog pattern), form fields, progress, empty states.
 *
 * All user/backend data is rendered through text nodes (see dom.js) — no
 * innerHTML with dynamic content.
 */

import { el, clamp } from "./dom.js";

/* ============================================================================
 * Icons — inline SVG, 24px viewBox, currentColor.
 * ==========================================================================*/

const ICONS = {
  logo: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='5' width='18' height='14' rx='2'/><path d='M7 5v14M17 5v14M3 12h18'/></svg>",
  dashboard: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='3' width='8' height='8' rx='1'/><rect x='13' y='3' width='8' height='5' rx='1'/><rect x='13' y='10' width='8' height='11' rx='1'/><rect x='3' y='13' width='8' height='8' rx='1'/></svg>",
  plus: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'><path d='M12 5v14M5 12h14'/></svg>",
  menu: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'><path d='M4 6h16M4 12h16M4 18h16'/></svg>",
  script: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M6 3h9l4 4v14H6z'/><path d='M9 9h7M9 13h7M9 17h5'/></svg>",
  storyboard: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='4' width='18' height='7' rx='1'/><rect x='3' y='14' width='7' height='7' rx='1'/><rect x='12' y='14' width='9' height='7' rx='1'/></svg>",
  mic: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='9' y='3' width='6' height='11' rx='3'/><path d='M5 11a7 7 0 0 0 14 0M12 18v3'/></svg>",
  music: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M9 18V6l10-2v12'/><circle cx='6.5' cy='18' r='2.5'/><circle cx='16.5' cy='16' r='2.5'/></svg>",
  captions: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='5' width='18' height='14' rx='2'/><path d='M7 13h4M7 16h8M13 13h4'/></svg>",
  timeline: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M3 7h18M3 12h12M3 17h15'/><circle cx='18' cy='12' r='2'/><circle cx='8' cy='17' r='2'/></svg>",
  export: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M12 15V3M7 8l5-5 5 5'/><path d='M4 15v5h16v-5'/></svg>",
  settings: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='3'/><path d='M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7 7 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.06-.4.1-.8.1-1.2z'/></svg>",
  cpu: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='6' y='6' width='12' height='12' rx='1'/><rect x='10' y='10' width='4' height='4'/><path d='M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4'/></svg>",
  check: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'><path d='M4 12l5 5L20 6'/></svg>",
  alert: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M12 3l10 18H2z'/><path d='M12 10v5M12 18.5v.5'/></svg>",
  info: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round'><circle cx='12' cy='12' r='9'/><path d='M12 11v5M12 8v.5'/></svg>",
  x: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round'><path d='M6 6l12 12M18 6L6 18'/></svg>",
  lock: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='5' y='11' width='14' height='9' rx='2'/><path d='M8 11V8a4 4 0 0 1 8 0v3'/></svg>",
  unlock: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='5' y='11' width='14' height='9' rx='2'/><path d='M8 11V8a4 4 0 0 1 7.5-2'/></svg>",
  play: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M7 4l13 8-13 8z'/></svg>",
  stop: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8'><rect x='6' y='6' width='12' height='12' rx='2'/></svg>",
  refresh: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M20 11a8 8 0 1 0-2 6'/><path d='M20 4v7h-7'/></svg>",
  film: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='4' width='18' height='16' rx='2'/><path d='M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4'/></svg>",
  folder: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M3 6h6l2 2h10v12H3z'/></svg>",
  chevron: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M9 6l6 6-6 6'/></svg>",
  wifi_off: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M2 8a15 15 0 0 1 4-2.5M12 20a15 15 0 0 0-10-4M5.6 13a10 10 0 0 1 3-2M12 20a10 10 0 0 0 6.4-2M9 15.5a6 6 0 0 1 4-1.2'/><circle cx='12' cy='20' r='1'/><path d='M3 3l18 18'/></svg>",
};

/**
 * @param {keyof typeof ICONS} name
 * @param {number} [size]
 * @returns {HTMLElement}
 */
export function icon(name, size = 18) {
  const node = el("span", { class: "icon-svg", style: { width: `${size}px`, height: `${size}px`, display: "inline-block", flex: "none" } });
  node.innerHTML = ICONS[name] || ICONS.info; // static, trusted markup only
  node.setAttribute("aria-hidden", "true");
  return node;
}

/* ============================================================================
 * Status badges
 * ==========================================================================*/

const BADGE_KINDS = new Set(["good", "warning", "serious", "critical", "neutral", "offline", "accent"]);

/**
 * @param {"good"|"warning"|"serious"|"critical"|"neutral"|"offline"|"accent"} kind
 * @param {string} label
 * @param {boolean} [dot]
 * @returns {HTMLElement}
 */
export function badge(kind, label, dot = true) {
  if (!BADGE_KINDS.has(kind)) kind = "neutral";
  return el("span", { class: `badge badge-${kind}` },
    dot ? el("span", { class: "dot" }) : null,
    label,
  );
}

/**
 * Badge for a job status value.
 * @param {string} status
 * @returns {HTMLElement}
 */
export function jobStatusBadge(status) {
  const map = {
    queued: ["neutral", "Queued"],
    preparing: ["accent", "Preparing"],
    loading_model: ["accent", "Loading model"],
    generating: ["accent", "Generating"],
    postprocessing: ["accent", "Postprocessing"],
    completed: ["good", "Completed"],
    failed: ["critical", "Failed"],
    canceled: ["warning", "Canceled"],
  };
  const [kind, label] = map[status] || ["neutral", status || "Unknown"];
  return badge(kind, label);
}

/**
 * Badge for a scene status (locked wins over everything).
 * @param {string} status
 * @param {boolean} locked
 * @returns {HTMLElement}
 */
export function sceneStatusBadge(status, locked) {
  if (locked || status === "locked") return badge("warning", "Locked");
  const map = {
    draft: ["neutral", "Draft"],
    queued: ["neutral", "Queued"],
    generating: ["accent", "Generating"],
    generated: ["accent", "Generated"],
    approved: ["good", "Approved"],
    failed: ["critical", "Failed"],
  };
  const [kind, label] = map[status] || ["neutral", status || "Unknown"];
  return badge(kind, label);
}

/**
 * Badge for a project status.
 * @param {string} status
 * @returns {HTMLElement}
 */
export function projectStatusBadge(status) {
  const map = {
    draft: ["neutral", "Draft"],
    planning: ["accent", "Planning"],
    generating: ["accent", "Generating"],
    rendering: ["accent", "Rendering"],
    completed: ["good", "Completed"],
    failed: ["critical", "Failed"],
    canceled: ["warning", "Canceled"],
  };
  const [kind, label] = map[status] || ["neutral", status || "Unknown"];
  return badge(kind, label);
}

/**
 * Readable labels for the pipeline's known stage values (raw value elsewhere).
 * Single source of truth for toasts, the Job Monitor, and the stage chips on
 * the per-stage screens.
 */
export const STAGE_LABELS = {
  plan: "Script plan",
  narration: "Narration",
  references: "Reference images",
  visuals: "Scene visuals",
  scene_visual: "Scene visual",
  visual_batch: "Visual batch",
  music: "Music",
  subtitles: "Subtitles",
  timeline: "Timeline",
  render_preview: "Preview render",
  render_final: "Final render",
  quality_control: "Quality check",
  thumbnails: "Thumbnails",
  metadata: "Metadata",
  render: "Final video render",
  pipeline: "Full render",
};

/**
 * Human label for a raw stage value; unknown values pass through.
 * @param {string} [stage]
 * @returns {string}
 */
export function stageLabel(stage) {
  if (typeof stage === "string" && stage.startsWith("thumbnail:")) return "Thumbnail";
  return STAGE_LABELS[stage] || stage || "unknown";
}

/**
 * Pipeline-stage chip: human stage label plus status
 * (completed/failed/running/pending).
 * @param {string} name - raw stage key
 * @param {{status?: string} | null | undefined} st
 * @returns {HTMLElement}
 */
export function stageChip(name, st) {
  const status = (st && st.status) || "pending";
  const kind = status === "completed" ? "good"
    : status === "failed" ? "critical"
    : status === "running" ? "accent"
    : "neutral";
  const statusLabel = { completed: "Completed", failed: "Failed", running: "Running", pending: "Pending" }[status] || status;
  return badge(kind, `${stageLabel(name)}: ${statusLabel}`);
}

/* ============================================================================
 * Toasts
 * ==========================================================================*/

const TOAST_TTL = 6000;

/**
 * Show a transient notification.
 * @param {"good"|"warning"|"critical"|"info"} kind
 * @param {string} title
 * @param {string} [message]
 */
export function toast(kind, title, message) {
  let region = document.getElementById("toasts");
  if (!region) {
    region = el("div", { id: "toasts" });
    document.body.append(region);
  }
  const border = kind === "good" ? "t-good" : kind === "warning" ? "t-warning" : kind === "critical" ? "t-critical" : "";
  const node = el("div", { class: `toast ${border}`, role: "status" },
    el("div", { class: "t-msg" },
      el("div", { class: "t-title" }, title),
      message ? el("div", { class: "muted small" }, message) : null,
    ),
    el("button", { class: "btn btn-ghost btn-sm t-close", type: "button", "aria-label": "Dismiss" }, icon("x", 14)),
  );
  const dismiss = () => node.remove();
  node.querySelector(".t-close").addEventListener("click", dismiss);
  region.append(node);
  while (region.children.length > 4) region.firstElementChild?.remove();
  setTimeout(dismiss, TOAST_TTL);
}

/**
 * Render a normalized ApiError into a toast.
 * @param {import("./api.js").ApiError} err
 * @param {string} [context]
 */
export function toastError(err, context) {
  const titles = {
    offline: "Backend unreachable",
    timeout: "Request timed out",
    auth: "Authentication problem",
    incompatible: "Service incompatibility",
    insufficient_vram: "Not enough VRAM",
    conflict: "Conflict",
    validation: "Validation failed",
    not_found: "Not found",
    server: "Server error",
    pending: "Not available yet",
    unknown: "Error",
  };
  toast(
    err.kind === "good" ? "info" : err.kind === "conflict" || err.kind === "insufficient_vram" || err.kind === "incompatible" ? "warning" : err.kind === "not_found" || err.kind === "server" ? "critical" : "info",
    titles[err.kind] || "Error",
    context ? `${context} — ${err.message}` : err.message,
  );
}

/* ============================================================================
 * Modal / dialog (reusable accessible pattern)
 * ==========================================================================*/

/**
 * Open a modal. Native <dialog> provides backdrop, Esc-to-close, and top-layer
 * stacking. Initial focus goes to the first actionable element; focus is
 * returned to the opener on close.
 *
 * @param {object} opts
 * @param {string} opts.title
 * @param {Node | ((body: HTMLElement) => void)} opts.body
 * @param {{label: string, kind?: string, onClick: (close: () => void) => void}[]} [opts.actions]
 * @param {string} [opts.kind] — "danger" | "default" for the confirm button
 * @returns {{dialog: HTMLDialogElement, close: () => void}}
 */
export function openModal({ title, body, actions = [] }) {
  const previous = document.activeElement;
  const dialog = el("dialog", { class: "modal", "aria-modal": "true", "aria-label": title });
  // Declared before every listener below registers it (a later `const` would
  // be a TDZ ReferenceError the moment openModal runs).
  const close = () => {
    dialog.close();
    setTimeout(() => dialog.remove(), 200);
    if (previous instanceof HTMLElement) previous.focus();
  };
  const foot = el("div", { class: "modal-foot" });
  for (const action of actions) {
    const cls = action.kind === "primary" ? "btn btn-primary" : action.kind === "danger" ? "btn btn-danger" : "btn";
    foot.append(el("button", { class: cls, type: "button" }, action.label));
    foot.lastElementChild.addEventListener("click", () => action.onClick(() => close()));
  }
  if (!actions.length) {
    foot.append(el("button", { class: "btn", type: "button" }, "Close"));
    foot.lastElementChild.addEventListener("click", close);
  }
  const bodyNode = typeof body === "function" ? el("div") : body;
  if (typeof body === "function") body(bodyNode);
  const content = bodyNode;
  dialog.append(
    el("div", { class: "modal-head" },
      el("h2", {}, title),
      el("button", { class: "btn btn-ghost btn-sm modal-x", type: "button", "aria-label": "Close dialog" }, icon("x", 14)),
    ),
    el("div", { class: "modal-body" }, content),
    foot,
  );
  dialog.querySelector(".modal-x").addEventListener("click", close);
  document.body.append(dialog);
  dialog.showModal();
  // Keep initial focus inside the dialog.
  const first = dialog.querySelector("button, input, select, textarea, [tabindex]");
  if (first) first.focus();
  // Basic focus trap.
  dialog.addEventListener("keydown", (ev) => {
    if (ev.key !== "Tab") return;
    const focusables = [...dialog.querySelectorAll("button, input, select, textarea, a[href], [tabindex]:not([tabindex='-1'])")]
      .filter((n) => !n.disabled && n.offsetParent !== null);
    if (!focusables.length) return;
    const firstEl = focusables[0];
    const lastEl = focusables[focusables.length - 1];
    if (ev.shiftKey && document.activeElement === firstEl) { ev.preventDefault(); lastEl.focus(); }
    else if (!ev.shiftKey && document.activeElement === lastEl) { ev.preventDefault(); firstEl.focus(); }
  });
  return { dialog, close };
}

/**
 * Confirmation modal. Resolves true on confirm, false on dismiss.
 * @param {object} opts
 * @param {string} opts.title
 * @param {string} opts.message
 * @param {string} [opts.confirmLabel]
 * @param {"danger"|"primary"} [opts.kind]
 * @returns {Promise<boolean>}
 */
export function confirm({ title, message, confirmLabel = "Confirm", kind = "danger" }) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (value) => {
      if (settled) return;
      settled = true;
      close();
      resolve(value);
    };
    const { close, dialog } = openModal({
      title,
      body: el("div", { class: "small" }, message),
      actions: [
        { label: "Cancel", onClick: () => settle(false) },
        { label: confirmLabel, kind, onClick: () => settle(true) },
      ],
    });
    dialog.addEventListener("cancel", () => settle(false), { once: true });
  });
}

/* ============================================================================
 * Form field wrapper
 * ==========================================================================*/

/**
 * Build a labeled field.
 * @param {object} opts
 * @param {string} opts.label
 * @param {HTMLElement} opts.input
 * @param {string} [opts.hint]
 * @returns {HTMLElement}
 */
export function field({ label, input, hint }) {
  const labelNode = el("label", {}, label);
  // Controls without an id get one, so the label's `for` always resolves and
  // screen readers pair the visible label with its control.
  const isControl = /^(INPUT|SELECT|TEXTAREA)$/.test(input.tagName);
  const baseId = (isControl && input.id) ? input.id : `fld-${Math.random().toString(36).slice(2, 8)}`;
  if (isControl && !input.id) input.id = baseId;
  labelNode.id = `${baseId}-label`;
  // Associate the label with a single form control (not a composite wrapper).
  if (isControl) labelNode.setAttribute("for", input.id);
  if (hint) input.setAttribute("aria-describedby", `${baseId}-hint`);
  const wrap = el("div", { class: "field" }, labelNode, input);
  if (hint) wrap.append(el("div", { class: "hint", id: `${baseId}-hint` }, hint));
  return wrap;
}

/**
 * Show an inline error under a field (or clear it).
 * @param {HTMLElement} fieldWrap — as returned by field()
 * @param {string | null} message
 */
export function setFieldError(fieldWrap, message) {
  const input = fieldWrap.querySelector("input, select, textarea");
  const existing = fieldWrap.querySelector(".error-text");
  if (existing) existing.remove();
  if (message) {
    const errNode = el("div", { class: "error-text", id: `${input ? input.id : "field"}-error` }, message);
    fieldWrap.append(errNode);
    if (input) {
      input.setAttribute("aria-invalid", "true");
      input.setAttribute("aria-describedby", errNode.id);
    }
  } else if (input) {
    input.removeAttribute("aria-invalid");
    const hint = fieldWrap.querySelector(".hint");
    if (hint) input.setAttribute("aria-describedby", hint.id);
    else input.removeAttribute("aria-describedby");
  }
}

/* ============================================================================
 * Progress
 * ==========================================================================*/

/**
 * Progress row for 0..1.
 * @param {number} value
 * @param {"warn"|"bad"|"default"} [variant]
 * @returns {HTMLElement}
 */
export function progress(value, variant = "default") {
  const pct = Math.round(clamp(value, 0, 1) * 100);
  const cls = variant === "default" ? "bar" : `bar ${variant}`;
  return el("div", { class: "progress-row", role: "progressbar", "aria-label": "Progress", "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100" },
    el("div", { class: "progress" }, el("div", { class: cls, style: { width: `${pct}%` } })),
    el("div", { class: "pct" }, `${pct}%`),
  );
}

/* ============================================================================
 * Loading / empty states
 * ==========================================================================*/

/**
 * Skeleton loading block.
 * @param {number} [rows]
 * @returns {HTMLElement}
 */
export function loadingState(rows = 3) {
  const box = el("div", { role: "status", "aria-label": "Loading" });
  box.append(el("div", { class: "row" }, el("span", { class: "spinner" }), el("span", { class: "muted small" }, "Loading…")));
  for (let i = 0; i < rows; i += 1) {
    box.append(el("div", { class: "skeleton", style: { margin: "8px 0", minHeight: `${18 + (i % 3) * 10}px` } }));
  }
  return box;
}

/**
 * @param {string} title
 * @param {string} [message]
 * @param {HTMLElement[]} [actions]
 * @returns {HTMLElement}
 */
export function emptyState(title, message, actions = []) {
  const node = el("div", { class: "empty-state", role: "status" },
    el("div", { class: "empty-title" }, title),
  );
  if (message) node.append(el("div", {}, message));
  if (actions.length) node.append(el("div", { class: "empty-actions" }, actions));
  return node;
}

/**
 * Generic error panel for a failed read.
 * @param {import("./api.js").ApiError} err
 * @param {HTMLElement} [retryButton]
 * @returns {HTMLElement}
 */
export function errorPanel(err, retryButton) {
  const node = el("div", { class: "panel", role: "alert" },
    el("div", { class: "row" },
      icon("alert", 20),
      el("div", {},
        el("div", { style: { fontWeight: "650" } }, err.kind === "offline" ? "Cannot reach the backend" : "Something went wrong"),
        el("div", { class: "muted small" }, err.message),
      ),
    ),
  );
  if (retryButton) node.append(el("div", { class: "mt" }, retryButton));
  return node;
}
