/**
 * Shared application state with a tiny subscription mechanism.
 *
 * No state-management library: pages read `state` and subscribe to changes
 * via `subscribe`. The only persisted value is the current project id
 * (sessionStorage) — never anything sensitive.
 */

import { el, fill } from "./dom.js";

/**
 * @typedef {"connecting"|"online"|"offline"} ConnectionState
 */

/**
 * @typedef {object} AppState
 * @property {import("./config.js").LvsConfig | null} config
 * @property {ConnectionState} connection
 * @property {"mock"|"local" | null} healthMode - last successful /health mode
 * @property {{stop: () => void} | null} feed - job-feed controller handle
 * @property {import("./events.js").FeedStatus | null} feedStatus
 * @property {import("./api.js").SystemStatus | null} systemStatus
 * @property {import("./api.js").Project[]} projects
 * @property {string | null} currentProjectId
 * @property {import("./api.js").ProjectSnapshot | null} snapshot
 * @property {import("./api.js").GenerationJob[]} jobs
 * @property {import("./api.js").ModelList | null} models
 * @property {import("./api.js").LlmModels | null} llmModels
 * @property {boolean} navCollapsed
 */

/** @type {AppState} */
export const state = {
  config: null,
  connection: "connecting",
  healthMode: null,
  feed: null,
  feedStatus: null,
  systemStatus: null,
  projects: [],
  currentProjectId: null,
  snapshot: null,
  jobs: [],
  models: null,
  llmModels: null,
  navCollapsed: false,
};

const STORAGE_KEY = "lvs-current-project";

/** Restore persisted project selection (safe: an opaque id). */
export function restoreCurrentProject() {
  try {
    const id = sessionStorage.getItem(STORAGE_KEY);
    if (id) state.currentProjectId = id;
  } catch {
    /* storage unavailable */
  }
}

/**
 * Persist project selection.
 * @param {string | null} id
 */
export function persistCurrentProject(id) {
  try {
    if (id) sessionStorage.setItem(STORAGE_KEY, id);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
}

const listeners = new Set();

/**
 * Update state and notify subscribers.
 * @param {Partial<AppState>} patch
 */
export function setState(patch) {
  Object.assign(state, patch);
  for (const fn of [...listeners]) fn(state);
}

/**
 * Subscribe to state changes.
 * @param {(s: AppState) => void} fn
 * @returns {() => void} unsubscribe
 */
export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * Derive the active (non-terminal) job count.
 * @param {import("./api.js").GenerationJob[]} jobs
 * @returns {number}
 */
export function countActiveJobs(jobs) {
  const terminal = new Set(["completed", "failed", "canceled"]);
  return jobs.reduce((n, j) => (terminal.has(j.status) ? n : n + 1), 0);
}

/**
 * Derive the queued job count.
 * @param {import("./api.js").GenerationJob[]} jobs
 * @returns {number}
 */
export function countQueuedJobs(jobs) {
  return jobs.reduce((n, j) => (j.status === "queued" ? n + 1 : n), 0);
}

/**
 * Find the newest asset with `settings.role === role` for a scene.
 * @param {import("./api.js").Asset[]} assets
 * @param {string} sceneId
 * @param {string} role
 * @returns {import("./api.js").Asset | null}
 */
export function latestAssetForScene(assets, sceneId, role) {
  const found = assets
    .filter((a) => a.scene_id === sceneId
      && (a.settings || {}).role === role
      && a.current !== false)
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  return found[0] || null;
}

/**
 * Project-scoped empty-state panel.
 * @param {string} message
 * @returns {HTMLElement}
 */
export function needsProject(message) {
  return el("div", { class: "empty-state", role: "status" },
    el("div", { class: "empty-title" }, "No project selected"),
    el("div", {}, message),
  );
}

/**
 * Insert or replace a project in the in-memory list by id. Used right after
 * creation so the new project is immediately visible and selectable without
 * waiting for a list round-trip.
 * @param {import("./api.js").Project} project
 */
export function upsertProject(project) {
  const others = state.projects.filter((p) => p.id !== project.id);
  setState({ projects: [...others, project] });
}

/**
 * Merge an incoming project list with the locally known projects by id.
 *
 * A stale boot fetch (initiated before a project was created) must not remove a
 * just-created selection, so entries that are known locally but absent from the
 * incoming list are preserved. Incoming entries still override matching ids.
 * @param {import("./api.js").Project[]} incoming
 */
export function reconcileProjects(incoming) {
  const known = new Map(state.projects.map((p) => [p.id, p]));
  for (const p of incoming) known.set(p.id, p);
  setState({ projects: [...known.values()] });
}
