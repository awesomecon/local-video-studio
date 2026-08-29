/**
 * Export screen: render controls and final-output state for the current project.
 *
 *  - "Render final video" / "Re-render final video" call
 *    POST /api/projects/{id}/render and return 202. Classic projects queue an
 *    FFmpeg-only job (stage "render"); Editorial projects additionally run a
 *    deterministic "editorial_visual" stage first, which renders the silent
 *    canvas master from the existing Edit Plan and registered assets.
 *  - In both modes the render consumes what already exists: narration, music,
 *    and captions (classic adds scene visuals). It never contacts the LLM,
 *    runs TTS, or generates replacement content.
 *  - While a render job is active the panel shows its status, progress and a
 *    Cancel action (POST /api/jobs/{id}/cancel).
 *  - The final video path and QC file path come from the project's stage
 *    state (render_final / quality_control outputs). The QC report itself
 *    has no read endpoint, so only its path and completion are shown.
 *  - A completed, recorded final-render asset has a project-scoped local
 *    download link.
 */

import { el, fmtDate, fmtDuration } from "../dom.js";
import { state, needsProject } from "../state.js";
import { getProject, getThumbnails, renderProject, cancelJob } from "../api.js";
import {
  loadingState,
  errorPanel,
  badge,
  jobStatusBadge,
  confirm,
  toast,
  toastError,
  progress,
  stageChip,
} from "../ui.js";
import { registerLiveUpdate } from "../app.js";
import { navigate, parseRoute } from "../router.js";

const TERMINAL = ["completed", "failed", "canceled"];

/* --- Mode-specific presentation -------------------------------------------
 * Classic / legacy projects (video_mode omitted, "classic", or unknown) keep
 * the original wording and rows; only an explicit video_mode === "editorial"
 * project gets the additive Editorial canvas presentation. The helpers below
 * are pure (snapshot in, presentation out) so the frontend logic tests can
 * pin the mode-specific presentation without a backend or network.
 */

const CLASSIC_DESCRIPTION =
  "Uses existing local narration and scene visuals. It does not contact the LLM, run TTS, or generate graphics.";
const EDITORIAL_DESCRIPTION =
  "Uses the existing Edit Plan, registered assets, narration, music, and captions. "
  + "It does not contact the LLM, run TTS, or generate replacement assets.";

const CLASSIC_WORKFLOW = "Timeline → preview → quality check → final MP4 → frame extraction";
const EDITORIAL_WORKFLOW =
  "Editorial canvas → timeline → preview → quality check → final MP4 → frame extraction";

const CLASSIC_FORCE_MESSAGE =
  "Timeline, preview, quality check, final MP4, and extracted frames will be rebuilt. "
  + "Existing scripts, narration, scene graphics, music, and captions will not be regenerated.";
const EDITORIAL_FORCE_MESSAGE =
  "The Editorial visual master and the downstream render outputs (timeline, preview, quality check, final MP4, and extracted frames) will be rebuilt. "
  + "The Edit Plan, registered assets, narration, music, and captions are not regenerated.";

/**
 * Only an explicit "editorial" mode opts in; omitted or unknown values
 * (legacy projects) read as classic.
 * @param {{video_mode?: any} | null | undefined} project
 * @returns {"classic"|"editorial"}
 */
export function exportVideoMode(project) {
  return project?.video_mode === "editorial" ? "editorial" : "classic";
}

/** @param {{video_mode?: any} | null | undefined} project */
export function exportDescriptionText(project) {
  return exportVideoMode(project) === "editorial" ? EDITORIAL_DESCRIPTION : CLASSIC_DESCRIPTION;
}

/** @param {{video_mode?: any} | null | undefined} project */
export function exportWorkflowText(project) {
  return exportVideoMode(project) === "editorial" ? EDITORIAL_WORKFLOW : CLASSIC_WORKFLOW;
}

/** @param {{video_mode?: any} | null | undefined} project */
export function exportForceConfirmMessage(project) {
  return exportVideoMode(project) === "editorial" ? EDITORIAL_FORCE_MESSAGE : CLASSIC_FORCE_MESSAGE;
}

/**
 * Readable explanations for the Edit Plan staleness reasons reported by the
 * backend (`stale_reasons`). Unrecognized entries are dropped so provenance
 * metadata can never break the screen.
 * @type {Record<string, string>}
 */
