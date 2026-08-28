/**
 * Storyboard screen: scene cards from `GET /api/projects/{id}`.
 *
 * Card contents: index, title, duration, selected backend, seed, visual
 * type, status badge and lock pill. Multi-shot groundwork (Phase 3): each
 * card also shows shot completion from the scene's `shot_summary`
 * ("n/m ready" plus failed/pending counts), lane chips for the ordered
 * shots, and a "renders X" tag when transition overlaps make the rendered
 * duration differ from the planned narration duration. Preview media uses
 * project-scoped local URLs. A legacy `media_base` in config.json is honored
 * only when it is a localhost URL. No base64 and no invented URLs.
 *
 * Actions (all non-idempotent, never retried automatically):
 *  - Generate    — no visual asset yet          (POST .../generate)
 *  - Regenerate  — visual exists, confirmed     (POST .../regenerate)
 *  - Approve     — status generated             (POST .../approve {lock:false})
 *  - Lock        — generated/approved           (POST .../approve {lock:true})
 *  - Unlock      — locked (re-approves, editable again)
 *  - Render scene — live: compile the scene's shots (POST .../render, 202)
 *  - Cancel job  — while a job is running for the scene
 *  - Cancel all  — any active job for the project
 *                  (POST .../jobs/cancel-all)
 *
 * The one intentionally disabled action is "Generate per shot" on scenes with
 * stored (materialized) shots: their visuals live on the shots, so the legacy
 * scene-level generation stands down (the Scene Editor owns them).
 *
 * Single source of truth: scenes with stored (materialized) shots manage
 * visuals per shot, so their scene-level Generate/Regenerate actions stand
 * down here and batch generation skips them entirely.
 *
 * Batch actions queue one backend job per missing visual and run them
 * sequentially, grouping compatible image-model work first (Krea → Ideogram
 * → Qwen) so a model family stays resident across stills and image-motion
 * source frames, then graphic screens and H3 video:
 *  - Generate all (N)          — every unlocked scene without a visual
 *  - All <type> (N)            — only scenes of that visual type
 * Both post to /api/projects/{id}/visuals/batch; existing visuals are never
 * archived. While a batch runs, the bar shows live progress and Cancel;
 * canceling the batch cancels every job it created (queued and in-flight),
 * and "Cancel all" in the header cancels all of the project's active jobs.
 * Per-scene child jobs ("scene_visual") drive each card's progress row.
 *
 * After every action the region reloads from the backend: nothing here is
 * optimistic.
 */

import { el, fmtDuration } from "../dom.js";
import { state, needsProject, latestAssetForScene } from "../state.js";
import {
  getProject,
  generateScene,
  regenerateScene,
  approveScene,
  cancelJob,
  queueVisualBatch,
  cancelAllProjectJobs,
  renderScene,
} from "../api.js";
import { laneChips, shotSummary, sceneHasExplicitShots } from "../shots.js";
import {
  loadingState,
  emptyState,
  errorPanel,
  badge,
  sceneStatusBadge,
  jobStatusBadge,
  confirm,
  toast,
  toastError,
  progress,
} from "../ui.js";
import { navigate, parseRoute } from "../router.js";
import { registerLiveUpdate } from "../app.js";

/** Scene visuals currently being generated from this browser session. */
const pendingVisualSceneIds = new Set();

const BATCH_JOB_TERMINAL = ["completed", "failed", "canceled"];

/** Display labels per backend VisualType value (mirrors scene-editor.js). */
const TYPE_LABELS = {
  graphic_screen: "graphic screens",
  text_overlay_still: "generated backgrounds + exact text",
  h3_audiovisual: "H3 video",
  h3_reference: "H3 reference",
  wan_video: "Wan video",
  krea2_still: "Krea stills",
  qwen_image_still: "Qwen Image text stills",
  flux_still: "Flux stills",
  image_motion: "image motion",
  title_card: "title cards",
  diagram: "diagrams",
  reused_media: "reused media",
  transition_only: "transitions",
  custom: "custom",
};

