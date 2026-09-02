/**
 * App bootstrap and shell: builds the top bar, sidebar navigation, and
 * content region; wires hash routing; drives the live top bar widgets.
 *
 * Live data (Stage 2):
 *  - GET /health              -> connection badge + mock/local mode badge
 *  - GET /api/system/status   -> active model + compact GPU/VRAM (boot fetch)
 *  - GET /api/projects        -> project switcher (persisted selection)
 *  - job feed (SSE / polling) -> queued/active job count + one-time
 *    completed/failed/canceled toasts (see notifyJobTransitions)
 *
 * Later stages replace the route placeholder in renderRoute() with real
 * screens and add per-screen data loading.
 */

import { el, fill, fmtGb, shortId } from "./dom.js";
import {
  state,
  restoreCurrentProject,
  persistCurrentProject,
  reconcileProjects,
  countQueuedJobs,
  countActiveJobs,
} from "./state.js";
import { loadConfig } from "./config.js";
import { parseRoute, onHashChange, navigate, onRoute } from "./router.js";
import { icon, badge, toast, STAGE_LABELS } from "./ui.js";
import { health, systemStatus, listProjects } from "./api.js";
import { createJobFeed } from "./events.js";
import { renderDashboard } from "./pages/dashboard.js";
import { renderNewProject } from "./pages/new-project.js";
import { renderProject } from "./pages/project.js";
import { renderScript } from "./pages/script.js";
import { renderStoryboard } from "./pages/storyboard.js";
import { renderThumbnails } from "./pages/thumbnails.js";
import { renderSceneEditor } from "./pages/scene-editor.js";
import { renderVoice } from "./pages/voice.js";
import { renderMusic } from "./pages/music.js";
import { renderCaptions } from "./pages/captions.js";
import { renderEditorial } from "./pages/editorial.js";
import { renderTimeline } from "./pages/timeline.js";
import { renderExport } from "./pages/export.js";
import { renderSettings } from "./pages/settings.js";
import { renderModels } from "./pages/models.js";
import { renderJobs } from "./pages/jobs.js";

/** @typedef {{name: string, hash: string, label: string, icon: string}} NavItem */

/** Primary studio navigation, in display order. */
const NAV_PRIMARY = /** @type {NavItem[]} */ ([
  { name: "dashboard", hash: "#/", label: "Dashboard", icon: "dashboard" },
  { name: "new-project", hash: "#/new", label: "New Project", icon: "plus" },
  { name: "project", hash: "#/project", label: "Project", icon: "folder" },
  { name: "script", hash: "#/script", label: "Script", icon: "script" },
  { name: "storyboard", hash: "#/storyboard", label: "Storyboard", icon: "storyboard" },
  { name: "thumbnails", hash: "#/thumbnails", label: "Thumbnails", icon: "storyboard" },
  { name: "voice", hash: "#/voice", label: "Voice", icon: "mic" },
  { name: "music", hash: "#/music", label: "Music", icon: "music" },
  { name: "captions", hash: "#/captions", label: "Captions", icon: "captions" },
  { name: "editorial", hash: "#/editorial", label: "Editorial", icon: "canvas" },
  { name: "timeline", hash: "#/timeline", label: "Timeline", icon: "timeline" },
  { name: "export", hash: "#/export", label: "Export", icon: "export" },
]);

/** System navigation, pinned to the sidebar foot. */
const NAV_FOOT = /** @type {NavItem[]} */ ([
  { name: "jobs", hash: "#/jobs", label: "Job Monitor", icon: "film" },
  { name: "models", hash: "#/models", label: "Models", icon: "cpu" },
  { name: "settings", hash: "#/settings", label: "Settings", icon: "settings" },
]);

const NAV_COLLAPSED_KEY = "lvs-nav-collapsed";
const HEALTH_RETRY_MS = 10000;

/** @type {HTMLElement|null} the `.content` main element. */
let contentEl = null;
/** @type {HTMLElement|null} the `.body` grid (nav + content). */
let bodyEl = null;
/** @type {(() => void)|null} live-update hook for the currently displayed screen. */
let liveUpdate = null;

/**
 * Register the current screen's live-update hook, called whenever the job
 * feed emits an update (SSE or polling). Pass null to clear (done on every
 * route change). Screens that need live data (Job Monitor) register one.
 * @param {(() => void)|null} fn
 */
export function registerLiveUpdate(fn) {
  liveUpdate = fn;
}
/** @type {HTMLElement|null} the `.topbar-status` slot container. */
let topbarStatusEl = null;
/** @type {HTMLButtonElement|null} the navigation toggle button. */
let navToggleEl = null;
/** @type {HTMLSelectElement|null} the project switcher. */
let switcherEl = null;

