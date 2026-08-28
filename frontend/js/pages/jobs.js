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
import { state } from "../state.js";
import { listJobs, cancelJob, retryJob } from "../api.js";
import {
  loadingState,
  emptyState,
  errorPanel,
  badge,
  jobStatusBadge,
  progress,
  confirm,
  toast,
  toastError,
  stageLabel,
} from "../ui.js";
import { registerLiveUpdate } from "../app.js";

const TERMINAL = ["completed", "failed", "canceled"];
/** Non-terminal states in which the job is actively consuming a worker. */
const ACTIVE = ["preparing", "loading_model", "generating", "postprocessing"];

/** Newest rows rendered before "Show all" is required. */
const JOB_ROW_CAP = 200;

/** Bumped on every render; lets the feed callback know the screen is gone. */
let generation = 0;

/** Known `parameters.current_stage` tokens written by parent jobs. */
const CURRENT_STAGE_LABELS = {
  queued: "Waiting for a worker",
  validating_inputs: "Validating inputs",
  timeline: "Building timeline",
  render_preview: "Rendering preview",
  quality_control: "Quality check",
  render_final: "Rendering final video",
  thumbnails: "Extracting thumbnail frames",
  done: "Wrapping up",
};

/** Status filter chips: [key, label, predicate]. */
const STATUS_FILTERS = [
  ["all", "All", () => true],
  ["active", "Active", (j) => ACTIVE.includes(j.status)],
  ["queued", "Queued", (j) => j.status === "queued"],
  ["failed", "Failed", (j) => j.status === "failed"],
  ["finished", "Finished", (j) => j.status === "completed" || j.status === "canceled"],
];

/** Session-scoped filter state, kept while navigating away and back. */
let statusFilter = "all";
/** Project id filter; empty string shows every project. */
let projectFilter = "";

/**
 * @param {string} raw - `parameters.current_stage`; batch tokens like
 *   "3/8 · S5 · h3_audiovisual" pass through untouched.
 * @returns {string}
 */