/** Same grouping the backend uses when ordering a batch queue. */
const BATCH_TYPE_ORDER = [
  "qwen_image_still",
  "krea2_still",
  "text_overlay_still",
  "image_motion",
  "graphic_screen",
  "h3_audiovisual",
];

const IMAGE_MODEL_LABELS = {
  krea: "Krea 2 stills",
  ideogram4_local: "Ideogram text images",
  qwen_image: "Qwen Image stills",
};

function typeLabel(value) {
  return TYPE_LABELS[value] || value || "still";
}

function typeOrder(value) {
  const index = BATCH_TYPE_ORDER.indexOf(value);
  return index === -1 ? BATCH_TYPE_ORDER.length : index;
}

/** Effective still-image backend, matching the server's batch routing.
 * Returns null for Graphic Screen, H3, real media, and other non-image work. */
function effectiveImageModel(scene) {
  const routed = scene.preferred_image_model;
  if (routed && routed !== "automatic") return routed;
  if (scene.needs_embedded_text) return "ideogram4_local";
  if (scene.visual_type === "ideogram4_still") return "ideogram4_local";
  if (scene.visual_type === "qwen_image_still") return "qwen_image";
  if (scene.visual_type === "image_motion"
      && scene.settings && scene.settings.image_motion_source === "qwen_image_2512") {
    return "qwen_image";
  }
  if (["krea2_still", "image_motion", "flux_still"].includes(scene.visual_type)) {
    return "krea";
  }
  return null;
}

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderStoryboard(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Storyboard")),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject(
      "Select a project in the top bar (or create one) to see its storyboard.",
    ));
    return screen;
  }
  screen.append(boardPanel());
  return screen;
}

/**
 * The storyboard panel with a Refresh action and the scene grid.
 * @returns {HTMLElement}
 */