const STALE_REASON_TEXT = {
  project: "project or editorial settings changed since the plan was generated",
  script: "the narration or script changed since the plan was generated",
  word_timings: "the narration word timings changed since the plan was generated",
};

/**
 * Reduce the snapshot's optional Edit Plan provenance metadata to the state
 * the readiness summary displays. Only the recognized plan_status values are
 * trusted: "stale" (with a best-effort list of readable reasons), "untracked",
 * and "current". A missing or malformed block (older backends, wrong types,
 * unknown values) degrades to "missing" so the screen never presents broken
 * metadata; an existing plan with an unrecognized status degrades to
 * "unknown" and keeps the classic "available" presentation. Stale and
 * untracked plans remain renderable and are never labeled broken.
 *
 * @param {import("../api.js").ProjectSnapshot | null | undefined} snap
 * @returns {{hasPlan: boolean, status: "missing"|"current"|"stale"|"untracked"|"unknown", reasons: string[]}}
 */
export function editorialPlanSummary(snap) {
  const editorial = (snap && typeof snap.editorial === "object" && snap.editorial) ? snap.editorial : null;
  if (!editorial || editorial.has_edit_plan !== true) {
    return { hasPlan: false, status: "missing", reasons: [] };
  }
  const planStatus = (typeof editorial.plan_status === "string") ? editorial.plan_status.trim() : null;
  if (planStatus === "stale") {
    const raw = Array.isArray(editorial.stale_reasons) ? editorial.stale_reasons : [];
    const seen = new Set();
    const reasons = [];
    for (const entry of raw) {
      const key = (typeof entry === "string") ? entry.trim() : "";
      if (key && STALE_REASON_TEXT[key] && !seen.has(key)) {
        seen.add(key);
        reasons.push(STALE_REASON_TEXT[key]);
      }
    }
    return { hasPlan: true, status: "stale", reasons };
  }
  if (planStatus === "untracked") return { hasPlan: true, status: "untracked", reasons: [] };
  if (planStatus === "current") return { hasPlan: true, status: "current", reasons: [] };
  return { hasPlan: true, status: "unknown", reasons: [] };
}

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderExport(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Export")),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject("Select a project in the top bar to render it."));
    return screen;
  }
  screen.append(exportPanel());
  return screen;
}

/**
 * @returns {HTMLElement}
 */
function exportPanel() {
  const body = el("div", { class: "panel-body" });
  const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Refresh");
  refreshBtn.onclick = () => load(body);

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Render & Export"),
      el("span", { class: "spacer" }),
      refreshBtn,
    ),
    body,
  );

  /**
   * @param {HTMLElement} region
   * @param {{skeleton?: boolean}} [opts] — omit the skeleton on live refreshes
   */
  let inflight = 0; // last-write-wins sequence, same pattern as timeline.js
  /** True once the panel content has rendered; a transient error must not
   *  replace it, but a first-load failure (only the skeleton on screen)
   *  still shows the error panel with its Retry action. */
  let hasContent = false;
  async function load(region, { skeleton = true } = {}) {
    const token = ++inflight;
    if (skeleton) region.replaceChildren(loadingState(4));
    try {
      const [snap, thumbnails] = await Promise.all([
        getProject(state.config, state.currentProjectId),
        getThumbnails(state.config, state.currentProjectId),
      ]);
      if (token !== inflight) return;
      const stages = (snap.stage_state && /** @type {any} */ (snap.stage_state).stages) || {};
      const jobs = (snap.jobs || []).filter((j) => j.stage === "render" || j.stage === "pipeline");
      const active = jobs.find((j) => !TERMINAL.includes(j.status)) || null;
      const last = jobs.slice().sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))[0] || null;
      region.replaceChildren(build(snap, stages, active, last, thumbnails));
      hasContent = true;
    } catch (err) {
      if (token !== inflight || hasContent) return;
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load(region) }, "Retry"),
      ));
    }
  }

  // Live path: render progress and final output update while the job runs
  // (no form state on this screen, so a full region rebuild is safe).
  registerLiveUpdate(() => load(body, { skeleton: false }));
  load(body);
  return panel;
}

