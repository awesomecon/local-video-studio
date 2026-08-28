/**
 * Dashboard screen: real project list plus a live overview of the selected
 * project. All data is fetched from the backend — nothing is invented; the
 * offline/error states are honest panels with retry.
 */

import { el, fmtDate, fmtDuration } from "../dom.js";
import {
  state,
  setState,
  persistCurrentProject,
  reconcileProjects,
  countActiveJobs,
  countQueuedJobs,
} from "../state.js";
import { listProjects, getProject, deleteProject } from "../api.js";
import {
  projectStatusBadge,
  loadingState,
  emptyState,
  errorPanel,
  confirm,
  toast,
  toastError,
  stageChip,
} from "../ui.js";
import { navigate } from "../router.js";
import { registerLiveUpdate } from "../app.js";

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderDashboard(_route) {
  const list = renderProjectList();
  const overview = renderSelectedOverview();
  // Live path: project status, job counts, and stage chips change as jobs
  // run, so refresh both regions (no skeleton flash) on every feed update.
  registerLiveUpdate(() => {
    list.refresh();
    if (overview) overview.refresh();
  });
  return el("div", { class: "screen" },
    el("div", { class: "screen-head" },
      el("h1", {}, "Dashboard"),
      el("div", { class: "screen-actions" },
        el("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("#/new") }, "New Project"),
      ),
    ),
    list.panel,
    overview ? overview.panel : null,
  );
}

/* ============================================================================
 * Project list
 * ==========================================================================*/

/**
 * @returns {{panel: HTMLElement, refresh: () => void}}
 */
function renderProjectList() {
  const body = el("div", { class: "panel-body" });
  const panel = el("section", { class: "panel" },
    el("div", { class: "panel-title" }, "Projects"),
    body,
  );
  // Last-write-wins sequence, same pattern as timeline.js.
  let inflight = 0;
  /** True once real content (or the empty state) has rendered; see script.js. */
  let hasContent = false;
  load(body);
  return { panel, refresh: () => load(body, { skeleton: false }) };

  /**
   * @param {HTMLElement} region
   * @param {{skeleton?: boolean}} [opts] — omit the skeleton on live refreshes
   */
  async function load(region, { skeleton = true } = {}) {
    const token = ++inflight;
    if (skeleton) region.replaceChildren(loadingState(3));
    try {
      const list = await listProjects(state.config);
      if (token !== inflight) return;
      reconcileProjects(Array.isArray(list.projects) ? list.projects : []);
      region.replaceChildren(state.projects.length ? buildList() : emptyState(
        "No projects yet",
        "Create your first project to start producing.",
        [el("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("#/new") }, "New Project")],
      ));
      hasContent = true;
    } catch (err) {
      // Keep previously rendered content; only surface the error when there
      // is nothing to show yet.
      if (token !== inflight || hasContent) return;
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load(region) }, "Retry"),
      ));
    }
  }

  function buildList() {
    return el("div", { class: "stack" },
      state.projects.map((p) => {
        const selected = p.id === state.currentProjectId;
        return el("div", { class: "row project-row" },
          el("div", { class: "grow" },
            el("div", { style: { fontWeight: "650" } }, p.title),
            el("div", { class: "muted small mono" }, p.slug),
          ),
          projectStatusBadge(p.status),
          el("div", { class: "muted small nowrap" }, `${fmtDuration(p.target_duration)} · ${p.aspect_ratio}`),
          el("div", { class: "muted small nowrap" }, fmtDate(p.created_at)),
          el("button", {
            class: "btn btn-sm" + (selected ? "" : " btn-primary"),
            type: "button",
            onclick: () => openProject(p.id),
          }, selected ? "Selected" : "Open"),
          el("button", {
            class: "btn btn-sm btn-danger",
            type: "button",
            "aria-label": `Delete ${p.title}`,
            onclick: () => removeProject(p),
          }, "Delete"),
        );
      }),
    );
  }

  /**
   * Confirm and permanently delete a project (files + index rows).
   * @param {import("../api.js").Project} p
   */
  async function removeProject(p) {
    const ok = await confirm({
      title: "Delete project?",
      message: `"${p.title}" (${p.slug}) will be permanently removed, including every scene, render, and generated file in its project directory. This cannot be undone.`,
      confirmLabel: "Delete permanently",
      kind: "danger",
    });
    if (!ok) return;
    try {
      await deleteProject(state.config, p.id);
      // Remove locally before refreshing: the list merge keeps entries known
      // locally but absent from the incoming list, so the deleted project
      // must be dropped from state first or it would be resurrected.
      const wasSelected = state.currentProjectId === p.id;
      setState({
        projects: state.projects.filter((item) => item.id !== p.id),
        ...(wasSelected ? { currentProjectId: null, snapshot: null } : {}),
      });
      if (wasSelected) persistCurrentProject(null);
      toast("good", "Project deleted", p.title);
      load(body, { skeleton: false });
    } catch (err) {
      toastError(err, `Deleting "${p.title}" failed`);
    }
  }
}