function boardPanel() {
  const body = el("div", { class: "panel-body" });
  const summaryEl = el("span", { class: "muted small" });
  const cancelAllBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button", hidden: true,
  }, "Cancel all");
  const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Refresh");
  const batchBar = el("div", { class: "row", style: { flexWrap: "wrap", alignItems: "center" } });

  /** @type {import("../api.js").Scene[]} */
  let scenes = [];
  /** @type {import("../api.js").Asset[]} */
  let assets = [];
  /** @type {import("../api.js").GenerationJob[]} */
  let jobs = [];

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Storyboard"),
      summaryEl,
      el("span", { class: "spacer" }),
      cancelAllBtn,
      refreshBtn,
    ),
    el("p", { class: "muted small" },
      "Generated previews stream from project-scoped local URLs. Generate a visual to fill a card."),
    batchBar,
    body,
  );

  refreshBtn.onclick = () => load(body);

  /**
   * Active (non-terminal) jobs for the project from the latest snapshot.
   * @returns {import("../api.js").GenerationJob[]}
   */
  function activeJobs() {
    return jobs.filter((j) => !BATCH_JOB_TERMINAL.includes(j.status));
  }

  /**
   * Show "Cancel all (N)" in the header while any project job is active.
   */
  function updateCancelAll() {
    const count = activeJobs().length;
    cancelAllBtn.hidden = count === 0;
    if (count > 0) {
      cancelAllBtn.disabled = false;
      cancelAllBtn.textContent = `Cancel all (${count})`;
      cancelAllBtn.title = `Cancel all ${count} active job(s) for this project. Visuals already generated are kept.`;
    }
  }

  /**
   * Cancel every active job for the project; the backend cancels batch
   * children together with their parent, so one call covers running batches.
   */
  cancelAllBtn.addEventListener("click", async () => {
    cancelAllBtn.disabled = true;
    const count = activeJobs().length;
    const ok = await confirm({
      title: "Cancel all jobs?",
      message: `${count} active job(s) will be canceled, including any running batch and its scene jobs. Visuals already generated are kept.`,
      confirmLabel: "Cancel all",
    });
    if (!ok) {
      cancelAllBtn.disabled = false;
      return;
    }
    try {
      const result = await cancelAllProjectJobs(state.config, state.currentProjectId);
      toast("info", `Canceled ${result.count} job(s)`, "Storyboard");
      load(body);
    } catch (err) {
      toastError(err, "cancel all jobs");
      cancelAllBtn.disabled = false;
    }
  });

  /**
   * Fetch a fresh snapshot and rebuild the grid.
   * @param {HTMLElement} region
   * @param {{skeleton?: boolean}} [opts] — omit the skeleton on live refreshes
   */
  let inflight = 0; // last-write-wins sequence, same pattern as timeline.js
  /** True once real content (or the empty state) has rendered; a transient
   *  error must not replace it, but a first-load failure (only the skeleton
   *  is on screen) still shows the error panel with its Retry action. */
  let hasContent = false;
  async function load(region, { skeleton = true } = {}) {
    const token = ++inflight;
    if (skeleton) region.replaceChildren(loadingState(4));
    try {
      const snap = await getProject(state.config, state.currentProjectId);
      if (token !== inflight) return;
      scenes = snap.scenes || [];
      assets = snap.assets || [];
      jobs = snap.jobs || [];
      const total = scenes.reduce((n, s) => n + (s.duration || 0), 0);
      summaryEl.replaceChildren(`${scenes.length} scenes · ${fmtDuration(total)} total`);
      updateBatchBar();
      updateCancelAll();
      region.replaceChildren(
        scenes.length
          ? el("div", { class: "scene-grid" }, ...scenes.map((s) => sceneCard(s)))
          : emptyState("No scenes yet", "Run planning from the Script screen to draft scenes."),
      );
      hasContent = true;
    } catch (err) {
      if (token !== inflight || hasContent) return;
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load(region) }, "Retry"),
      ));
    }
  }

  /**
   * @param {string} sceneId
   * @returns {import("../api.js").GenerationJob|null}
   */
  function activeJobFor(sceneId) {
    return jobs.find(
      (j) => j.scene_id === sceneId
        && !["completed", "failed", "canceled"].includes(j.status),
    ) || null;
  }

  /**
   * Rebuild the batch action bar from the latest snapshot data.
   * Shows either the running batch (progress + cancel) or queue buttons:
   * "Generate all (N)" plus one button per visual type that still has
   * missing visuals.
   */
  function updateBatchBar() {
    const activeBatch = jobs.find((j) => j.stage === "visual_batch"
      && !BATCH_JOB_TERMINAL.includes(j.status)) || null;
    if (activeBatch) {
      const stageText = activeBatch.parameters && activeBatch.parameters.current_stage;
      const cancelBtn = el("button", {
        class: "btn btn-ghost btn-sm", type: "button",
        title: "Cancel the whole batch and every scene job it created. Scenes already generated are kept.",
      }, "Cancel batch");
      cancelBtn.addEventListener("click", async () => {
        const ok = await confirm({
          title: "Cancel this batch?",
          message: "The batch and every job it created will be canceled, including any in progress. Scenes already generated are kept.",
          confirmLabel: "Cancel batch",
        });
        if (!ok) return;
        try {
          await cancelJob(state.config, activeBatch.id);
          toast("info", "Batch canceled");
          load(body);
        } catch (err) {
          toastError(err, "cancel batch");
        }
      });
      batchBar.replaceChildren(
        jobStatusBadge(activeBatch.status),
        el("span", { class: "muted small" },
          `Generating visuals ${stageText ? `· ${stageText} ` : ""}· ${Math.round((activeBatch.progress || 0) * 100)}%`),
        el("span", { style: { flex: "1 1 160px" } }, progress(activeBatch.progress || 0)),
        cancelBtn,
      );
      return;
    }
    const eligible = scenes.filter((s) => !s.locked
      && !sceneHasExplicitShots(s)
      && !latestAssetForScene(assets, s.id, "visual")
      && !pendingVisualSceneIds.has(s.id)
      && !activeJobFor(s.id));
    if (!eligible.length) {
      batchBar.replaceChildren(el("span", { class: "muted small" },
        "Every unlocked scene has a visual."),
      );
      return;
    }
    const counts = new Map();
    for (const scene of eligible) {
      const key = scene.visual_type || "flux_still";
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    const orderHint = "Queued by image model: Krea → Ideogram → Qwen, then graphic screens → H3 video. Compatible image jobs keep their model resident.";
    const masterBtn = el("button", {
      class: "btn btn-sm", type: "button",
      title: `Queue generation for all ${eligible.length} scene(s) still missing a visual. Existing visuals are never replaced. ${orderHint}`,
    }, `Generate all (${eligible.length})`);
    masterBtn.addEventListener("click", () => queueBatch(null));
    const modelCounts = new Map();
    for (const scene of eligible) {
      const model = effectiveImageModel(scene);
      if (model) modelCounts.set(model, (modelCounts.get(model) || 0) + 1);
    }
    const modelButtons = ["krea", "ideogram4_local"]
      .map((model) => {
        const count = modelCounts.get(model) || 0;
        const label = IMAGE_MODEL_LABELS[model];
        const btn = el("button", {
          class: "btn btn-ghost btn-sm", type: "button",
          disabled: count === 0,
          title: count
            ? `Queue ${count} scene(s) using ${label}. The batch stays on this image-model family where possible.`
            : `No missing scenes currently use ${label}. Exact or long wording belongs in Graphic Screen.`,
        }, `All ${label} (${count})`);
        btn.addEventListener("click", () => queueBatch(null, model));
        return btn;
      });
    /** @type {HTMLElement[]} */
    const typeButtons = [...counts.keys()]
      .sort((a, b) => typeOrder(a) - typeOrder(b) || String(a).localeCompare(String(b)))
      .map((key) => {
        const count = counts.get(key);
        const btn = el("button", {
          class: "btn btn-ghost btn-sm", type: "button",
          title: `Queue generation for ${count} ${typeLabel(key)} scene(s). ${orderHint}`,
        }, `All ${typeLabel(key)} (${count})`);
        btn.addEventListener("click", () => queueBatch(key));
        return btn;
      });
    batchBar.replaceChildren(masterBtn, ...modelButtons, ...typeButtons);
  }

  /**
   * Queue a backend batch job (all types, or one visual_type).
   * @param {string|null} visualType
   * @param {"krea"|"ideogram4_local"|null} imageModel
   */
  async function queueBatch(visualType, imageModel = null) {
    try {
      const job = await queueVisualBatch(state.config, state.currentProjectId,
        visualType ? { visual_type: visualType } : imageModel ? { image_model: imageModel } : {});
      const queued = (job.parameters && job.parameters.scene_ids) || [];
      toast("good",
        visualType ? `Queued all ${typeLabel(visualType)}` : imageModel ? `Queued all ${IMAGE_MODEL_LABELS[imageModel]}` : "Queued all missing visuals",
        `${queued.length} scene(s) · run in order`);
      renderStoryboardRefresh();
    } catch (err) {
      toastError(err, "queue visual batch");
    }
  }

  /**
   * @param {import("../api.js").Scene} scene
   * @returns {HTMLElement}
   */
  function sceneCard(scene) {
    const job = activeJobFor(scene.id);
    const asset = latestAssetForScene(assets, scene.id, "visual");
    const card = el("article", {
      class: "scene-card" + (scene.locked ? " locked" : ""),
      dataset: { sceneId: scene.id },
    });
    card.append(mediaRegion(scene, asset), bodyRegion(scene, job, asset));
    return card;
  }

  // Live path: job-feed frames refresh the grid in place (no skeleton).
  // "Generating…" state comes from pendingVisualSceneIds, which survives
  // rebuilds; everything else is honest backend data.
  registerLiveUpdate(() => load(body, { skeleton: false }));
  load(body);
  return panel;
}

