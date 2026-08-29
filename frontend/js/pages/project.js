/**
 * Project Details screen: view and edit the original brief via PATCH
 * /api/projects/{id}, with read-only provenance and explicit, pre-Save
 * invalidation warnings.
 *
 *  - Editable fields mirror the backend `ProjectEdit` contract (extra="forbid"):
 *    title, topic, video_mode, duration_mode, target_duration, aspect_ratio,
 *    fps, resolution, style, audience, narrator_preference, visual_quality,
 *    instructions. `video_mode` ("Video Style") omitted on old projects is
 *    classic; a mode change is a brief-level change and is only PATCHed when
 *    it differs from the saved value.
 *  - Read-only provenance: status, directory, selected script model, created /
 *    updated timestamps, and stage-state chips.
 *  - Editing a brief / narrator field, or a dimension field after planning,
 *    explains the invalidation BEFORE save; dimension edits after a plan also
 *    require an explicit "mark scenes stale" decision (no silent rewrite).
 *  - The form is built once; the live job-feed hook refreshes only the
 *    provenance / stage chips (and the editorial status region), never the
 *    input values, so edits survive ticks.
 *  - Editorial projects (video_mode === "editorial") get an extra
 *    "Editorial Preview" panel driven by the snapshot's `editorial` block
 *    ({ has_edit_plan, edit_plan_url, generate_url, preview_url }); classic
 *    and legacy projects never see it. Without a plan the panel offers a
 *    "Generate Edit Plan" button (only when generate_url is a non-empty
 *    string) that POSTs exactly once with no body, shows a pending label,
 *    and refreshes the panel on success; failures restore the button and
 *    follow the standard error/toast conventions. Generation never happens
 *    automatically on load or live refresh.
 */

import { el, fmtDate, fmtDuration } from "../dom.js";
import {
  state,
  needsProject,
  upsertProject,
} from "../state.js";
import { getProject, editProject, generateEditPlan } from "../api.js";
import {
  field,
  setFieldError,
  loadingState,
  errorPanel,
  icon,
  projectStatusBadge,
  confirm,
  toast,
  toastError,
  stageChip,
  badge,
  emptyState,
} from "../ui.js";
import { navigate } from "../router.js";
import { registerLiveUpdate } from "../app.js";

/** Editable brief fields and how they map to controls. */
const TEXT_FIELDS = [
  { key: "title", label: "Title", required: true, max: 1000 },
  { key: "topic", label: "Topic", required: true },
  { key: "style", label: "Style" },
  { key: "audience", label: "Audience" },
  { key: "visual_quality", label: "Visual quality" },
];
const OPTIONAL_TEXT_FIELDS = [
  { key: "narrator_preference", label: "Narrator preference", nullable: true },
];
const NUMBER_FIELDS = [
  { key: "target_duration", label: "Target duration (seconds)", min: 0.0001, step: 1 },
  { key: "fps", label: "FPS", min: 1, max: 240, step: 1 },
];
const ASPECT_OPTIONS = ["16:9", "9:16", "1:1"];
const DURATION_MODE_OPTIONS = ["fixed", "llm"];
const VIDEO_MODE_OPTIONS = [
  { value: "classic", label: "Classic — Existing scene-based generator" },
  { value: "editorial", label: "Editorial — Motion-graphics compositions" },
];
const BRIEF_FIELDS = new Set([
  "title", "topic", "style", "audience", "visual_quality", "instructions",
  "duration_mode", "video_mode",
]);
const DIMENSION_FIELDS = new Set([
  "target_duration", "aspect_ratio", "fps", "resolution",
]);

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderProject(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" },
      el("h1", {}, "Project Details"),
    ),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject("Select or create a project to view and edit its brief."));
    screen.append(el("div", { class: "mt" },
      el("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("#/new") }, "New Project"),
    ));
    return screen;
  }
  screen.append(buildScreen(state.currentProjectId));
  return screen;
}

/**
 * @param {string} projectId
 * @returns {HTMLElement}
 */
