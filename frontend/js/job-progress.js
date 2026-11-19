import { el, fmtDuration } from "./dom.js";
import { state } from "./state.js";
import { stageLabel } from "./ui.js";

const terminal = new Set(["completed", "failed", "canceled"]);
const pending = new Map();
const accepted = new Map();
const observers = new WeakMap();
let tracked = new Set();
let submissionId = 0;
let clock = null;
let refreshQueued = false;

const PHASES = {
  submitting: "Starting request",
  queued: "Queued",
  preparing: "Preparing inputs",
  waiting_for_gpu: "Waiting for the GPU",
  loading_model: "Loading model",
  waiting_for_workflow: "Waiting for ComfyUI",
  running_workflow: "Running ComfyUI workflow",
  sampling: "Sampling",
  retrieving_outputs: "Retrieving outputs",
  postprocessing: "Processing outputs",
  saving_outputs: "Saving outputs",
  saving_script: "Saving script",
  synthesizing_narration: "Generating narration",
  synthesizing_chunk: "Generating narration chunk",
  enhancing_narration: "Enhancing narration",
  joining_audio: "Joining audio",
  aligning_captions: "Aligning captions",
  generating_batch: "Generating scene visuals",
  normalizing_shots: "Preparing shots",
  preparing_compositions: "Preparing compositions",
  capturing_frames: "Capturing frames",
  finalizing_encoding: "Finalizing encoding",
  encoding_video: "Encoding video",
  encoding_scene: "Encoding scene",
  mixing_export: "Mixing audio and finishing video",
  preparing_artwork: "Preparing artwork",
  compositing_thumbnail: "Compositing thumbnail",
  validating_inputs: "Validating inputs",
  editorial_visual: "Rendering Editorial canvas",
  timeline: "Building timeline",
  score_mix: "Mixing music",
  render_preview: "Rendering preview",
  render_final: "Rendering final video",
  thumbnails: "Extracting thumbnail frames",
  quality_control: "Checking render quality",
  waiting_for_llm: "Waiting for the local language model",
  checking_model_output: "Checking model output",
  completed: "Completed",
  failed: "Failed",
  canceled: "Canceled",
};

const GENERATING = {
  plan: "Drafting script",
  editorial_plan: "Drafting Edit Plan",
  editorial_revision: "Drafting revision",
  editorial_composition: "Regenerating composition",
  editorial_asset_generate: "Generating image",
  performance_tags: "Generating delivery cues",
  thumbnail_prompt: "Drafting thumbnail prompt",
  music: "Generating music",
  music_settings: "Drafting music settings",
  auto_score: "Creating score suggestions",
  score_preview: "Rendering score preview",
  caption_alignment: "Aligning captions",
};

const projectActions = {
  plan: "plan",
  script: "plan",
  "editorial/plan": "editorial_plan",
  "editorial/revisions": "editorial_revision",
  "tts/generate": "narration",
  "tts/performance-tags": "performance_tags",
  "tts/performance-tags/regenerate": "performance_tags",
  "music/generate": "music",
  "music/score-preview": "score_preview",
  "music/auto-score": "auto_score",
  "music/suggest-settings": "music_settings",
  "captions/generate": "caption_alignment",
  "thumbnails/magic-prompt/regenerate": "thumbnail_prompt",
  "visuals/batch": "visual_batch",
  render: "render",
};

// Register generation submissions before fetch resolves, including endpoints
// whose synchronous response is an asset or plan instead of a job.
function submissionScope(path, body) {
  if (!path.startsWith("/api/")) return null;
  const [, , type, id, ...segments] = path.split("?")[0].split("/").map(decodeURIComponent);
  const action = segments.join("/");
  if (type === "scenes" && ["generate", "regenerate", "render"].includes(action)) {
    return { scene_id: id, stage: action === "render" ? "scene_render" : "scene_visual" };
  }
  if (type === "shots" && ["generate", "regenerate"].includes(action)) {
    return { shot_id: id, stage: "shot_generate" };
  }
  if (type !== "projects") return null;
  const scope = { project_id: id, stage: projectActions[action], parameters: {} };
  if (scope.stage === "editorial_revision") scope.parameters.composition_id = body?.composition_id ?? null;
  if (scope.stage === "performance_tags") scope.parameters.segment = body?.key ?? null;
  const [group, collection, target, item, asset] = segments;
  if (group === "editorial" && collection === "compositions") {
    scope.parameters.composition_id = target;
    if (item === "regenerate") scope.stage = "editorial_composition";
    if (item === "assets" && segments[5] === "generate") {
      scope.stage = "editorial_asset_generate";
      scope.parameters.editorial_asset_id = asset;
    }
  } else if (group === "tts" && collection === "narrations" && segments[5] === "regenerate") {
    scope.stage = "narration_chunk";
    scope.parameters.source_asset_id = target;
    scope.parameters.chunk_index = Number(asset);
  } else if (group === "render" && collection === "stages") {
    scope.stage = "render_stage";
    scope.parameters.stage = target;
  } else if (group === "thumbnails" && collection === "candidates" && (!target || item === "regenerate")) {
    const candidate = target || body?.candidate_id;
    scope.stage = candidate ? `thumbnail:${candidate}` : "thumbnail_request";
  }
  return typeof scope.stage === "string" ? scope : null;
}