/* ============================================================================
 * Scene card regions (module-level; receive data as parameters)
 * ==========================================================================*/

/**
 * Preview area: real media only when a trusted localhost media_base is
 * configured; otherwise a purposeful placeholder with the stored path.
 * @param {import("../api.js").Scene} scene
 * @param {import("../api.js").Asset|null} asset
 * @returns {HTMLElement}
 */
function mediaRegion(scene, asset) {
  const media = el("div", { class: "media" });
  if (asset) {
    const url = mediaUrl(asset);
    if (url) {
      media.append(
        asset.type === "video"
          ? el("video", { src: url, controls: true, muted: true })
          : el("img", { src: url, alt: `Scene visual: ${scene.title}` }),
      );
    } else {
      media.append(el("div", { class: "media-placeholder" },
        el("span", { class: "kind" }, asset.type || "media"),
        el("span", {
          class: "mono",
          style: { fontSize: "var(--text-xs)", wordBreak: "break-all" },
        }, asset.filepath),
        el("span", { class: "muted small" }, "stored locally - media streaming not available yet"),
      ));
    }
    if (asset.seed != null) media.append(el("span", { class: "seed-chip" }, `seed ${asset.seed}`));
  } else {
    media.append(el("div", { class: "media-placeholder" },
      el("span", { class: "kind" }, scene.visual_type || "no visual"),
      el("span", { class: "muted small" }, "no visual generated yet"),
    ));
  }
  if (scene.locked) media.append(el("span", { class: "lock-pill" }, "Locked"));
  return media;
}