function buildScreen(projectId) {
  const body = el("div", { class: "panel-body" });
  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Brief"),
      el("span", { class: "spacer" }),
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => load(body, provenance, projectId) }, "Refresh"),
    ),
    body,
  );
  // The provenance + stage chips live in their own region so the live feed can
  // refresh them without touching the form inputs.
  const provenance = el("div", { class: "panel-body" });
  const provenancePanel = el("section", { class: "panel" },
    el("div", { class: "panel-title" }, "Provenance (read-only)"),
    provenance,
  );
  // Editorial-only region: filled for editorial projects, kept empty
  // (renderless) for classic / legacy ones.
  const editorialRegion = el("div", {});

  load(body, provenance, editorialRegion, projectId);

  registerLiveUpdate(() => refreshProvenance(provenance, editorialRegion, projectId));
  return el("div", {},
    panel,
    editorialRegion,
    provenancePanel,
  );

  /** @param {HTMLElement} region @param {HTMLElement} prov @param {HTMLElement} editorial @param {string} id */
  async function load(region, prov, editorial, id) {
    region.replaceChildren(loadingState(4));
    prov.replaceChildren(loadingState(2));
    editorial.replaceChildren();
    /** @type {import("../api.js").ProjectSnapshot|null} */
    let snap;
    try {
      snap = await getProject(state.config, id);
    } catch (err) {
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load(region, prov, editorial, id) }, "Retry"),
      ));
      return;
    }
    const content = [buildForm(snap, id)];
    if (Array.isArray(snap.recovery) && snap.recovery.length) {
      const details = snap.recovery
        .map((item) => item.detail)
        .filter(Boolean)
        .join(" ");
      content.unshift(banner(el("div", {},
        el("strong", {}, "Project recovered with warnings"),
        el("div", {}, details || "Some portable project data could not be indexed."),
      )));
    }
    region.replaceChildren(...content);
    renderProvenance(prov, snap);
    renderEditorialRegion(editorial, snap, () => refreshProvenance(prov, editorial, id));
  }

  /** @param {HTMLElement} prov @param {HTMLElement} editorial @param {string} id */
  async function refreshProvenance(prov, editorial, id) {
    try {
      const snap = await getProject(state.config, id);
      renderProvenance(prov, snap);
      renderEditorialRegion(editorial, snap);
    } catch {
      /* keep last-known provenance; the next tick retries */
    }
  }
}

/**
 * Build the editable form (once) plus the action bar.
 * @param {import("../api.js").ProjectSnapshot} snap
 * @param {string} projectId — the project this form is bound to (defends against
 *   the switcher changing the global selection while this form is mounted).
 * @returns {HTMLElement}
 */
