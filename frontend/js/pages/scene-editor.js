/**
 * Scene Editor: edit one scene's metadata via `PATCH /api/scenes/{id}` and
 * manage its ordered shots through the multi-shot contracts (Phase 3).
 *
 *  - Fields mirror the backend `SceneEdit` (extra="forbid"): narration,
 *    visual prompt, negative prompt, visual type, model canvas, selected
 *    backend, camera instruction, seed, duration, references.
 *  - The backend dropdown is populated from `GET /api/models` descriptors —
 *    no hardcoded backend IDs; "automatic" is the planner default.
 *  - Locked scenes are read-only: every control is disabled and the lock
 *    is explained (unlock from the Storyboard).
 *  - Saving metadata (PATCH) is explicitly distinct from regenerating the
 *    visual (POST .../regenerate, confirmed, non-idempotent).
 *  - Current and previous visual assets show real generation metadata
 *    (model, version, quantization, workflow, seed, path). No base64.
 *  - Live updates: job-feed ticks refresh the status row, generation
 *    metadata, and button states in place; the PATCH form fields are never
 *    touched, so results appear without navigating and edits survive.
 *
 * Shots (below the narration/settings panels):
 *  - An ordered strip of shot chips plus a selected-shot form fed by
 *    GET/POST `/api/scenes/{id}/shots` and PATCH/DELETE `/api/shots/{id}`:
 *    lane, visual type, prompts, duration_seconds, start_mode,
 *    transition_in kind+duration, seed, and source trim fields; actions for
 *    add, duplicate, move up/down (index PATCH), guarded archive (confirm
 *    dialog; the server's 409 message is shown verbatim), approve/lock.
 *  - Overlay cues per selected shot via the overlay endpoints, covering
 *    kind/exact_text/template, anchor, x/y, width/height, start/duration,
 *    fades, and opacity.
 *  - Shot-scope generate/regenerate and per-scene render queue real backend
 *    jobs. Regeneration is confirmed because it archives current media, and
 *    all three actions require shot-form edits to be saved or reverted first.
 *  - Dirty-form protection: SSE/job-feed ticks never rebuild the strip or
 *    form while the form is dirty or a save is in flight, so in-progress
 *    edits are never overwritten by refreshed data.
 */

import { el, fmtDuration, fmtDate } from "../dom.js";
import { state, needsProject } from "../state.js";
import {
  getProject,
  models,
  editScene,
  generateScene,
  regenerateScene,
  importReusedMedia,
  importShotReusedMedia,
  importSceneGeneratedImage,
  importShotGeneratedImage,
  getGraphicScreen,
  listSceneShots,
  createShot,
  editShot,
  deleteShot,
  approveShot,
  generateShot,
  regenerateShot,
  renderScene,
  addShotOverlay,
  patchShotOverlay,
  removeShotOverlay,
} from "../api.js";
import {
  field,
  setFieldError,
  loadingState,
  errorPanel,
  badge,
  sceneStatusBadge,
  jobStatusBadge,
  confirm,
  toast,
  toastError,
  progress,
  icon,
} from "../ui.js";
import {
  SHOT_LANES,
  TRANSITION_KINDS,
  START_MODES,
  OVERLAY_KINDS,
  OVERLAY_ANCHORS,
  VISUAL_TYPES,
  laneLabel,
  defaultLane,
  isWiredVisualType,
  defaultNewShot,
  sceneHasExplicitShots,
  shotStatusBadge,
  transitionOverlap,
  fmtSecs,
  numOrNull,
} from "../shots.js";
import { navigate, parseRoute } from "../router.js";
import { registerLiveUpdate } from "../app.js";

/** Job statuses that are final (a scene has no running job at these). */
const TERMINAL_JOB_STATUSES = ["completed", "failed", "canceled"];

/**
 * @param {{name: string, param: string | null, param2?: string | null}} route
 * @returns {HTMLElement}
 */
export function renderSceneEditor(route) {
  const sceneId = route.param;
  const requestedShotId = route.param2 || null;
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" },
      el("h1", {}, "Scene Editor"),
      el("div", { class: "screen-actions" },
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => navigate("#/storyboard") }, "Back to Storyboard"),
      ),
    ),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject("Select a project in the top bar to edit its scenes."));
    return screen;
  }
  if (!sceneId) {
    screen.append(errorPanel(
      /** @type {any} */ (new Error("No scene selected.")),
      el("button", { class: "btn", type: "button", onclick: () => navigate("#/storyboard") }, "Go to Storyboard"),
    ));
    return screen;
  }
  screen.append(editorPanel(sceneId, requestedShotId));
  return screen;
}

/**
 * @param {string} sceneId
 * @param {string|null} [requestedShotId] — deep-linked shot to preselect
 * @returns {HTMLElement}
 */
function editorPanel(sceneId, requestedShotId = null) {
  const body = el("div", { class: "panel-body" });
  const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Refresh");
  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Scene"),
      el("span", { class: "spacer" }),
      refreshBtn,
    ),
    body,
  );
  refreshBtn.onclick = () => load(body, sceneId, null);
  load(body, sceneId, requestedShotId);
  return panel;
}

/**
 * Fetch snapshot, then build the two-column editor plus the shots panel.
 * @param {HTMLElement} region
 * @param {string} sceneId
 * @param {string|null} requestedShotId — shot to preselect on first load only
 */
async function load(region, sceneId, requestedShotId = null) {
  region.replaceChildren(loadingState(4));
  /** @type {import("../api.js").ProjectSnapshot} */
  let snap;
  try {
    snap = await getProject(state.config, state.currentProjectId);
  } catch (err) {
    region.replaceChildren(errorPanel(err,
      el("button", { class: "btn", type: "button", onclick: () => load(region, sceneId, requestedShotId) }, "Retry"),
    ));
    return;
  }
  const scene = (snap.scenes || []).find((s) => s.id === sceneId);
  if (!scene) {
    region.replaceChildren(errorPanel(
      /** @type {any} */ (new Error(`Scene not found in this project (id ${sceneId}).`)),
      el("button", { class: "btn", type: "button", onclick: () => navigate("#/storyboard") }, "Back to Storyboard"),
    ));
    return;
  }
  const assets = snap.assets || [];
  const jobs = snap.jobs || [];
  const job = activeJobFor(jobs, sceneId);
  // One editable source of truth: once the scene has stored shots, the
  // legacy scene-level visual recipe and its generation controls are
  // superseded and must not be editable alongside the shots.
  const materialized = sceneHasExplicitShots(scene);

  let form;
  let meta;
  let header;
  let shots;
  try {
    form = buildForm(scene, job, assets, snap.scenes || [], { materialized });
    meta = buildMeta(scene, assets, job);
    header = el("div", { id: "se-header" }, buildStatusRow(scene));
    if (scene.locked) header.append(buildLockedNote());
    shots = shotsController(scene, { initialShotId: requestedShotId });
  } catch (err) {
    region.replaceChildren(errorPanel(err,
      el("button", { class: "btn", type: "button", onclick: () => load(region, sceneId, requestedShotId) }, "Retry"),
    ));
    return;
  }
  region.replaceChildren(
    header,
    el("div", { class: "editor-grid" }, form, meta),
    shots.panel,
  );
  let lastSig = sceneSignature(sceneId, snap);

  // The awaited fetch above may have outlasted this screen; only claim the
  // live-update hook if this scene is still the active route.
  if (parseRoute().name !== "scene-editor" || parseRoute().param !== sceneId) return;

  // Live path: on each job-feed frame, refresh the status row, lock note,
  // generation metadata, and button states in place. The PATCH form fields
  // are never touched, so in-progress edits survive a tick mid-generation.
  let updating = false;
  registerLiveUpdate(async () => {
    if (updating) return;
    updating = true;
    try {
      const fresh = await getProject(state.config, state.currentProjectId);
      const sig = sceneSignature(sceneId, fresh);
      if (sig === lastSig) return;
      lastSig = sig;
      const fs = (fresh.scenes || []).find((s) => s.id === sceneId);
      if (!fs) { load(region, sceneId); return; }
      // A lock change enables/disables the whole PATCH form, so rebuild it
      // rather than patching in place.
      if (fs.locked !== scene.locked) { renderSceneEditorRefresh(sceneId); return; }
      const fAssets = fresh.assets || [];
      const fJob = activeJobFor(fresh.jobs || [], sceneId);
      header.replaceChildren(buildStatusRow(fs));
      if (fs.locked) header.append(buildLockedNote());
      const metaEl = /** @type {HTMLElement|null} */ (region.querySelector("#se-meta"));
      if (metaEl) metaEl.replaceChildren(...buildMeta(fs, fAssets, fJob).children);
      const regenBtn = /** @type {HTMLButtonElement|null} */ (region.querySelector("#se-regen"));
      if (regenBtn) {
        regenBtn.disabled = fs.locked || Boolean(fJob);
        if (regenBtn.textContent !== "Generating…") {
          regenBtn.textContent = hasVisual(fAssets, sceneId) ? "Regenerate visual" : "Generate visual";
        }
      }
      const inspectBtn = /** @type {HTMLButtonElement|null} */ (region.querySelector("#se-inspect"));
      if (inspectBtn) inspectBtn.disabled = fs.visual_type !== "graphic_screen";
      // Shot strip/form: the controller itself refuses to rebuild while its
      // form is dirty or a save is in flight, so SSE frames never clobber
      // in-progress edits.
      shots.refreshQuiet();
    } catch {
      /* the feed delivers the next frame; manual Refresh stays available */
    } finally {
      updating = false;
    }
  });
}

/**
 * Scene status row (title, badge, duration) above the two-column editor.
 * @param {import("../api.js").Scene} scene
 * @returns {HTMLElement}
 */
function buildStatusRow(scene) {
  return el("div", { class: "row", style: { alignItems: "center" } },
    el("span", { class: "sc-title", style: { fontSize: "var(--text-lg)" }, title: scene.title },
      `S${(scene.index ?? 0) + 1} · ${scene.title}`),
    sceneStatusBadge(scene.status, scene.locked),
    el("span", { class: "muted small" }, fmtDuration(scene.duration || 0)),
  );
}

/**
 * The locked-scene explainer shown under the status row when `scene.locked`.
 * @returns {HTMLElement}
 */
function buildLockedNote() {
  return el("div", { class: "readonly-note" },
    "This scene is locked. Unlock it from the Storyboard (or approve it without locking) to make changes.",
  );
}

/**
 * The non-terminal job for a scene, if any (drives the regen button state).
 * @param {import("../api.js").GenerationJob[]} jobs
 * @param {string} sceneId
 * @returns {import("../api.js").GenerationJob|null}
 */
function activeJobFor(jobs, sceneId) {
  return jobs.find((j) => j.scene_id === sceneId && !TERMINAL_JOB_STATUSES.includes(j.status)) || null;
}

/**
 * @param {import("../api.js").Asset[]} assets
 * @param {string} sceneId
 * @returns {boolean}
 */
function hasVisual(assets, sceneId) {
  return assets.some((a) => a.scene_id === sceneId && (a.settings || {}).role === "visual");
}

/**
 * Cheap fingerprint of this scene's live data (job states + asset set) so a
 * feed tick only re-fetches and re-renders when it actually changed.
 * @param {string} sceneId
 * @param {import("../api.js").ProjectSnapshot} snap
 * @returns {string}
 */
function sceneSignature(sceneId, snap) {
  const jobSig = (snap.jobs || []).filter((j) => j.scene_id === sceneId)
    .map((j) => `${j.id}:${j.status}:${Math.round((j.progress || 0) * 100)}`).join("|");
  const assetSig = (snap.assets || []).filter((a) => a.scene_id === sceneId)
    .map((a) => a.created_at).join("|");
  return `${jobSig}::${assetSig}`;
}

/* ============================================================================
 * Form (PATCH fields)
 * ==========================================================================*/


/** H3 canvas presets for AV shots (WIDTHxHEIGHT, 32 px aligned); "auto" = 768-short-edge rule. */
const H3_CANVASES = [
  ["auto", "Auto (768 short edge)"],
  ["1344x768", "1344 x 768 (landscape, full)"],
  ["1152x640", "1152 x 640 (landscape)"],
  ["1024x576", "1024 x 576 (landscape)"],
  ["896x512", "896 x 512 (landscape, compact)"],
  ["768x1344", "768 x 1344 (portrait, full)"],
  ["640x1152", "640 x 1152 (portrait)"],
  ["576x1024", "576 x 1024 (portrait)"],
];

/** Krea 2 Turbo presets stay near one megapixel for reliable 24 GB operation. */
const KREA_CANVASES = [
  ["auto", "Auto (match project aspect)"],
  ["1344x768", "1344 x 768 (landscape)"],
  ["1152x896", "1152 x 896 (landscape)"],
  ["1024x1024", "1024 x 1024 (square)"],
  ["896x1152", "896 x 1152 (portrait)"],
  ["768x1344", "768 x 1344 (portrait)"],
];

/** Official Qwen-Image-2512 aspect presets, capped for 24 GB operation. */
const QWEN_IMAGE_CANVASES = [
  ["auto", "Auto (match project aspect)"],
  ["1664x928", "1664 x 928 (landscape)"],
  ["1328x1328", "1328 x 1328 (square)"],
  ["928x1664", "928 x 1664 (portrait)"],
];

/**
 * @param {import("../api.js").Scene} scene
 * @param {import("../api.js").GenerationJob|null} job
 * @returns {HTMLElement}
 */
/**
 * @param {import("../api.js").Scene} scene
 * @param {import("../api.js").GenerationJob|null} job
 * @param {import("../api.js").Asset[]} assets
 * @param {import("../api.js").Scene[]} allScenes
 * @param {{materialized?: boolean}} [policy] — when the scene has explicit
 *   shots, every legacy scene-level visual/generation control freezes so the
 *   per-shot forms below are the only editable source of truth.
 * @returns {HTMLElement}
 */