/**
 * Build a media URL from a stored project-relative path. Only a localhost
 * media base (config.json `media_base`) is trusted per the security rules;
 * anything else yields null so the card falls back to the stored path.
 * @param {import("../api.js").Asset} asset
 * @returns {string|null}
 */
function mediaUrl(asset) {
  if (asset && typeof asset.url === "string" && asset.url.startsWith("/api/projects/")) {
    return asset.url;
  }
  const base = state.config.mediaBase;
  const filepath = asset && asset.filepath;
  if (!base || !filepath) return null;
  const url = `${base.replace(/\/+$/, "")}/${String(filepath).replace(/^\/+/, "")}`;
  return /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?(\/|$)/.test(url) ? url : null;
}

/**
 * @param {import("../api.js").Scene} scene
 * @param {import("../api.js").GenerationJob|null} job
 * @param {import("../api.js").Asset|null} asset
 * @returns {HTMLElement}
 */
function bodyRegion(scene, job, asset) {
  const body = el("div", { class: "body" });
  const num = (scene.index ?? 0) + 1;
  body.append(
    el("div", { class: "sc-head" },
      el("span", { class: "sc-num" }, `S${num}`),
      el("span", { class: "sc-title", title: scene.title }, scene.title),
      sceneStatusBadge(scene.status, scene.locked),
    ),
    el("div", { class: "sc-meta" },
      el("span", { class: "tag" }, fmtDuration(scene.duration || 0)),
      el("span", { class: "tag" }, scene.selected_backend === "automatic" ? "auto" : (scene.selected_backend || "?")),
      el("span", { class: "tag" }, scene.visual_type || "still"),
      scene.seed != null && !asset ? el("span", { class: "tag" }, `seed ${scene.seed}`) : null,
    ),
  );
  const shotsRow = buildShotsRow(scene);
  if (shotsRow) body.append(shotsRow);
  if (job) {
    body.append(el("div", { class: "row", style: { alignItems: "center" } },
      jobStatusBadge(job.status),
      el("span", { class: "muted small" }, `scene job · ${Math.round((job.progress || 0) * 100)}%`),
    ));
    body.append(progress(job.progress || 0));
  } else if (scene.status === "failed" || scene.status === "canceled") {
    body.append(el("div", { class: "muted small" },
      `Scene ${scene.status} - generate a visual to recover.`,
    ));
  }
  body.append(el("div", { class: "sc-actions" }, ...actionButtons(scene, job, asset)));
  return body;
}