function matchesSubmission(job, submission) {
  if (job.stage !== submission.stage
    && !(submission.stage === "thumbnail_request" && job.stage.startsWith("thumbnail:"))) return false;
  return ["project_id", "scene_id", "shot_id"].every((key) => !submission[key] || job[key] === submission[key])
    && Object.entries(submission.parameters || {}).every(([key, value]) => (job.parameters?.[key] ?? null) === value);
}

export function beginJobSubmission(path, body) {
  let scope;
  try { scope = submissionScope(path, body); }
  catch (err) {
    if (err instanceof URIError) return null;
    throw err;
  }
  if (!scope) return null;
  const id = `submitting-${++submissionId}`;
  pending.set(id, {
    project_id: state.currentProjectId, ...scope, id, status: "submitting",
    created_at: new Date().toISOString(),
  });
  refreshJobProgress();
  return (result) => {
    pending.delete(id);
    if (result?.id && result.project_id && result.stage && result.status
      && !terminal.has(result.status) && !state.jobs.some((job) => job.id === result.id)) {
      accepted.set(result.id, result);
    }
    refreshJobProgress();
  };
}

function progressJobs() {
  const jobs = [...state.jobs];
  const ids = new Set(jobs.map((job) => job.id));
  for (const [id, job] of accepted) {
    if (ids.has(id)) accepted.delete(id);
    else jobs.push(job);
  }
  for (const submission of pending.values()) {
    if (!jobs.some((job) => !terminal.has(job.status) && matchesSubmission(job, submission))) jobs.push(submission);
  }
  tracked = new Set(jobs.filter((job) => tracked.has(job.id) || !terminal.has(job.status)).map((job) => job.id));
  return jobs;
}

function orderJobs(a, b) {
  const priority = (job) => terminal.has(job.status) ? 2 : ["queued", "submitting"].includes(job.status) ? 1 : 0;
  return priority(a) - priority(b)
    || String(b.started_at || b.created_at).localeCompare(String(a.started_at || a.created_at));
}

function newestChild(job, jobs) {
  const seen = new Set([job.id]);
  let parentId = job.id;
  let deepest = null;
  while (true) {
    const child = jobs.filter((entry) => entry.parameters?.parent_job_id === parentId
      && !seen.has(entry.id) && !terminal.has(entry.status) && entry.status !== "queued").sort(orderJobs)[0];
    if (!child) return deepest;
    deepest = child;
    seen.add(child.id);
    parentId = child.id;
  }
}

function countLabel({ completed, total, unit }) {
  if (unit === "seconds") return `${fmtDuration(completed)} of ${fmtDuration(total)}`;
  if (unit === "scenes_processed") return `${completed} of ${total} scenes processed`;
  if (unit === "chunks") return `${completed} of ${total} chunks complete`;
  return `${completed} of ${total} ${unit}`;
}

function phaseTitle(job) {
  const detail = terminal.has(job.status) ? null : job.progress_detail;
  const phase = detail?.phase
    || (!terminal.has(job.status) && job.status !== "queued" && job.parameters?.current_stage)
    || job.status;
  return phase === "generating" ? GENERATING[job.stage] || "Generating" : PHASES[phase] || stageLabel(phase);
}