function buildForm(scene, job, assets, allScenes, policy = {}) {
  const locked = Boolean(scene.locked);
  const materialized = Boolean(policy.materialized);
  // Visual recipe + generation controls: frozen by an explicit lock, or by
  // materialized shots (superseded recipe).
  const visualLocked = locked || materialized;
  const narration = el("textarea", { id: "se-narration", rows: "6", disabled: locked }, scene.narration || "");
  const visual = el("textarea", { id: "se-visual", rows: "5", disabled: visualLocked }, scene.visual_prompt || "");
  const negative = el("textarea", { id: "se-negative", rows: "3", disabled: visualLocked }, scene.negative_prompt || "");
  const backend = el("select", { id: "se-backend", disabled: visualLocked });
  const visualType = el("select", { id: "se-visual-type", disabled: visualLocked });
  // Wired types list first so what actually runs is at the top; unwired
  // (mock-only) types follow, marked and dimmed where the browser allows.
  for (const wired of [true, false]) {
    for (const mode of VISUAL_TYPES.filter((m) => m.wired === wired)) {
      const attrs = wired ? { value: mode.value } : { value: mode.value, dataset: { unwired: "true" } };
      visualType.append(el("option", attrs, mode.label));
    }
  }
  const currentType = scene.visual_type || "flux_still";
  if (!Array.from(visualType.options).some((o) => o.value === currentType)) {
    visualType.append(el("option", { value: currentType }, currentType));
  }
  visualType.value = currentType;
  const h3Canvas = el("select", { id: "se-h3-canvas", disabled: visualLocked });
  for (const [value, label] of H3_CANVASES) {
    h3Canvas.append(el("option", { value }, label));
  }
  const currentCanvas = scene.settings?.h3_canvas || "auto";
  if (!Array.from(h3Canvas.options).some((o) => o.value === currentCanvas)) {
    h3Canvas.append(el("option", { value: currentCanvas }, currentCanvas));
  }
  h3Canvas.value = currentCanvas;
  const kreaCanvas = el("select", { id: "se-krea-canvas", disabled: visualLocked });
  for (const [value, label] of KREA_CANVASES) {
    kreaCanvas.append(el("option", { value }, label));
  }
  const currentKreaCanvas = scene.settings?.krea_canvas || "auto";
  if (!Array.from(kreaCanvas.options).some((o) => o.value === currentKreaCanvas)) {
    kreaCanvas.append(el("option", { value: currentKreaCanvas }, currentKreaCanvas));
  }
  kreaCanvas.value = currentKreaCanvas;
  const qwenImageCanvas = el("select", { id: "se-qwen-image-canvas", disabled: visualLocked });
  for (const [value, label] of QWEN_IMAGE_CANVASES) {
    qwenImageCanvas.append(el("option", { value }, label));
  }
  const currentQwenImageCanvas = scene.settings?.qwen_image_canvas || "auto";
  if (!Array.from(qwenImageCanvas.options).some((o) => o.value === currentQwenImageCanvas)) {
    qwenImageCanvas.append(el("option", { value: currentQwenImageCanvas }, currentQwenImageCanvas));
  }
  qwenImageCanvas.value = currentQwenImageCanvas;
  const imageMotionSource = el("select", { id: "se-image-motion-source", disabled: locked });
  imageMotionSource.append(
    el("option", { value: "krea2" }, "Krea 2 Turbo"),
    el("option", { value: "qwen_image_2512" }, "Qwen-Image-2512"),
  );
  imageMotionSource.value = scene.settings?.image_motion_source || "krea2";
  const preferredImageModel = el("select", { id: "se-image-model", disabled: visualLocked });
  preferredImageModel.append(
    el("option", { value: "automatic" }, "Automatic"),
    el("option", { value: "krea" }, "Krea 2 Turbo"),
    el("option", { value: "ideogram4_local" }, "Ideogram 4 (local)"),
    el("option", { value: "qwen_image" }, "Qwen-Image-2512"),
  );
  preferredImageModel.value = scene.preferred_image_model || "automatic";
  const ideogramPromptMode = el("select", { id: "se-ideogram-prompt-mode", disabled: visualLocked });
  ideogramPromptMode.append(
    el("option", { value: "quick" }, "Quick Generation"),
    el("option", { value: "precise" }, "Precise Text & Layout"),
  );
  ideogramPromptMode.value = scene.settings?.ideogram_prompt_mode || "quick";
  const ideogramPromptJson = el("textarea", {
    id: "se-ideogram-prompt-json", rows: "16", disabled: visualLocked,
    class: "mono small",
    placeholder: "Paste native Ideogram 4 / KJNodes JSON",
  }, scene.settings?.ideogram_prompt_json
    ? JSON.stringify(scene.settings.ideogram_prompt_json, null, 2)
    : "");
  const needsEmbeddedText = el("input", {
    id: "se-needs-embedded-text", type: "checkbox", disabled: visualLocked,
  });
  needsEmbeddedText.checked = Boolean(scene.needs_embedded_text);
  const textInImage = el("textarea", {
    id: "se-text-in-image", rows: "4", disabled: visualLocked,
    placeholder: "One short in-world phrase per line",
  }, scene.text_in_image || "");
  const textOverlayLayout = el("select", {
    id: "se-text-overlay-layout", disabled: visualLocked,
  });
  textOverlayLayout.append(
    el("option", { value: "auto" }, "Automatic"),
    el("option", { value: "hook" }, "Hook — top and bottom"),
    el("option", { value: "reveal" }, "Reveal — centered title"),
    el("option", { value: "quote" }, "Quotation — quote and citation"),
    el("option", { value: "cta" }, "CTA — stacked safe zones"),
  );
  textOverlayLayout.value = scene.settings?.text_overlay_layout || "auto";
  const cameraInstruction = el("select", {
    id: "se-camera-instruction", disabled: visualLocked,
  });
  cameraInstruction.append(
    el("option", { value: "slow push in" }, "Slow push in"),
    el("option", { value: "slow pull out" }, "Slow pull out"),
    el("option", { value: "pan left" }, "Pan left"),
    el("option", { value: "pan right" }, "Pan right"),
    el("option", { value: "drift up" }, "Drift up"),
    el("option", { value: "drift down" }, "Drift down"),
    el("option", { value: "locked" }, "Static / locked"),
  );
  const currentCameraInstruction = scene.camera_instruction || "locked";
  if (!Array.from(cameraInstruction.options).some((o) => o.value === currentCameraInstruction)) {
    cameraInstruction.append(
      el("option", { value: currentCameraInstruction }, currentCameraInstruction),
    );
  }
  cameraInstruction.value = currentCameraInstruction;
  const seed = el("input", { id: "se-seed", type: "number", min: "0", step: "1", disabled: visualLocked, value: scene.seed != null ? String(scene.seed) : "" });
  const duration = el("input", { id: "se-duration", type: "number", min: "1", step: "any", disabled: locked, value: scene.duration ? String(scene.duration) : "" });
  const references = el("input", { id: "se-references", type: "text", disabled: visualLocked, placeholder: "comma-separated", value: (scene.references || []).join(", ") });
  const reusedMediaFile = el("input", { id: "se-reused-media-file", type: "file", disabled: visualLocked, accept: "image/png,image/jpeg,image/webp,image/bmp,image/gif,image/tiff,video/mp4,video/quicktime,video/webm,video/x-matroska" });
  const reusedMediaTitle = el("input", { id: "se-reused-media-title", type: "text", disabled: visualLocked, placeholder: "Asset title / description" });
  const reusedMediaUrl = el("input", { id: "se-reused-media-url", type: "url", disabled: visualLocked, placeholder: "Source URL (optional; never fetched)" });
  const reusedMediaRights = el("textarea", { id: "se-reused-media-rights", rows: "3", disabled: visualLocked, placeholder: "Optional rights or license note" });
  const reusedMediaImport = el("button", { class: "btn btn-primary", type: "button", disabled: visualLocked }, "Import local media");
  const generatedImageFile = el("input", {
    id: "se-generated-image-file", type: "file", disabled: visualLocked,
    accept: "image/png,image/jpeg,image/webp,image/bmp,image/gif,image/tiff",
  });
  const generatedImageImport = el("button", {
    class: "btn btn-primary", type: "button", disabled: visualLocked,
  }, "Save & import AI image");
  const graphicSettings = scene.settings?.graphic_screen || {};
  const graphicInstructions = el("textarea", { id: "se-graphic-instructions", rows: "4", disabled: visualLocked }, graphicSettings.instructions || "");
  const graphicText = el("textarea", { id: "se-graphic-text", rows: "5", disabled: visualLocked, placeholder: "One exact visible string per line" }, (graphicSettings.exact_text || []).join("\n"));
  const onScreenText = el("textarea", { id: "se-on-screen-text", rows: "5", disabled: visualLocked, placeholder: "One requested visible string per line" }, (scene.settings?.on_screen_text || []).join("\n"));

  const fNarration = field({ label: "Narration", input: narration, hint: "The line this scene speaks in the final script." });
  const fVisual = field({ label: "Visual prompt", input: visual, hint: "What the visual model should draw for this scene." });
  const fNegative = field({ label: "Negative prompt", input: negative, hint: "Optional — recorded with the scene and used by backends with negative conditioning; the Krea 2 Turbo workflow (CFG 1.0, zeroed negative) ignores it, so keep unwanted content out of the visual prompt itself." });
  const fBackend = field({ label: "Visual backend", input: backend, hint: "From GET /api/models; 'automatic' lets the planner choose." });
  const fVisualType = field({ label: "Visual type", input: visualType });
  const fH3Canvas = field({ label: "H3 canvas", input: h3Canvas, hint: "Resolution of the H3 AV shot; smaller fits longer durations on one GPU." });
  const fKreaCanvas = field({ label: "Krea canvas", input: kreaCanvas, hint: "Resolution of the Krea still; auto uses a safe project-aspect preset near one megapixel." });
  const fQwenImageCanvas = field({ label: "Qwen Image canvas", input: qwenImageCanvas, hint: "Official Qwen-Image-2512 aspect preset; auto matches the project." });
  const fPreferredImageModel = field({ label: "Image model", input: preferredImageModel, hint: "Select Krea, local Ideogram 4, or Qwen for either a still or the source frame of Image Motion. Automatic preserves the visual type's legacy routing." });
  const fIdeogramPromptMode = field({ label: "Ideogram prompt mode", input: ideogramPromptMode, hint: "Quick expands the natural-language visual prompt locally. Precise sends validated native/KJNodes structured JSON with exact 0–1000 boxes." });
  const fIdeogramPromptJson = field({ label: "Precise Ideogram JSON", input: ideogramPromptJson, hint: "Native keys only. bbox order is [y_min, x_min, y_max, x_max] on a 0–1000 grid; text fields are preserved exactly." });
  const fImageMotionSource = field({ label: "Automatic motion source", input: imageMotionSource, hint: "Used only when Image model is Automatic." });
  const fNeedsEmbeddedText = field({ label: "Picture needs readable text", input: needsEmbeddedText, hint: "Generated background + exact text renders these words locally after image generation; other image modes ask the model to draw them." });
  const fTextInImage = field({ label: "Exact visible text", input: textInImage, hint: "One distinct text region per line. Generated background + exact text preserves these strings locally and never sends them to the background model." });
  const fTextOverlayLayout = field({ label: "Text layout", input: textOverlayLayout, hint: "Automatic recognizes short reveals, quotations, and full-video CTAs. Choose a preset to control the mobile-safe placement." });
  const fCameraInstruction = field({ label: "Image motion", input: cameraInstruction, hint: "Slow push-in is applied during preview/final rendering; static keeps the generated still locked." });
  const fSeed = field({ label: "Seed", input: seed, hint: "Non-negative integer; blank keeps the current seed." });
  const fDuration = field({ label: "Duration (seconds)", input: duration, hint: "Greater than 0; blank keeps the current duration." });
  const fRefs = field({ label: "References", input: references, hint: "Comma-separated reference file names." });
  const fReusedMedia = field({ label: "Local media", input: reusedMediaFile, hint: "Manually selected local file only. No remote download occurs." });
  const fReusedTitle = field({ label: "Source title", input: reusedMediaTitle, hint: "Required for reused-media provenance." });
  const fReusedUrl = field({ label: "Source URL", input: reusedMediaUrl, hint: "Recorded only; the app never fetches it." });
  const fReusedRights = field({ label: "Rights / license note (optional)", input: reusedMediaRights, hint: "Optional provenance note; leave blank for your own or AI-generated media." });
  const fGeneratedImage = field({ label: "Import existing AI image", input: generatedImageFile, hint: "Optional. Choose a locally generated image, then Save changes; it becomes this scene's current visual without running a model." });
  const fGraphicInstructions = field({ label: "Graphic instructions", input: graphicInstructions, hint: "Layout and design direction for the local Graphic Screen designer." });
  const fGraphicText = field({ label: "Exact on-screen text", input: graphicText, hint: "One visible string per line. These lines must exactly match the rendered screen." });
  const fOnScreenText = field({ label: "Requested in-image text", input: onScreenText, hint: "Qwen will try to spell each line exactly. Review the result: generated lettering is not deterministic." });
  const h3Quality = el("select", { id: "se-h3-quality", disabled: visualLocked });
  const h3LongShot = el("input", { id: "se-h3-long-shot", type: "checkbox", disabled: visualLocked });
  const effectiveDurationHint = el("span", { class: "muted small", id: "se-h3-duration-hint" });
  const continuityToggle = el("input", { id: "se-h3-continuity-enabled", type: "checkbox", disabled: visualLocked });
  const predecessorSelect = el("select", { id: "se-h3-predecessor", disabled: visualLocked });
  predecessorSelect.append(el("option", { value: "" }, "None (first in group)"));
  const continuityGroup = el("input", { id: "se-h3-continuity-group", type: "text", disabled: visualLocked, placeholder: "auto-assigned when blank" });
  const continuityStatus = el("div", { class: "muted small", id: "se-h3-continuity-status" });
  const h3DurationRow = el("div", { class: "row", id: "se-h3-duration-row" }, effectiveDurationHint);
  const continuityRow = el("div", { class: "grid-2", id: "se-h3-continuity-row" });
  const h3GroupRow = el("div", { class: "row", id: "se-h3-group-row" });
  const h3StatusRow = el("div", { class: "row", id: "se-h3-status-row" }, continuityStatus);
  const fH3Quality = field({ label: "H3 quality", input: h3Quality, hint: "Preset-driven resolution and duration policy. Custom uses the explicit canvas." });
  const fH3LongShot = field({ label: "Long shot (Fast / Safe only)", input: h3LongShot, hint: "Up to 20 s; slower to retry and cannot isolate a bad section." });
  const fContinuityToggle = field({ label: "Continue from previous H3 shot", input: continuityToggle, hint: "Chain this scene after an earlier H3 audiovisual scene." });
  const fPredecessorSelect = field({ label: "Predecessor scene", input: predecessorSelect, hint: "Earlier H3 scene in this project whose last frame seeds this shot." });
  const fContinuityGroup = field({ label: "Continuity group", input: continuityGroup, hint: "Optional. Auto-assigned from the predecessor when blank." });

  function updateVisualTypeDetails() {
    const mode = VISUAL_TYPES.find((item) => item.value === visualType.value);
    let hint = fVisualType.querySelector(".hint");
    if (!hint) {
      hint = el("div", { class: "hint", id: "se-visual-type-hint" });
      fVisualType.append(hint);
      visualType.setAttribute("aria-describedby", hint.id);
    }
    hint.textContent = mode?.description || "This project contains an unknown visual type; regeneration support cannot be determined.";
    const imageMotion = visualType.value === "image_motion";
    const exactTextComposite = visualType.value === "text_overlay_still";
    const routableImage = ["text_overlay_still", "image_motion", "krea2_still", "ideogram4_still", "qwen_image_still", "flux_still"].includes(visualType.value);
    const preferred = preferredImageModel.value;
    const effectiveModel = preferred !== "automatic"
      ? preferred
      : (exactTextComposite
        ? "krea"
        : visualType.value === "ideogram4_still"
        ? "ideogram4_local"
        : needsEmbeddedText.checked
        ? "ideogram4_local"
        : (visualType.value === "qwen_image_still"
          || (imageMotion && imageMotionSource.value === "qwen_image_2512")
          ? "qwen_image" : "krea"));
    fH3Canvas.hidden = visualType.value !== "h3_audiovisual";
    fPreferredImageModel.hidden = !routableImage;
    fImageMotionSource.hidden = !imageMotion || preferred !== "automatic";
    fNeedsEmbeddedText.hidden = !routableImage;
    fTextInImage.hidden = !routableImage
      || !(exactTextComposite || needsEmbeddedText.checked
        || ["ideogram4_local", "qwen_image"].includes(effectiveModel));
    fTextOverlayLayout.hidden = !exactTextComposite;
    fIdeogramPromptMode.hidden = exactTextComposite || !routableImage || effectiveModel !== "ideogram4_local";
    fIdeogramPromptJson.hidden = fIdeogramPromptMode.hidden || ideogramPromptMode.value !== "precise";
    fKreaCanvas.hidden = !routableImage || effectiveModel !== "krea";
    fQwenImageCanvas.hidden = !routableImage || effectiveModel !== "qwen_image";
    fCameraInstruction.hidden = !imageMotion;
    fGraphicInstructions.hidden = visualType.value !== "graphic_screen";
    fGraphicText.hidden = visualType.value !== "graphic_screen";
    // Preserve the legacy Qwen-only settings field for automatic legacy
    // scenes; explicit routed models use the common text_in_image field.
    fOnScreenText.hidden = !(visualType.value === "qwen_image_still" && preferred === "automatic");
    const isReused = visualType.value === "reused_media";
    fReusedMedia.hidden = !isReused;
    fReusedTitle.hidden = !isReused;
    fReusedUrl.hidden = !isReused;
    fReusedRights.hidden = !isReused;
    reusedMediaImport.hidden = !isReused;
    fGeneratedImage.hidden = !routableImage || exactTextComposite;
    generatedImageImport.hidden = !routableImage || exactTextComposite;
  }
  visualType.addEventListener("change", () => {
    if (visualType.value === "image_motion") {
      const normalized = cameraInstruction.value.trim().toLowerCase();
      if (["", "locked", "locked-off", "none", "no motion", "static"].includes(normalized)) {
        cameraInstruction.value = "slow push in";
      }
    } else if (["text_overlay_still", "krea2_still", "ideogram4_still", "qwen_image_still"].includes(visualType.value)) {
      cameraInstruction.value = "locked";
    }
    if (visualType.value === "text_overlay_still") {
      needsEmbeddedText.checked = true;
      if (preferredImageModel.value === "automatic") preferredImageModel.value = "krea";
    }
    if (visualType.value === "ideogram4_still") {
      preferredImageModel.value = "ideogram4_local";
    }
    if (visualType.value === "h3_audiovisual") {
      updateH3QualityOptions();
      if (currentType !== "h3_audiovisual") h3Quality.value = "standard";
      updateH3Details();
    }
    updateVisualTypeDetails();
  });
  imageMotionSource.addEventListener("change", updateVisualTypeDetails);
  preferredImageModel.addEventListener("change", updateVisualTypeDetails);
  needsEmbeddedText.addEventListener("change", updateVisualTypeDetails);
  ideogramPromptMode.addEventListener("change", updateVisualTypeDetails);

  reusedMediaImport.addEventListener("click", () => {
    if (!(reusedMediaFile.files && reusedMediaFile.files[0])) {
      toast("critical", "Choose a local file", "Select an image or video before importing reused media.");
      return;
    }
    doSave();
  });
  generatedImageImport.addEventListener("click", () => {
    if (!(generatedImageFile.files && generatedImageFile.files[0])) {
      toast("critical", "Choose an AI image", "Select a generated image before importing it.");
      return;
    }
    doSave();
  });

  let h3Policy = null;
  populatePredecessorOptions(allScenes);
  initializeH3Fields();
  // The change handlers own conditional visibility, so the initial render must
  // apply the same rules once; otherwise every field group starts visible for
  // scenes that should hide them.
  updateVisualTypeDetails();
  if (visualType.value !== "h3_audiovisual") updateH3Details();

  // Populate backend options from the live registry (GET /api/models).
  (async () => {
    backend.append(el("option", { value: "automatic" }, "automatic"));
    const current = scene.selected_backend || "automatic";
    try {
      const list = await models(state.config);
      for (const name of Object.keys(list.models || {})) {
        const d = list.models[name];
        backend.append(el("option", { value: name }, `${name}${d.quantization ? ` (${d.quantization})` : ""}`));
      }
    } catch {
      /* offline: fall back to automatic + current value below */
    }
    const hasCurrent = Array.from(backend.options).some((o) => o.value === current);
    if (!hasCurrent) backend.append(el("option", { value: current }, current));
    backend.value = current;
  })();

  (async () => {
    try {
      const res = await fetch(`${state.config.apiBase}/api/h3/policy`);
      if (res.ok) h3Policy = await res.json();
      if (visualType.value === "h3_audiovisual") {
        updateH3QualityOptions();
        updateH3Details();
      }
    } catch {
      /* offline: keep null; defaults apply */
    }
  })();

  function populatePredecessorOptions(allScenes) {
    predecessorSelect.innerHTML = '<option value="">None (first in group)</option>';
    const currentId = scene.id;
    const currentIndex = scene.index ?? 0;
    (allScenes || [])
      .filter(s => s.visual_type === "h3_audiovisual" && s.id !== currentId && (s.index ?? 0) < currentIndex)
      .forEach(s => {
        const label = `S${(s.index ?? 0) + 1} — ${s.title || "untitled"}`;
        predecessorSelect.append(el("option", { value: s.id }, label));
      });
  }

  function currentH3Quality() {
    const canvas = scene.settings?.h3_canvas;
    if (canvas && canvas !== "auto") return "custom";
    const raw = scene.settings?.h3_quality;
    if (raw && h3Policy?.presets?.[raw]) return raw;
    return currentType === "h3_audiovisual" ? "high" : "standard";
  }

  function updateH3QualityOptions() {
    h3Quality.innerHTML = "";
    const presets = h3Policy?.presets || {};
    const defaultQuality = currentH3Quality();
    const options = [
      { value: "fast_safe", label: "Fast / Safe" },
      { value: "standard", label: "Standard" },
      { value: "high", label: "High" },
      { value: "custom", label: "Custom" },
    ];
    for (const opt of options) {
      const p = presets[opt.value];
      const label = p ? `${opt.label} (${p.evidence || ""})` : opt.label;
      h3Quality.append(el("option", { value: opt.value }, label));
    }
    if (!Array.from(h3Quality.options).some(o => o.value === defaultQuality)) {
      h3Quality.append(el("option", { value: defaultQuality }, defaultQuality));
    }
    h3Quality.value = defaultQuality;
    updateH3Details();
  }

  function updateH3Details() {
    const quality = h3Quality.value;
    const preset = h3Policy?.presets?.[quality];
    const isCustom = quality === "custom";
    const longShotAllowed = preset?.long_shot_allowed ?? quality === "fast_safe";
    h3LongShot.disabled = visualLocked || !longShotAllowed || isCustom;
    if (isCustom || (preset && !preset.long_shot_allowed)) {
      h3LongShot.checked = false;
    }
    const requested = Number(duration.value);
    let hint = "";
    if (preset && Number.isFinite(requested) && requested > 0) {
      const fps = h3Policy?.fps || 24;
      const step = h3Policy?.frame_grid_step || 17;
      const offset = h3Policy?.frame_grid_offset || 5;
      let frames = Math.max(5, Math.round(requested * fps));
      while (frames % step !== offset) frames += 1;
      const effective = (frames / fps).toFixed(3);
      const maxS = h3LongShot.checked
        ? (h3Policy?.long_shot_max_seconds || 20)
        : preset.max_seconds;
      hint = `Effective: ${frames} frames ≈ ${effective} s (${fps} fps, ${step}k+${offset} grid).`;
      if (requested > maxS) {
        hint += ` Preset cap is ${maxS} s.`;
      }
    }
    effectiveDurationHint.textContent = hint;
    const isH3 = visualType.value === "h3_audiovisual";
    fH3Canvas.hidden = !isH3;
    fH3Quality.hidden = !isH3;
    fH3LongShot.hidden = !isH3;
    h3DurationRow.hidden = !isH3;
    continuityRow.hidden = !isH3;
    h3GroupRow.hidden = !isH3;
    h3StatusRow.hidden = !isH3;
  }

  h3Quality.addEventListener("change", () => {
    if (h3Quality.value !== "custom") h3Canvas.value = "auto";
    updateH3Details();
  });
  h3Canvas.addEventListener("change", () => {
    if (h3Canvas.value !== "auto") h3Quality.value = "custom";
    else if (h3Quality.value === "custom") h3Quality.value = "standard";
    updateH3Details();
  });
  h3LongShot.addEventListener("change", updateH3Details);
  duration.addEventListener("input", updateH3Details);

  continuityToggle.addEventListener("change", () => {
    const enabled = continuityToggle.checked;
    predecessorSelect.disabled = visualLocked || !enabled;
    continuityGroup.disabled = visualLocked || !enabled;
    if (!enabled) {
      predecessorSelect.value = "";
      continuityGroup.value = "";
    }
  });

  function initializeH3Fields() {
    const isH3 = visualType.value === "h3_audiovisual";
    if (!isH3) return;
    updateH3QualityOptions();
    h3LongShot.checked = Boolean(scene.settings?.h3_long_shot);
    updateH3Details();
    const block = scene.settings?.h3_continuity || {};
    continuityToggle.checked = Boolean(block.enabled);
    if (block.predecessor_scene_id) {
      predecessorSelect.value = block.predecessor_scene_id;
    }
    if (block.group) {
      continuityGroup.value = block.group;
    }
    continuityToggle.dispatchEvent(new Event("change"));
    const h3Status = scene.h3;
    if (h3Status && h3Status.status) {
      continuityStatus.textContent = `Continuity: ${h3Status.status}${h3Status.detail ? " — " + h3Status.detail : ""}`;
    }
  }

  const saveBtn = el("button", { class: "btn btn-primary", type: "button", disabled: locked }, "Save changes");
  const currentVisual = hasVisual(assets, scene.id);
  // Generation controls exist only while the scene-level recipe is the
  // source of truth; materialized shots own their visuals instead.
  const regenBtn = materialized ? null : el("button", { class: "btn", id: "se-regen", type: "button", disabled: locked || Boolean(job) }, currentVisual ? "Regenerate visual" : "Generate visual");
  const supersededNote = materialized
    ? el("div", { class: "readonly-note" },
        "This scene has explicit shots, so its per-shot settings are the editable source of truth. The scene-level visual recipe below is kept for compatibility (previews, legacy projects) and is disabled here; manage visuals in the Shots strip below.",
      )
    : null;
  const inspectBtn = el("button", { class: "btn btn-ghost", id: "se-inspect", type: "button", disabled: scene.visual_type !== "graphic_screen" }, "Inspect source");
  const errRegion = el("div", { class: "mt" });
  const inspection = el("div", { class: "mt" });

  function validate() {
    setFieldError(fDuration, null);
    setFieldError(fSeed, null);
    setFieldError(fH3Canvas, null);
    setFieldError(fIdeogramPromptJson, null);
    const d = Number(duration.value);
    if (duration.value.trim() !== "" && (!Number.isFinite(d) || d <= 0)) {
      setFieldError(fDuration, "Duration must be greater than 0.");
      return false;
    }
    if (!materialized && visualType.value === "h3_audiovisual" && duration.value.trim() !== "" && Number.isFinite(d)) {
      if (h3Quality.value === "custom" && h3Canvas.value === "auto") {
        setFieldError(fH3Canvas, "Custom quality requires an explicit H3 canvas.");
        return false;
      }
      const preset = h3Policy?.presets?.[h3Quality.value];
      const maxSeconds = h3LongShot.checked ? 20 : (preset ? preset.max_seconds : 20);
      if (d > maxSeconds) {
        setFieldError(fDuration, `Duration exceeds ${maxSeconds} s cap for ${preset?.label || "this preset"}. Use Fast / Safe for longer shots.`);
        return false;
      }
      if (h3LongShot.checked && preset && !preset.long_shot_allowed) {
        setFieldError(fDuration, "Long shot is only allowed with Fast / Safe.");
        return false;
      }
    }
    const s = seed.value.trim();
    if (s !== "" && (!Number.isInteger(Number(s)) || Number(s) < 0)) {
      setFieldError(fSeed, "Seed must be a non-negative integer.");
      return false;
    }
    if (!fIdeogramPromptMode.hidden && ideogramPromptMode.value === "precise") {
      if (!ideogramPromptJson.value.trim()) {
        setFieldError(fIdeogramPromptJson, "Precise mode requires native Ideogram JSON.");
        return false;
      }
      try {
        const parsed = JSON.parse(ideogramPromptJson.value);
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("root");
      } catch (_err) {
        setFieldError(fIdeogramPromptJson, "Enter one valid JSON object; field/schema validation runs when saved.");
        return false;
      }
    }
    return true;
  }

  let saving = false;
  function sceneEditBody() {
    // With materialized shots the legacy visual recipe is frozen: send only
    // the still-authoritative scene fields (narration clock, duration) so a
    // save can never quietly rewrite shot-superseded recipe values.
    if (materialized) {
      return {
        narration: narration.value,
        duration: duration.value.trim() === "" ? undefined : Number(duration.value),
      };
    }
    const refs = references.value.split(",").map((x) => x.trim()).filter(Boolean);
    return {
      narration: narration.value,
      visual_prompt: visual.value,
      negative_prompt: negative.value,
      visual_type: visualType.value,
      needs_embedded_text: needsEmbeddedText.checked,
      text_in_image: textInImage.value,
      text_overlay_layout: visualType.value === "text_overlay_still"
        ? textOverlayLayout.value : undefined,
      preferred_image_model: preferredImageModel.value,
      ideogram_prompt_mode: !fIdeogramPromptMode.hidden ? ideogramPromptMode.value : undefined,
      ideogram_prompt_json: !fIdeogramPromptJson.hidden
        ? JSON.parse(ideogramPromptJson.value)
        : undefined,
      h3_canvas: h3Canvas.value,
      krea_canvas: kreaCanvas.value,
      qwen_image_canvas: qwenImageCanvas.value,
      image_motion_source: visualType.value === "image_motion" ? imageMotionSource.value : undefined,
      selected_backend: backend.value,
      camera_instruction: cameraInstruction.value,
      // Blank means "keep the current value": the key is omitted (undefined
      // is dropped by JSON.stringify) instead of sending an explicit null
      // that the backend's exclude_none would silently ignore.
      seed: seed.value.trim() === "" ? undefined : Number(seed.value),
      duration: duration.value.trim() === "" ? undefined : Number(duration.value),
      references: refs,
      h3_quality: visualType.value === "h3_audiovisual" ? h3Quality.value : undefined,
      h3_long_shot: visualType.value === "h3_audiovisual" ? h3LongShot.checked : undefined,
      h3_continuity:
        visualType.value === "h3_audiovisual"
          ? (continuityToggle.checked
              ? {
                  enabled: true,
                  group: continuityGroup.value.trim() || undefined,
                  predecessor_scene_id: predecessorSelect.value || undefined,
                }
              : { enabled: false })
          : undefined,
      graphic_instructions: visualType.value === "graphic_screen" ? graphicInstructions.value : undefined,
      graphic_text: visualType.value === "graphic_screen"
        ? (graphicText.value === "" ? [] : graphicText.value.split("\n"))
        : undefined,
      on_screen_text: visualType.value === "qwen_image_still"
        ? (onScreenText.value === "" ? [] : onScreenText.value.split("\n"))
        : undefined,
    };
  }

  /** Persist the current form. Returns false after displaying a validation/API error. */
  async function persistChanges({ refresh = false, notify = false } = {}) {
    if (saving || locked) return;
    if (!validate()) return false;
    saving = true;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    errRegion.replaceChildren();
    try {
      await editScene(state.config, scene.id, sceneEditBody());
      if (notify) toast("good", "Scene saved", `S${(scene.index ?? 0) + 1}`);
      if (refresh) renderSceneEditorRefresh(scene.id);
      return true;
    } catch (err) {
      errRegion.replaceChildren(errorPanel(err));
      return false;
    } finally {
      saving = false;
      saveBtn.disabled = locked;
      saveBtn.textContent = "Save changes";
    }
  }

  /** Save metadata only (PATCH) — never regenerates the visual. */
  async function doSave() {
    const reusedFile = visualType.value === "reused_media"
      ? (reusedMediaFile.files && reusedMediaFile.files[0]) : null;
    const generatedFile = !fGeneratedImage.hidden
      ? (generatedImageFile.files && generatedImageFile.files[0]) : null;
    const title = reusedMediaTitle.value.trim();
    const licenseNote = reusedMediaRights.value.trim();
    if (reusedFile && !title) {
      toast("critical", "Source title required", "Provide a short title for the selected reused-media file.");
      return;
    }
    const saved = await persistChanges();
    if (saved !== true) return;
    const importingFile = Boolean(reusedFile || generatedFile);
    if (importingFile) {
      saving = true;
      saveBtn.disabled = true;
      saveBtn.textContent = "Importing…";
      reusedMediaImport.disabled = true;
      generatedImageImport.disabled = true;
    }
    try {
      if (reusedFile) {
        await importReusedMedia(state.config, scene.id, reusedFile, {
          title,
          source_url: reusedMediaUrl.value.trim(),
          license_note: licenseNote,
          classification: "documentary_evidence",
        });
        toast("good", "Scene saved and media attached", "The reused-media scene is ready to render locally.");
      } else if (generatedFile) {
        await importSceneGeneratedImage(state.config, scene.id, generatedFile);
        toast("good", "Scene saved and AI image attached", "The imported image is now the scene's current visual.");
      } else {
        toast("good", "Scene saved", `S${(scene.index ?? 0) + 1}`);
      }
      renderSceneEditorRefresh(scene.id);
    } catch (err) {
      errRegion.replaceChildren(errorPanel(err));
      toastError(err, reusedFile ? "import reused media" : "import AI image");
    } finally {
      if (importingFile) {
        saving = false;
        saveBtn.disabled = locked;
        saveBtn.textContent = "Save changes";
        reusedMediaImport.disabled = visualLocked;
        generatedImageImport.disabled = visualLocked;
      }
    }
  }

  let regenerating = false;
  /** Regenerate the visual (non-idempotent; confirmed). Legacy scenes only. */
  async function doRegenerate() {
    if (regenerating || locked || materialized || !regenBtn) return;
    if (currentVisual) {
      const ok = await confirm({
        title: `Regenerate scene S${(scene.index ?? 0) + 1}?`,
        message: "The current visual asset will be archived and a new one generated from the prompts below. This cannot be undone.",
        confirmLabel: "Regenerate",
      });
      if (!ok) return;
    }
    regenerating = true;
    regenBtn.disabled = true;
    regenBtn.textContent = "Generating…";
    try {
      // Generation reads persisted scene state. Save first so the exact text and instructions
      // currently visible in this form are the values sent to the local graphic designer.
      const saved = await persistChanges();
      if (!saved) return;
      if (currentVisual) await regenerateScene(state.config, scene.id);
      else await generateScene(state.config, scene.id);
      toast("good", currentVisual ? "Scene regenerated" : "Scene generated", `S${(scene.index ?? 0) + 1}`);
      renderSceneEditorRefresh(scene.id);
    } catch (err) {
      toastError(err, `regenerate scene S${(scene.index ?? 0) + 1}`);
    } finally {
      regenerating = false;
      if (regenBtn) {
        regenBtn.disabled = locked || Boolean(job);
        regenBtn.textContent = currentVisual ? "Regenerate visual" : "Generate visual";
      }
    }
  }

  saveBtn.onclick = doSave;
  if (regenBtn) regenBtn.onclick = doRegenerate;
  inspectBtn.onclick = async () => {
    inspection.replaceChildren(loadingState(2));
    try {
      const approved = await getGraphicScreen(state.config, scene.id);
      // `el` inserts text nodes, so the approved source is escaped and read-only.
      inspection.replaceChildren(el("details", { open: true },
        el("summary", {}, "Approved manifest and source (read-only)"),
        el("h3", {}, "Manifest"),
        el("pre", { class: "mono small" }, JSON.stringify(approved.manifest, null, 2)),
        el("h3", {}, "Sanitized HTML"),
        el("pre", { class: "mono small" }, approved.source),
      ));
    } catch (err) {
      inspection.replaceChildren(errorPanel(err));
    }
  };

  // With materialized shots only the still-authoritative scene fields stay
  // rendered (narration clock + duration); the superseded visual recipe
  // rows are removed outright rather than shown as dead controls.
  const metaRows = materialized
    ? [el("div", { class: "grid-2" }, fNarration, fDuration)]
    : [
        el("div", { class: "grid-2" }, fNarration, fVisual),
        el("div", { class: "grid-2" }, fNegative, fBackend),
        el("div", { class: "grid-2" }, fVisualType, fH3Canvas),
        el("div", { class: "grid-2" }, fPreferredImageModel, fNeedsEmbeddedText),
        el("div", { class: "grid-2" }, fTextInImage, fTextOverlayLayout),
        el("div", { class: "grid-2" }, fIdeogramPromptMode),
        el("div", { class: "grid-2" }, fIdeogramPromptJson),
        el("div", { class: "grid-2" }, fH3Quality, fH3LongShot),
        h3DurationRow,
        el("div", { class: "grid-2" }, fImageMotionSource),
        el("div", { class: "grid-2" }, fKreaCanvas, fCameraInstruction),
        el("div", { class: "grid-2" }, fQwenImageCanvas, fOnScreenText),
        el("div", { class: "grid-2" }, fDuration, fSeed),
        el("div", { class: "grid-2" }, fRefs),
        el("div", { class: "grid-2" }, fReusedMedia, fReusedTitle),
        el("div", { class: "grid-2" }, fReusedUrl, fReusedRights),
        el("div", { class: "row" }, reusedMediaImport),
        el("div", { class: "grid-2" }, fGeneratedImage),
        el("div", { class: "row" }, generatedImageImport),
        el("div", { class: "grid-2" }, fGraphicInstructions, fGraphicText),
        continuityRow.append(fContinuityToggle, fPredecessorSelect), h3GroupRow.append(fContinuityGroup),
        h3StatusRow,
      ];

  const actionButtons = materialized
    ? [saveBtn]
    : [saveBtn, /** @type {HTMLElement} */ (regenBtn), inspectBtn];

  return el("div", { class: "panel" },
    supersededNote,
    el("div", { class: "panel-title" }, materialized ? "Scene (narration & timing)" : "Metadata (PATCH)"),
    el("div", { class: "panel-body" },
      ...metaRows,
      el("div", { class: "row mt" }, ...actionButtons),
      errRegion,
      inspection,
    ),
  );
}

