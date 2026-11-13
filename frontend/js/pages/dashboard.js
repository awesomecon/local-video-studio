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
  latestAssetForScene,
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
  badge,
  icon,
  jobStatusBadge,
  progress,
  STAGE_LABELS,
} from "../ui.js";
import { navigate } from "../router.js";
import { registerLiveUpdate, syncProjectChrome } from "../app.js";
import { effectiveVideoMode } from "../video-mode.js";

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderDashboard(route) {
  const allProjects = route.name === "projects";
  const overview = allProjects ? null : renderSelectedOverview((projectId, cover) => list.updateCover(projectId, cover));
  const list = renderProjectList(overview?.refresh);
  const queue = allProjects ? null : el("aside", { class: "studio-aside", "aria-label": "Production activity" });
  if (queue) renderQueue(queue);
  registerLiveUpdate(() => {
    list.refresh();
    if (queue) renderQueue(queue);
  });
  return el("div", { class: "screen studio-screen" },
    el("div", { class: "screen-head studio-heading" },
      el("h1", {}, allProjects ? "All projects" : "Studio"),
      el("a", { class: "btn btn-primary", href: "#/new" }, icon("plus", 16), "New project"),
    ),
    el("div", { class: allProjects ? "studio-library" : "studio-columns" },
      el("div", { class: "studio-primary" },
        overview ? overview.panel : null,
        list.panel,
      ),
      queue,
    ),
  );
}

function renderQueue(queue) {
  const terminal = new Set(["completed", "failed", "canceled"]);
  const jobs = state.jobs.filter((job) => !terminal.has(job.status) && !job.parameters?.parent_job_id);
  queue.replaceChildren(
    el("div", { class: "studio-section-head" },
      el("h2", {}, "Production queue"),
      el("span", { class: "small muted" }, `${jobs.length} pending`),
    ),
    jobs.length ? el("ul", { class: "studio-queue" }, jobs.slice(0, 5).map((job) =>
      el("li", {},
        el("div", { class: "row studio-queue-title" },
          el("strong", {}, STAGE_LABELS[job.stage] || job.stage),
          jobStatusBadge(job.status),
        ),
        el("div", { class: "muted small" }, state.projects.find((p) => p.id === job.project_id)?.title || "Project job"),
        job.status === "queued" ? null : progress(job.progress),
      ),
    )) : el("p", { class: "muted small" }, "No production jobs are running or queued."),
    el("a", { class: "btn btn-ghost", href: "#/jobs" }, "Open job monitor", icon("chevron", 14)),
  );
}

/* ============================================================================
 * Project list
 * ==========================================================================*/

/**
 * @returns {{panel: HTMLElement, refresh: () => void, updateCover: (projectId: string, cover: import("../api.js").Asset|null) => void}}
 */
function renderProjectList(onProjectsChanged) {
  const body = el("div", { class: "panel-body" });
  const covers = new Map();
  let grid = false;
  const query = el("input", { class: "input", type: "search", placeholder: "Find a project", "aria-label": "Find a project" });
  const listBtn = el("button", { class: "btn btn-sm", type: "button", "aria-pressed": "true" }, "List");
  const gridBtn = el("button", { class: "btn btn-sm", type: "button", "aria-pressed": "false" }, "Grid");
  const panel = el("section", { class: "project-section" },
    el("div", { class: "studio-section-head project-toolbar" },
      el("h2", {}, "Your projects"),
      el("div", { class: "row project-tools" }, query, listBtn, gridBtn),
    ),
    body,
  );
  query.addEventListener("input", renderResults);
  listBtn.onclick = () => setLayout(false);
  gridBtn.onclick = () => setLayout(true);

  function setLayout(useGrid) {
    grid = useGrid;
    listBtn.setAttribute("aria-pressed", String(!grid));
    gridBtn.setAttribute("aria-pressed", String(grid));
    renderResults();
  }
  // Last-write-wins sequence, same pattern as timeline.js.
  let inflight = 0;
  /** True once real content (or the empty state) has rendered; see script.js. */
  let hasContent = false;
  load(body);
  return {
    panel,
    refresh: () => load(body, { skeleton: false }),
    updateCover(projectId, cover) {
      if (covers.get(projectId)?.url === cover?.url) return;
      if (cover) covers.set(projectId, cover);
      else covers.delete(projectId);
      const thumb = [...body.querySelectorAll(".project-thumb")]
        .find((item) => item.dataset.projectId === projectId);
      if (thumb) thumb.replaceChildren(coverImage(cover, 22));
    },
  };

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
      hasContent = true;
      renderResults();
      onProjectsChanged?.();
    } catch (err) {
      // Keep previously rendered content; only surface the error when there
      // is nothing to show yet.
      if (token !== inflight || hasContent) return;
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load(region) }, "Retry"),
      ));
    }
  }

  function renderResults() {
    if (!hasContent) return;
    const term = query.value.trim().toLowerCase();
    const projects = state.projects
      .filter((p) => `${p.title} ${p.topic || ""} ${p.slug}`.toLowerCase().includes(term))
      .sort((a, b) => String(b.updated_at || b.created_at).localeCompare(String(a.updated_at || a.created_at)));
    if (!projects.length) {
      body.replaceChildren(state.projects.length
        ? emptyState("No matching projects", "Try a different search.")
        : emptyState("No projects yet", "Create a project to start with a brief.",
          [el("a", { class: "btn btn-primary", href: "#/new" }, "New project")]));
      return;
    }
    body.replaceChildren(el("div", { class: grid ? "studio-projects project-grid" : "studio-projects" },
      projects.map((p) => {
        const selected = p.id === state.currentProjectId;
        return el("article", { class: "project-row" },
          el("div", { class: "project-thumb", dataset: { projectId: p.id }, "aria-hidden": "true" },
            coverImage(covers.get(p.id), 22)),
          el("div", { class: "project-info" },
            el("button", { class: "project-title", type: "button", onclick: () => openProject(p.id) }, p.title),
            el("div", { class: "muted small" }, `${fmtDuration(p.target_duration)} · ${p.aspect_ratio} · ${fmtDate(p.updated_at || p.created_at)}`),
          ),
          el("div", { class: "row project-status" }, projectStatusBadge(p.status), selected ? badge("accent", "Selected", false) : null),
          el("button", {
            class: "btn btn-sm btn-ghost project-delete",
            type: "button",
            "aria-label": `Delete ${p.title}`,
            onclick: () => removeProject(p),
            title: "Delete project",
          }, icon("trash", 16)),
        );
      }),
    ));
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
      covers.delete(p.id);
      if (wasSelected) setState({ currentProjectId: null });
      setState({ projects: state.projects.filter((item) => item.id !== p.id) });
      if (wasSelected) persistCurrentProject(null);
      syncProjectChrome();
      toast("good", "Project deleted", p.title);
      load(body, { skeleton: false });
    } catch (err) {
      toastError(err, `Deleting "${p.title}" failed`);
    }
  }
}

