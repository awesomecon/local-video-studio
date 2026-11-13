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

import { el, fill, fmtDuration, fmtGb, shortId } from "./dom.js";
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
import { initAlertSound } from "./alert-sound.js";
import { health, systemStatus, listProjects } from "./api.js";
import { effectiveVideoMode } from "./video-mode.js";
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
import { renderSystemPage } from "./pages/system.js";
import { renderJobs } from "./pages/jobs.js";
import { renderNotFound } from "./pages/not-found.js";

/** @typedef {{name: string, hash: string, label: string, icon: string}} NavItem */

const NAV_WORKSPACE = [
  { name: "dashboard", hash: "#/", label: "Studio", icon: "dashboard" },
  { name: "projects", hash: "#/projects", label: "All projects", icon: "folder" },
  { name: "jobs", hash: "#/jobs", label: "Job monitor", icon: "film" },
];
const NAV_START = [
  { name: "project", hash: "#/project", label: "Project brief", icon: "folder" },
];
const NAV_MAKE = [
  { name: "script", hash: "#/script", label: "Script", icon: "script" },
  { name: "storyboard", hash: "#/storyboard", label: "Storyboard", icon: "storyboard" },
  { name: "voice", hash: "#/voice", label: "Voice & narration", icon: "mic" },
  { name: "music", hash: "#/music", label: "Music & sound", icon: "music" },
  { name: "captions", hash: "#/captions", label: "Captions", icon: "captions" },
];
const NAV_FINISH = [
  { name: "editorial", hash: "#/editorial", label: "Editorial", icon: "canvas" },
  { name: "timeline", hash: "#/timeline", label: "Timeline", icon: "timeline" },
  { name: "thumbnails", hash: "#/thumbnails", label: "Thumbnails", icon: "thumbnail" },
  { name: "export", hash: "#/export", label: "Export", icon: "export" },
];

/** System navigation, pinned to the sidebar foot. */
const NAV_FOOT = /** @type {NavItem[]} */ ([
  { name: "models", hash: "#/models", label: "Models", icon: "cpu" },
  { name: "system", hash: "#/system", label: "System status", icon: "system" },
  { name: "settings", hash: "#/settings", label: "Settings", icon: "settings" },
]);

const NAV_COLLAPSED_KEY = "lvs-nav-collapsed";
const THEME_KEY = "lvs-theme";
const HEALTH_RETRY_MS = 10000;
const narrowLayout = matchMedia("(max-width: 960px)");

/** @type {HTMLElement|null} the `.content` main element. */
let contentEl = null;
/** @type {HTMLElement|null} the Storyboard nav button (mode-aware visibility). */
let storyboardNavEl = null;
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
let sidebarEl = null;
let breadcrumbEl = null;
let themeToggleEl = null;
let navScrimEl = null;
let drawerOpen = false;

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
 * The Storyboard button is kept by reference so the mode-aware sync can hide
 * it for Editorial projects without rebuilding the shell.
 * @returns {HTMLElement}
 */
function buildSidebar() {
  const make = NAV_MAKE.map(buildNavItem);
  storyboardNavEl = make.find((btn) => btn.dataset.route === "storyboard") || null;
  return el("nav", { class: "sidebar", id: "studio-navigation", "aria-label": "Studio navigation" },
    el("a", { class: "brand", href: "#/", "aria-label": "Local Video Studio home" },
      el("span", { class: "brand-mark" }, icon("logo", 22)),
      el("span", { class: "brand-name" }, "local", el("small", {}, "VIDEO STUDIO")),
    ),
    el("div", { class: "nav-section" }, "Workspace"),
    ...NAV_WORKSPACE.map(buildNavItem),
    el("div", { class: "project-nav" },
      el("div", { class: "nav-section" }, "Current project"),
      el("div", { class: "project-switcher" },
        (switcherEl = el("select", { disabled: true, "aria-label": "Current project", onchange: onSwitcherChange },
          el("option", { value: "" }, "No project"))),
      ),
      el("div", { class: "nav-section" }, "Start"),
      ...NAV_START.map(buildNavItem),
      el("div", { class: "nav-section" }, "Make"),
      ...make,
      el("div", { class: "nav-section" }, "Finish"),
      ...NAV_FINISH.map(buildNavItem),
    ),
    el("div", { class: "sidebar-foot" },
      el("div", { class: "nav-section" }, "Studio tools"),
      ...NAV_FOOT.map(buildNavItem),
    ),
  );
}