/* ============================================================================
 * Generation metadata (current + previous visual assets)
 * ==========================================================================*/

/**
 * @param {import("../api.js").Scene} scene
 * @param {import("../api.js").Asset[]} assets
 * @param {import("../api.js").GenerationJob|null} job
 * @returns {HTMLElement}
 */
function buildMeta(scene, assets, job) {
  const visual = assets
    .filter((a) => a.scene_id === scene.id && (a.settings || {}).role === "visual")
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  const current = visual[0] || null;
  const previous = visual[1] || null;

  const parts = [];
  if (job) {
    parts.push(
      el("div", { class: "row", style: { alignItems: "center" } },
        jobStatusBadge(job.status),
        el("span", { class: "muted small" }, `job running · ${Math.round((job.progress || 0) * 100)}%`),
      ),
      progress(job.progress || 0),
    );
  }
  parts.push(assetBlock("Current visual", current));
  if (previous) parts.push(assetBlock("Previous visual (archived)", previous));
  if (!current && !previous && !job) {
    parts.push(el("div", { class: "muted small" }, "No visual has been generated for this scene yet."));
  }
  return el("div", { class: "panel", id: "se-meta" },
    el("div", { class: "panel-title" }, "Generation metadata"),
    el("div", { class: "panel-body stack" }, ...parts),
  );
}