/**
 * Shot-completion row for a scene card: "n/m ready" plus failed/pending
 * counts from the snapshot's shot_summary, lane chips, and the rendered
 * duration when incoming-transition overlaps shift it off the plan.
 * Returns null when the backend predates the shot contracts (no data).
 * @param {import("../api.js").Scene} scene — snapshot payload incl. shots/shot_summary
 * @returns {HTMLElement|null}
 */
function buildShotsRow(scene) {
  if (!scene.shot_summary && !Array.isArray(scene.shots)) return null;
  const sum = shotSummary(scene);
  if (!sum.count) return null;
  const row = el("div", { class: "sc-meta", role: "group", "aria-label": "Shot completion" });
  const complete = sum.ready >= sum.count && sum.failed === 0;
  row.append(badge(
    complete ? "good" : sum.failed > 0 ? "serious" : "accent",
    `${sum.ready}/${sum.count} ready`,
  ));
  if (sum.failed > 0) row.append(badge("critical", `${sum.failed} failed`));
  if (sum.pending > 0) row.append(badge("neutral", `${sum.pending} pending`));
  if (Array.isArray(scene.shots)) row.append(...laneChips(scene.shots));
  const planned = Number(scene.duration) || 0;
  if (Math.abs(planned - sum.rendered) > 0.05) {
    row.append(el("span", {
      class: "tag",
      title: `Rendered length ${sum.rendered.toFixed(2)} s after subtracting incoming transition overlaps; planned narration duration is ${planned.toFixed(2)} s.`,
    }, `renders ${fmtDuration(sum.rendered)}`));
  }
  return row;
}

/* ============================================================================
 * Actions
 * ==========================================================================*/

/**
 * @param {import("../api.js").Scene} scene
 * @param {import("../api.js").GenerationJob|null} job
 * @param {import("../api.js").Asset|null} asset
 * @returns {HTMLElement[]}
 */
function actionButtons(scene, job, asset) {
  if (job) return [act("Cancel job", () => cancelJobAction(scene, job))];

  const btns = [act("Edit", () => navigate(`#/scene/${scene.id}`),
    { title: "Open the scene editor." })];
  btns.push(act("Render scene",
    () => renderScene(state.config, scene.id),
    { title: "Compile the scene's shots into a rendered video." }));
  if (sceneHasExplicitShots(scene)) {
    // One editable source of truth: with stored shots, visuals live on the
    // shots, so the legacy scene-level generation actions stand down.
    btns.push(el("button", {
      class: "btn btn-sm btn-pending",
      type: "button",
      disabled: true,
      title: "This scene has explicit shots - generate or regenerate visuals per shot in the Scene Editor.",
    }, "Generate per shot"));
    return btns;
  }
  if (pendingVisualSceneIds.has(scene.id)) {
    btns.push(act(asset ? "Regenerating…" : "Generating…", () => {}, {
      title: "A visual request for this scene is already in progress.",
      disabled: true,
    }));
    return btns;
  }
  if (scene.locked) {
    btns.push(act("Unlock", () => approve(scene, false),
      { title: "Re-approves the scene and makes it editable again." }));
  } else if (asset) {
    btns.push(act("Regenerate", () => regenerate(scene),
      {
        title: "Archive the current visual and generate a new one.",
        pendingLabel: "Regenerating…",
      }));
  } else {
    btns.push(act("Generate", () => generate(scene),
      { title: "Generate this scene's visual.", pendingLabel: "Generating…" }));
  }
  if (scene.status === "generated") btns.push(act("Approve", () => approve(scene, false)));
  if (scene.status === "generated" || scene.status === "approved") {
    btns.push(act("Lock", () => approve(scene, true),
      { title: "Lock prevents editing or regeneration until unlocked." }));
  }
  return btns;
}