function progressLine(job, { compact = false, label = true } = {}) {
  const finished = terminal.has(job.status);
  const detail = finished ? null : job.progress_detail;
  const title = phaseTitle(job);
  const measured = detail?.total > 0 && Number.isFinite(detail.completed);
  const count = measured ? countLabel(detail) : "";
  const name = stageLabel(job.stage === "render_stage" ? job.parameters?.stage || job.stage : job.stage);
  const start = job.started_at || job.created_at;
  return el("div", { class: `job-activity-line${compact ? " job-activity-compact" : ""}`, dataset: { status: job.status } },
    el("span", { class: "job-activity-title" }, label && name !== title ? `${name} · ${title}` : title),
    count ? el("span", { class: "job-activity-count" }, count) : null,
    !finished && start && !compact
      ? el("span", { class: "job-activity-clock", dataset: { jobClock: start }, "aria-label": "Elapsed time" },
          fmtDuration(Math.max(0, Math.floor((Date.now() - Date.parse(start)) / 1000))))
      : null,
    !finished && !compact ? el("progress", {
      class: "job-activity-bar", "aria-label": `${name} · ${title}`,
      max: measured ? detail.total : 1, value: measured ? detail.completed : null,
      "aria-valuetext": count || title,
    }) : null,
    job.status === "failed" && job.error && !compact && label
      ? el("span", { class: "job-activity-error" }, job.error) : null,
  );
}

export function jobProgress(matches, {
  initial = null, compact = false, label = true, summary = false, limit = 1, showTerminal = true,
  onActive = null,
} = {}) {
  const announcement = el("span", { class: "job-activity-announcement", role: "status" });
  const details = el("div", { class: "job-activity-details" });
  const panel = el("div", { class: "job-activity", hidden: true }, announcement, details);
  let stamp;
  observers.set(panel, (jobs) => {
    const candidates = jobs.filter(matches);
    if (initial && !candidates.some((job) => job.id === initial.id)) candidates.push(initial);
    candidates.sort((a, b) => {
      if (summary && !terminal.has(a.status) && !terminal.has(b.status)) {
        const preference = (job) => Number(job.project_id !== state.currentProjectId);
        return preference(a) - preference(b) || orderJobs(a, b);
      }
      return orderJobs(a, b);
    });
    const active = candidates.filter((job) => !terminal.has(job.status));
    onActive?.(active);
    const shown = active.length ? active.slice(0, limit)
      : showTerminal ? candidates.filter((job) => tracked.has(job.id)).slice(0, 1) : [];
    const entries = shown.map((job) => {
      const child = newestChild(job, jobs);
      return { job, child };
    });
    const next = JSON.stringify(entries.map(({ job, child }) => [job, child].map((entry) => entry && [
      entry.id, entry.status, entry.progress_detail, entry.parameters?.current_stage, entry.error, entry.started_at,
    ])));
    panel.hidden = !entries.length;
    if (next === stamp) return;
    stamp = next;
    details.replaceChildren(...entries.flatMap(({ job, child }) => {
      if (compact) return [progressLine(child || job, { compact, label })];
      return [progressLine(job, { label }), child && el("div", { class: "job-activity-child" }, progressLine(child))]
        .filter(Boolean);
    }));
    const message = entries.map(({ job, child }) => `${stageLabel(job.stage)} · ${phaseTitle(child || job)}`).join(". ");
    if (announcement.textContent !== message) announcement.textContent = message;
  });
  observers.get(panel)(progressJobs());
  if (!refreshQueued) {
    refreshQueued = true;
    queueMicrotask(() => {
      refreshQueued = false;
      refreshJobProgress();
    });
  }
  return panel;
}

export function jobLifecycleChanges(previous, next) {
  const group = (job) => terminal.has(job.status) ? job.status : "active";
  const last = new Map(previous.map((job) => [job.id, group(job)]));
  return next.filter((job) => last.get(job.id) !== group(job));
}

export function projectJobProgress(stages, options = {}) {
  const { projectId = state.currentProjectId, matches = () => true, ...display } = options;
  return jobProgress((job) => projectId && job.project_id === projectId && stages.some((stage) => (
    stage.endsWith(":") ? job.stage.startsWith(stage) : job.stage === stage
  )) && matches(job), display);
}

export function refreshJobProgress() {
  const jobs = progressJobs();
  for (const panel of document.querySelectorAll(".job-activity")) observers.get(panel)?.(jobs);
  if (clock === null && document.querySelector("[data-job-clock]")) {
    clock = setInterval(() => {
      const clocks = document.querySelectorAll("[data-job-clock]");
      if (!clocks.length) {
        clearInterval(clock);
        clock = null;
      }
      for (const node of clocks) {
        const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(node.dataset.jobClock)) / 1000));
        const text = fmtDuration(seconds);
        if (node.textContent !== text) node.textContent = text;
      }
    }, 1000);
  }
}