/* ============================================================================
 * Shell build
 * ==========================================================================*/

/**
 * Build a single sidebar nav button. The route name is stored as a data
 * attribute so `renderRoute()` can highlight the active item.
 * @param {NavItem} item
 * @returns {HTMLElement}
 */
function buildNavItem(item) {
  return el("button", {
    class: "nav-item",
    type: "button",
    title: item.label,
    dataset: { route: item.name },
    onclick: () => navigate(item.hash),
  },
    icon(item.icon, 18),
    el("span", { class: "nav-label" }, item.label),
  );
}

/**
 * Build the sidebar: primary section, items, and a pinned system foot.
 * @returns {HTMLElement}
 */
function buildSidebar() {
  const foot = el("div", { class: "sidebar-foot" },
    el("div", { class: "nav-section" }, "System"),
    ...NAV_FOOT.map(buildNavItem),
  );
  return el("nav", { class: "sidebar", "aria-label": "Primary navigation" },
    el("div", { class: "nav-section" }, "Studio"),
    ...NAV_PRIMARY.map(buildNavItem),
    foot,
  );
}

/**
 * Build the top bar: nav toggle, brand, project switcher, and the live status
 * slots (connection, mode, model, GPU/VRAM, jobs).
 * @returns {HTMLElement}
 */
function buildTopbar() {
  return el("header", { class: "topbar" },
    (navToggleEl = el("button", {
      class: "btn btn-ghost btn-sm nav-toggle",
      type: "button",
      title: "Collapse navigation",
      "aria-label": "Toggle navigation",
      onclick: toggleNav,
    }, icon("menu", 16))),
    el("div", { class: "brand" },
      el("span", { class: "brand-mark" }, icon("logo", 22)),
      el("span", { class: "brand-name" }, "Local Video Studio"),
      el("span", { class: "brand-sub" }, "local-first production"),
    ),
    el("div", { class: "topbar-spacer" }),
    el("div", { class: "project-switcher" },
      (switcherEl = el("select", {
        disabled: true,
        "aria-label": "Project",
        onchange: onSwitcherChange,
      }, el("option", { value: "" }, "No project"))),
    ),
    (topbarStatusEl = el("div", { class: "topbar-status", "aria-label": "Status" },
      el("span", { "data-slot": "connection" }),
      el("span", { "data-slot": "mode" }),
      el("span", { "data-slot": "model" }),
      el("span", { "data-slot": "gpu" }),
      el("span", { "data-slot": "jobs" }),
    )),
  );
}

/**
 * Build the whole shell into `#app`.
 */
function renderShell() {
  const app = document.getElementById("app");
  if (!app) return;
  app.setAttribute("aria-busy", "false");
  app.replaceChildren(
    buildTopbar(),
    (bodyEl = el("div", { class: "body" },
      buildSidebar(),
      (contentEl = el("main", { class: "content" })),
    )),
  );
}

/* ============================================================================
 * Navigation (persisted collapse)
 * ==========================================================================*/

/** Keep the toggle's title in sync with the current nav state. */
function syncToggleTitle() {
  if (navToggleEl) navToggleEl.title = state.navCollapsed ? "Expand navigation" : "Collapse navigation";
}

function toggleNav() {
  state.navCollapsed = bodyEl.classList.toggle("nav-collapsed");
  syncToggleTitle();
  try { localStorage.setItem(NAV_COLLAPSED_KEY, state.navCollapsed ? "1" : "0"); } catch { /* storage unavailable */ }
}

function restoreNav() {
  try {
    if (localStorage.getItem(NAV_COLLAPSED_KEY) === "1") {
      bodyEl.classList.add("nav-collapsed");
      state.navCollapsed = true;
    }
  } catch { /* storage unavailable */ }
  syncToggleTitle();
}

/* ============================================================================
 * Top bar status widgets
 * ==========================================================================*/

/**
 * @param {string} name - data-slot id
 * @returns {HTMLElement}
 */
function slot(name) {
  return topbarStatusEl.querySelector(`[data-slot="${name}"]`);
}

/**
 * Connection + pipeline mode badges.
 * @param {"connecting"|"online"|"offline"} conn
 * @param {string} [mode] - "mock" | "local" (online only)
 */
function renderConnection(conn, mode) {
  slot("connection").replaceChildren(
    conn === "online" ? badge("good", "Connected")
    : conn === "connecting" ? badge("offline", "Connecting...")
    : badge("offline", "Backend offline"),
  );
  fill(slot("mode"),
    conn === "online" && mode === "mock" ? badge("warning", "Mock pipeline")
    : conn === "online" ? badge("neutral", "Local")
    : null,
  );
}