function buildForm(snap, projectId) {
  const p = snap.project;
  // Baseline of editable values (what "unsaved changes" is measured against).
  const baseline = readProjectFields(p);

  const inputs = {};
  const fTitle = textField(inputs, "title", p.title, { required: true, max: 1000, hint: "Required, up to 1000 characters." });
  const fTopic = textField(inputs, "topic", p.topic, { required: true, max: 500, hint: "Required, up to 500 characters." });
  const fVideoMode = selectField(inputs, "video_mode", effectiveVideoMode(p), VIDEO_MODE_OPTIONS, {
    label: "Video Style",
    hint: "Classic: the existing scene-based generator. Editorial: motion-graphics compositions.",
  });
  const fDuration = numberField(inputs, "target_duration", p.target_duration, { min: 0.0001, step: "any", hint: "Greater than 0." });
  const fDurationMode = selectField(inputs, "duration_mode", p.duration_mode || "fixed", DURATION_MODE_OPTIONS, {
    hint: "fixed: scenes match the target. llm: the AI decides the runtime from its script (target is ignored).",
  });
  const fAspect = selectField(inputs, "aspect_ratio", p.aspect_ratio, ASPECT_OPTIONS);
  const fFps = numberField(inputs, "fps", p.fps, { min: 1, max: 240, step: 1, hint: "1–240." });
  const fResolution = resolutionField(inputs, p.resolution, { hint: "Positive pixels." });
  const fStyle = textField(inputs, "style", p.style, { required: true, max: 100, hint: "Required, up to 100 characters." });
  const fAudience = textField(inputs, "audience", p.audience, { required: true, max: 100, hint: "Required, up to 100 characters." });
  const fNarrator = textField(inputs, "narrator_preference", p.narrator_preference || "", { nullable: true, max: 300, hint: "Optional, up to 300 characters." });
  const fQuality = textField(inputs, "visual_quality", p.visual_quality, { required: true, max: 100, hint: "Required, up to 100 characters." });
  const fInstructions = textAreaField(inputs, "instructions", p.instructions, { max: 20000, hint: "Optional, up to 20000 characters." });

  const warnRegion = el("div", { class: "mt" });
  const errRegion = el("div", { class: "mt" });
  const saveBtn = el("button", { class: "btn btn-primary", type: "submit" }, "Save");
  const resetBtn = el("button", { class: "btn btn-ghost", type: "button" }, "Reset unsaved changes");
  const clearBtn = el("button", { class: "btn btn-ghost", type: "button" }, "Clear errors");

  const form = el("form", { novalidate: true, onsubmit: (ev) => { ev.preventDefault(); doSave(); } },
    el("div", { class: "grid-2" }, fTitle, fTopic),
    fVideoMode,
    el("div", { class: "grid-2" }, fDuration, fAspect),
    el("div", { class: "grid-2" }, fFps, fResolution),
    fDurationMode,
    el("div", { class: "grid-2" }, fStyle, fAudience),
    el("div", { class: "grid-2" }, fNarrator, fQuality),
    fInstructions,
    warnRegion,
    el("div", { class: "row mt" }, saveBtn, resetBtn, clearBtn),
    errRegion,
  );

  /** Recompute and show the pre-Save invalidation warning. */
  function updateWarning() {
    warnRegion.replaceChildren();
    const { changed, briefChanged, dimensionChanged, narratorChanged } = diffFields(baseline, readInputs(inputs));
    if (!changed.size) return;
    const hasPlan = (snap.scenes || []).length > 0;
    const stages = invalidatedStages({ briefChanged, dimensionChanged, narratorChanged });
    const bits = [];
    if (briefChanged) bits.push("the script plan, scene prompts/references/visuals, narration, music, subtitles, timeline, render work, thumbnails, and metadata");
    if (narratorChanged) bits.push("narration, subtitles, timeline, render work, thumbnails, and metadata");
    if (dimensionChanged) bits.push("timeline, render work, thumbnails, and metadata" + (hasPlan ? " (existing scenes will be marked stale)" : ""));
    const message = el("div", {},
      el("div", { class: "b-title" }, "This change will invalidate downstream work"),
      el("div", { class: "b-body" },
        "Editing " + describeChanges(changed) + " invalidates " + joinList(bits) +
        ". Existing scene narration/prompts are never rewritten. Re-plan from the Script screen.",
      ),
      stages.length ? el("div", { class: "b-body muted small" }, "Stages to invalidate: " + stages.join(", ")) : null,
    );
    warnRegion.append(banner(message));
  }

  function clearErrors() {
    for (const wrap of form.querySelectorAll(".field")) setFieldError(wrap, null);
    errRegion.replaceChildren();
  }

  function resetChanges() {
    setInputs(inputs, baseline);
    clearErrors();
    updateWarning();
  }

  resetBtn.onclick = resetChanges;
  clearBtn.onclick = clearErrors;
  // Live warning as the user edits any field.
  form.addEventListener("input", updateWarning);
  form.addEventListener("change", updateWarning);
  updateWarning();

  /** @type {{submitting: boolean}} */
  const guard = { submitting: false };
  async function doSave() {
    if (guard.submitting) return;
    clearErrors();
    const values = readInputs(inputs);
    const { changed, briefChanged, dimensionChanged, narratorChanged } = diffFields(baseline, values);
    if (!changed.size) {
      toast("info", "No changes", "The brief matches the saved project.");
      return;
    }
    const problems = validateInputs(inputs, values, changed);
    if (problems.length) {
      for (const [key, msg] of problems) setFieldError(inputs[key].wrap, msg);
      return;
    }
    const hasPlan = (snap.scenes || []).length > 0;
    let markStale = false;
    // Brief (title/topic/style/…) and dimension edits after planning require an
    // explicit "mark scenes stale or cancel" decision; scenes are never silently
    // rewritten.
    if ((briefChanged || dimensionChanged) && hasPlan) {
      const ok = await confirm({
        title: "Save and mark scenes stale?",
        message: "You changed " + describeChanges(changed) + ". The existing scenes' narration and prompts will be kept but marked stale (status → draft) so you can re-plan explicitly. Timeline, render work, thumbnails, and metadata are invalidated. Nothing is silently rewritten or deleted.",
        confirmLabel: "Save and mark scenes stale",
        kind: "primary",
      });
      if (!ok) return;
      markStale = true;
    }

    guard.submitting = true;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    // Build the PATCH body only from the changed fields (changed is a Set of
    // keys; read the new values from the validated inputs).
    const body = buildPatchBody(changed, values);
    if (markStale) body.mark_scenes_stale = true;
    try {
      const result = await editProject(state.config, projectId, body);
      const saved = result.project;
      upsertProject(saved);
      // Update baseline to the saved project so further edits diff correctly.
      Object.assign(baseline, readProjectFields(saved));
      const inv = result.invalidated_stages || [];
      toast("good", "Project saved", saved.title);
      if (inv.length) {
        errRegion.replaceChildren(banner(el("div", {},
          el("div", { class: "b-title" }, "Saved — downstream work invalidated"),
          el("div", { class: "b-body" }, "Stages invalidated: " + inv.join(", ") + "."),
        )));
      } else {
        errRegion.replaceChildren();
      }
      updateWarning();
    } catch (err) {
      errRegion.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: doSave }, "Try again"),
      ));
      toastError(err, "Save failed");
    } finally {
      guard.submitting = false;
      saveBtn.disabled = false;
      saveBtn.textContent = "Save";
    }
  }

  return form;
}