/**
 * @param {import("../api.js").ProjectSnapshot} snap
 * @param {Record<string, {status?: string, outputs?: string[], completed_at?: string}>} stages
 * @param {import("../api.js").GenerationJob|null} active
 * @param {import("../api.js").GenerationJob|null} last
 * @returns {HTMLElement}
 */
function build(snap, stages, active, last, thumbnails) {
  const project = snap.project;
  const mode = exportVideoMode(project);
  const parts = [];

  const selected = (thumbnails.candidates || []).find((candidate) => candidate.selected);
  // Only a project-scoped local URL is rendered/downloadable; a candidate
  // without one must not produce an img src or href of `null?download=true`
  // (same localMedia() rule as the Thumbnails screen).
  const selectedUrl = selected
    && typeof selected.file_url === "string"
    && selected.file_url.startsWith("/api/projects/")
    ? selected.file_url
    : null;
  parts.push(
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "Export thumbnail"),
      el("div", { class: "panel-body" },
        selected && selectedUrl
          ? el("div", { class: "row" },
              el("img", {
                class: "thumbnail-preview",
                style: { maxWidth: "360px" },
                src: selectedUrl,
                alt: "Selected export thumbnail",
              }),
              el("div", { class: "stack" },
                badge("good", "Selected"),
                el("span", { class: "mono small" }, selected.candidate_id),
                el("a", {
                  class: "btn btn-primary btn-sm",
                  href: `${selectedUrl}?download=true`,
                }, "Download thumbnail PNG"),
              ),
            )
          : el("div", { class: "row" },
              el("span", { class: "muted small" }, selected
                ? `Selected thumbnail (${selected.candidate_id}) is not available through the local API.`
                : "No export thumbnail selected."),
              el("button", {
                class: "btn btn-sm", type: "button",
                onclick: () => navigate("#/thumbnails"),
              }, "Open Thumbnail Studio"),
            ),
      ),
    ),
  );

  /* --- video spec ------------------------------------------------------- */
  parts.push(
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "Video spec"),
      el("div", { class: "panel-body" },
        el("dl", { class: "kv" },
          el("dt", {}, "Resolution"),
          el("dd", {}, project.resolution ? project.resolution.join("×") : "—"),
          el("dt", {}, "Aspect ratio"),
          el("dd", {}, project.aspect_ratio || "—"),
          el("dt", {}, "Frame rate"),
          el("dd", {}, project.fps ? `${project.fps} fps` : "—"),
          el("dt", {}, "Target duration"),
          el("dd", {}, fmtDuration(project.target_duration || 0)),
        ),
      ),
    ),
  );

  /* --- render controls --------------------------------------------------- */
  const runBtn = el("button", {
    class: "btn btn-primary", type: "button", disabled: Boolean(active),
  }, "Render final video");
  const forceBtn = el("button", {
    class: "btn", type: "button", disabled: Boolean(active),
  }, "Re-render final video");
  runBtn.onclick = () => doRender(false, project);
  forceBtn.onclick = () => doRender(true, project);

  const statusRegion = el("div", { class: "mt" },
    active ? jobState(active)
      : last ? lastJobNote(last)
      : el("span", { class: "muted small" }, "No render has been queued yet."),
  );

  parts.push(
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "Render controls"),
      el("div", { class: "panel-body" },
        el("p", { class: "muted small" }, exportDescriptionText(project)),
        renderInputSummary(snap, stages),
        el("div", { class: "row" },
          runBtn,
          forceBtn,
          el("span", { class: "spacer" }),
          el("span", { class: "muted small" }, exportWorkflowText(project)),
        ),
        el("div", { class: "row mt" },
          mode === "editorial" ? labeledChip("Editorial canvas", stages.editorial_visual) : null,
          stageChip("timeline", stages.timeline),
          stageChip("render_preview", stages.render_preview),
          stageChip("quality_control", stages.quality_control),
          stageChip("render_final", stages.render_final),
          stageChip("thumbnails", stages.thumbnails),
        ),
        statusRegion,
      ),
    ),
  );

  /* --- final output ------------------------------------------------------ */
  const finalStage = stages.render_final;
  const qcStage = stages.quality_control;
  const finalDone = finalStage && finalStage.status === "completed";
  const finalAsset = (snap.assets || [])
    .filter((asset) => asset.settings && asset.settings.role === "final_render")
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0];
  parts.push(
    el("div", { class: "panel" },
      el("div", { class: "row" },
        el("span", { class: "panel-title" }, "Final output"),
        el("span", { class: "spacer" }),
        stageChip("render_final", finalStage),
      ),
      el("div", { class: "panel-body" },
        finalDone
          ? el("dl", { class: "kv" },
              el("dt", {}, "Final video"),
              el("dd", { class: "mono" }, fullPath(snap.directory, (finalStage.outputs || [])[0])),
              el("dt", {}, "Completed"),
              el("dd", {}, finalStage.completed_at ? fmtDate(finalStage.completed_at) : "—"),
              el("dt", {}, "Quality control"),
              el("dd", {},
                qcStage && qcStage.status === "completed"
                  ? `recorded at ${fullPath(snap.directory, (qcStage.outputs || [])[0])}`
                  : "not recorded yet",
              ),
              finalAsset && finalAsset.url
                ? el("dt", {}, "Playback / download")
                : null,
              finalAsset && finalAsset.url
                ? el("dd", {}, el("a", {
                    class: "btn btn-primary btn-sm",
                    href: `${finalAsset.url}?download=true`,
                  }, "Download final MP4"))
                : null,
            )
          : el("span", { class: "muted small" }, "The final render has not completed yet."),
        finalDone && !(finalAsset && finalAsset.url)
          ? el("p", { class: "muted small" }, "The final file is recorded but not currently available through the local API.")
          : null,
      ),
    ),
  );

  return el("div", { class: "stack" }, ...parts);
}

