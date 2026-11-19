/**
 * Job Monitor screen: every generation job the backend knows about.
 *
 *  - Initial data comes from GET /api/jobs; afterwards the screen re-renders
 *    whenever the app-level job feed emits an update (SSE live, with the
 *    polling fallback handled by js/events.js — no extra connections here).
 *  - Rows show what each job is actually doing: the human stage name, the
 *    project title, the backend-reported sub-stage (`parameters.current_stage`,
 *    e.g. "quality_control" inside a render), the queue position while a job
 *    waits, progress + elapsed time while it runs, and start/finish
 *    timestamps with duration once it is terminal. The backend-redacted
 *    error text is shown for failures; nothing sensitive is stored here.
 *  - Status-group and project filters narrow the view client-side; `state.jobs`
 *    always keeps the full list so the top bar counts and toasts stay exact.
 *  - Rows are keyed by job id and patched in place. The once-a-second feed
 *    frames used to tear down and rebuild the whole list, which swallowed
 *    Cancel/Retry clicks mid-press and reset hover, focus, and selection;
 *    now only rows whose data actually changed are rebuilt.
 *  - Cancel is offered for non-terminal jobs the backend can actually stop
 *    (`cancelable === false` hides it — pipeline bookkeeping rows tolerate a
 *    mid-operation cancel as a no-op, which the UI must not offer); Retry for
 *    failed/canceled ones the backend can re-run (`executable === false` —
 *    child stage rows driven by a parent pipeline — hides it, as does an
 *    exhausted attempt budget, which the backend would reject with 409). A
 *    ticker refreshes elapsed times between feed frames while anything runs.
 *  - Terminal history is capped: only the newest JOB_ROW_CAP rows render
 *    until "Show all" is used, so a long-lived database cannot grow the DOM
 *    without bound; the summary line always counts the full list.
 */

import { el, fmtDate, fmtDuration, shortId } from "../dom.js";
import { state, persistCurrentProject } from "../state.js";
import { listJobs, cancelJob, retryJob } from "../api.js";
import {
  loadingState,
  emptyState,
  errorPanel,
  badge,
  jobStatusBadge,
  confirm,
  toast,
  toastError,
  stageLabel,
} from "../ui.js";
import { registerLiveUpdate } from "../app.js";
import { jobProgress } from "../job-progress.js";
import { navigate, sceneEditorHash } from "../router.js";
import { effectiveVideoMode } from "../video-mode.js";

const TERMINAL = ["completed", "failed", "canceled"];
/** Non-terminal states in which the job is actively consuming a worker. */
const ACTIVE = ["preparing", "loading_model", "generating", "postprocessing"];

/** Newest rows rendered before "Show all" is required. */
const JOB_ROW_CAP = 200;

/** Bumped on every render; lets the feed callback know the screen is gone. */
let generation = 0;

/** Status filter chips: [key, label, predicate]. */
const STATUS_FILTERS = [
  ["current", "Current work", () => true],
  ["active", "Active", (j) => ACTIVE.includes(j.status)],
  ["queued", "Queued", (j) => j.status === "queued"],
  ["failed", "Failed history", (j) => j.status === "failed"],
  ["finished", "Finished", (j) => j.status === "completed" || j.status === "canceled"],
  ["all", "All jobs", () => true],
];

/** Session-scoped filter state, kept while navigating away and back. */
let statusFilter = "current";
/** Project id filter; empty string shows every project. */
let projectFilter = "";

export function currentJobFailures(jobs) {
  const seen = new Set();
  // A newer attempt for the same output supersedes an older failure. A
  // canceled attempt leaves the most recent actual result relevant.
  return [...jobs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).filter((job) => {
    if (job.status === "canceled") return false;
    const params = job.parameters || {};
    const target = JSON.stringify([job.project_id, job.stage, job.scene_id, job.shot_id,
      params.stage, params.composition_id, params.editorial_asset_id, params.candidate_id,
      params.take_id, params.chunk_index, params.cue_id]);
    if (seen.has(target)) return false;
    seen.add(target);
    return job.status === "failed";
  });
}