/* --- Field builders ------------------------------------------------------- */

function textField(inputs, key, value, opts = {}) {
  const input = el("input", { id: `pd-${key}`, type: "text", value: value ?? "" });
  if (opts.max) input.setAttribute("maxlength", String(opts.max));
  const f = field({ label: opts.label ?? humanize(key), input, hint: opts.hint });
  inputs[key] = { input, wrap: f, kind: "text", nullable: !!opts.nullable, required: !!opts.required, max: opts.max };
  return f;
}

function textAreaField(inputs, key, value, opts = {}) {
  const input = el("textarea", { id: `pd-${key}`, rows: "4" }, value ?? "");
  const hint = opts.hint || "Optional.";
  const f = field({ label: humanize(key), input, hint });
  inputs[key] = { input, wrap: f, kind: "textarea", max: opts.max };
  return f;
}

function numberField(inputs, key, value, opts = {}) {
  const input = el("input", { id: `pd-${key}`, type: "number", value: String(value) });
  if (opts.min != null) input.setAttribute("min", String(opts.min));
  if (opts.max != null) input.setAttribute("max", String(opts.max));
  if (opts.step != null) input.setAttribute("step", String(opts.step));
  const f = field({ label: humanize(key), input, hint: opts.hint });
  inputs[key] = { input, wrap: f, kind: "number", min: opts.min, max: opts.max };
  return f;
}

function selectField(inputs, key, value, options, opts = {}) {
  // Options may be plain strings (label = value) or {value, label} pairs, so a
  // select can show descriptive copy for each raw value.
  const norm = options.map((o) => (typeof o === "string" ? { value: o, label: o } : o));
  const input = el("select", { id: `pd-${key}` },
    ...norm.map((o) => el("option", { value: o.value }, o.label)),
  );
  input.value = value;
  const f = field({ label: opts.label ?? humanize(key), input, hint: opts.hint });
  inputs[key] = { input, wrap: f, kind: "select" };
  return f;
}

function resolutionField(inputs, value, opts = {}) {
  const w = el("input", { id: "pd-resolution-w", type: "number", min: "1", step: "1", value: String(value[0]), style: { width: "110px" } });
  const h = el("input", { id: "pd-resolution-h", type: "number", min: "1", step: "1", value: String(value[1]), style: { width: "110px" } });
  const row = el("div", { class: "row", style: { gap: "6px" } }, w, "×", h);
  const f = field({ label: "Resolution (width × height)", input: row, hint: opts.hint });
  inputs.resolution = { input: { w, h }, wrap: f, kind: "resolution" };
  return f;
}

/* --- Value reading / diffing ---------------------------------------------- */