/**
 * Active render job: status, progress, cancel.
 * @param {import("../api.js").GenerationJob} job
 * @returns {HTMLElement}
 */
function jobState(job) {
  const cancelBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button",
  }, "Cancel render");
  cancelBtn.onclick = async () => {
    const ok = await confirm({
      title: "Cancel this render?",
      message: "The render job will be canceled. Completed stages are kept.",
      confirmLabel: "Cancel render",
    });
    if (!ok) return;
    try {
      await cancelJob(state.config, job.id);
      toast("info", "Render canceled");
      renderExportRefresh();
    } catch (err) {
      toastError(err, "cancel render");
    }
  };
  return el("div", { class: "stack" },
    el("div", { class: "row" },
      jobStatusBadge(job.status),
      badge("accent", renderStageLabel(job.parameters && job.parameters.current_stage)),
      el("span", { class: "muted small" }, `job ${job.id}`),
      el("span", { class: "spacer" }),
      cancelBtn,
    ),
    progress(job.progress || 0),
  );
}

/**
 * @param {import("../api.js").GenerationJob} job
 * @returns {HTMLElement}
 */
function lastJobNote(job) {
  const err = job.error ? (/** @type {any} */ (job.error).message || String(job.error)) : "";
  return el("div", { class: "row" },
    jobStatusBadge(job.status),
    el("span", { class: "muted small" }, `last render: ${job.status}${err ? ` — ${err}` : ""}`),
  );
}

/**
 * @param {boolean} force
 * @param {import("../api.js").Project} project — the mode selects the confirmation wording
 */
async function doRender(force, project) {
  if (force) {
    const ok = await confirm({
      title: "Re-render final video?",
      message: exportForceConfirmMessage(project),
      confirmLabel: "Re-render final video",
    });
    if (!ok) return;
  }
  try {
    const job = await renderProject(state.config, state.currentProjectId, { force });
    toast("good", "Render queued", `job ${job.id}`);
    renderExportRefresh();
  } catch (err) {
    toastError(err, "queue render");
  }
}

/**
 * Compact, non-authoritative readiness summary. The backend performs the
 * definitive file and provenance validation before it queues a render.
 * Classic / legacy projects count recorded scene visuals; Editorial projects
 * show Edit Plan availability and provenance status instead (there is no
 * scene grid to count there).
 */