/**
 * Active model + compact GPU/VRAM.
 * @param {import("./api.js").SystemStatus} sys
 */
function renderSystem(sys) {
  fill(slot("model"),
    sys.active_model
      ? el("span", { class: "small muted", title: "Active generation backend" }, `Model: ${sys.active_model}`)
      : null,
  );
  const gpu = sys.gpu || /** @type {any} */ ({});
  if (gpu.error) {
    slot("gpu").replaceChildren(el("span", {
      class: "small",
      style: { color: "var(--critical)" },
      title: gpu.error.message || "GPU inspection failed",
    }, "GPU unavailable"));
    return;
  }
  const device = (gpu.devices || /** @type {any[]} */ ([]))[0];
  if (!device) {
    slot("gpu").replaceChildren(el("span", { class: "small muted" }, "No GPU"));
    return;
  }
  const minFree = Number(gpu.minimum_free_vram_gb) || 0;
  const tight = device.free_gb < minFree;
  slot("gpu").replaceChildren(el("span", {
    class: "small",
    style: { color: tight ? "var(--warning)" : "var(--text-2)" },
    title: `${device.used_gb.toFixed(1)} of ${device.total_gb.toFixed(1)} GiB used - heavy jobs need ${minFree.toFixed(0)} GiB free`,
  }, `${device.name} - ${fmtGb(device.free_gb)} free`));
}

/**
 * Queued/active job count from the live feed.
 */
function renderJobCount() {
  const queued = countQueuedJobs(state.jobs);
  const active = countActiveJobs(state.jobs);
  slot("jobs").replaceChildren(el("span", {
    class: "badge " + (queued || active ? "badge-accent" : "badge-neutral"),
    title: `Job feed: ${state.feedStatus || "starting..."}`,
  }, `${queued} queued` + (active ? ` - ${active} active` : "")));
}

/* ============================================================================
 * Project switcher
 * ==========================================================================*/

function renderSwitcher() {
  if (!switcherEl) return;
  const select = switcherEl;
  select.replaceChildren(el("option", { value: "" }, "No project"));
  for (const p of state.projects) {
    select.append(el("option", { value: p.id }, `${p.title} (${p.slug})`));
  }
  select.disabled = state.connection !== "online";
  select.value = state.currentProjectId && state.projects.some((p) => p.id === state.currentProjectId)
    ? state.currentProjectId
    : "";
}

function onSwitcherChange(ev) {
  const id = /** @type {HTMLSelectElement} */ (ev.target).value || null;
  state.currentProjectId = id;
  persistCurrentProject(id);
  const p = state.projects.find((x) => x.id === id);
  toast("info", id ? "Project selected" : "Project cleared", p ? p.title : undefined);
  // Re-render the current screen so any project-bound form (e.g. Project
  // Details) is rebuilt for the newly selected project instead of saving to the
  // wrong id if the old form is still mounted.
  onRoute();
}

/* ============================================================================
 * Data boot
 * ==========================================================================*/

/**
 * Health check with honest periodic re-check (every 10s): while offline the
 * top bar recovers when the backend comes up, and an outage while "online"
 * flips the badge back to offline instead of leaving a stale "Connected".
 * @param {boolean} [initial]
 */
async function bootHealth(initial = true) {
  if (initial) renderConnection("connecting");
  try {
    const h = await health(state.config);
    state.connection = "online";
    state.healthMode = h.mode;
    renderConnection("online", h.mode);
  } catch {
    state.connection = "offline";
    renderConnection("offline");
  }
  renderSwitcher();
  setTimeout(() => bootHealth(false), HEALTH_RETRY_MS);
}

/** Active model + GPU/VRAM (one fetch per boot; the Models screen refines this). */
async function bootSystem() {
  try {
    const sys = await systemStatus(state.config);
    state.systemStatus = sys;
    renderSystem(sys);
  } catch {
    /* offline - the connection badge covers it */
  }
}

async function bootProjects() {
  try {
    const list = await listProjects(state.config);
    const incoming = Array.isArray(list.projects) ? list.projects : [];
    reconcileProjects(incoming);
    if (Array.isArray(list.recovery) && list.recovery.length) {
      const counts = list.recovery.reduce((acc, r) => {
        acc[r.type] = (acc[r.type] || 0) + 1;
        return acc;
      }, {});
      const summary = Object.entries(counts).map(([t, n]) => `${n} ${t}`).join(", ");
      toast("warning", "Project recovery", `Reconciled on-disk state: ${summary}. No files were deleted.`);
    }
  } catch {
    // Keep the existing in-memory list (e.g. a just-created project) rather than
    // clearing it because a background boot fetch failed.
  }
  renderSwitcher();
  // A mounted screen whose rows resolve project titles (Job Monitor) re-renders
  // only on job-feed frames; without a change to the job set no further frame
  // arrives, so poke the live hook to pick up the freshly loaded projects.
  if (liveUpdate) liveUpdate();
}