/**
 * Keep the mode-aware sidebar entries in sync with the selected project:
 * Editorial projects hide the Classic Storyboard nav item (its route still
 * renders a safe pointer to #/editorial); Classic, legacy, and no-project
 * selections show it. Timeline stays visible in both modes. Runs after the
 * project list reconciles, after project-switcher changes, and on every
 * route render, so the sidebar never shows the previous project's mode.
 */
export function syncModeAwareNavigation() {
  if (!storyboardNavEl) return;
  const project = state.projects.find((p) => p.id === state.currentProjectId) || null;
  storyboardNavEl.hidden = effectiveVideoMode(project) === "editorial";
}

export function syncProjectChrome() {
  renderSwitcher();
  syncModeAwareNavigation();
  const route = parseRoute().name;
  renderBreadcrumb(route);
  renderProjectContext(route);
}

/**
 * Build the top bar with navigation, context, theme and live status.
 * @returns {HTMLElement}
 */
function buildTopbar() {
  return el("header", { class: "topbar" },
    (navToggleEl = el("button", {
      class: "btn btn-ghost btn-sm nav-toggle",
      type: "button",
      "aria-controls": "studio-navigation",
      onclick: toggleNav,
    }, icon("menu", 16))),
    (breadcrumbEl = el("div", { class: "breadcrumb" })),
    el("div", { class: "topbar-spacer" }),
    (topbarStatusEl = el("div", { class: "topbar-status", "aria-label": "Status" },
      el("span", { "data-slot": "connection" }),
      el("span", { "data-slot": "mode" }),
      el("span", { "data-slot": "model" }),
      el("span", { "data-slot": "gpu" }),
      el("span", { "data-slot": "jobs" }),
    )),
    (themeToggleEl = el("button", {
      class: "btn btn-ghost btn-sm theme-toggle", type: "button",
      onclick: () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true),
    })),
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
    (bodyEl = el("div", { class: "body" },
      (sidebarEl = buildSidebar()),
      el("div", { class: "studio-body" },
        buildTopbar(),
        (contentEl = el("main", { class: "content", id: "studio-content", tabindex: -1 })),
      ),
      (navScrimEl = el("div", { class: "nav-scrim", hidden: true, "aria-hidden": "true", onclick: () => closeDrawer(true) })),
    )),
  );
}

/* ============================================================================
 * Navigation (persisted collapse)
 * ==========================================================================*/

function syncNavigation() {
  const narrow = narrowLayout.matches;
  bodyEl.classList.toggle("drawer-open", narrow && drawerOpen);
  sidebarEl.inert = narrow && !drawerOpen;
  contentEl.inert = narrow && drawerOpen;
  navScrimEl.hidden = !narrow || !drawerOpen;
  const expanded = narrow ? drawerOpen : !state.navCollapsed;
  const label = expanded ? "Close navigation" : "Open navigation";
  navToggleEl.title = label;
  navToggleEl.setAttribute("aria-label", label);
  navToggleEl.setAttribute("aria-expanded", String(expanded));
}

function closeDrawer(restoreFocus = false) {
  if (!drawerOpen) return;
  drawerOpen = false;
  syncNavigation();
  if (restoreFocus) navToggleEl.focus();
}