/**
 * Existing projects may omit video_mode; only an explicit editorial value
 * opts into the new generator.
 * @param {{video_mode?: any} | null | undefined} project
 */
export function effectiveVideoMode(project) {
  return project?.video_mode === "editorial" ? "editorial" : "classic";
}

/** @param {any} p */
export function readProjectFields(p) {
  return {
    title: p.title,
    topic: p.topic,
    target_duration: p.target_duration,
    duration_mode: p.duration_mode || "fixed",
    video_mode: effectiveVideoMode(p),
    aspect_ratio: p.aspect_ratio,
    fps: p.fps,
    resolution: p.resolution,
    style: p.style,
    audience: p.audience,
    narrator_preference: p.narrator_preference,
    visual_quality: p.visual_quality,
    instructions: p.instructions,
  };
}

/** @param {Record<string, any>} inputs */
export function readInputs(inputs) {
  const out = {};
  for (const key of Object.keys(inputs)) {
    const spec = inputs[key];
    if (spec.kind === "resolution") {
      out.resolution = [Number(spec.input.w.value), Number(spec.input.h.value)];
    } else if (spec.kind === "number") {
      out[key] = Number(spec.input.value);
    } else if (spec.kind === "text" || spec.kind === "textarea") {
      const raw = spec.input.value;
      out[key] = spec.nullable ? (raw.trim() ? raw : null) : raw;
    } else {
      out[key] = spec.input.value;
    }
  }
  return out;
}

/** @param {Record<string, any>} inputs @param {Record<string, any>} values */
export function setInputs(inputs, values) {
  for (const key of Object.keys(inputs)) {
    const spec = inputs[key];
    const v = values[key];
    if (spec.kind === "resolution") {
      spec.input.w.value = String(v[0]);
      spec.input.h.value = String(v[1]);
    } else if (spec.kind === "select") {
      spec.input.value = v;
    } else if (spec.kind === "text" || spec.kind === "textarea") {
      spec.input.value = v == null ? "" : v;
    } else {
      spec.input.value = String(v);
    }
  }
}

/**
 * @param {Record<string, any>} baseline
 * @param {Record<string, any>} values
 */
export function diffFields(baseline, values) {
  const changed = new Set();
  for (const key of Object.keys(values)) {
    if (!fieldsEqual(baseline[key], values[key])) changed.add(key);
  }
  return {
    changed,
    briefChanged: [...changed].some((k) => BRIEF_FIELDS.has(k)),
    dimensionChanged: [...changed].some((k) => DIMENSION_FIELDS.has(k)),
    narratorChanged: changed.has("narrator_preference"),
  };
}

/** @param {Set<string>} changed @param {Record<string, any>} values */
export function buildPatchBody(changed, values) {
  const body = {};
  for (const key of changed) body[key] = values[key];
  return body;
}

function fieldsEqual(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => x === b[i]);
  return a === b;
}

/**
 * @param {{briefChanged: boolean, dimensionChanged: boolean, narratorChanged: boolean}} d
 * @returns {string[]}
 */
function invalidatedStages(d) {
  const stages = new Set();
  if (d.briefChanged) {
    for (const s of ["plan", "references", "visuals", "narration", "music", "subtitles", "timeline", "render_preview", "quality_control", "render_final", "thumbnails", "metadata"]) stages.add(s);
  }
  if (d.dimensionChanged) {
    for (const s of ["timeline", "render_preview", "quality_control", "render_final", "thumbnails", "metadata"]) stages.add(s);
  }
  if (d.narratorChanged) {
    for (const s of ["narration", "subtitles", "timeline", "render_preview", "quality_control", "render_final", "thumbnails", "metadata"]) stages.add(s);
  }
  return [...stages];
}

/** @param {Set<string>} changed */
function describeChanges(changed) {
  const labels = [...changed].map(humanize);
  return joinList(labels);
}

function joinList(items) {
  if (items.length <= 1) return items[0] || "";
  return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
}

/* --- Validation (mirrors ProjectEdit bounds) ------------------------------ */

/**
 * @param {Record<string, any>} inputs
 * @param {Record<string, any>} values
 * @param {Set<string>} changed
 * @returns {[string, string][]}
 */