/* ============================================================================
 * Job completion notifications
 * ==========================================================================*/

const TERMINAL_JOB_STATUSES = new Set(["completed", "failed", "canceled"]);

/**
 * job id → last-seen status, diffed across feed frames so each job toasts
 * exactly once when it reaches a terminal state. The first frame only seeds
 * the map (every prev is undefined), so jobs that were already finished
 * before the page loaded never re-alert.
 * @type {Map<string, string>}
 */
const lastJobStatus = new Map();

/**
 * Toast for a job that just transitioned into a terminal status.
 * @param {import("./api.js").GenerationJob} job
 */
function announceTerminalJob(job) {
  const label = STAGE_LABELS[job.stage] || job.stage;
  const where = [job.scene_id ? `scene ${shortId(job.scene_id)}` : null]
    .filter(Boolean)
    .join(" · ") || label;
  if (job.status === "completed") toast("good", `${label} completed`, where);
  else if (job.status === "failed") toast("critical", `${label} failed`, job.error ? `${where} — ${job.error}` : where);
  else toast("warning", `${label} canceled`, where);
}

/**
 * Compare this feed frame with the previous one and announce jobs that just
 * reached a terminal status. Jobs leaving the feed are pruned from the map.
 * @param {import("./api.js").GenerationJob[]} jobs
 */
function notifyJobTransitions(jobs) {
  const seen = new Set();
  for (const job of jobs) {
    seen.add(job.id);
    const prev = lastJobStatus.get(job.id);
    const now = job.status;
    if (prev !== undefined && prev !== now
      && TERMINAL_JOB_STATUSES.has(now) && !TERMINAL_JOB_STATUSES.has(prev)) {
      // Child rows (e.g. one scene_visual per batch scene) are announced by
      // their parent job; per-row toasts would flood the toast region.
      if (!(job.parameters && job.parameters.parent_job_id)) announceTerminalJob(job);
    }
    lastJobStatus.set(job.id, now);
  }
  for (const id of [...lastJobStatus.keys()]) {
    if (!seen.has(id)) lastJobStatus.delete(id);
  }
}

function startJobFeed() {
  const feed = createJobFeed({
    config: state.config,
    onJobs: (jobs) => { state.jobs = jobs; notifyJobTransitions(jobs); renderJobCount(); if (liveUpdate) liveUpdate(); },
    onStatus: (st) => { state.feedStatus = st; renderJobCount(); if (liveUpdate) liveUpdate(); },
  });
  state.feed = feed;
  renderJobCount();
}

/* ============================================================================
 * Routing
 * ==========================================================================*/

/**
 * Route name → screen renderer. Each renderer returns a full `.screen`
 * element and is responsible for its own loading/error/empty states. Every
 * route in router.js has a registered renderer (enforced by
 * frontend/tests/static_checks.py).
 */
const SCREENS = {
  dashboard: renderDashboard,
  "new-project": renderNewProject,
  project: renderProject,
  script: renderScript,
  storyboard: renderStoryboard,
  thumbnails: renderThumbnails,
  "scene-editor": renderSceneEditor,
  voice: renderVoice,
  music: renderMusic,
  captions: renderCaptions,
  editorial: renderEditorial,
  timeline: renderTimeline,
  export: renderExport,
  settings: renderSettings,
  models: renderModels,
  jobs: renderJobs,
};

/**
 * Render the content area for the current route and highlight the active nav
 * item.
 */
function renderRoute() {
  const route = parseRoute();
  document.querySelectorAll(".nav-item").forEach((btn) => {
    if (btn.dataset.route === route.name) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
  if (!contentEl) return;
  registerLiveUpdate(null);
  const renderer = SCREENS[route.name];
  contentEl.replaceChildren(renderer(route));
  // Keep the top-bar project switcher in sync with the current selection and
  // the (possibly just-created) project list on every navigation.
  renderSwitcher();
}

/**
 * Bootstrap: resolve config, restore the project selection, build the shell,
 * start routing, and fetch the live top bar data.
 */
async function init() {
  state.config = await loadConfig();
  restoreCurrentProject();
  renderShell();
  restoreNav();
  renderRoute();
  onHashChange(renderRoute);
  startJobFeed();
  bootHealth();
  bootSystem();
  bootProjects();
}

init();