/**
 * @param {string} title
 * @param {import("../api.js").Asset|null} asset
 * @returns {HTMLElement}
 */
function assetBlock(title, asset) {
  if (!asset) return el("div", { class: "muted small" }, `${title}: none.`);
  const rows = [
    ["Model", asset.model || asset.backend || "?"],
    ["Model version", asset.model_version || "—"],
    ["Quantization", asset.quantization || "—"],
    ["Workflow", asset.workflow_version || "—"],
    ["Seed", asset.seed != null ? String(asset.seed) : "—"],
    ["Created", asset.created_at ? fmtDate(asset.created_at) : "—"],
  ];
  return el("div", { class: "stack" },
    el("h3", {}, title),
    asset.url ? el("img", {
      src: asset.url, alt: `${title} preview`,
      style: { maxWidth: "100%", maxHeight: "260px", objectFit: "contain" },
    }) : null,
    el("dl", { class: "kv" },
      ...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]),
    ),
    el("div", { class: "mono small muted", title: asset.filepath || "" }, asset.filepath || ""),
  );
}

/**
 * Re-render the editor from fresh backend data after a mutation.
 * @param {string} sceneId
 */
function renderSceneEditorRefresh(sceneId) {
  // The awaited mutation above may outlive this screen; never clobber
  // whatever route the user navigated to in the meantime.
  const route = parseRoute();
  if (route.name !== "scene-editor" || route.param !== sceneId) return;
  const content = document.querySelector(".content");
  if (content) content.replaceChildren(renderSceneEditor({ name: "scene-editor", param: sceneId }));
}

/* ============================================================================
 * Shots: ordered strip, selected-shot form, overlay cues (Phase 3)
 *
 * Data comes from GET /api/scenes/{id}/shots; mutations go through the shot
 * and overlay endpoints in api.js. The controller deliberately never
 * rebuilds the strip or the selected-shot form while that form is dirty or a
 * save is in flight, so live SSE/job-feed ticks cannot overwrite edits.
 * ==========================================================================*/

/**
 * Build the shot management panel for one scene.
 * @param {import("../api.js").Scene} scene — snapshot payload (locked, index, duration)
 * @param {{initialShotId?: string|null}} [opts] — deep-linked shot to select
 * @returns {{panel: HTMLElement, refreshQuiet: () => Promise<void>}}
 */