function validateInputs(inputs, values, changed) {
  const problems = [];
  const fail = (key, msg) => problems.push([key, msg]);
  const has = (key) => changed.has(key);
  if (has("title") && (!values.title || !values.title.trim())) fail("title", "Title is required.");
  else if (has("title") && values.title.length > 1000) fail("title", "Title must be 1000 characters or fewer.");
  if (has("topic") && (!values.topic || !values.topic.trim())) fail("topic", "Topic is required.");
  else if (has("topic") && values.topic.length > 500) fail("topic", "Topic must be 500 characters or fewer.");
  if (has("target_duration") && (!Number.isFinite(values.target_duration) || values.target_duration <= 0)) fail("target_duration", "Duration must be a number greater than 0.");
  const fps = values.fps;
  if (has("fps") && (!Number.isInteger(fps) || fps < 1 || fps > 240)) fail("fps", "FPS must be an integer from 1 to 240.");
  const [w, h] = values.resolution;
  if (has("resolution") && (!Number.isInteger(w) || w <= 0)) fail("resolution", "Width must be a positive integer.");
  else if (has("resolution") && (!Number.isInteger(h) || h <= 0)) fail("resolution", "Height must be a positive integer.");
  if (has("style") && (!values.style || !values.style.trim())) fail("style", "Style is required.");
  else if (has("style") && values.style.length > 100) fail("style", "Style must be 100 characters or fewer.");
  if (has("audience") && (!values.audience || !values.audience.trim())) fail("audience", "Audience is required.");
  else if (has("audience") && values.audience.length > 100) fail("audience", "Audience must be 100 characters or fewer.");
  if (has("narrator_preference") && values.narrator_preference && values.narrator_preference.length > 300) fail("narrator_preference", "Narrator preference must be 300 characters or fewer.");
  if (has("visual_quality") && (!values.visual_quality || !values.visual_quality.trim())) fail("visual_quality", "Visual quality is required.");
  else if (has("visual_quality") && values.visual_quality.length > 100) fail("visual_quality", "Visual quality must be 100 characters or fewer.");
  if (has("instructions") && values.instructions && values.instructions.length > 20000) fail("instructions", "Instructions must be 20000 characters or fewer.");
  return problems;
}

/* --- Provenance + stage chips --------------------------------------------- */

/**
 * @param {HTMLElement} prov
 * @param {import("../api.js").ProjectSnapshot} snap
 */
function renderProvenance(prov, snap) {
  const p = snap.project;
  const stages = (snap.stage_state && snap.stage_state.stages) || {};
  prov.replaceChildren(
    el("dl", { class: "kv" },
      el("dt", {}, "Status"), el("dd", {}, projectStatusBadge(p.status)),
      el("dt", {}, "Directory"), el("dd", {}, el("span", { class: "mono" }, snap.directory || "—")),
      el("dt", {}, "Selected script model"), el("dd", {}, el("span", { class: "mono" }, p.selected_llm_model || "auto")),
      el("dt", {}, "Created"), el("dd", {}, el("span", { class: "mono" }, fmtDate(p.created_at))),
      el("dt", {}, "Updated"), el("dd", {}, el("span", { class: "mono" }, fmtDate(p.updated_at))),
    ),
    Object.keys(stages).length
      ? el("div", { class: "row mt", style: { flexWrap: "wrap", gap: "8px" } },
        ...Object.entries(stages).map(([name, st]) => stageChip(name, st)))
      : el("div", { class: "muted small mt" }, "No pipeline stages have run yet — run planning from the Script screen."),
  );
}

/* --- Editorial Preview ----------------------------------------------------- */

const GENERATE_PLAN_LABEL = "Generate Edit Plan";
const GENERATE_PLAN_PENDING_LABEL = "Generating…";

/**
 * Non-empty backend-provided string or null; malformed values (numbers,
 * whitespace, objects) never produce a request URL.
 * @param {unknown} value
 */