function openProject(id) {
  state.currentProjectId = id;
  persistCurrentProject(id);
  navigate("#/project"); // open the Project Details screen for this selection
}

/* ============================================================================
 * Selected project overview
 * ==========================================================================*/

/**
 * @returns {{panel: HTMLElement, refresh: () => void}|null}
 */
function renderSelectedOverview() {
  if (!state.currentProjectId) return null;
  const body = el("div", { class: "panel-body" });
  /**
   * @param {HTMLElement} region
   * @param {{skeleton?: boolean}} [opts] — omit the skeleton on live refreshes
   */
  let inflight = 0; // last-write-wins sequence, same pattern as timeline.js
  /** True once real content has rendered; see the project list above. */
  let hasContent = false;
  async function load(region, { skeleton = true } = {}) {
    const token = ++inflight;
    if (skeleton) region.replaceChildren(loadingState(2));
    try {
      const snap = await getProject(state.config, state.currentProjectId);
      if (token !== inflight) return;
      region.replaceChildren(buildOverview(snap));
      hasContent = true;
    } catch (err) {
      // Keep previously rendered content; only surface the error when there
      // is nothing to show yet.
      if (token !== inflight || hasContent) return;
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load(region) }, "Retry"),
      ));
    }
  }
  const panel = el("section", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Selected project"),
      el("span", { class: "spacer" }),
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => load(body) }, "Refresh"),
    ),
    body,
  );
  load(body);
  return { panel, refresh: () => load(body, { skeleton: false }) };

  /** @param {import("../api.js").ProjectSnapshot} snap */
  function buildOverview(snap) {
    const p = snap.project;
    const jobs = snap.jobs || [];
    const active = countActiveJobs(jobs);
    const queued = countQueuedJobs(jobs);
    const failed = jobs.filter((j) => j.status === "failed").length;
    const stages = (snap.stage_state && /** @type {any} */ (snap.stage_state).stages) || {};

    return el("div", { class: "stack" },
      el("div", { class: "row" },
        el("span", { style: { fontWeight: "650" } }, p.title),
        projectStatusBadge(p.status),
        el("span", { class: "spacer" }),
        snap.directory ? el("span", { class: "muted small mono" }, snap.directory) : null,
      ),
      el("dl", { class: "kv" },
        el("dt", {}, "Scenes"), el("dd", {}, String((snap.scenes || []).length)),
        el("dt", {}, "Active jobs"), el("dd", {}, String(active)),
        el("dt", {}, "Queued jobs"), el("dd", {}, String(queued)),
        el("dt", {}, "Failed jobs"), el("dd", {}, String(failed)),
        el("dt", {}, "Style"), el("dd", {}, p.style),
        el("dt", {}, "Aspect ratio"), el("dd", {}, p.aspect_ratio),
        el("dt", {}, "Target duration"), el("dd", {}, fmtDuration(p.target_duration)),
      ),
      Object.keys(stages).length
        ? el("div", { class: "row" },
          ...Object.entries(stages).map(([name, st]) => stageChip(name, /** @type {any} */ (st))),
        )
        : el("div", { class: "muted small" }, "No pipeline stages have run yet — run planning from the Script screen."),
    );
  }
}