function shotsController(scene, opts = {}) {
  const sceneLocked = Boolean(scene.locked);
  const sceneNumber = (scene.index ?? 0) + 1;
  const plannedDuration = Number(scene.duration) || 0;
  const initialShotId = opts.initialShotId || null;

  let shots = [];
  let meta = /** @type {any} */ ({ count: 0, rendered_duration_seconds: 0 });
  let selectedId = null;
  let selectionInitialized = false;
  let dirty = false;
  let saving = false;
  let overlayFormOpen = false;
  let inflight = 0;
  let quietBusy = 0;
  let rendering = false;
  let lastSig = "";

  const summarySlot = el("span", { class: "row", style: { flexWrap: "wrap" } });
  const addBtn = el("button", {
    class: "btn btn-sm", type: "button",
    disabled: sceneLocked,
    title: "Append a new shot at the end of the scene.",
  }, "Add shot");
  const renderSceneBtn = el("button", {
    class: "btn btn-sm",
    type: "button",
    disabled: true,
    title: "Compile all ready shot media and overlays into this scene's rendered video.",
  }, "Render scene");
  const strip = el("div", { class: "shot-strip", role: "group", "aria-label": `Ordered shots of scene S${sceneNumber}` });
  const detailErr = el("div", { role: "alert" });
  const detailWrap = el("div", {});
  // Inline Add-shot chooser: a new shot must start from a valid
  // lane/visual-type combination, never an unwired default.
  const addChooser = el("div", { class: "overlay-form", hidden: true });

  const panel = el("div", { class: "panel", id: "se-shots-panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Shots"),
      summarySlot,
      el("span", { class: "spacer" }),
      renderSceneBtn,
      addBtn,
    ),
    el("div", { class: "panel-body stack" },
      el("p", { class: "muted small" },
        "Shots play in order inside the scene; a later shot overlaps the previous shot's tail by its incoming transition, so rendered length = Σdurations − Σincoming overlaps. Editing stays in this editor in V1."),
      addChooser,
      strip,
      detailErr,
      detailWrap,
    ),
  );

  const current = () => shots.find((s) => s.id === selectedId) || null;

  function showDetailError(err) {
    detailErr.replaceChildren(errorPanel(err));
  }

  /**
   * Ask before dropping unsaved edits. Resolves true when safe to proceed.
   * @returns {Promise<boolean>}
   */
  function confirmDiscard() {
    if (!dirty) return Promise.resolve(true);
    return confirm({
      title: "Discard unsaved shot edits?",
      message: "The selected-shot form has unsaved changes that this action would discard.",
      confirmLabel: "Discard",
    });
  }

  /** Cheap fingerprint so feed ticks only touch the DOM when data changed. */
  function sigOf(data) {
    return JSON.stringify([
      (data.shots || []).map((s) => [
        s.id, s.index, s.status, s.locked, s.duration_seconds,
        s.start_mode, Array.isArray(s.overlays) ? s.overlays.length : 0,
      ]),
      data.rendered_duration_seconds,
      data.count,
    ]);
  }

  async function fetchShots() {
    return listSceneShots(state.config, scene.id);
  }

  /**
   * Apply fetched shot data. `rebuildDetail` replaces the selected-shot form
   * (safe only when not dirty); the overlays-only path keeps form edits.
   * @param {any} data
   * @param {{rebuildDetail?: boolean}} [opts]
   */
  function apply(data, opts = {}) {
    shots = data.shots || [];
    meta = data;
    lastSig = sigOf(data);
    // Honor a deep-linked shot once, before the first-shot fallback.
    if (!selectionInitialized) {
      selectionInitialized = true;
      if (initialShotId && shots.some((s) => s.id === initialShotId)) {
        selectedId = initialShotId;
      }
    }
    if (!shots.some((s) => s.id === selectedId)) {
      selectedId = shots.length ? shots[0].id : null;
      opts.rebuildDetail = true;
    }
    renderHead();
    renderStrip();
    if (opts.rebuildDetail) {
      dirty = false;
      overlayFormOpen = false;
      renderDetail();
    } else if (!overlayFormOpen) {
      renderOverlaysOnly();
    }
  }

  /** Full pull: fetch and rebuild everything (used after mutations). */
  async function pull() {
    const token = ++inflight;
    let data;
    try {
      data = await fetchShots();
    } catch (err) {
      if (token === inflight) showDetailError(err);
      return;
    }
    if (token !== inflight) return;
    apply(data, { rebuildDetail: true });
  }

  /**
   * Live-tick path: never runs while the form is dirty or a save is in
   * flight, and only re-renders the summary line, chips, and overlay rows —
   * the selected-shot form fields are left exactly as the user typed them.
   */
  async function refreshQuiet() {
    if (dirty || saving || quietBusy) return;
    quietBusy += 1;
    try {
      const token = ++inflight;
      const data = await fetchShots();
      if (token !== inflight) return;
      if (sigOf(data) === lastSig) return;
      apply(data, { rebuildDetail: false });
    } catch {
      /* transient read failure: the next tick or manual Refresh recovers */
    } finally {
      quietBusy -= 1;
    }
  }

  function renderHead() {
    const count = Number.isFinite(meta.count) ? meta.count : shots.length;
    const rendered = Number(meta.rendered_duration_seconds) || 0;
    const planned = Number.isFinite(meta.scene_duration) ? meta.scene_duration : plannedDuration;
    const parts = [
      el("span", { class: "muted small" },
        `${count} shot${count === 1 ? "" : "s"}
        · rendered ${fmtDuration(rendered)}
        · planned ${fmtDuration(planned)}`.replace(/\s+/g, " ")),
    ];
    if (Math.abs(planned - rendered) > 1 / 24) {
      parts.push(badge("warning", `off plan by ${fmtSecs(Math.abs(planned - rendered))}`));
    }
    if (meta.materialized === false) {
      parts.push(el("span", { class: "tag", title: "Projected from this legacy scene's single visual; the first edit stores it as a real shot." }, "implicit"));
    }
    renderSceneBtn.disabled = rendering || shots.length === 0;
    renderSceneBtn.title = shots.length
      ? "Compile all ready shot media and overlays into this scene's rendered video."
      : "Add at least one shot before rendering the scene.";
    summarySlot.replaceChildren(...parts);
  }

  function renderStrip() {
    if (!shots.length) {
      strip.replaceChildren(el("span", { class: "muted small" }, "No shots."));
      return;
    }
    strip.replaceChildren(...shots.map((shot) => {
      const selected = shot.id === selectedId;
      const kind = (shot.transition_in && shot.transition_in.kind) || "cut";
      const overlap = transitionOverlap(shot);
      const chip = el("button", {
        class: `shot-chip${selected ? " selected" : ""}${shot.locked ? " locked" : ""}`,
        type: "button",
        "aria-pressed": String(selected),
        title: `Shot #${shot.index + 1}: ${shot.title || "(untitled)"} — click to edit`,
        onclick: () => selectShot(shot.id),
      },
        el("span", { class: "sch-head" },
          el("span", { class: "sch-num" }, `#${shot.index + 1}`),
          el("span", { class: "sch-title" }, shot.title || "(untitled)"),
          shot.locked ? el("span", { class: "sch-lock" }, icon("lock", 12)) : null,
        ),
        el("span", { class: "sch-meta" },
          el("span", { class: `lane-chip lane-${shot.lane || "image"}` }, laneLabel(shot.lane)),
          shotStatusBadge(shot.status, false),
          el("span", { class: "tag" }, fmtSecs(shot.duration_seconds)),
          kind !== "cut"
            ? el("span", { class: "tag", title: "Incoming transition overlapping the previous shot" }, `${kind}${overlap > 0 ? ` ${fmtSecs(overlap)}` : ""}`)
            : null,
          shot.start_mode === "weighted" ? el("span", { class: "tag tag-accent", title: "The timing compiler may retime weighted shots to fit narration." }, "weighted") : null,
          Array.isArray(shot.overlays) && shot.overlays.length
            ? el("span", { class: "tag" }, `${shot.overlays.length} overlay${shot.overlays.length === 1 ? "" : "s"}`)
            : null,
        ),
      );
      return chip;
    }));
  }

  async function selectShot(id) {
    if (id === selectedId) return;
    const ok = await confirmDiscard();
    if (!ok) return;
    selectedId = id;
    dirty = false;
    renderStrip();
    renderDetail();
  }

  /**
   * Materialize a legacy scene's projected implicit shot before mutating it.
   *
   * API gap (see frontend/API_GAPS.md): PATCH/DELETE/overlay endpoints 404
   * on the deterministic `<scene>-implicit` id because only /approve
   * materializes it. Client-side workaround: create one placeholder shot at
   * index 0 (which materializes the projection verbatim) and immediately
   * archive that placeholder, leaving exactly the materialized shot behind.
   * @param {Record<string, any>|null} shot
   */
  async function ensureMaterialized(shot) {
    if (!shot || !shot.implicit) return;
    const seedDur = Number(shot.duration_seconds) > 0
      ? Number(shot.duration_seconds)
      : (plannedDuration > 0 ? plannedDuration : 5);
    const placeholder = await createShot(state.config, scene.id, {
      index: 0,
      title: "",
      duration_seconds: seedDur,
    });
    try {
      await deleteShot(state.config, placeholder.id);
    } catch (err) {
      toastError(err, "archive the materialization placeholder");
      throw err;
    }
    toast("info", "Legacy shot materialized",
      "The projected single-visual shot is now a real, stored shot.");
  }

  /* --- selected-shot detail ------------------------------------------- */

  function renderDetail() {
    detailErr.replaceChildren();
    const shot = current();
    if (!shot) {
      detailWrap.replaceChildren(
        el("p", { class: "muted small" }, "No shots in this scene yet."),
      );
      return;
    }
    detailWrap.replaceChildren(buildShotDetail(shot));
  }

  /**
   * @param {Record<string, any>} shot
   * @returns {HTMLElement}
   */
  function buildShotDetail(shot) {
    const editable = !sceneLocked && !shot.locked;

    const title = el("input", { id: "se-sh-title", type: "text", maxlength: "1000", disabled: !editable, value: String(shot.title || "") });
    const lane = el("select", { id: "se-sh-lane", disabled: !editable });
    for (const item of SHOT_LANES) lane.append(el("option", { value: item.value }, item.label));
    if (!SHOT_LANES.some((item) => item.value === shot.lane)) {
      lane.append(el("option", { value: shot.lane }, laneLabel(shot.lane)));
    }
    lane.value = shot.lane || "image";

    const visualType = el("select", { id: "se-sh-type", disabled: !editable });
    for (const wired of [true, false]) {
      for (const mode of VISUAL_TYPES.filter((m) => m.wired === wired)) {
        const attrs = wired ? { value: mode.value } : { value: mode.value, dataset: { unwired: "true" } };
        visualType.append(el("option", attrs, mode.label));
      }
    }
    const currentType = shot.visual_type || "flux_still";
    if (!Array.from(visualType.options).some((o) => o.value === currentType)) {
      visualType.append(el("option", { value: currentType }, currentType));
    }
    visualType.value = currentType;

    const visualPrompt = el("textarea", { id: "se-sh-prompt", rows: "4", disabled: !editable }, shot.visual_prompt || "");
    const negativePrompt = el("textarea", { id: "se-sh-negative", rows: "2", disabled: !editable }, shot.negative_prompt || "");
    const ideogramPromptMode = el("select", { id: "se-sh-ideogram-mode", disabled: !editable });
    ideogramPromptMode.append(
      el("option", { value: "quick" }, "Quick Generation"),
      el("option", { value: "precise" }, "Precise Text & Layout"),
    );
    ideogramPromptMode.value = shot.settings?.ideogram_prompt_mode || "quick";
    const ideogramText = el("textarea", {
      id: "se-sh-ideogram-text", rows: "3", disabled: !editable,
      placeholder: "One exact in-image string per line",
    }, shot.settings?.text_in_image || "");
    const textOverlayBackground = el("select", {
      id: "se-sh-text-overlay-background", disabled: !editable,
    });
    textOverlayBackground.append(
      el("option", { value: "krea" }, "Krea 2 Turbo"),
      el("option", { value: "ideogram4_local" }, "Ideogram 4 (text-free background)"),
      el("option", { value: "qwen_image" }, "Qwen Image (text-free background)"),
    );
    textOverlayBackground.value = shot.settings?.text_overlay_background_model || "krea";
    const textOverlayLayout = el("select", {
      id: "se-sh-text-overlay-layout", disabled: !editable,
    });
    textOverlayLayout.append(
      el("option", { value: "auto" }, "Automatic"),
      el("option", { value: "hook" }, "Hook — top and bottom"),
      el("option", { value: "reveal" }, "Reveal — centered title"),
      el("option", { value: "quote" }, "Quotation — quote and citation"),
      el("option", { value: "cta" }, "CTA — stacked safe zones"),
    );
    textOverlayLayout.value = shot.settings?.text_overlay_layout || "auto";
    const ideogramPromptJson = el("textarea", {
      id: "se-sh-ideogram-json", rows: "14", disabled: !editable,
      class: "mono small", placeholder: "Paste native Ideogram 4 / KJNodes JSON",
    }, shot.settings?.ideogram_prompt_json
      ? JSON.stringify(shot.settings.ideogram_prompt_json, null, 2)
      : "");
    const cameraInstruction = el("input", {
      id: "se-sh-camera", type: "text", maxlength: "4000", disabled: !editable,
      placeholder: "e.g. slow push in, pan left, locked",
      value: String(shot.camera_instruction || ""),
    });
    const duration = el("input", {
      id: "se-sh-duration", type: "number", min: "0.001", step: "any",
      disabled: !editable, value: shot.duration_seconds != null ? String(shot.duration_seconds) : "",
    });
    const startMode = el("select", { id: "se-sh-start-mode", disabled: !editable });
    for (const mode of START_MODES) startMode.append(el("option", { value: mode.value }, mode.label));
    startMode.value = shot.start_mode === "weighted" ? "weighted" : "fixed";

    const transKind = el("select", { id: "se-sh-trans-kind", disabled: !editable });
    for (const kind of TRANSITION_KINDS) transKind.append(el("option", { value: kind.value }, kind.label));
    const incomingKind = (shot.transition_in && shot.transition_in.kind) || "cut";
    if (!TRANSITION_KINDS.some((k) => k.value === incomingKind)) {
      transKind.append(el("option", { value: incomingKind }, incomingKind));
    }
    transKind.value = incomingKind;
    const transDur = el("input", {
      id: "se-sh-trans-dur", type: "number", min: "0", step: "any",
      disabled: !editable || incomingKind === "cut",
      value: String((shot.transition_in && shot.transition_in.duration_seconds) || 0),
    });
    transKind.addEventListener("change", () => {
      transDur.disabled = !editable || transKind.value === "cut";
      if (transKind.value === "cut") transDur.value = "0";
    });

    const seed = el("input", {
      id: "se-sh-seed", type: "number", min: "0", step: "1",
      disabled: !editable, value: shot.seed != null ? String(shot.seed) : "",
    });
    const sourceAssetId = el("input", {
      id: "se-sh-src-asset", type: "text", maxlength: "100", disabled: !editable,
      placeholder: "asset id for imported media (REAL lane)",
      value: shot.source_asset_id ? String(shot.source_asset_id) : "",
    });
    const reusedMediaFile = el("input", {
      id: "se-sh-reused-media-file", type: "file", disabled: !editable,
      accept: "image/png,image/jpeg,image/webp,image/bmp,image/gif,image/tiff,video/mp4,video/quicktime,video/webm,video/x-matroska",
    });
    const reusedMediaTitle = el("input", {
      id: "se-sh-reused-media-title", type: "text", disabled: !editable,
      placeholder: "Asset title / description", value: shot.source?.title || "",
    });
    const reusedMediaUrl = el("input", {
      id: "se-sh-reused-media-url", type: "url", disabled: !editable,
      placeholder: "Source URL (optional; never fetched)", value: shot.source?.source_url || "",
    });
    const reusedMediaRights = el("textarea", {
      id: "se-sh-reused-media-rights", rows: "3", disabled: !editable,
      placeholder: "Optional rights or license note",
    }, shot.source?.license_note || "");
    const reusedMediaImport = el("button", {
      class: "btn btn-primary btn-sm", type: "button", disabled: !editable,
    }, "Save shot & import local media");
    const generatedImageFile = el("input", {
      id: "se-sh-generated-image-file", type: "file", disabled: !editable,
      accept: "image/png,image/jpeg,image/webp,image/bmp,image/gif,image/tiff",
    });
    const generatedImageImport = el("button", {
      class: "btn btn-primary btn-sm", type: "button", disabled: !editable,
    }, "Save shot & import AI image");
    const trimIn = el("input", {
      id: "se-sh-trim-in", type: "number", min: "0", step: "any",
      disabled: !editable, value: shot.source_in_seconds != null ? String(shot.source_in_seconds) : "",
    });
    const trimOut = el("input", {
      id: "se-sh-trim-out", type: "number", min: "0", step: "any",
      disabled: !editable, value: shot.source_out_seconds != null ? String(shot.source_out_seconds) : "",
    });

    const fTitle = field({ label: "Title", input: title });
    const fLane = field({ label: "Lane", input: lane, hint: "Editorial source policy: real archival, generated image, H3 motion, or HTML graphics." });
    const fVisualType = field({ label: "Visual type", input: visualType, hint: "Production implementation; unwired types are mock-only placeholders." });
    const fPrompt = field({ label: "Visual prompt", input: visualPrompt, hint: "What the visual model should draw for this shot." });
    const fNegative = field({ label: "Negative prompt", input: negativePrompt });
    const fIdeogramMode = field({ label: "Ideogram prompt mode", input: ideogramPromptMode, hint: "Quick uses the official Magic Prompt with your local LLM. Precise accepts native/KJNodes JSON." });
    const fIdeogramText = field({ label: "Exact visible text", input: ideogramText, hint: "One distinct text region per line. Composite mode renders it locally after background generation." });
    const fTextOverlayBackground = field({ label: "Background model", input: textOverlayBackground, hint: "The model generates only the artwork; Local Video Studio renders the exact text afterward." });
    const fTextOverlayLayout = field({ label: "Text layout", input: textOverlayLayout, hint: "Preset for mobile-safe exact-text placement." });
    const fIdeogramJson = field({ label: "Precise Ideogram JSON", input: ideogramPromptJson, hint: "Native bbox order: [y_min, x_min, y_max, x_max] on the 0–1000 grid." });
    const fCamera = field({ label: "Camera instruction", input: cameraInstruction, hint: "Deterministic motion applied at render time where supported." });
    const fDuration = field({ label: "Duration (seconds)", input: duration, hint: "Full length of this shot's visual; must be finite and positive." });
    const fStartMode = field({ label: "Start mode", input: startMode, hint: "Weighted shots may be retimed by the compiler to fit narration; fixed shots never move." });
    const fTransKind = field({ label: "Transition in", input: transKind, hint: "How this shot enters over the previous one (scene opening ignores this)." });
    const fTransDur = field({ label: "Overlap (seconds)", input: transDur, hint: "Must be shorter than both adjacent shots; cut forces 0." });
    const fSeed = field({ label: "Seed", input: seed, hint: "Non-negative integer recorded with the shot." });
    const fSrcAsset = field({ label: "Source asset id", input: sourceAssetId });
    const fReusedMedia = field({ label: "Local media", input: reusedMediaFile, hint: "The selected image or video is copied into this project; no remote download occurs." });
    const fReusedTitle = field({ label: "Source title", input: reusedMediaTitle, hint: "Required provenance for reused media." });
    const fReusedUrl = field({ label: "Source URL", input: reusedMediaUrl, hint: "Recorded only; the app never fetches it." });
    const fReusedRights = field({ label: "Rights / license note (optional)", input: reusedMediaRights, hint: "Optional provenance note; leave blank for your own or AI-generated media." });
    const fGeneratedImage = field({ label: "Import existing AI image", input: generatedImageFile, hint: "Optional. Choose a locally generated image, then Save shot; it becomes this shot's current visual without running a model." });
    const fTrimIn = field({ label: "Source in (s)", input: trimIn });
    const fTrimOut = field({ label: "Source out (s)", input: trimOut, hint: "Set in and out together to trim source video; clear both to use the whole source." });

    function updateShotTypeFields() {
      const ideogram = visualType.value === "ideogram4_still";
      const exactTextComposite = visualType.value === "text_overlay_still";
      const reused = visualType.value === "reused_media";
      const generatedImage = ["krea2_still", "ideogram4_still", "qwen_image_still", "flux_still", "image_motion"].includes(visualType.value);
      fIdeogramMode.hidden = !ideogram;
      fIdeogramText.hidden = !(exactTextComposite || (ideogram && ideogramPromptMode.value === "quick"));
      fIdeogramJson.hidden = !ideogram || ideogramPromptMode.value !== "precise";
      fTextOverlayBackground.hidden = !exactTextComposite;
      fTextOverlayLayout.hidden = !exactTextComposite;
      fSrcAsset.hidden = reused;
      fReusedMedia.hidden = !reused;
      fReusedTitle.hidden = !reused;
      fReusedUrl.hidden = !reused;
      fReusedRights.hidden = !reused;
      reusedMediaImport.hidden = !reused;
      fGeneratedImage.hidden = !generatedImage;
      generatedImageImport.hidden = !generatedImage;
      if (exactTextComposite) cameraInstruction.value = "locked";
      if (ideogram) cameraInstruction.value = "locked";
      if (reused) {
        lane.value = "real";
        cameraInstruction.value = "locked";
      }
    }
    visualType.addEventListener("change", updateShotTypeFields);
    ideogramPromptMode.addEventListener("change", updateShotTypeFields);
    updateShotTypeFields();

    const watched = [title, lane, visualType, visualPrompt, negativePrompt, ideogramPromptMode,
      ideogramText, ideogramPromptJson, textOverlayBackground, textOverlayLayout,
      cameraInstruction,
      duration, startMode, transKind, transDur, seed, sourceAssetId, trimIn, trimOut,
      reusedMediaFile, reusedMediaTitle, reusedMediaUrl, reusedMediaRights,
      generatedImageFile];
    for (const control of watched) {
      control.addEventListener("input", () => { dirty = true; });
      control.addEventListener("change", () => { dirty = true; });
    }

    /* actions --------------------------------------------------------- */
    const idx = shot.index;
    const nextShot = shots.find((s) => s.index === idx + 1) || null;

    const saveBtn = el("button", { class: "btn btn-primary btn-sm", type: "button", disabled: !editable }, "Save shot");
    const revertBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: !editable }, "Revert");
    const upBtn = el("button", {
      class: "btn btn-sm", type: "button",
      disabled: !editable || idx <= 0,
      title: idx <= 0 ? "Already first." : "Swap with the previous shot.",
    }, "Move up");
    const downBtn = el("button", {
      class: "btn btn-sm", type: "button",
      disabled: !editable || !nextShot,
      title: nextShot ? "Swap with the next shot." : "Already last.",
    }, "Move down");
    const dupBtn = el("button", {
      class: "btn btn-sm", type: "button", disabled: sceneLocked,
      title: "Insert a copy directly after this shot.",
    }, "Duplicate");
    const archiveBtn = el("button", {
      class: "btn btn-danger btn-sm", type: "button", disabled: sceneLocked,
      title: "Archive this shot's media and remove it from the order.",
    }, "Archive");
    const approveBtn = el("button", {
      class: "btn btn-sm", type: "button",
      hidden: shot.status === "approved",
      title: "Mark this shot approved.",
    }, "Approve");
    const lockBtn = el("button", {
      class: "btn btn-sm", type: "button",
      hidden: shot.locked || !(shot.status === "ready" || shot.status === "approved"),
      title: "Lock prevents editing or regeneration until unlocked.",
    }, "Lock");
    const unlockBtn = el("button", {
      class: "btn btn-sm", type: "button",
      hidden: !shot.locked,
      title: "Re-approves without locking, making the shot editable again.",
    }, "Unlock");
    const generationBusy = shot.status === "queued" || shot.status === "generating";
    const generateBtn = el("button", {
      class: "btn btn-sm", type: "button",
      disabled: !editable || generationBusy,
      title: "Generate this shot from its saved recipe. Existing matching media may be reused.",
    }, generationBusy ? "Generating…" : "Generate");
    const regenerateBtn = el("button", {
      class: "btn btn-sm", type: "button",
      disabled: !editable || generationBusy,
      title: "Archive this shot's current media and force a new generation.",
    }, generationBusy ? "Regenerating…" : "Regenerate");

    saveBtn.addEventListener("click", async () => {
      if (saving) return;
      const reusedFile = visualType.value === "reused_media"
        ? (reusedMediaFile.files && reusedMediaFile.files[0]) : null;
      const generatedFile = !fGeneratedImage.hidden
        ? (generatedImageFile.files && generatedImageFile.files[0]) : null;
      const sourceTitle = reusedMediaTitle.value.trim();
      const licenseNote = reusedMediaRights.value.trim();
      if (reusedFile && !sourceTitle) {
        toast("critical", "Source title required", "Provide a short title for the selected reused-media file.");
        return;
      }
      const built = collectShotBody(shot, {
        fDuration, fSeed, fTransDur, fTrimIn, fTrimOut, fIdeogramJson, fIdeogramText,
        values: { title, lane, visualType, visualPrompt, negativePrompt, ideogramPromptMode, ideogramText, ideogramPromptJson, textOverlayBackground, textOverlayLayout, cameraInstruction, duration, startMode, transKind, transDur, seed, sourceAssetId, trimIn, trimOut },
      });
      if (!built.ok) return;
      saving = true;
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving…";
      try {
        await ensureMaterialized(shot);
        await editShot(state.config, shot.id, built.body);
        if (reusedFile) {
          await importShotReusedMedia(state.config, shot.id, reusedFile, {
            title: sourceTitle,
            source_url: reusedMediaUrl.value.trim(),
            license_note: licenseNote,
            classification: "documentary_evidence",
          });
        } else if (generatedFile) {
          await importShotGeneratedImage(state.config, shot.id, generatedFile);
        }
        dirty = false;
        toast("good", reusedFile ? "Shot saved and media attached" : generatedFile ? "Shot saved and AI image attached" : "Shot saved", `#${idx + 1} of S${sceneNumber}`);
        await pull();
      } catch (err) {
        showDetailError(err);
        saveBtn.disabled = false;
        saveBtn.textContent = "Save shot";
      } finally {
        saving = false;
      }
    });
    reusedMediaImport.addEventListener("click", () => {
      if (!(reusedMediaFile.files && reusedMediaFile.files[0])) {
        toast("critical", "Choose a local file", "Select an image or video before importing reused media.");
        return;
      }
      saveBtn.click();
    });
    generatedImageImport.addEventListener("click", () => {
      if (!(generatedImageFile.files && generatedImageFile.files[0])) {
        toast("critical", "Choose an AI image", "Select a generated image before importing it.");
        return;
      }
      saveBtn.click();
    });
    revertBtn.addEventListener("click", () => {
      dirty = false;
      renderDetail();
    });

    async function move(direction) {
      if (!(await confirmDiscard())) return;
      const targetIdx = idx + direction;
      upBtn.disabled = true;
      downBtn.disabled = true;
      try {
        await ensureMaterialized(shot);
        await editShot(state.config, shot.id, { index: targetIdx });
        dirty = false;
        await pull();
      } catch (err) {
        showDetailError(err);
        upBtn.disabled = !editable || idx <= 0;
        downBtn.disabled = !editable || !nextShot;
      }
    }
    upBtn.addEventListener("click", () => move(-1));
    downBtn.addEventListener("click", () => move(1));

    dupBtn.addEventListener("click", async () => {
      if (!(await confirmDiscard())) return;
      dupBtn.disabled = true;
      try {
        await createShot(state.config, scene.id, {
          index: idx + 1,
          title: shot.title ? `${shot.title} (copy)` : "Shot copy",
          duration_seconds: Number(shot.duration_seconds) > 0 ? Number(shot.duration_seconds) : 5,
          lane: shot.lane || "image",
          visual_type: shot.visual_type || "flux_still",
          selected_backend: shot.selected_backend || "automatic",
          visual_prompt: shot.visual_prompt || "",
          negative_prompt: shot.negative_prompt || "",
          camera_instruction: shot.camera_instruction || "",
          transition_in: {
            kind: (shot.transition_in && shot.transition_in.kind) || "cut",
            duration_seconds: Number(shot.transition_in && shot.transition_in.duration_seconds) || 0,
          },
          seed: shot.seed != null ? Number(shot.seed) : undefined,
          references: Array.isArray(shot.references) ? shot.references : [],
          settings: shot.settings ? { ...shot.settings } : {},
          // Copies detach their cue ids: ids must stay unique per shot, and
          // project-scope resolution expects no duplicates across shots.
          overlays: Array.isArray(shot.overlays)
            ? shot.overlays.map((o) => ({ ...o, id: undefined }))
            : undefined,
        });
        // The copy inherits its own incoming transition; the overlap stays
        // valid because its new predecessor (the original) has the same
        // duration the constraint was checked against.
        toast("good", "Shot duplicated", `Inserted after #${idx + 1}`);
        dirty = false;
        await pull();
      } catch (err) {
        showDetailError(err);
        dupBtn.disabled = false;
      }
    });

    archiveBtn.addEventListener("click", async () => {
      const ok = await confirm({
        title: `Archive shot #${idx + 1}?`,
        message: "Its media files are archived and the shot leaves the scene order. Locked shots are refused by the backend (409). This cannot be undone from this screen.",
        confirmLabel: "Archive shot",
      });
      if (!ok) return;
      archiveBtn.disabled = true;
      try {
        await ensureMaterialized(shot);
        const result = await deleteShot(state.config, shot.id);
        toast("info", "Shot archived",
          `${result.archived_assets.length} media file(s) archived`
            + (result.scene_reverted_to_implicit ? " · scene reverted to its implicit shot" : ""));
        dirty = false;
        await pull();
      } catch (err) {
        // Guarded archive: show the backend refusal verbatim (e.g. the 409
        // "unlock the shot before archiving it").
        showDetailError(err);
        toastError(err, `archive shot #${idx + 1}`);
        archiveBtn.disabled = false;
      }
    });

    async function approve(lock) {
      approveBtn.disabled = true;
      lockBtn.disabled = true;
      unlockBtn.disabled = true;
      try {
        await approveShot(state.config, shot.id, { lock });
        toast("good", lock ? "Shot locked" : "Shot approved", `#${idx + 1} of S${sceneNumber}`);
        dirty = false;
        await pull();
      } catch (err) {
        showDetailError(err);
        approveBtn.disabled = false;
        lockBtn.disabled = false;
        unlockBtn.disabled = false;
      }
    }
    approveBtn.addEventListener("click", () => approve(false));
    lockBtn.addEventListener("click", () => approve(true));
    unlockBtn.addEventListener("click", () => approve(false));

    async function queueGeneration(force) {
      if (dirty) {
        toast("warning", "Unsaved shot edits",
          "Save or revert this shot before generating so the job uses the values shown here.");
        return;
      }
      if (force) {
        const ok = await confirm({
          title: `Regenerate shot #${idx + 1}?`,
          message: "The current shot media will be archived and replaced by a newly generated visual.",
          confirmLabel: "Regenerate",
        });
        if (!ok) return;
      }
      generateBtn.disabled = true;
      regenerateBtn.disabled = true;
      const activeBtn = force ? regenerateBtn : generateBtn;
      activeBtn.textContent = force ? "Regenerating…" : "Generating…";
      try {
        await ensureMaterialized(shot);
        const job = force
          ? await regenerateShot(state.config, shot.id)
          : await generateShot(state.config, shot.id);
        toast("good", force ? "Shot regeneration queued" : "Shot generation queued",
          `#${idx + 1} of S${sceneNumber} · job ${job.id}`);
        await pull();
      } catch (err) {
        showDetailError(err);
        toastError(err, force ? "regenerate shot" : "generate shot");
        generateBtn.disabled = !editable;
        regenerateBtn.disabled = !editable;
        generateBtn.textContent = "Generate";
        regenerateBtn.textContent = "Regenerate";
      }
    }
    generateBtn.addEventListener("click", () => queueGeneration(false));
    regenerateBtn.addEventListener("click", () => queueGeneration(true));

    /* assemble -------------------------------------------------------- */
    const container = el("div", { class: "stack" });

    if (shot.implicit) {
      container.append(el("div", { class: "readonly-note" },
        "This scene predates stored shots: the entry below is projected from the scene's single visual recipe. Its first edit (save, move, archive, or overlay) materializes it as a real stored shot.",
      ));
    }

    container.append(
      el("div", { class: "row", style: { flexWrap: "wrap" } },
        el("span", { class: "sc-title", style: { fontSize: "var(--text-md)" } },
          `Shot #${idx + 1}`),
        shotStatusBadge(shot.status, shot.locked),
        el("span", { class: "muted small" },
          `contributes ${fmtSecs(Math.max(0, Number(shot.duration_seconds) - transitionOverlap(shot)))} of ${fmtSecs(shot.duration_seconds)} to the scene`),
        el("span", { class: "spacer" }),
        upBtn, downBtn, dupBtn, archiveBtn,
        generateBtn, regenerateBtn,
        approveBtn, lockBtn, unlockBtn,
      ),
      el("div", { class: "grid-2" }, fTitle, fLane),
      el("div", { class: "grid-2" }, fVisualType, fCamera),
      el("div", { class: "grid-2" }, fPrompt, fNegative),
      el("div", { class: "grid-2" }, fIdeogramMode, fIdeogramText),
      el("div", { class: "grid-2" }, fTextOverlayBackground, fTextOverlayLayout),
      fIdeogramJson,
      el("div", { class: "grid-2" }, fDuration, fStartMode),
      el("div", { class: "grid-2" }, fTransKind, fTransDur),
      el("div", { class: "grid-2" }, fSeed, fSrcAsset),
      el("div", { class: "grid-2" }, fReusedMedia, fReusedTitle),
      el("div", { class: "grid-2" }, fReusedUrl, fReusedRights),
      el("div", { class: "row" }, reusedMediaImport),
      el("div", { class: "grid-2" }, fGeneratedImage),
      el("div", { class: "row" }, generatedImageImport),
      el("div", { class: "grid-2" }, fTrimIn, fTrimOut),
      el("div", { class: "row" }, saveBtn, revertBtn),
    );

    const overlaysSection = el("div", { class: "stack mt", id: "se-overlays" });
    container.append(overlaysSection);
    fillOverlays(overlaysSection, shot, editable);
    return container;
  }

  /**
   * Validate the selected-shot form and build a ShotEdit body.
   * @param {Record<string, any>} shot
   * @param {{fDuration: HTMLElement, fSeed: HTMLElement, fTransDur: HTMLElement,
   *   fTrimIn: HTMLElement, fTrimOut: HTMLElement,
   *   values: Record<string, any>}} ctx
   * @returns {{ok: boolean, body?: Record<string, any>}}
   */
  function collectShotBody(shot, ctx) {
    const v = ctx.values;
    for (const fw of [ctx.fDuration, ctx.fSeed, ctx.fTransDur, ctx.fTrimIn, ctx.fTrimOut, ctx.fIdeogramJson]) {
      setFieldError(fw, null);
    }
    let ok = true;
    const fail = (fw, msg) => { setFieldError(fw, msg); ok = false; };

    const durV = numOrNull(v.duration.value);
    if (durV === null || Number.isNaN(durV)) fail(ctx.fDuration, "A duration is required.");
    else if (!(durV > 0)) fail(ctx.fDuration, "Duration must be greater than 0.");
    else if (durV > 3600) fail(ctx.fDuration, "Duration must stay below 3600 seconds.");

    let seedValue;
    const seedRaw = v.seed.value.trim();
    if (seedRaw === "") seedValue = null;
    else {
      const n = Number(seedRaw);
      if (!Number.isInteger(n) || n < 0) fail(ctx.fSeed, "Seed must be a non-negative integer.");
      seedValue = n;
    }

    const inV = numOrNull(v.trimIn.value);
    const outV = numOrNull(v.trimOut.value);
    if ((inV === null) !== (outV === null)) {
      fail(ctx.fTrimOut, "Set source in and out together, or clear both.");
    } else if (inV !== null && inV < 0) {
      fail(ctx.fTrimIn, "Source in must be 0 or more.");
    } else if (outV !== null && inV !== null && outV <= inV) {
      fail(ctx.fTrimOut, "Source out must be greater than source in.");
    }

    const kind = v.transKind.value;
    let overlap = 0;
    if (kind !== "cut") {
      const td = numOrNull(v.transDur.value);
      if (td === null || Number.isNaN(td)) fail(ctx.fTransDur, "An overlap is required for non-cut transitions.");
      else if (td <= 0) fail(ctx.fTransDur, "Non-cut transitions need a positive overlap.");
      else {
        const prev = shots.find((s) => s.index === shot.index - 1);
        const limit = Math.min(prev ? Number(prev.duration_seconds) : durV, durV);
        if (td >= limit) {
          fail(ctx.fTransDur, `Overlap must be strictly shorter than both adjacent shots (< ${Math.round(limit * 100) / 100} s here).`);
        } else overlap = td;
      }
    }

    let precisePrompt;
    if (v.visualType.value === "text_overlay_still" && !v.ideogramText.value.trim()) {
      fail(ctx.fIdeogramText, "Generated background + exact text requires at least one visible text line.");
    }
    if (v.visualType.value === "ideogram4_still" && v.ideogramPromptMode.value === "precise") {
      if (!v.ideogramPromptJson.value.trim()) {
        fail(ctx.fIdeogramJson, "Precise mode requires native Ideogram JSON.");
      } else {
        try {
          precisePrompt = JSON.parse(v.ideogramPromptJson.value);
          if (!precisePrompt || Array.isArray(precisePrompt) || typeof precisePrompt !== "object") {
            throw new Error("root");
          }
        } catch (_err) {
          fail(ctx.fIdeogramJson, "Enter one valid Ideogram JSON object.");
        }
      }
    }

    if (!ok) return { ok: false };
    const body = {
      title: v.title.value.trim(),
      lane: v.lane.value,
      visual_type: v.visualType.value,
      visual_prompt: v.visualPrompt.value,
      negative_prompt: v.negativePrompt.value,
      camera_instruction: v.cameraInstruction.value.trim(),
      duration_seconds: durV,
      start_mode: v.startMode.value,
      transition_in: { kind, duration_seconds: kind === "cut" ? 0 : overlap },
      // Explicit nulls clear these optional fields server-side.
      source_asset_id: v.sourceAssetId.value.trim() || null,
      source_in_seconds: inV,
      source_out_seconds: outV,
      settings: v.visualType.value === "ideogram4_still"
        ? {
            ...(shot.settings || {}),
            ideogram_prompt_mode: v.ideogramPromptMode.value,
            text_in_image: v.ideogramText.value,
            ...(v.ideogramPromptMode.value === "precise"
              ? { ideogram_prompt_json: precisePrompt }
              : { ideogram_prompt_json: undefined }),
          }
        : v.visualType.value === "text_overlay_still"
          ? {
              ...(shot.settings || {}),
              text_in_image: v.ideogramText.value,
              text_overlay_background_model: v.textOverlayBackground.value,
              text_overlay_layout: v.textOverlayLayout.value,
            }
          : { ...(shot.settings || {}) },
    };
    if (seedValue !== null) body.seed = seedValue;
    return { ok: true, body };
  }

  /* --- overlay cues ---------------------------------------------------- */

  /**
   * Refresh just the overlay list under the selected shot (keeps shot-form
   * edits intact across overlay mutations and quiet ticks).
   */
  function renderOverlaysOnly() {
    const section = detailWrap.querySelector("#se-overlays");
    const shot = current();
    if (!section || !shot) return;
    fillOverlays(/** @type {HTMLElement} */ (section), shot, !sceneLocked && !shot.locked);
  }

  /**
   * @param {HTMLElement} section — container to fill
   * @param {Record<string, any>} shot
   * @param {boolean} editable
   */
  function fillOverlays(section, shot, editable) {
    section.replaceChildren();
    const cues = Array.isArray(shot.overlays) ? shot.overlays : [];
    section.append(el("h3", {}, `Overlay cues (${cues.length})`));
    if (!cues.length) {
      section.append(el("p", { class: "muted small" },
        "No overlay cues on this shot. Exact-text cards, graphics, and image crops can be timed over the shot here."));
    }
    for (const cue of cues) {
      section.append(overlayRow(cue, shot, editable));
    }
    const addBtn = el("button", {
      class: "btn btn-sm", type: "button",
      disabled: !editable,
      title: editable ? "Attach a timed overlay cue to this shot." : "Unlock the shot to attach overlays.",
    }, "Add overlay");
    const formSlot = el("div");
    addBtn.addEventListener("click", () => {
      openOverlayForm(formSlot, shot, null);
    });
    section.append(addBtn, formSlot);
  }

  /**
   * @param {Record<string, any>} cue
   * @param {Record<string, any>} shot
   * @param {boolean} editable
   * @returns {HTMLElement}
   */
  function overlayRow(cue, shot, editable) {
    const anchorLabel = (() => {
      const found = OVERLAY_ANCHORS.find(([value]) => value === cue.anchor);
      return found ? found[1] : String(cue.anchor || "center").replace(/_/g, " ");
    })();
    const headline = cue.kind === "exact_text"
      ? `"${cue.exact_text || ""}"`
      : cue.template ? `template: ${cue.template}` : `asset ${cue.asset_id || "?"}`;
    const bits = [
      anchorLabel,
      cue.x != null || cue.y != null ? `@ ${cue.x ?? "—"}, ${cue.y ?? "—"}` : null,
      cue.width != null && cue.height != null ? `box ${cue.width}×${cue.height}` : null,
      `t ${fmtSecs(cue.start_seconds)} + ${fmtSecs(cue.duration_seconds)}`,
      cue.fade_in_seconds || cue.fade_out_seconds
        ? `fades ${fmtSecs(cue.fade_in_seconds)} / ${fmtSecs(cue.fade_out_seconds)}`
        : null,
      `opacity ${cue.opacity}`,
    ].filter(Boolean);

    const row = el("div", { class: "overlay-row" },
      el("span", { class: "tag tag-accent" }, cue.kind || "?"),
      el("div", { class: "ov-main" },
        el("div", { class: "ov-text" }, headline),
        el("div", { class: "ov-meta" }, bits.join(" · ")),
      ),
    );
    const editBtn = el("button", { class: "btn btn-sm", type: "button", disabled: !editable }, "Edit");
    editBtn.addEventListener("click", () => {
      const slot = /** @type {HTMLElement|null} */ (row.parentElement?.querySelector(":scope > div:last-child"));
      if (slot) openOverlayForm(slot, shot, cue);
    });
    const removeBtn = el("button", { class: "btn btn-danger btn-sm", type: "button", disabled: !editable }, "Remove");
    removeBtn.addEventListener("click", async () => {
      const ok = await confirm({
        title: `Remove the ${cue.kind} overlay?`,
        message: cue.kind === "exact_text"
          ? `Exact text: "${cue.exact_text || ""}". It will disappear from the shot composition.`
          : "The cue detaches from this shot; the underlying asset itself is kept.",
        confirmLabel: "Remove cue",
      });
      if (!ok) return;
      removeBtn.disabled = true;
      try {
        await ensureMaterialized(shot);
        await removeShotOverlay(state.config, shot.id, cue.id);
        toast("info", "Overlay removed", `Shot #${shot.index + 1}`);
        await pullQuietPreservingForm();
      } catch (err) {
        showDetailError(err);
        removeBtn.disabled = false;
      }
    });
    row.append(editBtn, removeBtn);
    return row;
  }

  /**
   * Fetch fresh shots and refresh head/strip/overlays without touching the
   * selected-shot form (used after overlay add/edit/remove).
   */
  async function pullQuietPreservingForm() {
    const token = ++inflight;
    try {
      const data = await fetchShots();
      if (token !== inflight) return;
      shots = data.shots || [];
      meta = data;
      lastSig = sigOf(data);
      if (!shots.some((s) => s.id === selectedId)) {
        selectedId = shots.length ? shots[0].id : null;
        dirty = false;
        renderHead();
        renderStrip();
        renderDetail();
        return;
      }
      renderHead();
      renderStrip();
      renderOverlaysOnly();
    } catch (err) {
      showDetailError(err);
    }
  }

  /**
   * Inline add/edit form for one overlay cue.
   * @param {HTMLElement} slot — container replaced by the form
   * @param {Record<string, any>} shot
   * @param {Record<string, any>|null} existing — cue being edited, or null
   */
  function openOverlayForm(slot, shot, existing) {
    overlayFormOpen = true;
    const isEdit = Boolean(existing);
    const kindSel = el("select", { id: "se-ov-kind" });
    for (const k of OVERLAY_KINDS) kindSel.append(el("option", { value: k.value }, k.label));
    const kindValue = existing ? existing.kind : "exact_text";
    if (!OVERLAY_KINDS.some((k) => k.value === kindValue)) {
      kindSel.append(el("option", { value: kindValue }, kindValue));
    }
    kindSel.value = kindValue;

    const exactText = el("textarea", {
      id: "se-ov-text", rows: "3", maxlength: "2000",
      placeholder: "The exact visible string. Rendered locally through the sanctioned text path.",
    }, existing && existing.exact_text != null ? String(existing.exact_text) : "");
    const template = el("input", {
      id: "se-ov-template", type: "text", maxlength: "200",
      placeholder: "optional reusable template name",
      value: existing ? String(existing.template || "") : "",
    });
    const assetId = el("input", {
      id: "se-ov-asset", type: "text", maxlength: "100",
      placeholder: "project asset id (graphic/image kinds)",
      value: existing && existing.asset_id != null ? String(existing.asset_id) : "",
    });
    const anchor = el("select", { id: "se-ov-anchor" });
    for (const [value, label] of OVERLAY_ANCHORS) anchor.append(el("option", { value }, label));
    anchor.value = OVERLAY_ANCHORS.some(([v]) => v === (existing && existing.anchor))
      ? existing.anchor : "center";
    const x = el("input", { id: "se-ov-x", type: "number", step: "any", value: existing && existing.x != null ? String(existing.x) : "" });
    const y = el("input", { id: "se-ov-y", type: "number", step: "any", value: existing && existing.y != null ? String(existing.y) : "" });
    const width = el("input", { id: "se-ov-w", type: "number", min: "0", step: "any", value: existing && existing.width != null ? String(existing.width) : "" });
    const height = el("input", { id: "se-ov-h", type: "number", min: "0", step: "any", value: existing && existing.height != null ? String(existing.height) : "" });
    const start = el("input", {
      id: "se-ov-start", type: "number", min: "0", step: "any",
      value: existing ? String(existing.start_seconds ?? 0) : "0",
    });
    const dur = el("input", {
      id: "se-ov-dur", type: "number", min: "0", step: "any",
      value: existing && existing.duration_seconds != null ? String(existing.duration_seconds) : "",
    });
    const fadeIn = el("input", { id: "se-ov-fi", type: "number", min: "0", step: "any", value: existing ? String(existing.fade_in_seconds ?? 0) : "0" });
    const fadeOut = el("input", { id: "se-ov-fo", type: "number", min: "0", step: "any", value: existing ? String(existing.fade_out_seconds ?? 0) : "0" });
    const opacity = el("input", {
      id: "se-ov-opacity", type: "number", min: "0.01", max: "1", step: "0.05",
      value: existing && existing.opacity != null ? String(existing.opacity) : "1",
    });

    const fKind = field({ label: "Kind", input: kindSel });
    const fText = field({ label: "Exact text", input: exactText });
    const fTemplate = field({ label: "Template", input: template });
    const fAsset = field({ label: "Asset id", input: assetId });
    const fAnchor = field({ label: "Anchor", input: anchor, hint: "Nine-point canvas anchor; x/y place that point in project pixels." });
    const fX = field({ label: "X", input: x });
    const fY = field({ label: "Y", input: y });
    const fW = field({ label: "Width", input: width });
    const fH = field({ label: "Height", input: height, hint: "Width and height are set together." });
    const fStart = field({ label: "Start (s)", input: start, hint: `Shot-local time; must fit inside the shot (0–${fmtSecs(shot.duration_seconds)}).` });
    const fDur = field({ label: "Duration (s)", input: dur });
    const fFI = field({ label: "Fade in (s)", input: fadeIn });
    const fFO = field({ label: "Fade out (s)", input: fadeOut, hint: "Fades together cannot exceed the cue duration." });
    const fOpacity = field({ label: "Opacity", input: opacity, hint: "Between 0 and 1." });

    function syncVisibility() {
      const isText = kindSel.value === "exact_text";
      fText.hidden = !isText;
      fAsset.hidden = isText;
    }
    kindSel.addEventListener("change", syncVisibility);
    syncVisibility();

    const errSlot = el("div");
    const submitBtn = el("button", { class: "btn btn-primary btn-sm", type: "button" },
      isEdit ? "Save cue" : "Add cue");
    const cancelBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Cancel");

    function closeForm() {
      overlayFormOpen = false;
      slot.replaceChildren();
    }
    cancelBtn.addEventListener("click", closeForm);

    submitBtn.addEventListener("click", async () => {
      errSlot.replaceChildren();
      for (const fw of [fText, fAsset, fDur, fStart, fFI, fOpacity]) setFieldError(fw, null);
      let ok = true;
      const fail = (fw, msg) => { setFieldError(fw, msg); ok = false; };

      const startV = numOrNull(start.value);
      if (startV === null || Number.isNaN(startV) || startV < 0) fail(fStart, "Start must be 0 or more.");
      const durV = numOrNull(dur.value);
      if (durV === null || Number.isNaN(durV) || !(durV > 0)) fail(fDur, "Duration must be greater than 0.");
      const shotDur = Number(shot.duration_seconds) || 0;
      if (startV !== null && durV !== null && Number.isFinite(startV) && Number.isFinite(durV)) {
        if (startV + durV > shotDur + 1e-6) {
          fail(fDur, `The cue must fit inside the shot (${fmtSecs(shotDur)}); it ends at ${fmtSecs(startV + durV)}.`);
        }
      }
      const fiRaw = numOrNull(fadeIn.value);
      const foRaw = numOrNull(fadeOut.value);
      if (fiRaw === null) fail(fFI, "Fade in is required (use 0 for none).");
      else if (foRaw === null) fail(fFO, "Fade out is required (use 0 for none).");
      else if (Number.isNaN(fiRaw) || Number.isNaN(foRaw)) fail(fFI, "Fades must be numbers.");
      else if (fiRaw < 0 || foRaw < 0) fail(fFI, "Fades cannot be negative.");
      else if (durV !== null && Number.isFinite(durV) && fiRaw + foRaw > durV + 1e-6) {
        fail(fFO, "Fades together cannot exceed the cue duration.");
      }
      const fi = Number.isFinite(fiRaw) ? fiRaw : 0;
      const fo = Number.isFinite(foRaw) ? foRaw : 0;
      const opV = numOrNull(opacity.value);
      if (opV === null || Number.isNaN(opV) || opV <= 0 || opV > 1) fail(fOpacity, "Opacity must be between 0 and 1.");
      const wV = numOrNull(width.value);
      const hV = numOrNull(height.value);
      if ((wV === null) !== (hV === null)) fail(fH, "Width and height are set together.");
      else if (Number.isNaN(wV) || Number.isNaN(hV)) fail(fH, "Width and height must be numbers.");
      const body = {
        kind: kindSel.value,
        template: template.value.trim(),
        anchor: anchor.value,
        start_seconds: startV === null || Number.isNaN(startV) ? 0 : startV,
        duration_seconds: durV,
        x: numOrNull(x.value),
        y: numOrNull(y.value),
        width: wV,
        height: hV,
        fade_in_seconds: fi,
        fade_out_seconds: fo,
        opacity: opV === null || Number.isNaN(opV) ? 1 : opV,
      };
      if (body.kind === "exact_text") {
        if (!(exactText.value.trim())) fail(fText, "Exact-text overlays require the exact string.");
        body.exact_text = exactText.value;
        body.asset_id = null;
      } else {
        const aid = assetId.value.trim();
        if (!aid) fail(fAsset, `${body.kind} overlays require a project asset id.`);
        body.asset_id = aid || null;
        body.exact_text = null;
      }
      if (!ok) return;

      submitBtn.disabled = true;
      try {
        await ensureMaterialized(shot);
        if (isEdit && existing) {
          await patchShotOverlay(state.config, shot.id, existing.id, body);
          toast("good", "Overlay saved", `Shot #${shot.index + 1}`);
        } else {
          await addShotOverlay(state.config, shot.id, body);
          toast("good", "Overlay added", `Shot #${shot.index + 1}`);
        }
        closeForm();
        await pullQuietPreservingForm();
      } catch (err) {
        errSlot.replaceChildren(errorPanel(err));
        submitBtn.disabled = false;
      }
    });

    slot.replaceChildren(el("div", { class: "overlay-form" },
      el("div", { class: "grid-2" }, fKind, fAnchor),
      fText,
      el("div", { class: "grid-2" }, fAsset, fTemplate),
      el("div", { class: "grid-2" }, fX, fY),
      el("div", { class: "grid-2" }, fW, fH),
      el("div", { class: "grid-2" }, fStart, fDur),
      el("div", { class: "grid-2" }, fFI, fFO),
      fOpacity,
      errSlot,
      el("div", { class: "row" }, submitBtn, cancelBtn),
    ));
  }

  /* --- Add-shot chooser --------------------------------------------------
   * A new shot needs an explicit lane/visual-type pair: the backend's bare
   * defaults (image / flux_still) are unwired, so "Add shot" opens this
   * chooser pre-filled from the shared defaultNewShot() policy instead of
   * silently creating a placeholder recipe.
   * ---------------------------------------------------------------------- */

  function openAddChooser() {
    const base = defaultNewShot(shots);
    addChooser.replaceChildren();

    const typeSel = el("select", { id: "se-add-type", "aria-label": "Visual type for the new shot" });
    for (const wired of [true, false]) {
      for (const mode of VISUAL_TYPES.filter((m) => m.wired === wired)) {
        const attrs = wired ? { value: mode.value } : { value: mode.value, dataset: { unwired: "true" } };
        typeSel.append(el("option", attrs, mode.label));
      }
    }
    if (!Array.from(typeSel.options).some((o) => o.value === base.visual_type)) {
      typeSel.append(el("option", { value: base.visual_type }, base.visual_type));
    }
    typeSel.value = base.visual_type;

    const laneSel = el("select", { id: "se-add-lane", "aria-label": "Lane for the new shot" });
    for (const item of SHOT_LANES) laneSel.append(el("option", { value: item.value }, item.label));
    laneSel.value = base.lane;
    // Keep the pair coherent: picking a visual type proposes its canonical
    // lane; the editorial lane stays overridable for deliberate cases.
    typeSel.addEventListener("change", () => {
      laneSel.value = defaultLane(typeSel.value);
      syncWiredHint();
    });

    const wiredHint = el("span", { class: "muted small" });
    function syncWiredHint() {
      wiredHint.textContent = isWiredVisualType(typeSel.value)
        ? "Wired local implementation."
        : "Unwired mock-only placeholder - generation will fail honestly until its phase lands.";
    }
    syncWiredHint();

    const durInput = el("input", {
      id: "se-add-duration", type: "number", min: "0.001", step: "any",
      value: String(base.duration_seconds),
      "aria-label": "Duration in seconds for the new shot",
    });

    const fType = field({ label: "Visual type", input: typeSel });
    const fLane = field({ label: "Lane", input: laneSel, hint: "Editorial source policy; defaults to the type's canonical lane." });
    const fDur = field({ label: "Duration (s)", input: durInput });
    fType.append(wiredHint);

    const errSlot = el("div");
    const createBtn = el("button", { class: "btn btn-primary btn-sm", type: "button" }, "Add shot");
    const cancelBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Cancel");

    function closeChooser() {
      addChooser.hidden = true;
      addChooser.replaceChildren();
    }

    cancelBtn.addEventListener("click", closeChooser);
    createBtn.addEventListener("click", async () => {
      errSlot.replaceChildren();
      const d = numOrNull(durInput.value);
      if (d === null || Number.isNaN(d) || !(d > 0) || d > 3600) {
        errSlot.replaceChildren(errorPanel(
          /** @type {any} */ (new Error("Duration must be greater than 0 and below 3600 seconds.")),
        ));
        return;
      }
      createBtn.disabled = true;
      try {
        await createShot(state.config, scene.id, {
          title: "",
          duration_seconds: d,
          lane: laneSel.value,
          visual_type: typeSel.value,
        });
        toast("good", "Shot added",
          `${laneLabel(laneSel.value)} · ${typeSel.value} · appended to S${sceneNumber}`);
        dirty = false;
        closeChooser();
        await pull();
      } catch (err) {
        errSlot.replaceChildren(errorPanel(err));
        createBtn.disabled = false;
      }
    });

    addChooser.append(
      el("div", { class: "grid-2" }, fType, fLane),
      el("div", { class: "grid-2" }, fDur),
      errSlot,
      el("div", { class: "row" }, createBtn, cancelBtn),
    );
    addChooser.hidden = false;
    typeSel.focus();
  }

  addBtn.addEventListener("click", async () => {
    if (sceneLocked) return;
    if (!addChooser.hidden) {
      addChooser.hidden = true;
      return;
    }
    if (!(await confirmDiscard())) return;
    openAddChooser();
  });

  renderSceneBtn.addEventListener("click", async () => {
    if (rendering || !shots.length) return;
    if (dirty) {
      toast("warning", "Unsaved shot edits",
        "Save or revert this shot before rendering so the scene uses the values shown here.");
      return;
    }
    rendering = true;
    renderSceneBtn.disabled = true;
    renderSceneBtn.textContent = "Rendering…";
    try {
      const job = await renderScene(state.config, scene.id);
      toast("good", "Scene render queued", `S${sceneNumber} · job ${job.id}`);
      await pull();
    } catch (err) {
      showDetailError(err);
      toastError(err, `render scene S${sceneNumber}`);
    } finally {
      rendering = false;
      renderSceneBtn.textContent = "Render scene";
      renderSceneBtn.disabled = shots.length === 0;
    }
  });

  pull();
  return { panel, refreshQuiet };
}