function toggleNav() {
  if (narrowLayout.matches) {
    if (drawerOpen) closeDrawer(true);
    else {
      drawerOpen = true;
      syncNavigation();
      (sidebarEl.querySelector('[aria-current="page"]') || sidebarEl.querySelector(".brand")).focus();
    }
    return;
  }
  state.navCollapsed = bodyEl.classList.toggle("nav-collapsed");
  syncNavigation();
  try { localStorage.setItem(NAV_COLLAPSED_KEY, state.navCollapsed ? "1" : "0"); } catch { /* storage unavailable */ }
}

function restoreNav() {
  try {
    if (localStorage.getItem(NAV_COLLAPSED_KEY) === "1") {
      bodyEl.classList.add("nav-collapsed");
      state.navCollapsed = true;
    }
  } catch { /* storage unavailable */ }
  syncNavigation();
  narrowLayout.addEventListener("change", () => {
    const restoreFocus = narrowLayout.matches && sidebarEl.contains(document.activeElement);
    drawerOpen = false;
    syncNavigation();
    if (restoreFocus) navToggleEl.focus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drawerOpen) closeDrawer(true);
  });
}

function setTheme(theme, persist = false) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const label = dark ? "Switch to light theme" : "Switch to dark theme";
  themeToggleEl.title = label;
  themeToggleEl.setAttribute("aria-label", label);
  themeToggleEl.replaceChildren(icon(dark ? "sun" : "moon", 18));
  if (persist) {
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* storage unavailable */ }
  }
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
      class: "small critical",
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
    class: `small${tight ? " warning" : " muted"}`,
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
  const options = [["", "No project"], ...state.projects.map((project) => [project.id, project.title])];
  const unchanged = select.options.length === options.length
    && options.every(([value, label], index) => select.options[index].value === value && select.options[index].text === label);
  if (!unchanged) {
    select.replaceChildren(...options.map(([value, label]) => el("option", { value }, label)));
  }
  select.disabled = state.connection !== "online" || state.projects.length === 0;
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
  const route = parseRoute().name;
  if (route === "scene-editor") navigate(effectiveVideoMode(p) === "editorial" ? "#/editorial" : "#/storyboard");
  else onRoute();
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

/**
 * localStorage key for the recovery-set fingerprint (see
 * recoveryFingerprint). Non-sensitive: a sorted list of backend-provided
 * recovery entries, used only to avoid re-toasting a persistent conflict on
 * every boot.
 */
const RECOVERY_SEEN_KEY = "lvs-recovery-seen";

/**
 * Stable fingerprint of a recovery set: it changes exactly when the set of
 * entries (type, slug, project, detail) changes.
 * @param {Array<{type: string, slug?: string, project_id?: string, detail: string}>} recovery
 * @returns {string}
 */
function recoveryFingerprint(recovery) {
  return JSON.stringify(
    recovery
      .map((r) => [r.type || "", r.slug || "", r.project_id || "", r.detail || ""])
      .sort(),
  );
}

/**
 * Surface the backend's on-disk recovery report once per *change*, not once
 * per boot: the same persistent conflict (e.g. a directory whose name does
 * not match its project slug) is returned by GET /api/projects on every
 * startup, so a boot-time-only toast is noise. The fingerprint is persisted
 * in localStorage; when storage is unavailable the toast falls back to
 * showing every time (honest over silent). The message names each entry
 * (slug + human detail) and offers a View button to the System status
 * screen, which renders the same entries persistently.
 * @param {Array<{type: string, slug?: string, project_id?: string, detail: string}>} recovery
 */
function noteRecoveryChange(recovery) {
  const current = recoveryFingerprint(recovery);
  let previous = null;
  try { previous = localStorage.getItem(RECOVERY_SEEN_KEY); } catch { /* storage unavailable */ }
  if (previous === current) return;
  try { localStorage.setItem(RECOVERY_SEEN_KEY, current); } catch { /* storage unavailable */ }
  if (!recovery.length) {
    if (previous !== null) {
      toast("good", "Project recovery", "The previously reported recovery issues no longer appear on startup.");
    }
    return;
  }
  const shown = recovery.slice(0, 3).map((r) => {
    const detail = (r.detail || "no detail").replace(/[.!?\s]+$/, "");
    return `${r.slug || r.type}: ${detail}.`;
  });
  if (recovery.length > 3) shown.push(`…and ${recovery.length - 3} more`);
  toast("warning", "Project recovery",
    `${shown.join(" — ")} No files were deleted.`,
    { label: "View", onClick: () => navigate("#/system") },
  );
}