function recoveryDestination(job, project) {
  if (job.stage.startsWith("thumbnail")) return ["#/thumbnails", "Open Thumbnails"];
  if (job.stage === "plan") return ["#/script", "Open Script"];
  if (job.stage.startsWith("narration")) return ["#/voice", "Open Voice"];
  if (job.stage === "caption_alignment" || job.stage === "subtitles") return ["#/captions", "Open Captions"];
  if (job.stage === "music" || job.stage === "score_mix") return ["#/music", "Open Music"];
  if (job.stage.startsWith("editorial_")) return ["#/editorial", "Open Editorial"];
  if (job.stage.startsWith("render") || job.stage === "pipeline" || job.stage === "quality_control") {
    return ["#/export", "Open Export"];
  }
  if (job.stage === "timeline") return ["#/timeline", "Open Timeline"];
  if (job.scene_id && effectiveVideoMode(project) === "classic") {
    return [sceneEditorHash(job.scene_id, job.shot_id), "Open scene"];
  }
  if (["visuals", "visual_batch", "references"].includes(job.stage)) {
    return effectiveVideoMode(project) === "editorial"
      ? ["#/editorial", "Open Editorial"] : ["#/storyboard", "Open Storyboard"];
  }
  return ["#/project", "Open project"];
}

/**
 * @param {string | null | undefined} iso
 * @returns {number} ms epoch, or NaN
 */