export function renderInputSummary(snap, stages) {
  const editorial = exportVideoMode(snap.project) === "editorial";
  const assets = snap.assets || [];
  const narrationReady = Boolean(
    stages.narration && stages.narration.status === "completed"
    || assets.some((asset) => asset.settings && asset.settings.role === "narration")
  );
  const captionsReady = Boolean(stages.subtitles && stages.subtitles.status === "completed");
  const musicReady = assets.some((asset) => asset.settings && asset.settings.role === "music");
  const rows = editorial ? editPlanSummaryRow(snap) : sceneVisualsSummaryRow(snap, assets);
  rows.push(
    el("dt", {}, "Narration"),
    el("dd", {}, narrationReady ? "recorded" : "not recorded; backend will verify the local file"),
    el("dt", {}, "Optional inputs"),
    el("dd", {}, `${captionsReady ? "captions recorded"
        : editorial ? "captions derived from narration" : "captions derived from scenes"}; ${musicReady ? "music recorded" : "no music"}`),
  );
  return el("dl", { class: "kv" }, ...rows);
}

/**
 * Classic readiness row: how many scenes have a recorded visual asset.
 * @param {import("../api.js").ProjectSnapshot} snap
 * @param {import("../api.js").Asset[]} assets
 * @returns {Array<HTMLElement>}
 */
function sceneVisualsSummaryRow(snap, assets) {
  const sceneIds = new Set((snap.scenes || []).map((scene) => scene.id));
  const visualIds = new Set(assets
    .filter((asset) => asset.scene_id && asset.settings && asset.settings.role === "visual")
    .map((asset) => asset.scene_id));
  const visualCount = [...sceneIds].filter((id) => visualIds.has(id)).length;
  return [
    el("dt", {}, "Scene visuals"),
    el("dd", {}, `${visualCount}/${sceneIds.size} recorded`),
  ];
}

/**
 * Editorial readiness row: Edit Plan availability and provenance status.
 * Stale and untracked plans stay renderable and are never labeled broken;
 * missing or malformed metadata degrades to the "not generated" fallback.
 * @param {import("../api.js").ProjectSnapshot} snap
 * @returns {Array<HTMLElement>}
 */
function editPlanSummaryRow(snap) {
  const plan = editorialPlanSummary(snap);
  const text = !plan.hasPlan
    ? "not generated yet — an Edit Plan is required before rendering"
    : plan.status === "current"
      ? "current"
      : plan.status === "stale"
        ? `stale — ${plan.reasons.length ? plan.reasons.join("; ")
          : "tracked inputs changed since the plan was generated"}; still renderable`
        : plan.status === "untracked"
          ? "available — freshness unverified (the plan may predate provenance tracking); still renderable"
          : "available";
  return [el("dt", {}, "Edit Plan"), el("dd", {}, text)];
}

/**
 * Human label for a render sub-stage while a job is active; unknown values
 * fall back to the generic "Rendering".
 * @param {string|undefined} stage
 * @returns {string}
 */
export function renderStageLabel(stage) {
  return ({
    queued: "Queued",
    validating_inputs: "Validating inputs",
    editorial_visual: "Rendering Editorial canvas",
    timeline: "Building timeline",
    render_preview: "Rendering preview",
    quality_control: "Quality check",
    render_final: "Rendering final MP4",
    thumbnails: "Extracting frames",
  })[stage] || "Rendering";
}

/**
 * Pipeline chip identical to ui.stageChip but with a caller-supplied label —
 * used for the editorial_visual stage, which the shared STAGE_LABELS map does
 * not know.
 * @param {string} label
 * @param {{status?: string} | null | undefined} st
 * @returns {HTMLElement}
 */
function labeledChip(label, st) {
  const status = (st && st.status) || "pending";
  const kind = status === "completed" ? "good"
    : status === "failed" ? "critical"
    : status === "running" ? "accent"
    : "neutral";
  const statusLabel = { completed: "Completed", failed: "Failed", running: "Running", pending: "Pending" }[status] || status;
  return badge(kind, `${label}: ${statusLabel}`);
}

/**
 * @param {string|null} directory
 * @param {string|undefined} relative
 * @returns {string}
 */
function fullPath(directory, relative) {
  if (!relative) return "—";
  if (!directory) return relative;
  return `${directory.replace(/\/+$/, "")}/${relative}`;
}

/**
 * Re-render from fresh backend data after a mutation.
 */
function renderExportRefresh() {
  // The awaited mutation above may outlive this screen; never clobber
  // whatever route the user navigated to in the meantime.
  if (parseRoute().name !== "export") return;
  const content = document.querySelector(".content");
  if (content) content.replaceChildren(renderExport({ name: "export", param: null }));
}