function currentStageLabel(raw) {
  return CURRENT_STAGE_LABELS[raw] || raw;
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
    "aria-label": "Filter by project",
    onchange: (ev) => {
      projectFilter = /** @type {HTMLSelectElement} */ (ev.target).value;
      renderList(state.jobs);
    },
  });

  const controls = el("div", { class: "row" },
    chipRow, summary, el("span", { class: "spacer" }), projectSelect);
  const body = el("div", { class: "stack" });
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Job Monitor")),
    el("div", { class: "panel" },
      el("div", { class: "row" },
        el("span", { class: "panel-title" }, "Jobs"),
        feedSlot,
        el("span", { class: "spacer" }),
        refreshBtn,
      ),
      el("div", { class: "panel-body stack" }, controls, body),
    ),
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
      job.status, job.progress, job.updated_at, job.error || "",
      params.current_stage || "", params.parent_job_id || "",
      ctx.queuePos.get(job.id) || "", (project && project.title) || "",
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
    const filtered = all
      .filter((j) => predicate(j) && (!projectFilter || j.project_id === projectFilter))
      .sort((a, b) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0));
    summary.textContent = summaryText(all, filtered.length);
    if (!all.length) {
      rows.clear();
      body.replaceChildren(emptyState("No jobs yet",
        "Jobs appear here when scenes are generated, narration is recorded, or a render runs."));
      return;
    }
    if (!filtered.length) {
      rows.clear();
      body.replaceChildren(emptyState("Nothing matches",
        "No jobs match the current filters."));
      return;
    }
    const ctx = buildCtx(all);
    const visible = showAllJobs || filtered.length <= JOB_ROW_CAP
      ? filtered
      : filtered.slice(0, JOB_ROW_CAP);
    // Detach all children first; row nodes stay alive in `rows` and are
    // re-appended in sorted order below (append() moves existing nodes).
    body.replaceChildren();
    const seen = new Set();
    for (const job of visible) {
      seen.add(job.id);
      let entry = rows.get(job.id);
      if (!entry) {
        entry = { node: el("div", { class: "panel", "data-job": job.id }), stamp: "" };
        rows.set(job.id, entry);
      }
      const stamp = rowStamp(job, ctx);
      if (entry.stamp !== stamp) {
        entry.node.replaceChildren(el("div", { class: "panel-body stack" }, ...jobBody(job, ctx)));
        entry.stamp = stamp;
      }
      body.append(entry.node);
    }
    for (const [id, entry] of rows) {
      if (!seen.has(id)) {
        entry.node.remove();
        rows.delete(id);
      }
    }
    if (visible.length < filtered.length) {
      body.append(el("button", {
        class: "btn btn-ghost btn-sm", type: "button",
        onclick: () => { showAllJobs = true; renderList(state.jobs); },
      }, `Show all ${filtered.length} jobs`));
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
    } else if (job.status !== "queued" && !Number.isNaN(startMs)) {
      bits.push(
        " · running for ",
        // data-elapsed nodes are refreshed by the 1s ticker between feed frames.
        el("span", { class: "mono", "data-elapsed": "1", "data-start": String(startIso) },
          fmtDuration((Date.now() - startMs) / 1000)),
      );
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
    const parts = [
      el("div", { class: "row" },
        jobStatusBadge(job.status),
        badge("neutral", stageLabel(job.stage), false),
        params.parent_job_id
          ? el("span", { class: "tag", title: `part of job ${params.parent_job_id}` }, "batch")
          : null,
        el("span", { class: "small muted" }, job.backend || "—"),
        el("span", { class: "spacer" }),
        el("span", { class: "small muted mono" }, `job ${shortId(job.id)}`),
      ),
      el("div", { class: "small muted" },
        project ? project.title : `project ${shortId(job.project_id)}`,
        job.scene_id ? ` · scene ${shortId(job.scene_id)}` : "",
      ),
    ];

    const current = typeof params.current_stage === "string" ? params.current_stage : "";
    if (current && !isTerminal && job.status !== "queued") {
      parts.push(el("div", { class: "small" },
        el("span", { class: "muted" }, "Current stage: "),
        el("span", {}, currentStageLabel(current)),
      ));
    }

    parts.push(timesRow(job));

    if (job.status === "queued") {
      const pos = ctx.queuePos.get(job.id);
      parts.push(el("div", { class: "small muted" },
        pos ? `Queue position #${pos}` : "Queued",
        job.priority ? ` · priority ${job.priority}` : "",
      ));
    } else if (!isTerminal) {
      parts.push(progress(job.progress || 0));
    }

    if (job.attempt_count > 0) {
      parts.push(el("div", { class: "small muted" },
        `Attempt ${job.attempt_count} of ${job.max_attempts || "—"}`));
    }

    if (isTerminal && job.status !== "completed" && job.error) {
      parts.push(el("div", { class: "warning-list" },
        el("div", { class: "witem crit" },
          el("span", { class: "small mono" }, job.error),
        ),
      ));
    }

    const actions = [];
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

    if (actions.length) {
      parts.push(el("div", { class: "row" }, ...actions));
    }
    return parts;
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
      const list = await listJobs(state.config);
      if (gen !== generation) return;
      state.jobs = list.jobs;
      renderFeedBadge();
      renderList(list.jobs);
    } catch (err) {
      if (gen !== generation) return;
      body.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load() }, "Retry"),
      ));
    }
  }

  // Between feed frames, refresh only the self-updating elapsed spans — the
  // rows themselves stay untouched, so hover/focus/selection survive.
  const ticker = setInterval(() => {
    if (gen !== generation || !screen.isConnected) {
      clearInterval(ticker);
      return;
    }
    const now = Date.now();
    screen.querySelectorAll("[data-elapsed]").forEach((node) => {
      const startMs = parseTime(node.getAttribute("data-start"));
      if (!Number.isNaN(startMs)) node.textContent = fmtDuration((now - startMs) / 1000);
    });
  }, 1000);

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