function usableUrl(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * Primary "Generate Edit Plan" action. One click issues exactly one bodyless
 * POST to the snapshot-provided generate URL (no force flag); the button is
 * disabled with a pending label while the request runs and is restored with
 * the standard error surfaces if it fails. On success the panel is refreshed
 * through `onGenerated` so it re-renders as "Edit Plan available".
 * @param {string} generateUrl
 * @param {HTMLElement} errors — region for the inline failure panel
 * @param {(() => any) | null} [onGenerated]
 * @returns {HTMLElement}
 */
function buildGeneratePlanButton(generateUrl, errors, onGenerated = null) {
  const button = el("button", { class: "btn btn-primary", type: "button" }, GENERATE_PLAN_LABEL);
  let pending = false;
  button.addEventListener("click", async () => {
    if (pending) return;
    pending = true;
    button.disabled = true;
    button.textContent = GENERATE_PLAN_PENDING_LABEL;
    errors.replaceChildren();
    try {
      await generateEditPlan(state.config, generateUrl);
      if (onGenerated) await onGenerated();
    } catch (err) {
      pending = false;
      button.disabled = false;
      button.textContent = GENERATE_PLAN_LABEL;
      errors.replaceChildren(errorPanel(err));
      toastError(err, "Edit Plan generation failed");
    }
  });
  return button;
}

/**
 * Editorial Preview panel for editorial projects; classic / legacy projects
 * get null so the region stays empty and the rest of the page is untouched.
 * A missing or malformed `editorial` snapshot is treated defensively as
 * has_edit_plan=false, and a missing/malformed generate_url simply omits
 * the Generate button. The generate endpoint is never called during render.
 *
 * @param {import("../api.js").ProjectSnapshot} snap
 * @param {(() => any) | null} [onGenerated] — refresh hook after a successful generation
 * @returns {HTMLElement | null}
 */
export function editorialPreviewSection(snap, onGenerated = null) {
  if (effectiveVideoMode(snap && snap.project) !== "editorial") return null;
  const editorial = (snap && typeof snap.editorial === "object" && snap.editorial) ? snap.editorial : null;
  const hasPlan = !!(editorial && editorial.has_edit_plan);
  const previewUrl = hasPlan ? usableUrl(editorial.preview_url) : null;
  const generateUrl = hasPlan ? null : usableUrl(editorial && editorial.generate_url);
  const body = el("div", { class: "panel-body" });
  const errors = el("div", { class: "mt" });
  if (!hasPlan) {
    body.append(emptyState(
      "No Edit Plan yet",
      "This editorial project has no Edit Plan generated yet. Once one exists, a deterministic HTML preview becomes available here.",
      generateUrl ? [buildGeneratePlanButton(generateUrl, errors, onGenerated)] : [],
    ));
  } else {
    body.append(el("div", { class: "row", style: { flexWrap: "wrap", gap: "10px" } },
      badge("good", "Edit Plan available"),
      previewUrl
        ? el("a", { class: "btn btn-primary btn-sm", href: previewUrl, target: "_blank", rel: "noopener" }, "Open Preview")
        : null,
    ));
  }
  body.append(errors);
  return el("section", { class: "panel" },
    el("div", { class: "panel-title" }, "Editorial Preview"),
    body,
  );
}

/**
 * Mount (or clear) the Editorial Preview panel for the current snapshot.
 * Pure rendering: no request is issued here. After a successful generation
 * the default hook re-fetches the snapshot so the panel switches to
 * "Edit Plan available" + "Open Preview".
 * @param {HTMLElement} region
 * @param {import("../api.js").ProjectSnapshot} snap
 * @param {(() => any) | null} [onGenerated]
 */
export function renderEditorialRegion(region, snap, onGenerated = null) {
  const hook = onGenerated || (() => reloadEditorialRegion(region, snap));
  const section = editorialPreviewSection(snap, hook);
  region.replaceChildren(...(section ? [section] : []));
}

/**
 * Re-read the project snapshot and re-render only the Editorial Preview
 * region (the form inputs and provenance of the mounted screen are refreshed
 * by the caller's own hook when present).
 * @param {HTMLElement} region
 * @param {import("../api.js").ProjectSnapshot} snap
 */
async function reloadEditorialRegion(region, snap) {
  const fresh = await getProject(state.config, snap.project.id);
  renderEditorialRegion(region, fresh);
}

/* --- Small helpers -------------------------------------------------------- */

/**
 * @param {HTMLElement} content
 * @returns {HTMLElement}
 */
function banner(content) {
  return el("div", { class: "banner banner-warning" },
    icon("alert", 18),
    el("div", {}, content),
  );
}

/** @param {string} key */
function humanize(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