function parseTime(iso) {
  if (!iso) return NaN;
  return Date.parse(iso);
}

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderJobs(_route) {
  generation += 1;
  const gen = generation;

  const feedSlot = el("span", { class: "badge-row" });
  const refreshBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button",
  }, "Refresh");
  const chipRow = el("div", { class: "row" });
  const summary = el("span", { class: "small muted" });
  const projectSelect = el("select", {
    class: "input",
    "aria-label": "Filter by project",
    onchange: (ev) => {
      projectFilter = /** @type {HTMLSelectElement} */ (ev.target).value;
      showAllJobs = false;
      renderList(state.jobs);
    },
  });

  const controls = el("div", { class: "row jobs-filters" },
    chipRow, summary, el("span", { class: "spacer" }), projectSelect);
  const body = el("div", { class: "jobs-list" });
  const screen = el("div", { class: "screen jobs-screen" },
    el("div", { class: "screen-head" },
      el("h1", {}, "Job Monitor"),
      el("div", { class: "screen-actions" }, feedSlot, refreshBtn),
    ),
    controls,
    body,
  );

  /** job id → {node, stamp}; row elements persist across feed frames. */
  const rows = new Map();
  /** Last-rendered controls signature; untouched controls survive frames. */
  let controlsSig = "";
  /** Reset on every mount: each visit starts bounded, expansion is per visit. */
  let showAllJobs = false;

  /**
   * Display context derived from the full (unfiltered) job list.
   * @param {import("../api.js").GenerationJob[]} all
   */
  function buildCtx(all) {
    const projectsById = new Map(state.projects.map((p) => [p.id, p]));
    // Drain order of the persistent queue (storage/database.py
    // claim_queued_job): priority DESC, then oldest first.
    const queued = all
      .filter((j) => j.status === "queued")
      .sort((a, b) => (b.priority - a.priority)
        || (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0));
    const queuePos = new Map(queued.map((j, i) => [j.id, i + 1]));
    return { projectsById, queuePos };
  }

  /**
   * Everything that affects a row's rendered content. A feed frame with the
   * same stamp leaves the existing DOM node (and its listeners) untouched.
   * @param {import("../api.js").GenerationJob} job
   * @param {ReturnType<typeof buildCtx>} ctx
   * @returns {string}
   */
  function rowStamp(job, ctx) {
    const params = job.parameters || {};
    const project = ctx.projectsById.get(job.project_id);
    return [
      ACTIVE.includes(job.status) ? "active" : job.status, job.error || "", job.started_at, job.completed_at,
      params.parent_job_id || "",
      ctx.queuePos.get(job.id) || "", (project && project.title) || "",
      job.scene_title || "", statusFilter,
      job.executable === false ? "no-retry" : "",
      job.attempt_count >= job.max_attempts ? "exhausted" : "",
    ].join("|");
  }

  function syncControls() {
    const sig = `${statusFilter}|${projectFilter}|`
      + state.projects.map((p) => `${p.id}:${p.title}`).join(",");
    if (sig === controlsSig) return;
    controlsSig = sig;
    chipRow.replaceChildren(...STATUS_FILTERS.map(([key, label]) => {
      const selected = key === statusFilter;
      const chip = el("button", {
        class: selected ? "btn btn-sm" : "btn btn-ghost btn-sm",
        type: "button",
        "aria-pressed": selected ? "true" : "false",
      }, label);
      chip.onclick = () => {
        statusFilter = key;
        showAllJobs = false;
        renderList(state.jobs);
      };
      return chip;
    }));
    projectSelect.replaceChildren(
      el("option", { value: "" }, "All projects"),
      ...state.projects.map((p) => el("option", { value: p.id }, p.title)),
    );
    projectSelect.value = projectFilter;
    if (projectSelect.value !== projectFilter) {
      // The filtered project vanished from the list; fall back to all.
      projectFilter = "";
      projectSelect.value = "";
    }
  }

  /**
   * @param {import("../api.js").GenerationJob[]} all
   * @param {number} shown
   * @returns {string}
   */
  function summaryText(all, shown) {
    const active = all.filter((j) => ACTIVE.includes(j.status)).length;
    const queued = all.filter((j) => j.status === "queued").length;
    const failed = all.filter((j) => j.status === "failed").length;
    const scope = shown === all.length ? `${all.length} jobs` : `${shown} of ${all.length} jobs`;
    return `${scope} · ${active} active · ${queued} queued · ${failed} failed`;
  }

  /**
   * @param {import("../api.js").GenerationJob[]} jobs
   */
  function renderList(jobs) {
    if (gen !== generation) return;
    syncControls();
    const all = Array.isArray(jobs) ? jobs : [];
    const predicate = (STATUS_FILTERS.find(([key]) => key === statusFilter) || STATUS_FILTERS[0])[2];
    const scoped = all.filter((job) => !projectFilter || job.project_id === projectFilter);
    const current = statusFilter === "current";
    const ctx = buildCtx(all);
    const groups = current ? [
      ["Running", scoped.filter((job) => ACTIVE.includes(job.status))],
      ["Queued", scoped.filter((job) => job.status === "queued")
        .sort((a, b) => ctx.queuePos.get(a.id) - ctx.queuePos.get(b.id))],
      ["Needs attention", currentJobFailures(scoped)],
    ] : [[null, scoped.filter(predicate)
      .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))]];
    const count = groups.reduce((total, [, jobs]) => total + jobs.length, 0);
    summary.textContent = current ? `${count} current` : summaryText(scoped, count);
    if (!all.length) {
      rows.clear();
      body.replaceChildren(emptyState("No jobs yet",
        "Jobs appear here when scenes are generated, narration is recorded, or a render runs."));
      return;
    }
    if (!count) {
      rows.clear();
      body.replaceChildren(current
        ? emptyState("No work needs attention", "Running jobs, queued jobs, and unresolved failures appear here.",
          [el("button", { class: "btn btn-sm", type: "button", onclick: () => {
            statusFilter = "all";
            renderList(state.jobs);
          } }, "View job history")])
        : emptyState("Nothing matches", "No jobs match the current filters."));
      return;
    }
    const groupsStamp = JSON.stringify([statusFilter, showAllJobs, groups.map(([name, jobs]) =>
      [name, jobs.length, (showAllJobs ? jobs : jobs.slice(0, JOB_ROW_CAP)).map((job) => job.id)])]);
    const layoutChanged = body.dataset.groups !== groupsStamp || !body.querySelector(".job-row");
    const sections = [];
    const seen = new Set();
    for (const [name, jobs] of groups) {
      if (!jobs.length) continue;
      const visible = showAllJobs ? jobs : jobs.slice(0, JOB_ROW_CAP);
      const nodes = visible.map((job) => {
        seen.add(job.id);
        let entry = rows.get(job.id);
        if (!entry) {
          entry = { node: el("article", { class: "job-row", "data-job": job.id }), stamp: "" };
          rows.set(job.id, entry);
        }
        const stamp = rowStamp(job, ctx);
        if (entry.stamp !== stamp) {
          entry.node.replaceChildren(...jobBody(job, ctx));
          entry.stamp = stamp;
        }
        return entry.node;
      });
      if (!layoutChanged) continue;
      sections.push(el("section", { class: "jobs-group", "aria-label": name || "Job history" },
        name ? el("h2", { class: "jobs-group-title" }, name, el("span", { class: "muted" }, String(jobs.length))) : null,
        ...nodes,
        visible.length < jobs.length ? el("button", {
          class: "btn btn-ghost btn-sm", type: "button",
          onclick: () => { showAllJobs = true; renderList(state.jobs); },
        }, `Show all ${jobs.length} jobs`) : null,
      ));
    }
    for (const [id, entry] of rows) {
      if (!seen.has(id)) {
        entry.node.remove();
        rows.delete(id);
      }
    }
    if (layoutChanged) {
      body.dataset.groups = groupsStamp;
      body.replaceChildren(...sections);
    }
  }

  /** One compact timestamps/elapsed line; the elapsed span self-updates. */
  function timesRow(job) {
    const startIso = job.started_at || job.created_at || null;
    const endIso = job.completed_at || null;
    const startMs = parseTime(startIso);
    const endMs = parseTime(endIso) || parseTime(job.updated_at);
    /** @type {(Node|string)[]} */
    const bits = [`created ${fmtDate(job.created_at)}`];
    if (TERMINAL.includes(job.status)) {
      if (endIso && job.status === "completed") bits.push(` · finished ${fmtDate(endIso)}`);
      if (!Number.isNaN(startMs) && !Number.isNaN(endMs)) {
        bits.push(" · took ", el("span", { class: "mono" }, fmtDuration((endMs - startMs) / 1000)));
      }
    }
    return el("div", { class: "small muted" }, ...bits);
  }

  /**
   * Body parts for a job row: status + stage + backend, project/scene, the
   * current sub-stage, queue position or progress, timestamps, terminal
   * detail (error for failures), and permitted actions.
   * @param {import("../api.js").GenerationJob} job
   * @param {ReturnType<typeof buildCtx>} ctx
   * @returns {HTMLElement[]}
   */
  function jobBody(job, ctx) {
    const project = ctx.projectsById.get(job.project_id);
    const params = job.parameters || {};
    const isTerminal = TERMINAL.includes(job.status);
    const main = el("div", { class: "job-what stack" },
      el("div", { class: "row job-name" },
        el("h2", { class: "job-stage" }, stageLabel(job.stage)),
        params.parent_job_id
          ? el("span", { class: "tag", title: `part of job ${params.parent_job_id}` }, "batch")
          : null,
        el("span", { class: "small muted" }, job.backend || "—"),
      ),
      el("div", { class: "job-ctx" },
        project ? project.title : `project ${shortId(job.project_id)}`,
        job.scene_title ? ` · ${job.scene_title}` : job.scene_id ? ` · scene ${shortId(job.scene_id)}` : "",
      ),
    );
    const meta = el("div", { class: "job-meta" },
      el("span", { class: "job-id" }, `job ${shortId(job.id)}`),
      timesRow(job),
    );

    if (job.status === "queued") {
      const pos = ctx.queuePos.get(job.id);
      main.append(el("div", { class: "small muted" },
        pos ? `Queue position #${pos}` : "Queued",
        job.priority ? ` · priority ${job.priority}` : "",
      ));
    }
    if (!isTerminal) main.append(jobProgress((entry) => entry.id === job.id, { initial: job, label: false }));

    if (job.attempt_count > 0) {
      meta.append(el("div", { class: "small muted" },
        `Attempt ${job.attempt_count} of ${job.max_attempts || "—"}`));
    }

    if (isTerminal && job.status !== "completed" && job.error) {
      main.append(el("div", { class: "job-failure small mono" }, job.error));
    }

    const actions = [];
    if (job.status === "failed" && project) {
      const [hash, label] = recoveryDestination(job, project);
      actions.push(el("button", { class: "btn btn-sm", type: "button", onclick: () => {
        state.currentProjectId = project.id;
        persistCurrentProject(project.id);
        navigate(hash);
      } }, label));
    }
    // The backend reports cancelable=false for pipeline bookkeeping rows:
    // canceling them mid-operation is a tolerated no-op, so no button.
    if (!isTerminal && job.cancelable !== false) {
      const cancelBtn = el("button", {
        class: "btn btn-ghost btn-sm", type: "button",
      }, "Cancel");
      cancelBtn.onclick = async () => {
        const ok = await confirm({
          title: `Cancel this ${stageLabel(job.stage)} job?`,
          message: "The job will be canceled. Completed stages are kept.",
          confirmLabel: "Cancel job",
        });
        if (!ok) return;
        try {
          const updated = await cancelJob(state.config, job.id);
          state.jobs = state.jobs.map((j) => (j.id === updated.id ? updated : j));
          toast("info", "Job canceled", `job ${shortId(job.id)}`);
          renderList(state.jobs);
        } catch (err) {
          toastError(err, "cancel job");
        }
      };
      actions.push(cancelBtn);
    }
    if (
      (job.status === "failed" || job.status === "canceled")
      && job.executable !== false
      // An exhausted attempt budget makes the backend reject every retry
      // (storage/jobs.py raises 409 "exhausted its configured attempts").
      && job.attempt_count < job.max_attempts
    ) {
      const retryBtn = el("button", {
        class: "btn btn-ghost btn-sm", type: "button",
      }, "Retry");
      retryBtn.onclick = async () => {
        try {
          const updated = await retryJob(state.config, job.id);
          state.jobs = state.jobs.map((j) => (j.id === updated.id ? updated : j));
          toast("good", "Job requeued and running", `job ${shortId(job.id)}`);
          renderList(state.jobs);
        } catch (err) {
          toastError(err, "retry job");
        }
      };
      actions.push(retryBtn);
    }

    return [main,
      el("div", { class: "job-state" },
        ACTIVE.includes(job.status) ? badge("accent", "Active") : jobStatusBadge(job.status),
        el("div", { class: "job-actions" }, ...actions),
      ), meta,
    ];
  }

  function renderFeedBadge() {
    const st = state.feedStatus;
    feedSlot.replaceChildren(
      st === "live" ? badge("good", "Live (SSE)")
      : st === "reconnecting" ? badge("warning", "Reconnecting...")
      : st === "polling" ? badge("accent", "Polling")
      : st === "offline" ? badge("critical", "Feed offline")
      : badge("neutral", "Feed starting..."),
    );
  }

  async function load() {
    if (gen !== generation) return;
    renderFeedBadge();
    body.replaceChildren(loadingState(4));
    try {
      const before = state.jobs;
      const list = await listJobs(state.config);
      if (gen !== generation) return;
      if (state.jobs === before) state.jobs = list.jobs;
      renderFeedBadge();
      renderList(state.jobs);
    } catch (err) {
      if (gen !== generation) return;
      body.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load() }, "Retry"),
      ));
    }
  }

  refreshBtn.onclick = () => load();
  renderFeedBadge();
  // Live path: the feed already delivered fresh jobs to state.jobs, so just
  // re-render (no extra request). Manual/initial loads fetch explicitly.
  registerLiveUpdate(() => {
    if (gen !== generation) return;
    renderFeedBadge();
    renderList(state.jobs);
  });
  load();
  return screen;
}