/**
 * @param {string} label
 * @param {() => (Promise<void>|void)} fn
 * @param {{title?: string, pendingLabel?: string, disabled?: boolean}} [opts]
 * @returns {HTMLElement}
 */
function act(label, fn, opts = {}) {
  const button = el("button", {
    class: "btn btn-sm",
    type: "button",
    title: opts.title || undefined,
    disabled: opts.disabled || false,
  }, label);
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = opts.pendingLabel || label;
    try {
      await fn();
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = label;
    }
  });
  return button;
}

/**
 * @param {import("../api.js").Scene} scene
 * @param {import("../api.js").GenerationJob} job
 */
async function cancelJobAction(scene, job) {
  const ok = await confirm({
    title: "Cancel this job?",
    message: `Cancel the ${job.status} job for scene S${(scene.index ?? 0) + 1}? The scene stays editable.`,
    confirmLabel: "Cancel job",
  });
  if (!ok) return;
  try {
    await cancelJob(state.config, job.id);
    toast("info", "Job canceled", `Scene S${(scene.index ?? 0) + 1}`);
    renderStoryboardRefresh();
  } catch (err) {
    toastError(err, "cancel job");
  }
}

/**
 * @param {import("../api.js").Scene} scene
 */
async function generate(scene) {
  if (pendingVisualSceneIds.has(scene.id)) return;
  pendingVisualSceneIds.add(scene.id);
  try {
    await generateScene(state.config, scene.id);
    toast("good", "Visual generated", `Scene S${(scene.index ?? 0) + 1}`);
  } catch (err) {
    toastError(err, `generate scene S${(scene.index ?? 0) + 1}`);
  } finally {
    pendingVisualSceneIds.delete(scene.id);
    renderStoryboardRefresh();
  }
}

/**
 * @param {import("../api.js").Scene} scene
 */
async function regenerate(scene) {
  const ok = await confirm({
    title: `Regenerate scene S${(scene.index ?? 0) + 1}?`,
    message: "The current visual asset will be archived and a new one generated. This cannot be undone.",
    confirmLabel: "Regenerate",
  });
  if (!ok) return;
  if (pendingVisualSceneIds.has(scene.id)) return;
  pendingVisualSceneIds.add(scene.id);
  try {
    await regenerateScene(state.config, scene.id);
    toast("good", "Scene regenerated", `Scene S${(scene.index ?? 0) + 1}`);
  } catch (err) {
    toastError(err, `regenerate scene S${(scene.index ?? 0) + 1}`);
  } finally {
    pendingVisualSceneIds.delete(scene.id);
    renderStoryboardRefresh();
  }
}

/**
 * @param {import("../api.js").Scene} scene
 * @param {boolean} lock
 */
async function approve(scene, lock) {
  try {
    await approveScene(state.config, scene.id, { lock });
    toast("good", lock ? "Scene locked" : "Scene approved", `Scene S${(scene.index ?? 0) + 1}`);
    renderStoryboardRefresh();
  } catch (err) {
    toastError(err, `approve scene S${(scene.index ?? 0) + 1}`);
  }
}

/**
 * Re-render the whole storyboard from fresh backend data (honest reload).
 */
function renderStoryboardRefresh() {
  // The awaited mutation above may outlive this screen (up to 600s); never
  // clobber whatever route the user navigated to in the meantime.
  if (parseRoute().name !== "storyboard") return;
  const content = document.querySelector(".content");
  if (content) content.replaceChildren(renderStoryboard({ name: "storyboard", param: null }));
}