function openProject(id, hash = "#/project") {
  state.currentProjectId = id;
  persistCurrentProject(id);
  navigate(hash);
}

function coverImage(cover, iconSize) {
  if (!cover) return icon("film", iconSize);
  const image = el("img", { src: cover.url, alt: "" });
  image.onerror = () => image.replaceWith(icon("film", iconSize));
  return image;
}

/* ============================================================================
 * Selected project overview
 * ==========================================================================*/

/**
 * @returns {{panel: HTMLElement, refresh: () => void}|null}
 */
function renderSelectedOverview(onCover) {
  const body = el("div", {});
  /**
   * @param {HTMLElement} region
   * @param {{skeleton?: boolean}} [opts] — omit the skeleton on live refreshes
   */
  let inflight = 0; // last-write-wins sequence, same pattern as timeline.js
  /** True once real content has rendered; see the project list above. */
  let hasContent = false;
  async function load(region, { skeleton = false } = {}) {
    const token = ++inflight;
    const selected = state.projects.find((project) => project.id === state.currentProjectId);
    const latest = state.projects.reduce((best, project) =>
      !best || project.updated_at > best.updated_at ? project : best, null);
    const projectId = selected?.id || latest?.id;
    if (!projectId) {
      panel.hidden = true;
      region.replaceChildren();
      hasContent = false;
      return;
    }
    panel.hidden = false;
    if (skeleton) region.replaceChildren(loadingState(2));
    try {
      const snap = await getProject(state.config, projectId);
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
  const panel = el("section", { class: "continue-section", hidden: true },
    el("div", { class: "studio-section-head" },
      el("h2", {}, "Pick up where you left off"),
    ),
    body,
  );
  return { panel, refresh: () => load(body) };

  /** @param {import("../api.js").ProjectSnapshot} snap */
  function buildOverview(snap) {
    const p = snap.project;
    const scenes = snap.scenes || [];
    const assets = snap.assets || [];
    const stages = snap.stage_state?.stages || {};
    const approved = scenes.filter((scene) => ["approved", "locked"].includes(scene.status) && !scene.shot_summary?.stale).length;
    const editorial = effectiveVideoMode(p) === "editorial";
    const plan = snap.editorial;
    const hasPlan = plan?.has_edit_plan === true;
    const next = !scenes.length ? ["#/script", "Start with Script"]
      : editorial ? ["#/editorial", hasPlan ? "Open Edit Plan" : "Build Edit Plan"]
      : approved < scenes.length ? ["#/storyboard", "Review storyboard"]
      : stages.render_final?.status === "completed" ? ["#/export", "Open delivery"]
      : ["#/timeline", "Review the sequence"];
    const localImage = (asset) => asset?.url?.startsWith(`/api/projects/${encodeURIComponent(p.id)}/assets/`) && ["image", "thumbnail"].includes(asset.type);
    const cover = assets.find((asset) => asset.current !== false && asset.settings?.role === "thumbnail" && localImage(asset))
      || scenes.map((scene) => latestAssetForScene(assets, scene.id, "visual")).find(localImage);
    onCover(p.id, cover || null);
    const summary = editorial ? (hasPlan ? (plan?.stale ? "Edit Plan needs review" : "Edit Plan available") : "Edit Plan pending")
      : scenes.length ? `${approved} of ${scenes.length} scenes approved` : "No scenes planned";
    return el("div", {},
      el("article", { class: "welcome-feature" },
        el("div", { class: "welcome-image" },
          coverImage(cover, 54),
          el("div", { class: "image-corner" }, projectStatusBadge(p.status)),
        ),
        el("div", { class: "welcome-copy" },
          el("h3", {}, p.title),
          el("p", { class: "small muted" }, `${scenes.length} scenes · ${fmtDuration(p.target_duration)} · ${p.aspect_ratio}`),
          editorial || !scenes.length ? null : progress(approved / scenes.length),
          el("p", { class: "small" }, summary),
          el("button", { class: "btn btn-primary welcome-continue", type: "button", onclick: () => openProject(p.id, next[0]) },
            next[1], icon("chevron", 14)),
        ),
      ),
      Object.keys(stages).length
        ? el("details", { class: "studio-stage-record" },
          el("summary", {}, "Production stages"),
          el("div", { class: "row" }, Object.entries(stages).map(([name, stage]) => stageChip(name, stage))),
        ) : null,
    );
  }
}