async function bootProjects() {
  try {
    const list = await listProjects(state.config);
    const incoming = Array.isArray(list.projects) ? list.projects : [];
    reconcileProjects(incoming);
    noteRecoveryChange(Array.isArray(list.recovery) ? list.recovery : []);
  } catch {
    // Keep the existing in-memory list (e.g. a just-created project) rather than
    // clearing it because a background boot fetch failed.
  }
  syncProjectChrome();
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
  projects: renderDashboard,
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
  system: renderSystemPage,
  jobs: renderJobs,
  "not-found": renderNotFound,
};

function renderBreadcrumb(route) {
  const projectTools = [...NAV_START, ...NAV_MAKE, ...NAV_FINISH];
  const tool = projectTools.find((item) => item.name === route);
  const project = state.projects.find((item) => item.id === state.currentProjectId);
  const label = tool?.label || [...NAV_WORKSPACE, ...NAV_FOOT].find((item) => item.name === route)?.label
    || ({ "new-project": "New project", "scene-editor": "Scene editor" })[route];
  const showProject = project && (tool || route === "scene-editor");
  breadcrumbEl.textContent = ["Workspace", showProject ? project.title : null, label].filter(Boolean).join(" / ");
}

function renderProjectContext(route) {
  const screen = contentEl.firstElementChild;
  const head = screen?.querySelector(".screen-head");
  if (!head) return;
  screen.querySelector(".project-context")?.remove();
  const projectRoute = [...NAV_START, ...NAV_MAKE, ...NAV_FINISH].some((item) => item.name === route)
    || route === "scene-editor";
  const project = projectRoute && state.projects.find((item) => item.id === state.currentProjectId);
  if (!project) return;
  const editorial = effectiveVideoMode(project) === "editorial";
  head.after(el("div", { class: "project-context" },
    route !== "project" ? el("a", { href: "#/project" }, project.title) : el("strong", {}, project.title),
    el("span", {}, `${editorial ? "Editorial" : "Classic"} · ${project.aspect_ratio} · ${fmtDuration(project.target_duration)} target`),
    route === "project" ? el("a", { href: editorial ? "#/editorial" : "#/storyboard" },
      editorial ? "Open Editorial" : "Open Storyboard") : null,
  ));
}

/**
 * Render the content area for the current route and highlight the active nav
 * item.
 */
function renderRoute() {
  const route = parseRoute();
  const activeRoute = route.name === "scene-editor" ? "storyboard" : route.name;
  document.querySelectorAll(".nav-item").forEach((btn) => {
    if (btn.dataset.route === activeRoute) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
  if (!contentEl) return;
  registerLiveUpdate(null);
  const renderer = SCREENS[route.name];
  contentEl.replaceChildren(renderer(route));
  contentEl.scrollTop = 0;
  if (drawerOpen) {
    closeDrawer();
    contentEl.focus({ preventScroll: true });
  }
  syncProjectChrome();
}

/**
 * Bootstrap: resolve config, restore the project selection, build the shell,
 * start routing, and fetch the live top bar data.
 */
async function init() {
  state.config = await loadConfig();
  restoreCurrentProject();
  renderShell();
  let theme = "light";
  try { theme = localStorage.getItem(THEME_KEY) || theme; } catch { /* storage unavailable */ }
  setTheme(theme);
  restoreNav();
  initAlertSound();
  renderRoute();
  onHashChange(renderRoute);
  startJobFeed();
  bootHealth();
  bootSystem();
  bootProjects();
}

init();
