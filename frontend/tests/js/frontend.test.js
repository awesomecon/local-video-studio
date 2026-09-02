/**
 * Focused frontend logic tests (Phase 3 review fixes).
 *
 * Pure ES modules only - no backend, no network. Executed by
 * `run_js_tests.py`, which loads this file through headless Chromium via a
 * generated harness page and reads the results block below.
 *
 * Covered areas:
 *   1. Materialized scenes     - sceneHasExplicitShots() decides when the
 *                                legacy scene-level visual editor stands down.
 *   2. Timeline overlap geometry - compiledShotSpans()/compiledSpanSeconds()
 *                                keep full shot durations, overlap later
 *                                shots over the prior tail by the incoming
 *                                transition, and never consume the first
 *                                shot's incoming transition.
 *   3. Add Shot defaults       - defaultNewShot() always proposes a wired
 *                                lane/visual-type combination.
 *   4. Selected-shot navigation  - sceneEditorHash()/parseRoute() round-trip
 *                                the /shot/{id} deep link (including
 *                                hyphenated implicit ids).
 *   5. Video style (video_mode)  - the New Project form defaults the selector
 *                                to classic and POSTs the selected mode; an
 *                                omitted/unknown mode on an existing project
 *                                reads as classic; a changed mode is PATCHed
 *                                and an unchanged one is not; reset restores
 *                                the saved mode.
 *   6. Editorial Preview section - only editorial snapshots render the panel;
 *                                no plan shows the empty state, a plan shows
 *                                the status and an Open Preview link that
 *                                opens preview_url in a new tab with
 *                                rel="noopener".
 *   7. Editorial Generate Edit Plan - the button appears only for an
 *                                editorial project without a plan and a valid
 *                                generate_url, one click issues exactly one
 *                                bodyless POST with a disabled pending label,
 *                                success refreshes the panel to the plan
 *                                state, failure restores the button with the
 *                                standard error surfaces, and live refreshes
 *                                never trigger generation.
 *   8. Editorial plan provenance  - current plans keep the good state with a
 *                                small Current note; stale plans warn with
 *                                readable reasons (project / script /
 *                                word_timings, multiple allowed); untracked
 *                                plans get a neutral note and are never
 *                                called stale or broken; missing/malformed
 *                                plan_status degrades to the classic state;
 *                                Open Preview survives every plan state, no
 *                                Generate button appears once a plan exists,
 *                                and rendering these states issues no
 *                                network calls.
 *   9. Export mode presentation   - classic and legacy screens keep the
 *                                original wording, readiness rows, chips,
 *                                and confirmation text; editorial screens
 *                                show the additive workflow (Editorial
 *                                canvas → timeline → preview → quality check
 *                                → final MP4 → frame extraction), an
 *                                editorial_visual chip before timeline, a
 *                                readable "Rendering Editorial canvas" stage
 *                                label, and Edit Plan provenance in the
 *                                readiness summary (stale/untracked plans
 *                                stay usable, missing/malformed metadata
 *                                degrades to "not generated", and rendering
 *                                the summary issues no network requests).
 *  10. Editorial display settings - Captions / Editorial text switches are
 *                                shown only for strict-boolean snapshot
 *                                values behind a usable project-local
 *                                settings_url; a change PATCHes only the
 *                                changed field, one mutation in flight,
 *                                both controls disabled while saving,
 *                                live refreshes preserve the pending
 *                                controls, success re-renders from a fresh
 *                                snapshot, failure restores the previous
 *                                value with the standard error surfaces,
 *                                and the full Edit Plan is never fetched on
 *                                this path.
 *  11. Editorial composition overview - "Show compositions" is the only
 *                                fetcher of the Edit Plan (explicit click
 *                                via edit_plan_url); the compact list
 *                                renders number/id, start, duration,
 *                                template, and asset/element/event/
 *                                narration-ref/evidence/illustration/locked
 *                                counts; malformed plans and compositions
 *                                degrade to error states or placeholders
 *                                without crashing; loading, error + retry,
 *                                and the open list all survive live
 *                                refreshes; the safe Download link is built
 *                                only from project-local paths.
 *  12. Export display settings    - the editorial readiness summary reports
 *                                captions / editorial text enabled|disabled
 *                                only for strict booleans (malformed values
 *                                are omitted, never guessed); classic and
 *                                legacy screens are unchanged.
 *  13. Editorial workspace screen  - the dedicated Editorial screen routes
 *                                to the workspace page: classic projects get
 *                                a pointer state with no plan fetch, the
 *                                no-plan state exposes the guarded Generate
 *                                action (one bodyless POST, then the screen
 *                                lands in the workspace), a plan renders the
 *                                time-proportional sequence strip, the
 *                                selected composition's strict detail
 *                                controls, and an embedded preview that is
 *                                mounted only behind the explicit toggle and
 *                                only for the mounted project's exact
 *                                project-local preview path (anything else
 *                                degrades to an unavailable note); template
 *                                labels and the preview aspect-ratio helper
 *                                degrade malformed input to readable
 *                                fallbacks.
 */

import {
  compiledShotSpans,
  compiledSpanSeconds,
  defaultLane,
  defaultNewShot,
  implicitShotFromScene,
  isWiredVisualType,
  sceneHasExplicitShots,
} from "../../js/shots.js";
import { parseRoute, sceneEditorHash } from "../../js/router.js";
import {
  renderEditorial,
  safeEditorialPreviewUrl,
  templateLabel,
  previewAspectRatio,
} from "../../js/pages/editorial.js";
import { renderNewProject } from "../../js/pages/new-project.js";
import { state } from "../../js/state.js";
import {
  effectiveVideoMode,
  readProjectFields,
  diffFields,
  buildPatchBody,
  setInputs,
  editorialPreviewSection,
  renderEditorialRegion,
  buildEditorialDisplayControls,
  summarizeEditPlanCompositions,
  summarizeEditorialRevision,
  parseCompositionEditor,
  safeEditPlanDownloadUrl,
  localApiPath,
  projectEditorialApiPath,
  createEditorialController,
} from "../../js/pages/project.js";
import {
  renderExport,
  exportVideoMode,
  exportDescriptionText,
  exportWorkflowText,
  exportForceConfirmMessage,
  editorialPlanSummary,
  editorialDisplaySettings,
  renderInputSummary,
  renderStageLabel,
} from "../../js/pages/export.js";

const results = [];

function record(name, fn) {
  try {
    fn();
    results.push([name, true, ""]);
  } catch (err) {
    results.push([name, false, String(err && err.message ? err.message : err)]);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || "assertion failed");
}

function eq(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${msg || "eq"}: got ${a}, want ${b}`);
}

// Async twin of record(): for tests that drive a real submit + a stubbed
// fetch. Resolves via the microtask queue, which --virtual-time-budget flushes.
async function recordAsync(name, fn) {
  try {
    await fn();
    results.push([name, true, ""]);
  } catch (err) {
    results.push([name, false, String(err && err.message ? err.message : err)]);
  }
}

// Flush the microtask queue a handful of turns (enough for the app's
// createProject -> request -> fetch -> res.text() -> JSON.parse chain).
async function flush(turns = 30) {
  for (let i = 0; i < turns; i++) await Promise.resolve();
}

// Stub globalThis.fetch, recording each call and answering via handler(call).
function stubFetch(handler) {
  const calls = [];
  globalThis.fetch = (url, opts) => {
    const call = {
      url: String(url),
      method: (opts && opts.method) || "GET",
      body: (opts && opts.body != null) ? JSON.parse(opts.body) : null,
    };
    calls.push(call);
    const outcome = (handler && handler(call)) || {};
    const status = outcome.status || 200;
    const payload = outcome.payload !== undefined ? outcome.payload : {};
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };
  return calls;
}

/* --- 1. Materialized scenes -------------------------------------------- */

record("materialized: shot_summary.materialized=true wins", () => {
  eq(sceneHasExplicitShots({ shot_summary: { materialized: true } }), true);
});

record("materialized: implicit-only projection is not explicit", () => {
  eq(sceneHasExplicitShots({
    shots: [{ id: "s-implicit", implicit: true }],
    shot_summary: { materialized: false },
  }), false);
});

record("materialized: any stored shot makes the scene explicit", () => {
  eq(sceneHasExplicitShots({
    shots: [{ id: "a", implicit: false }, { id: "b", implicit: false }],
    shot_summary: { materialized: true },
  }), true);
});

record("materialized: legacy payload without shots is not explicit", () => {
  eq(sceneHasExplicitShots({ id: "sc1", duration: 5 }), false);
  eq(sceneHasExplicitShots(null), false);
});

record("materialized: shots present without summary counts as stored", () => {
  // Older snapshots mark every projected entry implicit; a non-implicit
  // entry therefore means stored shots exist.
  eq(sceneHasExplicitShots({ shots: [{ id: "x", implicit: false }] }), true);
});

/* --- 2. Timeline overlap geometry --------------------------------------- */

const GEOM_SHOTS = [
  { id: "a", index: 0, duration_seconds: 5, transition_in: { kind: "cut", duration_seconds: 0 } },
  { id: "b", index: 1, duration_seconds: 6, transition_in: { kind: "crossfade", duration_seconds: 2 } },
  { id: "c", index: 2, duration_seconds: 4, transition_in: { kind: "fade_through_black", duration_seconds: 1 } },
];

record("geometry: full durations, later shots overlap the prior tail", () => {
  const spans = compiledShotSpans(GEOM_SHOTS);
  eq(spans.map((s) => s.start), [0, 3, 8], "compiled starts");
  eq(spans.map((s) => s.duration), [5, 6, 4], "full durations kept");
  eq(spans.map((s) => s.overlap), [0, 2, 1], "incoming overlaps");
});

record("geometry: span equals sum(durations) - sum(non-first overlaps)", () => {
  eq(compiledSpanSeconds(compiledShotSpans(GEOM_SHOTS)), 12);
  // Backend formula computed independently.
  const backendStyle = GEOM_SHOTS.reduce(
    (n, s, i) => n + s.duration_seconds - (i ? s.transition_in.duration_seconds : 0),
    0,
  );
  eq(compiledSpanSeconds(compiledShotSpans(GEOM_SHOTS)), backendStyle);
});

record("geometry: first shot's incoming transition never consumes width", () => {
  const withFirstOverlap = [
    { id: "a", index: 0, duration_seconds: 5, transition_in: { kind: "crossfade", duration_seconds: 0.35 } },
    ...GEOM_SHOTS.slice(1),
  ];
  const spans = compiledShotSpans(withFirstOverlap);
  eq(spans[0].start, 0, "first shot still starts at zero");
  eq(spans[0].overlap, 0, "first overlap ignored");
  eq(compiledSpanSeconds(spans), 12, "span unchanged");
});

record("geometry: single-shot scene ignores its incoming transition entirely", () => {
  const solo = compiledShotSpans([
    { id: "only", index: 0, duration_seconds: 7, transition_in: { kind: "dip_to_white", duration_seconds: 1 } },
  ]);
  eq(solo.length, 1);
  eq(solo[0].start, 0);
  eq(solo[0].overlap, 0);
  eq(compiledSpanSeconds(solo), 7);
});

record("geometry: invalid oversized overlap is clamped defensively", () => {
  const bad = [
    { id: "a", index: 0, duration_seconds: 5, transition_in: { kind: "cut", duration_seconds: 0 } },
    { id: "b", index: 1, duration_seconds: 6, transition_in: { kind: "crossfade", duration_seconds: 99 } },
  ];
  const spans = compiledShotSpans(bad);
  eq(spans[1].overlap, 5, "clamped to the shorter adjacent duration");
  eq(spans[1].start, 0, "start stays within the scene");
  assert(Number.isFinite(compiledSpanSeconds(spans)));
});

record("geometry: overlay markers anchor at compiled starts", () => {
  // Marker absolute time = itemStart + span.start + cue.start; verify the
  // span.start component the timeline positions from.
  const spans = compiledShotSpans(GEOM_SHOTS);
  const cueAt = (spanIdx, cueStart) => spans[spanIdx].start + cueStart;
  eq(cueAt(0, 1), 1, "cue on first shot");
  eq(cueAt(1, 0.5), 3.5, "cue on overlapped second shot");
  eq(cueAt(2, 2), 10, "cue on third shot");
});

/* --- 3. Add Shot defaults ------------------------------------------------ */

record("add-shot: empty scene defaults to a wired combination", () => {
  const d = defaultNewShot([]);
  eq(d.visual_type, "krea2_still");
  eq(isWiredVisualType(d.visual_type), true, "default must be wired");
  eq(d.lane, defaultLane(d.visual_type));
  eq(d.lane, "image");
  eq(d.duration_seconds, 5);
});

record("add-shot: inherits the previous shot's wired recipe", () => {
  const d = defaultNewShot([{ visual_type: "qwen_image_still", duration_seconds: 7 }]);
  eq(d.visual_type, "qwen_image_still");
  eq(d.lane, "image");
  eq(d.duration_seconds, 7);
});

record("add-shot: never silently inherits an unwired type", () => {
  const d = defaultNewShot([{ visual_type: "flux_still", duration_seconds: 4 }]);
  eq(d.visual_type, "krea2_still", "unwired previous falls back to wired default");
  eq(isWiredVisualType(d.visual_type), true);
  eq(d.duration_seconds, 4, "duration still inherits");
});

record("add-shot: H3 predecessor maps to the H3 lane", () => {
  const d = defaultNewShot([{ visual_type: "h3_audiovisual", duration_seconds: 6 }]);
  eq(d.visual_type, "h3_audiovisual");
  eq(d.lane, "h3");
});

record("add-shot: garbage durations fall back to five seconds", () => {
  eq(defaultNewShot([{ visual_type: "graphic_screen", duration_seconds: NaN }]).duration_seconds, 5);
  eq(defaultNewShot([{ visual_type: "graphic_screen", duration_seconds: -3 }]).duration_seconds, 5);
});

/* --- 4. Selected-shot navigation ----------------------------------------- */

record("navigation: hash builder emits plain and deep-linked forms", () => {
  eq(sceneEditorHash("sc1"), "#/scene/sc1");
  eq(sceneEditorHash("sc1", null), "#/scene/sc1");
  eq(sceneEditorHash("sc1", "sh2"), "#/scene/sc1/shot/sh2");
  eq(sceneEditorHash("sc1", "sc1-implicit"), "#/scene/sc1/shot/sc1-implicit");
});

record("navigation: parser resolves scene-only routes", () => {
  window.location.hash = "#/scene/abc123";
  const route = parseRoute();
  eq(route.name, "scene-editor");
  eq(route.param, "abc123");
  eq(route.param2 || null, null);
});

record("navigation: parser resolves the exact-shot deep link", () => {
  window.location.hash = "#/scene/abc123/shot/def-456-ghi";
  const route = parseRoute();
  eq(route.name, "scene-editor");
  eq(route.param, "abc123");
  eq(route.param2, "def-456-ghi");
});

record("navigation: builder/parser round-trip preserves the shot", () => {
  const hash = sceneEditorHash("scene-9", "scene-9-implicit");
  window.location.hash = hash;
  const route = parseRoute();
  eq(route.name, "scene-editor");
  eq(route.param, "scene-9");
  eq(route.param2, "scene-9-implicit");
  eq(hash, sceneEditorHash(route.param, route.param2));
});

record("navigation: other routes are unaffected", () => {
  window.location.hash = "#/timeline";
  eq(parseRoute().name, "timeline");
  window.location.hash = "#/storyboard";
  eq(parseRoute().name, "storyboard");
});

/* --- sanity: implicit projection feeds the same helpers ------------------ */

record("implicit projection keeps ids stable for deep links", () => {
  const scene = { id: "sc-42", title: "T", duration: 8, status: "generated", locked: false, transition: "" };
  const implicit = implicitShotFromScene(scene);
  eq(implicit.id, "sc-42-implicit");
  eq(implicit.status, "ready");
  eq(sceneHasExplicitShots({ shots: [implicit] }), false);
});

/* --- 5. Video style (video_mode) ---------------------------------------- */

// A legacy project payload that omits `video_mode` entirely.
const LEGACY_PROJECT = {
  id: "proj-1", slug: "legacy", title: "T", topic: "P",
  target_duration: 120, duration_mode: "fixed", aspect_ratio: "16:9",
  fps: 24, resolution: [1920, 1080], style: "documentary", audience: "general",
  narrator_preference: null, visual_quality: "balanced", instructions: "",
};

record("video-mode: omitted/unknown mode on an existing project means classic", () => {
  eq(effectiveVideoMode(LEGACY_PROJECT), "classic", "omitted -> classic");
  eq(effectiveVideoMode({}), "classic", "empty payload -> classic");
  eq(effectiveVideoMode(null), "classic", "null -> classic");
  eq(effectiveVideoMode({ video_mode: "classic" }), "classic", "explicit classic");
  eq(effectiveVideoMode({ video_mode: "editorial" }), "editorial", "explicit editorial preserved");
  eq(effectiveVideoMode({ video_mode: "weird" }), "classic", "unknown value falls back to classic");
});

record("video-mode: baseline readProjectFields is classic when the field is missing", () => {
  eq(readProjectFields(LEGACY_PROJECT).video_mode, "classic", "missing -> classic baseline");
  eq(readProjectFields({ ...LEGACY_PROJECT, video_mode: "editorial" }).video_mode, "editorial", "saved editorial preserved in baseline");
});

record("video-mode: a changed mode is PATCHed; an unchanged one is omitted", () => {
  const baseline = readProjectFields(LEGACY_PROJECT); // classic
  const edited = { ...baseline, video_mode: "editorial" };
  const d = diffFields(baseline, edited);
  assert(d.changed.has("video_mode"), "change detected");
  const body = buildPatchBody(d.changed, edited);
  eq(body.video_mode, "editorial", "PATCH carries the new mode");
  eq(Object.keys(body).length, 1, "only the changed field is sent");

  const dSame = diffFields(baseline, { ...baseline });
  eq(buildPatchBody(dSame.changed, { ...baseline }), {}, "no change -> empty PATCH body");
});

record("video-mode: reset restores the saved mode", () => {
  const sel = document.createElement("select");
  sel.append(new Option("Classic", "classic"), new Option("Editorial", "editorial"));
  sel.value = "editorial"; // an unsaved edit
  const inputs = { video_mode: { input: sel, kind: "select" } };
  setInputs(inputs, { video_mode: "classic" });
  eq(sel.value, "classic", "reset back to the saved classic");
});

record("video-mode: New Project form defaults the selector to classic", () => {
  const node = renderNewProject({ name: "new-project", param: null });
  const sel = node.querySelector("#np-video-mode");
  assert(sel, "the Video Style selector is present");
  eq(sel.value, "classic", "defaults to classic");
  eq([...sel.options].map((o) => o.value), ["classic", "editorial"], "option order");
});

await recordAsync("video-mode: New Project POST carries the selected video_mode", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch((call) => {
    if (call.method === "POST" && call.url.endsWith("/api/projects")) {
      return {
        payload: {
          project: { ...LEGACY_PROJECT, id: "new-1", title: call.body.title, video_mode: call.body.video_mode },
          scenes: [], assets: [], jobs: [], directory: "/tmp/lvs", stage_state: {},
        },
      };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });

  // (a) leaving the selector at its default posts classic
  let node = renderNewProject({ name: "new-project", param: null });
  node.querySelector("#np-title").value = "How Local LLMs Work";
  node.querySelector("#np-topic").value = "Local LLMs";
  node.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  let post = calls.find((c) => c.method === "POST");
  assert(post, "POST /api/projects was issued");
  eq(post.body.video_mode, "classic", "default posts classic");
  node.remove();

  // (b) choosing editorial posts editorial
  node = renderNewProject({ name: "new-project", param: null });
  node.querySelector("#np-title").value = "Editorial piece";
  node.querySelector("#np-topic").value = "Motion graphics";
  node.querySelector("#np-video-mode").value = "editorial";
  node.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await flush();
  post = calls.filter((c) => c.method === "POST").pop();
  eq(post.body.video_mode, "editorial", "selected mode is posted");
  node.remove();
});

/* --- 6. Editorial Preview section ----------------------------------------- */

// Snapshot fixtures shaped like GET /api/projects/{id} responses.
function projectSnapshot(project, editorial) {
  const snap = {
    project, scenes: [], assets: [], jobs: [],
    directory: "/tmp/lvs", stage_state: {},
  };
  if (editorial !== undefined) snap.editorial = editorial;
  return snap;
}
const EDITORIAL_PROJECT = { ...LEGACY_PROJECT, id: "proj-ed", video_mode: "editorial" };
const EDIT_PLAN_META = {
  has_edit_plan: true,
  edit_plan_url: "/api/projects/proj-ed/editorial/edit-plan",
  preview_url: "/api/projects/proj-ed/editorial/preview",
};

record("editorial-preview: classic project shows no Editorial Preview", () => {
  const classic = { ...LEGACY_PROJECT, video_mode: "classic" };
  eq(editorialPreviewSection(projectSnapshot(classic, EDIT_PLAN_META)), null,
    "classic snapshot renders nothing even with a plan block");
  eq(editorialPreviewSection(projectSnapshot(classic)), null);
});

record("editorial-preview: legacy project without video_mode shows no Editorial Preview", () => {
  eq(editorialPreviewSection(projectSnapshot(LEGACY_PROJECT)), null,
    "omitted video_mode reads as classic -> no section");
  eq(editorialPreviewSection(projectSnapshot({ ...LEGACY_PROJECT, video_mode: "weird" })), null,
    "unknown video_mode also stays classic");
});

record("editorial-preview: editorial project without a plan shows the empty state", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, {
    has_edit_plan: false,
    edit_plan_url: EDIT_PLAN_META.edit_plan_url,
    preview_url: EDIT_PLAN_META.preview_url,
  }));
  assert(node, "the section renders for editorial projects");
  eq(node.querySelector(".panel-title").textContent, "Editorial Preview");
  assert(node.querySelector(".empty-state"), "empty state is shown");
  assert(!node.querySelector("a"), "no Open Preview link without a plan");
  assert(node.textContent.includes("Edit Plan"), "explains the missing Edit Plan");
});

record("editorial-preview: editorial project with a plan shows status + Open Preview", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, EDIT_PLAN_META));
  assert(node, "the section renders");
  eq(node.querySelector(".panel-title").textContent, "Editorial Preview");
  assert(!node.querySelector(".empty-state"), "no empty state once the plan exists");
  const badge = node.querySelector(".badge");
  assert(badge && badge.textContent.includes("Edit Plan available"), "status badge present");
  const link = node.querySelector("a");
  assert(link && link.textContent === "Open Preview", "Open Preview link present");
});

record("editorial-preview: Open Preview uses preview_url, _blank, and noopener", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, EDIT_PLAN_META));
  const link = node.querySelector("a");
  eq(link.getAttribute("href"), EDIT_PLAN_META.preview_url, "href is preview_url");
  eq(link.getAttribute("target"), "_blank", "opens in a new tab");
  assert((link.getAttribute("rel") || "").split(/\s+/).includes("noopener"),
    `rel contains noopener (got ${JSON.stringify(link.getAttribute("rel"))})`);
});

record("editorial-preview: missing editorial snapshot degrades to the empty state", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT)); // no editorial block
  assert(node, "editorial projects still render the panel defensively");
  assert(node.querySelector(".empty-state"), "missing snapshot counts as has_edit_plan=false");
  assert(!node.querySelector("a"), "no link without a plan");
});

/* --- 7. Editorial Generate Edit Plan --------------------------------------- */

const GENERATE_URL = "/api/projects/proj-ed/editorial/plan";
const NO_PLAN_META = {
  has_edit_plan: false,
  edit_plan_url: EDIT_PLAN_META.edit_plan_url,
  generate_url: GENERATE_URL,
  preview_url: EDIT_PLAN_META.preview_url,
};

// Matches the button in either its idle or pending label (the pending state
// rewrites the text in place, so class-based lookup stays stable).
function findGenerateButton(node) {
  return [...node.querySelectorAll("button")].find(
    (b) => b.textContent === "Generate Edit Plan" || b.textContent === "Generating…",
  ) || null;
}

record("editorial-generate: classic project shows no Editorial section or Generate button", () => {
  const classic = { ...LEGACY_PROJECT, video_mode: "classic" };
  eq(editorialPreviewSection(projectSnapshot(classic, NO_PLAN_META)), null,
    "classic snapshot renders nothing even with a usable generate_url");
});

record("editorial-generate: editorial project without a plan shows the Generate button", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, NO_PLAN_META));
  assert(node, "the section renders for editorial projects");
  assert(node.querySelector(".empty-state"), "empty state explanation remains");
  const btn = findGenerateButton(node);
  assert(btn, "Generate Edit Plan button is present");
  eq(btn.textContent, "Generate Edit Plan", "button label");
  eq(btn.disabled, false, "button starts enabled");
  assert(!node.querySelector("a"), "no Open Preview link without a plan");
});

record("editorial-generate: missing or malformed generate_url omits the button defensively", () => {
  const noUrl = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, { has_edit_plan: false }));
  assert(noUrl.querySelector(".empty-state"), "empty state still rendered");
  assert(!findGenerateButton(noUrl), "no button without generate_url");

  const emptyUrl = editorialPreviewSection(
    projectSnapshot(EDITORIAL_PROJECT, { ...NO_PLAN_META, generate_url: "" }));
  assert(!findGenerateButton(emptyUrl), "no button for an empty-string generate_url");

  const badUrl = editorialPreviewSection(
    projectSnapshot(EDITORIAL_PROJECT, { ...NO_PLAN_META, generate_url: 42 }));
  assert(!findGenerateButton(badUrl), "no button for a non-string generate_url");

  const garbage = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, "not-an-object"));
  assert(garbage.querySelector(".empty-state"), "malformed editorial block degrades to the empty state");
  assert(!findGenerateButton(garbage), "malformed editorial block shows no broken button");
});

record("editorial-generate: editorial project with a plan keeps the preview link and hides Generate", () => {
  const node = editorialPreviewSection(
    projectSnapshot(EDITORIAL_PROJECT, { ...EDIT_PLAN_META, generate_url: GENERATE_URL }));
  assert(node, "the section renders");
  const badge = node.querySelector(".badge");
  assert(badge && badge.textContent.includes("Edit Plan available"), "status badge remains");
  const link = node.querySelector("a");
  assert(link && link.textContent === "Open Preview", "Open Preview link remains");
  assert(!findGenerateButton(node), "no Generate button once a plan exists");
});

await recordAsync("editorial-generate: one click issues exactly one bodyless POST to generate_url", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch((call) => {
    if (call.method === "POST" && call.url === GENERATE_URL) return { payload: { version: 1 } };
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, NO_PLAN_META));
  const btn = findGenerateButton(node);
  btn.click();
  // The pending state is observable synchronously, before the stubbed fetch resolves.
  eq(btn.disabled, true, "button disabled while pending");
  eq(btn.textContent, "Generating…", "pending label shown");
  btn.click(); // a second click while pending must be ignored
  await flush();
  const posts = calls.filter((c) => c.method === "POST");
  eq(posts.length, 1, "exactly one POST issued");
  eq(posts[0].url, GENERATE_URL, "POSTs the exact snapshot generate_url");
  eq(posts[0].body, null, "no request body and no force flag");
});

await recordAsync("editorial-generate: success refreshes the panel to Edit Plan available", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const withPlan = projectSnapshot(
    EDITORIAL_PROJECT, { ...EDIT_PLAN_META, generate_url: GENERATE_URL });
  const calls = stubFetch((call) => {
    if (call.method === "POST" && call.url === GENERATE_URL) return { payload: { version: 1 } };
    if (call.method === "GET" && call.url === "/api/projects/proj-ed") return { payload: withPlan };
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, NO_PLAN_META));
  const btn = findGenerateButton(region);
  assert(btn, "Generate button present before the click");
  btn.click();
  await flush();
  assert(region.textContent.includes("Edit Plan available"), "plan badge after refresh");
  const link = region.querySelector("a");
  assert(link && link.textContent === "Open Preview", "Open Preview after refresh");
  assert(!findGenerateButton(region), "Generate button gone after refresh");
  eq(calls.filter((c) => c.method === "POST").length, 1, "still exactly one POST");
  const gets = calls.filter((c) => c.method === "GET");
  eq(gets.length, 1, "panel refreshed by one snapshot re-read");
  eq(gets[0].url, "/api/projects/proj-ed", "re-read the project snapshot");
});

await recordAsync("editorial-generate: failure restores the button and surfaces the error", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch((call) => {
    if (call.method === "POST" && call.url === GENERATE_URL) {
      return { status: 502, payload: { detail: "planner exploded" } };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, NO_PLAN_META));
  const btn = findGenerateButton(node);
  btn.click();
  await flush();
  eq(btn.disabled, false, "button re-enabled after failure");
  eq(btn.textContent, "Generate Edit Plan", "original label restored");
  assert(node.textContent.includes("planner exploded"), "inline error panel shows the backend message");
  assert(document.querySelectorAll("#toasts .toast").length >= 1, "error toast surfaced");
  eq(calls.filter((c) => c.method === "POST").length, 1, "failure never auto-retries the POST");
});

await recordAsync("editorial-generate: repeated live refreshes never trigger generation", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch(() => ({ status: 404, payload: { detail: "unexpected call" } }));
  const region = document.createElement("div");
  for (let i = 0; i < 4; i++) {
    renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, NO_PLAN_META));
  }
  await flush();
  eq(calls.length, 0, "mounting and refreshing the panel issues no requests at all");
  assert(findGenerateButton(region), "the Generate button is present but fires only on click");
});

await recordAsync("editorial-generate: live refresh preserves the one in-flight action", async () => {
  state.config = { apiBase: "", mediaBase: null };
  let finishPost;
  const calls = [];
  globalThis.fetch = (url, opts) => {
    const method = (opts && opts.method) || "GET";
    calls.push({ url: String(url), method });
    if (method === "GET") {
      const withPlan = projectSnapshot(
        EDITORIAL_PROJECT, { ...EDIT_PLAN_META, generate_url: GENERATE_URL });
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify(withPlan)),
      });
    }
    return new Promise((resolve) => {
      finishPost = () => resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify({ version: 1 })),
      });
    });
  };
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, NO_PLAN_META));
  const pendingButton = findGenerateButton(region);
  pendingButton.click();
  assert(pendingButton.disabled, "the original action is pending");

  // A live snapshot tick with the old has_edit_plan=false state must not
  // replace the guarded button with a fresh enabled action.
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, NO_PLAN_META));
  eq(findGenerateButton(region), pendingButton, "pending action remains mounted");
  eq(calls.length, 1, "the refresh issues no second request");

  finishPost();
  await flush();
  eq(calls.filter((c) => c.method === "POST").length, 1, "only one POST completes");
});

/* --- 8. Editorial plan provenance (staleness status) --------------------- */

const CURRENT_PLAN_META = {
  has_edit_plan: true,
  plan_status: "current",
  stale: false,
  stale_reasons: [],
  edit_plan_url: EDIT_PLAN_META.edit_plan_url,
  generate_url: GENERATE_URL,
  preview_url: EDIT_PLAN_META.preview_url,
};

function stalePlanMeta(reasons) {
  return {
    has_edit_plan: true,
    plan_status: "stale",
    stale: true,
    stale_reasons: reasons,
    edit_plan_url: EDIT_PLAN_META.edit_plan_url,
    generate_url: GENERATE_URL,
    preview_url: EDIT_PLAN_META.preview_url,
  };
}

const UNTRACKED_PLAN_META = {
  has_edit_plan: true,
  plan_status: "untracked",
  stale: null,
  stale_reasons: [],
  edit_plan_url: EDIT_PLAN_META.edit_plan_url,
  generate_url: GENERATE_URL,
  preview_url: EDIT_PLAN_META.preview_url,
};

record("editorial-provenance: current plan keeps the good state with a small Current note", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, CURRENT_PLAN_META));
  assert(node, "the section renders");
  const badge = node.querySelector(".badge");
  assert(badge && badge.textContent.includes("Edit Plan available"), "good status badge kept");
  assert(badge.classList.contains("badge-good"), "badge keeps the good tone");
  assert(node.textContent.includes("Current"), "small Current indication present");
  const link = node.querySelector("a");
  assert(link && link.textContent === "Open Preview", "Open Preview kept");
  assert(!findGenerateButton(node), "no Generate button for a current plan");
});

record("editorial-provenance: stale plan shows a warning and the project reason", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, stalePlanMeta(["project"])));
  const badge = node.querySelector(".badge");
  assert(badge && badge.textContent.includes("stale"), "warning status says stale");
  assert(badge.classList.contains("badge-warning"), "badge uses the warning tone");
  assert(node.textContent.includes("settings changed"), "project reason explained readably");
  assert(!findGenerateButton(node), "no Generate button for a stale plan");
});

record("editorial-provenance: stale plan explains the script reason", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, stalePlanMeta(["script"])));
  assert(node.textContent.includes("narration or script changed"), "script reason explained readably");
  assert(!node.textContent.includes("word timings changed"), "no unrelated reason listed");
});

record("editorial-provenance: stale plan explains the word_timings reason", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, stalePlanMeta(["word_timings"])));
  assert(node.textContent.includes("word timings changed"), "word_timings reason explained readably");
});

record("editorial-provenance: multiple stale reasons are all listed", () => {
  const node = editorialPreviewSection(projectSnapshot(
    EDITORIAL_PROJECT, stalePlanMeta(["project", "script", "word_timings"])));
  assert(node.textContent.includes("settings changed"), "project reason listed");
  assert(node.textContent.includes("narration or script changed"), "script reason listed");
  assert(node.textContent.includes("word timings changed"), "word_timings reason listed");
  const badge = node.querySelector(".badge");
  assert(badge && badge.textContent.includes("stale"), "still a single stale warning");
});

record("editorial-provenance: untracked plan is neutral, never stale or broken", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, UNTRACKED_PLAN_META));
  assert(node, "the section renders");
  const badge = node.querySelector(".badge");
  assert(badge && badge.textContent.includes("Edit Plan available"), "plan availability still shown");
  assert(badge.classList.contains("badge-neutral"), "neutral tone for unknown freshness");
  assert(node.textContent.includes("predat"), "explains the plan may predate tracking");
  assert(!/stale/i.test(node.textContent), "never labeled stale");
  assert(!/broken/i.test(node.textContent), "never labeled broken");
});

record("editorial-provenance: missing or unknown plan_status falls back to Edit Plan available", () => {
  const legacy = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, EDIT_PLAN_META));
  const legacyBadge = legacy.querySelector(".badge");
  assert(legacyBadge && legacyBadge.textContent.includes("Edit Plan available"),
    "older backend without plan_status keeps the classic state");
  const weird = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, {
    ...EDIT_PLAN_META, plan_status: "exploded", stale: "yes", stale_reasons: { project: true },
  }));
  const weirdBadge = weird.querySelector(".badge");
  assert(weirdBadge && weirdBadge.textContent.includes("Edit Plan available"),
    "unknown plan_status keeps the classic state");
  assert(!weird.querySelector(".empty-state"), "preview never hidden by malformed metadata");
  const nonString = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, {
    ...EDIT_PLAN_META, plan_status: 42,
  }));
  const nonStringBadge = nonString.querySelector(".badge");
  assert(nonStringBadge && nonStringBadge.textContent.includes("Edit Plan available"),
    "non-string plan_status keeps the classic state");
});

record("editorial-provenance: stale and untracked plans retain Open Preview", () => {
  for (const meta of [stalePlanMeta(["project"]), stalePlanMeta([]), UNTRACKED_PLAN_META, CURRENT_PLAN_META]) {
    const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, meta));
    const link = node.querySelector("a");
    assert(link && link.textContent === "Open Preview",
      `Open Preview kept for ${meta.plan_status}`);
    eq(link.getAttribute("href"), EDIT_PLAN_META.preview_url, "href is preview_url");
  }
});

record("editorial-provenance: no Generate button when has_edit_plan=true", () => {
  for (const meta of [CURRENT_PLAN_META, stalePlanMeta(["script"]), UNTRACKED_PLAN_META, EDIT_PLAN_META]) {
    const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, meta));
    assert(!findGenerateButton(node),
      `no Generate button for ${meta.plan_status || "missing plan_status"}`);
    // Once a plan exists, the only actions are the explicit, read-only ones:
    // Open Preview (anchor) and the on-demand "Show compositions" fetcher
    // (these fixtures all carry a usable edit_plan_url).
    const buttons = [...node.querySelectorAll("button")];
    eq(buttons.map((b) => b.textContent), ["Show compositions"],
      `only the Show compositions action remains for ${meta.plan_status || "missing plan_status"}`);
  }
});

await recordAsync("editorial-provenance: rendering plan states performs no network calls", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch(() => ({ status: 404, payload: { detail: "unexpected call" } }));
  const region = document.createElement("div");
  for (const meta of [CURRENT_PLAN_META, stalePlanMeta(["script", "word_timings"]), UNTRACKED_PLAN_META]) {
    renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, meta));
  }
  await flush();
  eq(calls.length, 0, "displaying current/stale/untracked states issues no requests");
});

/* --- 9. Export screen mode presentation ---------------------------------- */

// The Export screen fetches exactly two things (project snapshot + thumbnail
// studio state); the stub answers only those, and any other call fails loudly.
const THUMBNAILS_EMPTY = { plan: {}, candidates: [], selection: null, legacy_frames: [], jobs: [] };

const CLASSIC_EXPORT_PROJECT = { ...LEGACY_PROJECT, id: "proj-xc", video_mode: "classic" };
const LEGACY_EXPORT_PROJECT = { ...LEGACY_PROJECT, id: "proj-xl" }; // video_mode omitted
const EDITORIAL_EXPORT_PROJECT = { ...LEGACY_PROJECT, id: "proj-xe", video_mode: "editorial" };

const CLASSIC_SCENES = [
  { id: "sc-x1", title: "One", duration: 30 },
  { id: "sc-x2", title: "Two", duration: 60 },
];
const CLASSIC_ASSETS = [
  { id: "as-xv", scene_id: "sc-x1", created_at: "2026-01-01T00:00:00Z", settings: { role: "visual" } },
  { id: "as-xn", scene_id: "sc-x1", created_at: "2026-01-01T00:00:00Z", settings: { role: "narration" } },
  { id: "as-xm", scene_id: null, created_at: "2026-01-01T00:00:00Z", settings: { role: "music" } },
];

const EDIT_PLAN_CURRENT = { has_edit_plan: true, plan_status: "current" };
const EDIT_PLAN_STALE = { has_edit_plan: true, plan_status: "stale", stale_reasons: ["project", "word_timings"] };
const EDIT_PLAN_UNTRACKED = { has_edit_plan: true, plan_status: "untracked" };
const EDIT_PLAN_LEGACY = { has_edit_plan: true }; // no plan_status (older backends)

function exportSnapshot(project, editorial, { stages = {}, scenes = [], assets = [], jobs = [] } = {}) {
  const snap = { project, scenes, assets, jobs, directory: "/tmp/lvs", stage_state: { version: 1, stages } };
  if (editorial !== undefined) snap.editorial = editorial;
  return snap;
}

async function renderExportScreen(projectId, snap) {
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === `/api/projects/${projectId}`) return { payload: snap };
    if (call.method === "GET" && call.url === `/api/projects/${projectId}/thumbnails`) return { payload: THUMBNAILS_EMPTY };
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  state.config = { apiBase: "", mediaBase: null };
  state.currentProjectId = projectId;
  const screen = renderExport({ name: "export", param: null });
  await flush();
  return { screen, calls };
}

function renderControlsPanel(screen) {
  const panel = [...screen.querySelectorAll(".panel")].find(
    (p) => (p.querySelector(".panel-title")?.textContent || "") === "Render controls",
  );
  assert(panel, "the render controls panel rendered");
  return panel;
}

function chipRowBadges(panel) {
  const row = panel.querySelector(".row.mt");
  assert(row, "the stage-chip row exists");
  return [...row.querySelectorAll(".badge")].map((b) => b.textContent);
}

function closeModals() {
  document.querySelectorAll("dialog.modal").forEach((d) => d.remove());
}

record("export: mode text helpers keep the classic presentation verbatim", () => {
  for (const project of [CLASSIC_EXPORT_PROJECT, LEGACY_EXPORT_PROJECT, { video_mode: "weird" }, {}]) {
    eq(exportVideoMode(project), "classic");
    eq(exportDescriptionText(project),
      "Uses existing local narration and scene visuals. It does not contact the LLM, run TTS, or generate graphics.");
    eq(exportWorkflowText(project), "Timeline → preview → quality check → final MP4 → frame extraction");
    eq(exportForceConfirmMessage(project),
      "Timeline, preview, quality check, final MP4, and extracted frames will be rebuilt. " +
      "Existing scripts, narration, scene graphics, music, and captions will not be regenerated.");
  }
  eq(exportVideoMode(EDITORIAL_EXPORT_PROJECT), "editorial");
  eq(exportDescriptionText(EDITORIAL_EXPORT_PROJECT),
    "Uses the existing Edit Plan, registered assets, narration, music, and captions. " +
    "It does not contact the LLM, run TTS, or generate replacement assets.");
  eq(exportWorkflowText(EDITORIAL_EXPORT_PROJECT),
    "Editorial canvas → timeline → preview → quality check → final MP4 → frame extraction");
  eq(exportForceConfirmMessage(EDITORIAL_EXPORT_PROJECT),
    "The Editorial visual master and the downstream render outputs (timeline, preview, quality check, final MP4, and extracted frames) will be rebuilt. " +
    "The Edit Plan, registered assets, narration, music, and captions are not regenerated.");
});

record("export: editorialPlanSummary reduces current/stale/untracked metadata", () => {
  const snap = (editorial) => exportSnapshot(EDITORIAL_EXPORT_PROJECT, editorial);
  eq(editorialPlanSummary(snap(EDIT_PLAN_CURRENT)), { hasPlan: true, status: "current", reasons: [] });
  eq(editorialPlanSummary(snap(EDIT_PLAN_UNTRACKED)), { hasPlan: true, status: "untracked", reasons: [] });
  eq(editorialPlanSummary(snap({ ...EDIT_PLAN_STALE, stale_reasons: ["word_timings", "project"] })),
    {
      hasPlan: true, status: "stale",
      reasons: [
        "the narration word timings changed since the plan was generated",
        "project or editorial settings changed since the plan was generated",
      ],
    }, "reasons are deduplicated and translated");
});

record("export: editorialPlanSummary degrades missing and malformed metadata", () => {
  const snap = (editorial) => exportSnapshot(EDITORIAL_EXPORT_PROJECT, editorial);
  eq(editorialPlanSummary(snap({ has_edit_plan: false })), { hasPlan: false, status: "missing", reasons: [] });
  eq(editorialPlanSummary(snap(undefined)), { hasPlan: false, status: "missing", reasons: [] }, "no editorial block");
  eq(editorialPlanSummary(snap(null)), { hasPlan: false, status: "missing", reasons: [] }, "null block");
  eq(editorialPlanSummary(snap("corrupt")), { hasPlan: false, status: "missing", reasons: [] }, "non-object block");
  eq(editorialPlanSummary(snap(42)), { hasPlan: false, status: "missing", reasons: [] }, "non-object number");
  eq(editorialPlanSummary(snap({ has_edit_plan: "yes", plan_status: "current" })),
    { hasPlan: false, status: "missing", reasons: [] }, "malformed truthy availability is not trusted");
  eq(editorialPlanSummary(snap({ ...EDIT_PLAN_LEGACY, plan_status: "exploded" })),
    { hasPlan: true, status: "unknown", reasons: [] }, "unknown status keeps the plan available");
  eq(editorialPlanSummary(snap({ ...EDIT_PLAN_LEGACY, plan_status: 42, stale: "yes", stale_reasons: { project: true } })),
    { hasPlan: true, status: "unknown", reasons: [] }, "malformed provenance fields keep the plan available");
  eq(editorialPlanSummary(snap({ ...EDIT_PLAN_STALE, stale_reasons: ["??", 7, null, ""] })),
    { hasPlan: true, status: "stale", reasons: [] }, "unrecognizable reasons drop out");
  // Classic snapshots never get the Edit Plan row, editorial block or not.
  const classic = exportSnapshot(CLASSIC_EXPORT_PROJECT, EDIT_PLAN_CURRENT);
  assert(renderInputSummary(classic, classic.stage_state.stages).textContent.includes("Scene visuals"),
    "classic summary unchanged");
});

record("export: editorial_visual stage gets a readable label", () => {
  eq(renderStageLabel("editorial_visual"), "Rendering Editorial canvas");
  eq(renderStageLabel("timeline"), "Building timeline", "classic stages unchanged");
  eq(renderStageLabel("render_preview"), "Rendering preview", "classic stages unchanged");
  eq(renderStageLabel("quality_control"), "Quality check", "classic stages unchanged");
  eq(renderStageLabel("render_final"), "Rendering final MP4", "classic stages unchanged");
  eq(renderStageLabel("thumbnails"), "Extracting frames", "classic stages unchanged");
  eq(renderStageLabel("queued"), "Queued", "queued unchanged");
  eq(renderStageLabel("nope"), "Rendering", "unknown values still fall back");
  eq(renderStageLabel(undefined), "Rendering", "undefined still falls back");
});

await recordAsync("export: classic screen keeps the unchanged presentation", async () => {
  const snap = exportSnapshot(CLASSIC_EXPORT_PROJECT, undefined, { scenes: CLASSIC_SCENES, assets: CLASSIC_ASSETS });
  const { screen, calls } = await renderExportScreen(CLASSIC_EXPORT_PROJECT.id, snap);
  const panel = renderControlsPanel(screen);
  eq(panel.querySelector("p.muted.small").textContent,
    "Uses existing local narration and scene visuals. It does not contact the LLM, run TTS, or generate graphics.");
  const summary = panel.querySelector("dl.kv");
  assert(summary.textContent.includes("Scene visuals"), "scene visual count row kept");
  assert(summary.textContent.includes("1/2 recorded"), "visual count computed as before");
  assert(summary.textContent.includes("captions derived from scenes"), "classic caption wording kept");
  assert(!summary.textContent.includes("Edit Plan"), "no Edit Plan row on classic screens");
  assert(panel.textContent.includes("Timeline → preview → quality check → final MP4 → frame extraction"),
    "classic workflow text kept");
  eq(chipRowBadges(panel), [
    "Timeline: Pending", "Preview render: Pending", "Quality check: Pending",
    "Final render: Pending", "Thumbnails: Pending",
  ], "no editorial canvas chip on classic screens");
  eq(calls.map((c) => `${c.method} ${c.url}`),
    ["GET /api/projects/proj-xc", "GET /api/projects/proj-xc/thumbnails"],
    "only the snapshot and thumbnail fetches");
});

await recordAsync("export: legacy project (omitted video_mode) keeps the classic presentation", async () => {
  const snap = exportSnapshot(LEGACY_EXPORT_PROJECT, undefined, { scenes: CLASSIC_SCENES, assets: CLASSIC_ASSETS });
  const { screen } = await renderExportScreen(LEGACY_EXPORT_PROJECT.id, snap);
  const panel = renderControlsPanel(screen);
  eq(panel.querySelector("p.muted.small").textContent,
    "Uses existing local narration and scene visuals. It does not contact the LLM, run TTS, or generate graphics.");
  assert(panel.querySelector("dl.kv").textContent.includes("Scene visuals"), "scene visuals row kept");
  assert(panel.textContent.includes("Timeline → preview → quality check → final MP4 → frame extraction"),
    "classic workflow kept");
  eq(chipRowBadges(panel)[0], "Timeline: Pending", "first chip is timeline, not editorial canvas");
});

await recordAsync("export: editorial screen shows the additive workflow (current plan)", async () => {
  const snap = exportSnapshot(EDITORIAL_EXPORT_PROJECT, EDIT_PLAN_CURRENT,
    { stages: { editorial_visual: { status: "completed" } } });
  const { screen, calls } = await renderExportScreen(EDITORIAL_EXPORT_PROJECT.id, snap);
  const panel = renderControlsPanel(screen);
  eq(panel.querySelector("p.muted.small").textContent,
    "Uses the existing Edit Plan, registered assets, narration, music, and captions. " +
    "It does not contact the LLM, run TTS, or generate replacement assets.");
  const summary = panel.querySelector("dl.kv");
  assert(summary.textContent.includes("Edit Plan"), "Edit Plan readiness row present");
  assert(summary.textContent.includes("current"), "current plan status shown");
  assert(!summary.textContent.includes("Scene visuals"), "no scene visual count on editorial screens");
  assert(summary.textContent.includes("captions derived from narration"), "editorial caption wording");
  assert(panel.textContent.includes("Editorial canvas → timeline → preview → quality check → final MP4 → frame extraction"),
    "compact editorial workflow");
  eq(chipRowBadges(panel), [
    "Editorial canvas: Completed", "Timeline: Pending", "Preview render: Pending",
    "Quality check: Pending", "Final render: Pending", "Thumbnails: Pending",
  ], "editorial canvas chip leads the stage row");
  eq(calls.map((c) => `${c.method} ${c.url}`),
    ["GET /api/projects/proj-xe", "GET /api/projects/proj-xe/thumbnails"],
    "the page renders from the existing snapshot only (no Edit Plan fetch)");
});

await recordAsync("export: editorial stale plan remains usable (never broken)", async () => {
  const snap = exportSnapshot(EDITORIAL_EXPORT_PROJECT, EDIT_PLAN_STALE);
  const { screen } = await renderExportScreen(EDITORIAL_EXPORT_PROJECT.id, snap);
  const panel = renderControlsPanel(screen);
  const text = panel.querySelector("dl.kv").textContent;
  assert(text.includes("Edit Plan"), "Edit Plan row present");
  assert(text.includes("stale"), "stale status surfaced");
  assert(text.includes("project or editorial settings changed"), "project reason readable");
  assert(text.includes("word timings changed"), "word_timings reason readable");
  assert(text.includes("still renderable"), "stale plan stays renderable");
  assert(!text.includes("broken"), "never labeled broken");
  eq(chipRowBadges(panel)[0], "Editorial canvas: Pending", "plan state does not change the stage chips");
});

await recordAsync("export: editorial untracked plan remains usable", async () => {
  const snap = exportSnapshot(EDITORIAL_EXPORT_PROJECT, EDIT_PLAN_UNTRACKED);
  const { screen } = await renderExportScreen(EDITORIAL_EXPORT_PROJECT.id, snap);
  const text = renderControlsPanel(screen).querySelector("dl.kv").textContent;
  assert(text.includes("Edit Plan"), "Edit Plan row present");
  assert(text.includes("freshness unverified"), "explains the unverifiable plan");
  assert(text.includes("still renderable"), "untracked plan stays renderable");
  assert(!text.toLowerCase().includes("stale"), "never labeled stale");
  assert(!text.includes("broken"), "never labeled broken");
});

await recordAsync("export: editorial missing/malformed metadata falls back to not generated", async () => {
  for (const editorial of [undefined, { has_edit_plan: false }, "corrupt", 42]) {
    const snap = exportSnapshot(EDITORIAL_EXPORT_PROJECT, editorial);
    const { screen } = await renderExportScreen(EDITORIAL_EXPORT_PROJECT.id, snap);
    const text = renderControlsPanel(screen).querySelector("dl.kv").textContent;
    assert(text.includes("Edit Plan"), `Edit Plan row survives ${String(typeof editorial)}`);
    assert(text.includes("not generated"), `missing plan stated for ${String(typeof editorial)}`);
    assert(!text.includes("broken"), "malformed metadata never reads as broken");
  }
});

await recordAsync("export: active editorial render shows the canvas stage label", async () => {
  const job = {
    id: "job-xe1", project_id: "proj-xe", scene_id: null, stage: "render", backend: "ffmpeg",
    status: "preparing", progress: 0.12, priority: 0,
    parameters: {
      force: false, current_stage: "editorial_visual",
      stages: ["editorial_visual", "timeline", "render_preview", "quality_control", "render_final", "thumbnails"],
    },
    attempt_count: 1, max_attempts: 3, error: null,
    created_at: "2026-01-02T00:00:00Z", updated_at: "2026-01-02T00:00:01Z",
    started_at: null, completed_at: null,
  };
  const snap = exportSnapshot(EDITORIAL_EXPORT_PROJECT, EDIT_PLAN_CURRENT,
    { stages: { editorial_visual: { status: "running" } }, jobs: [job] });
  const { screen } = await renderExportScreen(EDITORIAL_EXPORT_PROJECT.id, snap);
  const panel = renderControlsPanel(screen);
  assert(panel.textContent.includes("Rendering Editorial canvas"), "readable label for the active sub-stage");
  assert(panel.textContent.includes("Preparing"), "job status badge present");
  assert(panel.textContent.includes("job job-xe1"), "job id shown");
  eq(chipRowBadges(panel)[0], "Editorial canvas: Running", "running chip status");
});

await recordAsync("export: building the readiness summary issues no network requests", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch(() => ({ status: 404, payload: { detail: "unexpected call" } }));
  const metas = [EDIT_PLAN_CURRENT, EDIT_PLAN_STALE, EDIT_PLAN_UNTRACKED, EDIT_PLAN_LEGACY,
    { has_edit_plan: false }, "corrupt"];
  for (const meta of metas) {
    const snap = exportSnapshot(EDITORIAL_EXPORT_PROJECT, meta);
    const summary = renderInputSummary(snap, snap.stage_state.stages);
    assert(summary.textContent.includes("Edit Plan"), `summary renders for ${typeof meta}`);
    assert(!summary.textContent.includes("Scene visuals"), "editorial summary never counts scene visuals");
  }
  const classic = exportSnapshot(CLASSIC_EXPORT_PROJECT, undefined, { scenes: CLASSIC_SCENES, assets: CLASSIC_ASSETS });
  const classicSummary = renderInputSummary(classic, classic.stage_state.stages);
  assert(classicSummary.textContent.includes("Scene visuals"), "classic summary unchanged");
  await flush();
  eq(calls.length, 0, "summary rendering is pure: zero requests");
});

await recordAsync("export: editorial force-render confirmation explains the additive rebuild", async () => {
  const snap = exportSnapshot(EDITORIAL_EXPORT_PROJECT, EDIT_PLAN_CURRENT);
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === "/api/projects/proj-xe") return { payload: snap };
    if (call.method === "GET" && call.url === "/api/projects/proj-xe/thumbnails") return { payload: THUMBNAILS_EMPTY };
    if (call.method === "POST" && call.url === "/api/projects/proj-xe/render") {
      return { payload: {
        id: "job-xe2", project_id: "proj-xe", scene_id: null, stage: "render", backend: "ffmpeg",
        status: "queued", progress: 0, priority: 0,
        parameters: { force: true, current_stage: "queued" },
        attempt_count: 0, max_attempts: 3, error: null,
        created_at: "2026-01-03T00:00:00Z", updated_at: "2026-01-03T00:00:00Z",
        started_at: null, completed_at: null,
      } };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  state.config = { apiBase: "", mediaBase: null };
  state.currentProjectId = EDITORIAL_EXPORT_PROJECT.id;
  const screen = renderExport({ name: "export", param: null });
  await flush();
  const panel = renderControlsPanel(screen);
  const forceBtn = [...panel.querySelectorAll("button")].find((b) => b.textContent === "Re-render final video");
  assert(forceBtn, "force button present");
  forceBtn.click();
  await flush();
  const modal = document.querySelector("dialog.modal");
  assert(modal, "confirmation dialog opened");
  eq(modal.querySelector(".modal-head h2").textContent, "Re-render final video?");
  const message = modal.querySelector(".modal-body").textContent;
  assert(message.includes("The Editorial visual master and the downstream render outputs"), "rebuild scope explained");
  assert(message.includes("The Edit Plan, registered assets, narration, music, and captions are not regenerated."),
    "inputs are not regenerated");
  const confirmBtn = [...modal.querySelectorAll(".modal-foot button")].find((b) => b.textContent === "Re-render final video");
  assert(confirmBtn, "confirm action present");
  confirmBtn.click();
  await flush();
  const posts = calls.filter((c) => c.method === "POST");
  eq(posts.length, 1, "one render POST after confirmation");
  eq(posts[0].url, "/api/projects/proj-xe/render", "posts to the render endpoint");
  eq(posts[0].body, { force: true }, "force flag preserved");
  closeModals();
});

await recordAsync("export: classic force-render confirmation is unchanged", async () => {
  const snap = exportSnapshot(CLASSIC_EXPORT_PROJECT, undefined, { scenes: CLASSIC_SCENES, assets: CLASSIC_ASSETS });
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === "/api/projects/proj-xc") return { payload: snap };
    if (call.method === "GET" && call.url === "/api/projects/proj-xc/thumbnails") return { payload: THUMBNAILS_EMPTY };
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  state.config = { apiBase: "", mediaBase: null };
  state.currentProjectId = CLASSIC_EXPORT_PROJECT.id;
  const screen = renderExport({ name: "export", param: null });
  await flush();
  const panel = renderControlsPanel(screen);
  const forceBtn = [...panel.querySelectorAll("button")].find((b) => b.textContent === "Re-render final video");
  assert(forceBtn, "force button present");
  forceBtn.click();
  await flush();
  const modal = document.querySelector("dialog.modal");
  assert(modal, "confirmation dialog opened");
  const message = modal.querySelector(".modal-body").textContent;
  eq(message,
    "Timeline, preview, quality check, final MP4, and extracted frames will be rebuilt. " +
    "Existing scripts, narration, scene graphics, music, and captions will not be regenerated.");
  const cancelBtn = [...modal.querySelectorAll(".modal-foot button")].find((b) => b.textContent === "Cancel");
  assert(cancelBtn, "cancel action present");
  cancelBtn.click();
  await flush();
  eq(calls.filter((c) => c.method === "POST").length, 0, "canceling issues no render request");
  closeModals();
});

// Leave shared app state the way later screens expect it.
state.currentProjectId = null;
closeModals();

/* --- 10. Editorial display settings (Project Details) --------------------- */

const SETTINGS_URL = "/api/projects/proj-ed/editorial/settings";
const EDIT_PLAN_URL = "/api/projects/proj-ed/editorial/edit-plan";
const EDITORIAL_TEXT_LABEL = "Editorial text";

// Plan metadata carrying strict-boolean display settings; tests override
// individual fields via the `extra` argument.
function settingsMeta(extra = {}) {
  return {
    has_edit_plan: true,
    plan_status: "current",
    stale: false,
    stale_reasons: [],
    edit_plan_url: EDIT_PLAN_URL,
    preview_url: EDIT_PLAN_META.preview_url,
    settings_url: SETTINGS_URL,
    captions_enabled: true,
    editorial_text_enabled: false,
    ...extra,
  };
}

function findSettingInput(node, key) {
  return [...node.querySelectorAll("input")].find((i) => i.dataset.editorialSetting === key) || null;
}

function findCompositionsButton(node) {
  return [...node.querySelectorAll("button")].find((b) => b.textContent === "Show compositions") || null;
}

record("editorial-settings: classic and legacy snapshots render no display controls", () => {
  const classic = { ...LEGACY_PROJECT, video_mode: "classic" };
  eq(editorialPreviewSection(projectSnapshot(classic, settingsMeta())), null,
    "classic stays null even with a full settings block");
  eq(editorialPreviewSection(projectSnapshot(LEGACY_PROJECT, settingsMeta())), null,
    "omitted video_mode reads as classic");
  // The no-plan state never shows the switches (the snapshot fields are only
  // populated once a plan exists).
  const noPlan = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, {
    has_edit_plan: false, settings_url: SETTINGS_URL,
    captions_enabled: true, editorial_text_enabled: true,
  }));
  assert(noPlan, "no-plan state still renders the panel");
  eq(noPlan.querySelectorAll("input[type=checkbox]").length, 0, "no controls without a plan");
});

record("editorial-settings: strict boolean settings render two independent checkboxes", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, settingsMeta()));
  assert(node, "the section renders");
  const captions = findSettingInput(node, "captions_enabled");
  const text = findSettingInput(node, "editorial_text_enabled");
  assert(captions && text, "both controls present");
  eq(captions.type, "checkbox");
  eq(captions.checked, true, "starts from the snapshot value");
  eq(text.checked, false, "starts from the snapshot value");
  assert(node.textContent.includes("Captions"), "Captions label shown");
  assert(node.textContent.includes(EDITORIAL_TEXT_LABEL), "Editorial text label shown");
});

record("editorial-settings: malformed settings_url or non-boolean values omit controls defensively", () => {
  const remoteUrl = "http" + "s://remote-studio.example.com/api/projects/proj-ed/editorial/settings";
  const cases = {
    "remote settings_url": { ...settingsMeta(), settings_url: remoteUrl },
    "protocol-relative settings_url": { ...settingsMeta(), settings_url: "//cdn.example.com/settings" },
    "cross-project settings_url": { ...settingsMeta(), settings_url: "/api/projects/other/editorial/settings" },
    "wrong endpoint settings_url": { ...settingsMeta(), settings_url: EDIT_PLAN_URL },
    "backslash settings_url": { ...settingsMeta(), settings_url: "/\\host/settings" },
    "non-string settings_url": { ...settingsMeta(), settings_url: 42 },
    "empty settings_url": { ...settingsMeta(), settings_url: "   " },
    "missing settings_url": { ...settingsMeta(), settings_url: null },
    "neither value a strict boolean": { ...settingsMeta(), captions_enabled: "yes", editorial_text_enabled: 1 },
  };
  for (const [name, meta] of Object.entries(cases)) {
    const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, meta));
    assert(node, `the section still renders (${name})`);
    eq(node.querySelectorAll("input[type=checkbox]").length, 0,
      `no controls for ${name}`);
  }
  // Per-field omission: a broken value hides only its own control.
  const mixed = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT,
    { ...settingsMeta(), captions_enabled: "yes" }));
  eq(mixed.querySelectorAll("input[type=checkbox]").length, 1, "only the valid control remains");
  assert(findSettingInput(mixed, "editorial_text_enabled"), "the valid control is the surviving one");
  // The standalone builder agrees.
  eq(buildEditorialDisplayControls(
    { ...settingsMeta(), settings_url: remoteUrl }, createEditorialController(),
    document.createElement("div"), null, "proj-ed"), null,
  "builder omits the row for a remote URL");
});

await recordAsync("editorial-settings: one click PATCHes only the changed field, never the Edit Plan", async () => {
  state.config = { apiBase: "", mediaBase: null };
  let captions = true, textOn = false;
  const snapFor = () => projectSnapshot(
    EDITORIAL_PROJECT, settingsMeta({ captions_enabled: captions, editorial_text_enabled: textOn }));
  const calls = stubFetch((call) => {
    if (call.method === "PATCH" && call.url === SETTINGS_URL) {
      if (call.body && call.body.captions_enabled !== undefined) captions = call.body.captions_enabled;
      if (call.body && call.body.editorial_text_enabled !== undefined) textOn = call.body.editorial_text_enabled;
      return { payload: { version: 1 } };
    }
    if (call.method === "GET" && call.url === "/api/projects/proj-ed") return { payload: snapFor() };
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, snapFor());
  const caps = findSettingInput(region, "captions_enabled");
  caps.checked = false;
  caps.dispatchEvent(new Event("change", { bubbles: true }));
  await flush();
  const patches = calls.filter((c) => c.method === "PATCH");
  eq(patches.length, 1, "exactly one PATCH issued");
  eq(patches[0].url, SETTINGS_URL, "PATCHes the snapshot settings_url");
  eq(patches[0].body, { captions_enabled: false }, "body carries only the changed field");
  assert(!calls.some((c) => c.url === EDIT_PLAN_URL),
    "display settings never GET/PUT the full Edit Plan");
  eq(calls.filter((c) => c.method === "GET").length, 1, "success refreshes from one fresh snapshot");
  const freshCaps = findSettingInput(region, "captions_enabled");
  assert(freshCaps && !freshCaps.checked, "fresh-snapshot re-render reflects the saved value");
  // Second, independent toggle: only its own field is PATCHed.
  const text = findSettingInput(region, "editorial_text_enabled");
  text.checked = true;
  text.dispatchEvent(new Event("change", { bubbles: true }));
  await flush();
  const patches2 = calls.filter((c) => c.method === "PATCH");
  eq(patches2.length, 2, "the second change issues its own PATCH");
  eq(patches2[1].body, { editorial_text_enabled: true }, "second body carries only its field");
});

await recordAsync("editorial-settings: one mutation in flight, both controls off, refreshes preserve pending controls", async () => {
  state.config = { apiBase: "", mediaBase: null };
  let captions = true, textOn = false;
  const snapFor = () => projectSnapshot(
    EDITORIAL_PROJECT, settingsMeta({ captions_enabled: captions, editorial_text_enabled: textOn }));
  const calls = [];
  let finishPatch;
  globalThis.fetch = (url, opts) => {
    const method = (opts && opts.method) || "GET";
    const body = (opts && opts.body != null) ? JSON.parse(opts.body) : null;
    calls.push({ url: String(url), method, body });
    if (method === "GET") {
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify(snapFor())) });
    }
    return new Promise((resolve) => {
      finishPatch = () => {
        if (body.captions_enabled !== undefined) captions = body.captions_enabled;
        if (body.editorial_text_enabled !== undefined) textOn = body.editorial_text_enabled;
        resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ version: 1 })) });
      };
    });
  };
  const region = document.createElement("div");
  renderEditorialRegion(region, snapFor());
  const caps = findSettingInput(region, "captions_enabled");
  caps.checked = false;
  caps.dispatchEvent(new Event("change", { bubbles: true }));
  await flush(2);
  const boxes = [...region.querySelectorAll("input")];
  eq(boxes.length, 2, "both controls still mounted");
  assert(boxes.every((b) => b.disabled), "both controls disabled while saving");
  // A live snapshot tick with newer data must not replace the pending controls.
  renderEditorialRegion(region, snapFor());
  assert([...region.querySelectorAll("input")].every((b) => b.disabled),
    "pending controls survive the live tick");
  eq(calls.filter((c) => c.method === "PATCH").length, 1, "the tick issues no duplicate PATCH");
  // A second change while saving is undone, not queued or double-issued.
  const text = findSettingInput(region, "editorial_text_enabled");
  text.checked = true;
  text.dispatchEvent(new Event("change", { bubbles: true }));
  eq(text.checked, false, "the in-flight-locked flip is reverted");
  eq(calls.filter((c) => c.method === "PATCH").length, 1, "no second mutation is queued");
  finishPatch();
  await flush();
  eq(calls.filter((c) => c.method === "PATCH").length, 1, "exactly one PATCH issued end to end");
  eq(calls.filter((c) => c.method === "GET").length, 1, "one snapshot re-read after the save");
  const fresh = [...region.querySelectorAll("input")];
  assert(fresh.length === 2 && fresh.every((b) => !b.disabled), "controls re-enabled after the save");
  const freshCaps = findSettingInput(region, "captions_enabled");
  assert(freshCaps && !freshCaps.checked, "fresh-snapshot re-render reflects the saved value");
});

await recordAsync("editorial-settings: failure restores the previous value and the standard error surfaces", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch((call) => {
    if (call.method === "PATCH" && call.url === SETTINGS_URL) {
      return { status: 500, payload: { detail: "settings rejected" } };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, settingsMeta()));
  const caps = findSettingInput(region, "captions_enabled");
  caps.checked = false; // the user asked for captions off
  caps.dispatchEvent(new Event("change", { bubbles: true }));
  await flush();
  eq(caps.checked, true, "the checkbox is restored to the previous value");
  assert([...region.querySelectorAll("input")].every((b) => !b.disabled),
    "both controls re-enabled after the failure");
  assert(region.textContent.includes("settings rejected"), "inline error panel shows the backend message");
  // The toast region caps its stack, so look the specific toast up by text.
  const toastShown = [...document.querySelectorAll("#toasts .toast")]
    .some((t) => t.textContent.includes("Editorial display setting not saved"));
  assert(toastShown, "error toast surfaced");
  eq(calls.filter((c) => c.method === "PATCH").length, 1, "failure never auto-retries the PATCH");
  assert(!calls.some((c) => c.url === EDIT_PLAN_URL), "no Edit Plan traffic on this path");
});

await recordAsync("editorial-settings: mounting, controls, and live refreshes issue zero requests", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch(() => ({ status: 404, payload: { detail: "unexpected call" } }));
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, settingsMeta()));
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, settingsMeta()));
  await flush();
  eq(calls.length, 0, "rendering and refreshes never fetch or PATCH anything");
  assert(findSettingInput(region, "captions_enabled"), "controls present");
  assert(findCompositionsButton(region), "the explicit composition action is available");
});

await recordAsync("editorial-settings: saving invalidates an older composition fetch", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const freshSnap = projectSnapshot(EDITORIAL_PROJECT, settingsMeta({ captions_enabled: false }));
  let finishPlan;
  globalThis.fetch = (url, opts) => {
    const method = (opts && opts.method) || "GET";
    if (String(url) === EDIT_PLAN_URL) {
      return new Promise((resolve) => {
        finishPlan = () => resolve({
          ok: true, status: 200,
          text: () => Promise.resolve(JSON.stringify({ compositions: [] })),
        });
      });
    }
    if (String(url) === SETTINGS_URL && method === "PATCH") {
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify({ version: 1 })),
      });
    }
    if (String(url) === "/api/projects/proj-ed" && method === "GET") {
      return Promise.resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify(freshSnap)),
      });
    }
    return Promise.resolve({
      ok: false, status: 404,
      text: () => Promise.resolve(JSON.stringify({ detail: "unexpected request" })),
    });
  };
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, settingsMeta()));
  findCompositionsButton(region).click();
  await flush(2);
  const caps = findSettingInput(region, "captions_enabled");
  caps.checked = false;
  caps.dispatchEvent(new Event("change", { bubbles: true }));
  await flush();
  assert(findSettingInput(region, "captions_enabled")?.checked === false,
    "the saved setting refreshes the mounted section");
  finishPlan();
  await flush();
  renderEditorialRegion(region, projectSnapshot(
    EDITORIAL_PROJECT, { ...settingsMeta({ captions_enabled: false }), plan_status: "stale", stale_reasons: ["script"] }));
  assert(region.textContent.includes("Edit Plan is stale"),
    "the superseded fetch cannot poison the controller or suppress later live refreshes");
});

/* --- 11. Editorial composition overview ----------------------------------- */

const COMPOSITION_META = {
  has_edit_plan: true,
  plan_status: "current",
  stale: false,
  stale_reasons: [],
  edit_plan_url: EDIT_PLAN_URL,
  preview_url: EDIT_PLAN_META.preview_url,
};

const SAMPLE_PLAN = {
  schema_version: "editorial.edit-plan/1",
  project_id: "proj-ed",
  width: 1920, height: 1080, fps: 30,
  editorial_text_enabled: false,
  captions_enabled: true,
  compositions: [
    {
      id: "comp-a", start: 0, duration: 5.5, template: "archiveCanvas",
      assets: [
        { id: "as-1", type: "image", evidence_class: "evidence", locked: true },
        { id: "as-2", type: "image", evidence_class: "illustration", locked: false },
        { id: "as-3", type: "image", evidence_class: "illustration" },
      ],
      elements: [1, 2, 3, 4, 5],
      events: [1, 2, 3],
      narration_refs: ["n-1", "n-2"],
      caption_refs: [],
    },
    {
      id: "comp-b", start: 5.5, duration: 7, template: "bigTextReveal",
      assets: [], elements: [], events: [], narration_refs: [],
    },
  ],
};

record("editorial-compositions: the action is available for every plan state, never for classic", () => {
  for (const meta of [COMPOSITION_META, stalePlanMeta(["script"]), UNTRACKED_PLAN_META, EDIT_PLAN_META]) {
    const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, meta));
    assert(findCompositionsButton(node),
      `Show compositions available for ${meta.plan_status || "missing plan_status"}`);
  }
  const classic = { ...LEGACY_PROJECT, video_mode: "classic" };
  eq(editorialPreviewSection(projectSnapshot(classic, COMPOSITION_META)), null,
    "classic projects never see the composition UI");
});

record("editorial-compositions: a non-local edit_plan_url never yields the action or a link", () => {
  const badUrls = [
    "http" + "://remote.example/api/projects/proj-ed/editorial/edit-plan",
    "http" + "s://remote.example/api/projects/proj-ed/editorial/edit-plan",
    "//cdn.example.com/api/projects/proj-ed/editorial/edit-plan",
    "/static/edit-plan.json",
    "/api/projects/other/editorial/edit-plan",
    "/api/projects/proj-ed/editorial/settings",
    "/\\host/edit-plan",
    42,
    null,
  ];
  for (const bad of badUrls) {
    const node = editorialPreviewSection(projectSnapshot(
      EDITORIAL_PROJECT, { ...COMPOSITION_META, edit_plan_url: bad }));
    assert(node, `the section still renders for ${JSON.stringify(bad)}`);
    eq(findCompositionsButton(node), null, `no action for ${JSON.stringify(bad)}`);
    assert(![...node.querySelectorAll("a")].some((a) => a.textContent === "Download Edit Plan JSON"),
      `no download link for ${JSON.stringify(bad)}`);
  }
});

await recordAsync("editorial-compositions: rendering and live refresh never fetch the Edit Plan", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch(() => ({ status: 404, payload: { detail: "unexpected call" } }));
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  await flush();
  eq(calls.length, 0, "the full plan is fetched only by an explicit action");
  assert(findCompositionsButton(region), "the explicit action is still there");
});

await recordAsync("editorial-compositions: one explicit click fetches edit_plan_url once and renders the summary", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === EDIT_PLAN_URL) return { payload: SAMPLE_PLAN };
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  eq(calls.filter((c) => c.url === EDIT_PLAN_URL).length, 1, "exactly one explicit fetch");
  const text = region.textContent;
  assert(text.includes("comp-a"), "first composition id shown");
  assert(text.includes("comp-b"), "second composition id shown");
  assert(text.includes("archiveCanvas"), "template shown");
  assert(text.includes("bigTextReveal"), "second template shown");
  assert(text.includes("start 0:00 · duration 0:06"), "start/duration rendered (5.5s rounds to 0:06)");
  assert(text.includes("3 assets"), "asset count");
  assert(text.includes("5 elements"), "element count");
  assert(text.includes("3 events"), "event count");
  assert(text.includes("2 narration refs"), "narration-reference count");
  assert(text.includes("1 evidence"), "evidence asset count");
  assert(text.includes("2 illustration"), "illustration asset count");
  assert(text.includes("1 locked"), "locked asset count");
});

await recordAsync("editorial-compositions: loading state shows and clicks cannot duplicate the fetch", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = [];
  let finishPlan;
  globalThis.fetch = (url, opts) => {
    calls.push({ url: String(url), method: (opts && opts.method) || "GET" });
    return new Promise((resolve) => {
      finishPlan = () => resolve({
        ok: true, status: 200,
        text: () => Promise.resolve(JSON.stringify(SAMPLE_PLAN)),
      });
    });
  };
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  const btn = findCompositionsButton(region);
  btn.click();
  await flush(2);
  assert(btn.disabled, "the action is disabled while loading");
  assert(region.textContent.includes("Loading…"), "loading state shown");
  btn.disabled = false;
  btn.click(); // a re-click (even forced) must not start a second fetch
  await flush(2);
  eq(calls.length, 1, "no duplicate fetch while one is in flight");
  finishPlan();
  await flush();
  eq(calls.length, 1, "still exactly one fetch end to end");
  assert(region.textContent.includes("comp-a"), "the list rendered after the load");
  assert(!btn.disabled, "the action re-enables for an explicit re-fetch");
});

await recordAsync("editorial-compositions: failure shows the error state and Retry re-fetches", async () => {
  state.config = { apiBase: "", mediaBase: null };
  let fail = true;
  const calls = stubFetch((call) => {
    if (call.url === EDIT_PLAN_URL) {
      return fail ? { status: 500, payload: { detail: "plan read failed" } } : { payload: SAMPLE_PLAN };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  assert(region.textContent.includes("plan read failed"), "error state explains the failure");
  const retry = [...region.querySelectorAll("button")].find((b) => b.textContent === "Retry");
  assert(retry, "a Retry action is offered");
  fail = false;
  retry.click();
  await flush();
  eq(calls.filter((c) => c.url === EDIT_PLAN_URL).length, 2, "Retry issues exactly one more fetch");
  assert(region.textContent.includes("comp-a"), "the list rendered after the retry");
});

await recordAsync("editorial-compositions: a malformed plan payload yields the error state, not a crash", async () => {
  state.config = { apiBase: "", mediaBase: null };
  stubFetch((call) => {
    if (call.url === EDIT_PLAN_URL) return { payload: { compositions: "nope" } };
    return { status: 404, payload: { detail: "unexpected" } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  assert(region.textContent.includes("not a readable composition list"),
    "an unreadable plan explains itself");
  assert(!region.querySelector("[role=list]"), "no phantom composition rows were rendered");
  assert(!region.textContent.includes("comp-a"), "no plan content rendered");
});

record("editorial-compositions: malformed composition entries degrade to placeholders", () => {
  const plan = {
    compositions: [
      {
        id: "ok", start: 1, duration: 2, template: "archiveCanvas",
        assets: [{ evidence_class: "evidence", locked: true }, { locked: "yes" }, "junk", null],
        elements: "nope", events: [1], narration_refs: "nope",
      },
      "just-a-string",
      null,
      [1, 2, 3],
      { start: "soon", duration: null, template: 42, assets: "nope" },
    ],
  };
  const sum = summarizeEditPlanCompositions(plan);
  assert(sum.ok, "a compositions array is readable even with garbage entries");
  eq(sum.compositions.length, 5, "every entry produces a row");
  const a = sum.compositions[0];
  eq(a.id, "ok");
  eq(a.start, 1);
  eq(a.duration, 2);
  eq(a.template, "archiveCanvas");
  eq(a.assetCount, 4, "raw asset entries are counted");
  eq(a.elementCount, 0, "non-array elements count as zero");
  eq(a.eventCount, 1);
  eq(a.narrationRefCount, 0, "non-array refs count as zero");
  eq(a.evidenceAssetCount, 1, "only the strict evidence class matches");
  eq(a.illustrationAssetCount, 0, "an unrecognized evidence_class is neither");
  eq(a.lockedAssetCount, 1, "only strict true counts as locked");
  const s = sum.compositions[1];
  eq(s.id, "—", "non-object entry gets a placeholder id");
  eq(s.start, null);
  eq(s.duration, null);
  eq(s.template, "—");
  const e = sum.compositions[4];
  eq(e.start, null, "a non-numeric start is not trusted");
  eq(e.template, "—", "a non-string template falls back");
  eq(e.assetCount, 0, "non-array assets count as zero");
});

record("editorial-compositions: a plan without a compositions array is unreadable", () => {
  for (const plan of [null, "plan", 42, {}, { compositions: "nope" }, { compositions: {} }, []]) {
    eq(summarizeEditPlanCompositions(plan).ok, false, `unreadable plan: ${JSON.stringify(plan)}`);
  }
});

await recordAsync("editorial-compositions: Open Preview is kept and the open list survives live refreshes", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch((call) => {
    if (call.url === EDIT_PLAN_URL) return { payload: SAMPLE_PLAN };
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  const list = region.querySelector("[role=list]");
  assert(list, "the composition list rendered");
  const openPreview = region.querySelector("a");
  assert(openPreview && openPreview.textContent === "Open Preview",
    "Open Preview remains the first link with the list open");
  // A live tick carrying newer plan metadata must not rebuild the open list
  // or re-fetch the plan.
  renderEditorialRegion(region, projectSnapshot(
    EDITORIAL_PROJECT, { ...COMPOSITION_META, plan_status: "stale", stale_reasons: ["script"] }));
  eq(region.querySelector("[role=list]"), list, "the open list is not rebuilt by a live tick");
  eq(calls.filter((c) => c.url === EDIT_PLAN_URL).length, 1, "the tick re-issues no Edit Plan fetch");
});

/* --- 11b. Editorial composition mutations ------------------------------- */

const EDITOR_PLAN = {
  ...SAMPLE_PLAN,
  compositions: [{
    id: "comp/ edit", start: 0, duration: 5, template: "illustrationCanvas",
    assets: [
      {
        id: "asset/one", label: "Hero", type: "generated_image",
        evidence_class: "illustration", locked: false,
        generation: { prompt: "A rust-red Mars illustration", model: "krea", seed: 4 },
      },
      { id: "evidence", label: "Archive", type: "historical_photo", evidence_class: "evidence", locked: true },
    ],
    elements: [
      { id: "headline", type: "text", text: "1949", role: "headline" },
      { id: "document", type: "document", text: "Project Mars", role: "supporting-text" },
      { id: "hero", type: "image", asset_id: "asset/one", role: "illustration" },
    ],
    events: [{ time: 0, action: "fadeUp", target: "headline" }],
    narration_refs: ["scene-1"], caption_refs: [],
  }],
};

record("editorial-editor: strict parser exposes only validated controls", () => {
  const parsed = parseCompositionEditor(EDITOR_PLAN);
  assert(parsed.ok, "valid composition array is readable");
  eq(parsed.compositions[0].templateKnown, true);
  eq(parsed.compositions[0].elements.map((item) => item.editable), [true, true, false]);
  eq(parsed.compositions[0].events[0].actionKnown, true);
  eq(parsed.compositions[0].assets[1].evidence, true);
  const malformed = parseCompositionEditor({ compositions: [{
    id: 12, template: "invented", duration: Infinity,
    assets: [{ id: null, locked: "yes" }],
    elements: [{ id: "x", type: "html", text: "<img onerror=alert(1)>" }],
    events: [{ action: "inventedGlitch" }],
  }] }).compositions[0];
  eq(malformed.id, null, "non-string ids cannot become endpoints");
  eq(malformed.templateKnown, false, "unknown templates are disabled");
  eq(malformed.duration, null, "non-finite durations are rejected");
  eq(malformed.elements[0].editable, false, "unknown element types cannot be edited");
  eq(malformed.events[0].actionKnown, false, "unknown motion is disabled");
});

record("editorial-revision: structural diff identifies added changed and removed compositions", () => {
  const before = structuredClone(EDITOR_PLAN);
  const after = structuredClone(EDITOR_PLAN);
  after.compositions[0].elements[0].text = "TEN RULERS";
  after.compositions.push({
    ...structuredClone(after.compositions[0]),
    id: "added", start: 5, duration: 3,
  });
  const diff = summarizeEditorialRevision(before, after);
  assert(diff.ok);
  assert(diff.changed);
  eq(diff.rows.find((item) => item.id === "comp/ edit").state, "changed");
  eq(diff.rows.find((item) => item.id === "added").state, "added");
  const removed = summarizeEditorialRevision(after, before);
  eq(removed.rows.find((item) => item.id === "added").state, "removed");
  eq(summarizeEditorialRevision(before, structuredClone(before)).changed, false);
});

await recordAsync("editorial-revision: preview is non-mutating and apply uses the server revision id", async () => {
  state.config = { apiBase: "", mediaBase: null };
  let current = structuredClone(EDITOR_PLAN);
  const proposed = structuredClone(EDITOR_PLAN);
  proposed.compositions[0].elements[0].text = "TEN RULERS";
  const revisionId = "a".repeat(32);
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === EDIT_PLAN_URL) return { payload: current };
    if (call.method === "POST" && call.url.endsWith("/editorial/revisions")) {
      return { payload: { revision_id: revisionId, plan: proposed } };
    }
    if (call.method === "POST" && call.url.endsWith(`/revisions/${revisionId}/apply`)) {
      current = proposed;
      return { payload: current };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  const textarea = region.querySelector('[data-ed-revision-instruction="sequence"]');
  textarea.value = "Add ten rulers and focus one.";
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  region.querySelector('[data-ed-preview-revision="sequence"]').click();
  await flush();
  eq(calls.at(-1), {
    url: "/api/projects/proj-ed/editorial/revisions",
    method: "POST",
    body: { instruction: "Add ten rulers and focus one." },
  });
  eq(region.querySelector('[data-ed-text="headline"]').value, "1949",
    "preview does not replace the current Edit Plan");
  assert(region.textContent.includes("Unapplied preview"));
  assert(region.textContent.includes("Nothing changes until Apply"));
  region.querySelector(`[data-ed-apply-revision="${revisionId}"]`).click();
  await flush();
  eq(calls.at(-1), {
    url: `/api/projects/proj-ed/editorial/revisions/${revisionId}/apply`,
    method: "POST", body: null,
  });
  eq(region.querySelector('[data-ed-text="headline"]').value, "TEN RULERS");
  eq(calls.filter((call) => call.method === "GET").length, 1,
    "proposal and apply responses update the editor without another GET");
});

await recordAsync("editorial-revision: composition scope is sent explicitly", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const revisionId = "b".repeat(32);
  const calls = stubFetch((call) => {
    if (call.method === "GET") return { payload: EDITOR_PLAN };
    if (call.method === "POST" && call.url.endsWith("/editorial/revisions")) {
      return { payload: { revision_id: revisionId, plan: EDITOR_PLAN } };
    }
    return { status: 404, payload: { detail: "unexpected" } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  const textarea = region.querySelector('[data-ed-revision-instruction="comp/ edit"]');
  textarea.value = "Make the reveal more deliberate.";
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
  region.querySelector('[data-ed-preview-revision="comp/ edit"]').click();
  await flush();
  eq(calls.at(-1).body, {
    instruction: "Make the reveal more deliberate.",
    composition_id: "comp/ edit",
  });
});

await recordAsync("editorial-editor: regeneration and all deterministic PATCHes are exact and do not refetch", async () => {
  state.config = { apiBase: "", mediaBase: null };
  let current = structuredClone(EDITOR_PLAN);
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === EDIT_PLAN_URL) return { payload: current };
    if (call.method === "POST" && call.url.endsWith("/regenerate")) return { payload: current };
    if (call.method === "PATCH" && call.url.includes("/editorial/compositions/")) {
      if (call.body.duration != null) current.compositions[0].duration = call.body.duration;
      if (call.body.template) current.compositions[0].template = call.body.template;
      if (call.body.text_updates) current.compositions[0].elements[0].text = call.body.text_updates.headline;
      if (call.body.event_actions) current.compositions[0].events[0].action = call.body.event_actions[0];
      return { payload: current };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  const encoded = "/api/projects/proj-ed/editorial/compositions/comp%2F%20edit";
  [...region.querySelectorAll("button")].find((b) => b.textContent === "Regenerate").click();
  await flush();
  eq(calls.at(-1), { url: `${encoded}/regenerate`, method: "POST", body: null }, "bodyless regeneration");

  let input = region.querySelector('[data-ed-duration="comp/ edit"]');
  input.value = "6";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  [...region.querySelectorAll("button")].find((b) => b.textContent === "Save duration").click();
  await flush();
  eq(calls.at(-1), { url: encoded, method: "PATCH", body: { duration: 6 } });

  let select = region.querySelector('[data-ed-template="comp/ edit"]');
  select.value = "archiveCanvas";
  select.dispatchEvent(new Event("change", { bubbles: true }));
  [...region.querySelectorAll("button")].find((b) => b.textContent === "Save template").click();
  await flush();
  eq(calls.at(-1), { url: encoded, method: "PATCH", body: { template: "archiveCanvas" } });

  input = region.querySelector('[data-ed-text="headline"]');
  input.value = "ELON";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  [...region.querySelectorAll("button")].find((b) => b.textContent === "Save text").click();
  await flush();
  eq(calls.at(-1), { url: encoded, method: "PATCH", body: { text_updates: { headline: "ELON" } } });

  select = region.querySelector('[data-ed-event="0"]');
  select.value = "collapseToBlack";
  select.dispatchEvent(new Event("change", { bubbles: true }));
  [...region.querySelectorAll("button")].find((b) => b.textContent === "Save events").click();
  await flush();
  eq(calls.at(-1), { url: encoded, method: "PATCH", body: { event_actions: { 0: "collapseToBlack" } } });
  eq(calls.filter((call) => call.method === "GET").length, 1,
    "returned plans update the open editor without an automatic GET");
  eq(region.querySelector('[data-ed-text="headline"]').value, "ELON",
    "returned text is visible in the retained editor");
});

await recordAsync("editorial-editor: asset locking is scoped and evidence offers no unlock", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch((call) => {
    if (call.method === "GET") return { payload: EDITOR_PLAN };
    return { payload: EDITOR_PLAN };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  assert(region.textContent.includes("Locked (evidence)"), "evidence lock is fixed");
  const unlocks = [...region.querySelectorAll("button")].filter((b) => b.textContent === "Unlock");
  eq(unlocks.length, 0, "evidence never offers unlock");
  [...region.querySelectorAll("button")].find((b) => b.textContent === "Lock").click();
  await flush();
  eq(calls.at(-1), {
    url: "/api/projects/proj-ed/editorial/compositions/comp%2F%20edit/assets/asset%2Fone",
    method: "PATCH", body: { locked: true },
  });
  eq(calls.filter((call) => call.method === "GET").length, 1, "locking does not refetch the plan");
});

await recordAsync("editorial-editor: generated images run only after the explicit asset action", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = stubFetch((call) => {
    if (call.method === "GET") return { payload: EDITOR_PLAN };
    return { payload: EDITOR_PLAN };
  });
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  await flush();
  eq(calls.length, 0, "mounting does not generate or fetch a plan");
  findCompositionsButton(region).click();
  await flush();
  const generate = region.querySelector('[data-ed-generate-asset="asset/one"]');
  assert(generate, "validated generated_image instructions expose the explicit action");
  generate.click();
  generate.click();
  await flush();
  eq(calls, [
    { url: EDIT_PLAN_URL, method: "GET", body: null },
    {
      url: "/api/projects/proj-ed/editorial/compositions/comp%2F%20edit/assets/asset%2Fone/generate",
      method: "POST", body: null,
    },
  ], "one guarded bodyless POST uses locally encoded ids and no follow-up GET");
});

await recordAsync("editorial-editor: local replacement sends multipart once with no manual content type", async () => {
  state.config = { apiBase: "", mediaBase: null };
  const calls = [];
  globalThis.fetch = (url, opts = {}) => {
    calls.push({ url: String(url), opts });
    return Promise.resolve({
      ok: true, status: 200,
      text: () => Promise.resolve(JSON.stringify(EDITOR_PLAN)),
    });
  };
  const region = document.createElement("div");
  renderEditorialRegion(region, projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  findCompositionsButton(region).click();
  await flush();
  const fileInput = region.querySelector('[data-ed-file="asset/one"]');
  const image = new File([new Uint8Array([1, 2, 3])], "replacement.png", { type: "image/png" });
  Object.defineProperty(fileInput, "files", { configurable: true, value: [image] });
  fileInput.dispatchEvent(new Event("change", { bubbles: true }));
  const evidence = region.querySelector('[data-ed-evidence="asset/one"]');
  evidence.checked = true;
  const replace = region.querySelector('[data-ed-replace="asset/one"]');
  replace.click();
  replace.click();
  await flush();
  eq(calls.length, 2, "one explicit GET and one guarded replacement POST");
  const call = calls[1];
  eq(call.url,
    "/api/projects/proj-ed/editorial/compositions/comp%2F%20edit/assets/asset%2Fone/replace");
  eq(call.opts.method, "POST");
  assert(call.opts.body instanceof FormData, "replacement body is multipart FormData");
  eq(call.opts.body.get("file").name, "replacement.png");
  eq(call.opts.body.get("evidence"), "true");
  eq(call.opts.credentials, "same-origin");
  assert(!call.opts.headers, "the browser owns the multipart Content-Type boundary");
});

record("editorial-download: the link is built only from a project-local edit_plan_url", () => {
  const node = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, COMPOSITION_META));
  const anchors = [...node.querySelectorAll("a")];
  eq(anchors[0].textContent, "Open Preview", "Open Preview stays the first link");
  const dl = anchors.find((a) => a.textContent === "Download Edit Plan JSON");
  assert(dl, "the download link is present");
  eq(dl.getAttribute("href"), EDIT_PLAN_URL + "?download=true", "href appends ?download=true");
  eq(dl.getAttribute("target"), "_blank", "opens in a new tab");
  assert((dl.getAttribute("rel") || "").includes("noopener"), "rel carries noopener");
});

record("editorial-download: remote, malformed, and cross-project values yield no link", () => {
  const cases = [
    "http" + "://remote.example/api/projects/proj-ed/editorial/edit-plan",
    "http" + "s://remote.example/api/projects/proj-ed/editorial/edit-plan",
    "//cdn.example.com/api/projects/proj-ed/editorial/edit-plan",
    "/static/edit-plan.json",
    "/api/projects/proj-ed/editorial/edit-plan?download=true",
    "/api/projects/proj-ed/editorial/edit-plan#frag",
    "/api/projects/proj-ed/editorial\\edit-plan",
    "/api/projects/proj-ed/editorial/ edit-plan",
    42,
    null,
    "",
  ];
  for (const bad of cases) {
    const node = editorialPreviewSection(projectSnapshot(
      EDITORIAL_PROJECT, { ...COMPOSITION_META, edit_plan_url: bad }));
    assert(node, `the section still renders for ${JSON.stringify(bad)}`);
    assert(![...node.querySelectorAll("a")].some((a) => a.textContent === "Download Edit Plan JSON"),
      `no download link for ${JSON.stringify(bad)}`);
  }
  // Valid shape but another project's id: never becomes a link.
  const cross = editorialPreviewSection(projectSnapshot(EDITORIAL_PROJECT, {
    ...COMPOSITION_META, edit_plan_url: "/api/projects/other-project/editorial/edit-plan",
  }));
  assert(![...cross.querySelectorAll("a")].some((a) => a.textContent === "Download Edit Plan JSON"),
    "a cross-project-looking path is rejected");
});

record("download-url: the validator pins safe project-local paths only", () => {
  eq(safeEditPlanDownloadUrl("/api/projects/proj-ed/editorial/edit-plan", "proj-ed"),
    "/api/projects/proj-ed/editorial/edit-plan?download=true");
  eq(safeEditPlanDownloadUrl("/api/projects/proj-ed/editorial/edit-plan"),
    "/api/projects/proj-ed/editorial/edit-plan?download=true",
    "without a known project id only the /api/projects/ prefix is enforced");
  eq(safeEditPlanDownloadUrl("  /api/projects/proj-ed/editorial/edit-plan  ", "proj-ed"),
    "/api/projects/proj-ed/editorial/edit-plan?download=true", "surrounding whitespace is trimmed");
  eq(safeEditPlanDownloadUrl("http" + "s://remote.example/api/projects/proj-ed/editorial/edit-plan", "proj-ed"),
    null, "absolute remote URLs are rejected");
  eq(safeEditPlanDownloadUrl("//cdn.example.com/api/projects/proj-ed/editorial/edit-plan", "proj-ed"),
    null, "protocol-relative URLs are rejected");
  eq(safeEditPlanDownloadUrl("/api/projects/other/editorial/edit-plan", "proj-ed"),
    null, "cross-project paths are rejected");
  eq(safeEditPlanDownloadUrl("/static/plan.json", "proj-ed"), null, "paths outside /api/projects/ are rejected");
  eq(safeEditPlanDownloadUrl("/api/projects/proj-ed/editorial/edit-plan?x=1", "proj-ed"), null,
    "an existing query string is rejected");
  eq(safeEditPlanDownloadUrl("/api/projects/proj-ed/editorial/edit-plan#f", "proj-ed"), null,
    "fragments are rejected");
  eq(safeEditPlanDownloadUrl(42), null);
  eq(safeEditPlanDownloadUrl(null), null);
  eq(localApiPath("https:" + "//remote.example/api"), null, "localApiPath rejects any scheme URL");
  eq(localApiPath("/api/projects/proj-ed/editorial/settings"), "/api/projects/proj-ed/editorial/settings");
  eq(projectEditorialApiPath(SETTINGS_URL, "proj-ed", "settings"), SETTINGS_URL);
  eq(projectEditorialApiPath("/api/projects/other/editorial/settings", "proj-ed", "settings"), null,
    "request paths cannot cross projects");
  eq(projectEditorialApiPath(EDIT_PLAN_URL, "proj-ed", "settings"), null,
    "request paths must match the exact endpoint");
  eq(projectEditorialApiPath("/\\host/settings", "proj-ed", "settings"), null,
    "request paths reject backslashes");
});

/* --- 12. Export readiness summary: display settings ----------------------- */

record("export: editorialDisplaySettings trusts only strict booleans", () => {
  const snap = (ed) => exportSnapshot(EDITORIAL_EXPORT_PROJECT, ed);
  eq(editorialDisplaySettings(snap({ captions_enabled: true, editorial_text_enabled: false })),
    { captions: true, editorialText: false });
  eq(editorialDisplaySettings(snap({ captions_enabled: "yes", editorial_text_enabled: 1 })),
    { captions: null, editorialText: null }, "strings/numbers are never guessed");
  eq(editorialDisplaySettings(snap({ captions_enabled: null })),
    { captions: null, editorialText: null }, "null means no plan, not a value");
  eq(editorialDisplaySettings(snap(null)), { captions: null, editorialText: null });
  eq(editorialDisplaySettings(snap("corrupt")), { captions: null, editorialText: null });
});

record("export: editorial readiness summary reports strict boolean display settings", () => {
  const snap = exportSnapshot(EDITORIAL_EXPORT_PROJECT,
    { ...EDIT_PLAN_CURRENT, captions_enabled: true, editorial_text_enabled: false });
  const dl = renderInputSummary(snap, {});
  eq([...dl.querySelectorAll("dt")].map((n) => n.textContent),
    ["Edit Plan", "Captions", "Editorial text", "Narration", "Optional inputs"]);
  const dds = [...dl.querySelectorAll("dd")].map((n) => n.textContent);
  eq(dds[1], "enabled", "captions on");
  eq(dds[2], "disabled", "editorial text off");
  const flipped = renderInputSummary(exportSnapshot(EDITORIAL_EXPORT_PROJECT,
    { ...EDIT_PLAN_CURRENT, captions_enabled: false, editorial_text_enabled: true }), {});
  const flippedDds = [...flipped.querySelectorAll("dd")].map((n) => n.textContent);
  eq(flippedDds[1], "disabled");
  eq(flippedDds[2], "enabled");
});

record("export: malformed display settings are omitted, never guessed", () => {
  const metas = [
    { ...EDIT_PLAN_CURRENT, captions_enabled: "yes", editorial_text_enabled: null },
    { ...EDIT_PLAN_CURRENT, captions_enabled: 1, editorial_text_enabled: "false" },
    { has_edit_plan: true },
  ];
  for (const meta of metas) {
    const dl = renderInputSummary(exportSnapshot(EDITORIAL_EXPORT_PROJECT, meta), {});
    eq([...dl.querySelectorAll("dt")].map((n) => n.textContent),
      ["Edit Plan", "Narration", "Optional inputs"], "no display rows without strict booleans");
  }
  const mixed = renderInputSummary(exportSnapshot(EDITORIAL_EXPORT_PROJECT,
    { ...EDIT_PLAN_CURRENT, captions_enabled: true, editorial_text_enabled: "on" }), {});
  eq([...mixed.querySelectorAll("dt")].map((n) => n.textContent),
    ["Edit Plan", "Captions", "Narration", "Optional inputs"],
    "only the strict-boolean row survives");
});

record("export: classic and legacy summaries never show display settings", () => {
  const dl = renderInputSummary(
    exportSnapshot(CLASSIC_EXPORT_PROJECT,
      { has_edit_plan: true, captions_enabled: true, editorial_text_enabled: true },
      { scenes: CLASSIC_SCENES, assets: CLASSIC_ASSETS }), {});
  eq([...dl.querySelectorAll("dt")].map((n) => n.textContent),
    ["Scene visuals", "Narration", "Optional inputs"], "classic rows unchanged");
  const legacy = renderInputSummary(exportSnapshot(LEGACY_EXPORT_PROJECT, undefined), {});
  eq([...legacy.querySelectorAll("dt")].map((n) => n.textContent),
    ["Scene visuals", "Narration", "Optional inputs"], "legacy rows unchanged");
});

/* --- 13. Editorial workspace screen --------------------------------------- */

const WS_PROJECT = { ...LEGACY_PROJECT, id: "proj-ws", video_mode: "editorial" };
const WS_CLASSIC_PROJECT = { ...LEGACY_PROJECT, id: "proj-wc", video_mode: "classic" };
const WS_META = {
  has_edit_plan: true,
  plan_status: "current",
  stale: false,
  stale_reasons: [],
  edit_plan_url: "/api/projects/proj-ws/editorial/edit-plan",
  preview_url: "/api/projects/proj-ws/editorial/preview",
  settings_url: "/api/projects/proj-ws/editorial/settings",
  captions_enabled: true,
  editorial_text_enabled: true,
  caption_style: "editorialPhrase",
};
const WS_PLAN = {
  schema_version: 1,
  project_id: "proj-ws",
  width: 1080, height: 1920, fps: 30,
  captions_enabled: true,
  editorial_text_enabled: true,
  compositions: [
    {
      id: "c1", start: 0, duration: 4, template: "bigTextReveal",
      assets: [],
      elements: [{ id: "e1", type: "text", text: "OPEN", role: "headline" }],
      events: [{ time: 0, action: "fade", target: "canvas" }],
      narration_refs: [],
    },
    {
      id: "c2", start: 4, duration: 6, template: "archiveCanvas",
      assets: [{ id: "a1", type: "historical_photo", evidence_class: "evidence", locked: true, label: "Archive" }],
      elements: [], events: [], narration_refs: ["n1"],
    },
  ],
};

record("editorial-workspace: parser resolves the dedicated route", () => {
  window.location.hash = "#/editorial";
  eq(parseRoute().name, "editorial");
  window.location.hash = "#/timeline";
  eq(parseRoute().name, "timeline", "the adjacent route is unaffected");
  window.location.hash = "#/";
});

record("editorial-workspace: template labels are readable, unknown values pass through", () => {
  eq(templateLabel("archiveCanvas"), "Archive canvas");
  eq(templateLabel("documentReveal"), "Document reveal");
  eq(templateLabel("comparisonCanvas"), "Comparison canvas");
  eq(templateLabel("illustrationCanvas"), "Illustration canvas");
  eq(templateLabel("bigTextReveal"), "Big text reveal");
  eq(templateLabel("inventedTemplate"), "inventedTemplate", "unknown stays identifiable");
  eq(templateLabel(42), "—");
  eq(templateLabel(null), "—");
});

record("editorial-workspace: preview URL is trusted only for the mounted project's exact path", () => {
  const preview = "/api/projects/proj-ws/editorial/preview";
  eq(safeEditorialPreviewUrl(preview, "proj-ws"), preview, "exact project-local path accepted");
  eq(safeEditorialPreviewUrl("http" + "s://remote.example/preview", "proj-ws"), null, "remote rejected");
  eq(safeEditorialPreviewUrl("//cdn.example.com/preview", "proj-ws"), null, "protocol-relative rejected");
  eq(safeEditorialPreviewUrl("/api/projects/other/editorial/preview", "proj-ws"), null, "cross-project rejected");
  eq(safeEditorialPreviewUrl("/api/projects/proj-ws/editorial/edit-plan", "proj-ws"), null, "wrong endpoint rejected");
  eq(safeEditorialPreviewUrl(preview + "?ts=1", "proj-ws"), null, "query junk rejected");
  eq(safeEditorialPreviewUrl(preview + "#f", "proj-ws"), null, "fragment rejected");
  eq(safeEditorialPreviewUrl("/\\host/preview", "proj-ws"), null, "backslash rejected");
  eq(safeEditorialPreviewUrl("  " + preview, "proj-ws"), null, "surrounding whitespace rejected");
  eq(safeEditorialPreviewUrl(preview, null), null, "no mounted project -> no preview");
  eq(safeEditorialPreviewUrl(42, "proj-ws"), null);
  eq(safeEditorialPreviewUrl(null, "proj-ws"), null);
});

record("editorial-workspace: preview aspect prefers the plan, then project ratio, then portrait", () => {
  eq(previewAspectRatio({ width: 1080, height: 1920 }, {}),
    { css: "1080 / 1920", number: 0.5625 });
  eq(previewAspectRatio({ width: 0, height: 1920 }, { aspect_ratio: "16:9" }),
    { css: "16 / 9", number: 16 / 9 }, "non-positive plan size falls back");
  eq(previewAspectRatio(null, { aspect_ratio: "1:1" }), { css: "1 / 1", number: 1 });
  eq(previewAspectRatio(null, { aspect_ratio: "weird" }),
    { css: "9 / 16", number: 9 / 16 }, "unparsable ratio falls back");
  eq(previewAspectRatio(null, {}), { css: "9 / 16", number: 9 / 16 }, "Editorial portrait default");
});

await recordAsync("editorial-workspace: classic project gets the pointer state and no plan fetch", async () => {
  state.config = { apiBase: "", mediaBase: null };
  state.currentProjectId = "proj-wc";
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === "/api/projects/proj-wc") {
      return { payload: projectSnapshot(WS_CLASSIC_PROJECT) };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const screen = renderEditorial({ name: "editorial", param: null });
  await flush();
  assert(screen.textContent.includes("Classic project"), "classic pointer state shown");
  assert(!screen.querySelector(".ed-strip"), "no sequence strip for classic projects");
  assert(!screen.querySelector("iframe"), "no preview frame for classic projects");
  assert(!calls.some((c) => c.url.includes("/editorial/")), "no Edit Plan or preview traffic");
  screen.remove();
  state.currentProjectId = null;
});

await recordAsync("editorial-workspace: no-plan state exposes the guarded Generate action and lands in the workspace", async () => {
  state.config = { apiBase: "", mediaBase: null };
  state.currentProjectId = "proj-ws";
  let planExists = false;
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === "/api/projects/proj-ws") {
      const meta = planExists
        ? WS_META
        : {
          has_edit_plan: false,
          generate_url: "/api/projects/proj-ws/editorial/plan",
          edit_plan_url: WS_META.edit_plan_url,
          preview_url: WS_META.preview_url,
        };
      return { payload: projectSnapshot(WS_PROJECT, meta) };
    }
    if (call.method === "POST" && call.url === "/api/projects/proj-ws/editorial/plan") {
      planExists = true;
      return { payload: WS_PLAN };
    }
    if (call.method === "GET" && call.url === "/api/projects/proj-ws/editorial/edit-plan") {
      return { payload: WS_PLAN };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const screen = renderEditorial({ name: "editorial", param: null });
  await flush();
  assert(screen.textContent.includes("No Edit Plan yet"), "no-plan empty state shown");
  const gen = [...screen.querySelectorAll("button")].find((b) => b.textContent === "Generate Edit Plan");
  assert(gen, "Generate Edit Plan action present");
  assert(!screen.querySelector(".ed-strip"), "no workspace before the plan exists");
  gen.click();
  gen.click(); // a second click while pending must be ignored
  await flush();
  const posts = calls.filter((c) => c.method === "POST" && c.url === "/api/projects/proj-ws/editorial/plan");
  eq(posts.length, 1, "exactly one bodyless generation POST");
  eq(posts[0].body, null, "no request body and no force flag");
  assert(screen.querySelector(".ed-strip"), "the screen lands in the workspace after generation");
  assert(screen.textContent.includes("Big text reveal"), "strip renders the plan's compositions");
  screen.remove();
  state.currentProjectId = null;
});

await recordAsync("editorial-workspace: plan renders strip, detail, and a preview gated behind the toggle", async () => {
  state.config = { apiBase: "", mediaBase: null };
  state.currentProjectId = "proj-ws";
  let planFetches = 0;
  const calls = stubFetch((call) => {
    if (call.method === "GET" && call.url === "/api/projects/proj-ws") {
      return { payload: projectSnapshot(WS_PROJECT, WS_META) };
    }
    if (call.method === "GET" && call.url === "/api/projects/proj-ws/editorial/edit-plan") {
      planFetches += 1;
      return { payload: WS_PLAN };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const screen = renderEditorial({ name: "editorial", param: null });
  await flush();
  eq(planFetches, 1, "the plan is read exactly once on load");
  const strip = screen.querySelector(".ed-strip");
  assert(strip, "the sequence strip rendered");
  const cards = [...strip.querySelectorAll(".ed-card")];
  eq(cards.length, 2, "one card per composition");
  assert(screen.textContent.includes("Big text reveal"), "readable template label (1)");
  assert(screen.textContent.includes("Archive canvas"), "readable template label (2)");
  assert(screen.textContent.includes("0:00–0:04"), "first timing range");
  assert(screen.textContent.includes("0:04–0:10"), "second timing range");
  assert(screen.textContent.includes("1 locked"), "locked asset count surfaced on the card");
  // The detail panel opens on the first composition by default.
  assert(screen.textContent.includes("c1"), "first composition selected by default");
  assert(screen.querySelector('[data-ed-text="e1"]'), "deterministic text control exposed");
  assert(screen.querySelector('[data-ed-regen="c1"]'), "per-composition Regenerate available");
  // Selecting another card repaints the detail without any request.
  cards[1].click();
  await flush(2);
  eq(planFetches, 1, "selection issues no fetch");
  assert(screen.querySelector('[data-ed-regen="c2"]'), "detail follows the selection");
  const selectedCard = strip.querySelector(".ed-card.selected");
  assert(selectedCard && selectedCard.getAttribute("data-ed-comp-card") === "c2", "strip highlight follows the selection");
  // The embedded preview stays off until the explicit toggle.
  const toggle = screen.querySelector("[data-ed-preview-toggle]");
  assert(toggle, "the preview toggle is present");
  assert(!screen.querySelector("iframe"), "no iframe before the explicit toggle");
  toggle.click();
  await flush(2);
  const frame = screen.querySelector("iframe.ed-preview-frame");
  assert(frame, "the iframe mounts on toggle");
  assert(String(frame.getAttribute("src")).startsWith("/api/projects/proj-ws/editorial/preview?ts="),
    "the frame loads the validated preview URL with a cache stamp");
  assert(!calls.some((c) => c.method !== "GET"), "no mutations are ever issued passively");
  screen.remove();
  state.currentProjectId = null;
});

await recordAsync("editorial-workspace: an untrusted preview_url degrades to the unavailable note", async () => {
  state.config = { apiBase: "", mediaBase: null };
  state.currentProjectId = "proj-ws";
  stubFetch((call) => {
    if (call.method === "GET" && call.url === "/api/projects/proj-ws") {
      return { payload: projectSnapshot(WS_PROJECT, {
        ...WS_META,
        preview_url: "/api/projects/other/editorial/preview",
      }) };
    }
    if (call.method === "GET" && call.url === "/api/projects/proj-ws/editorial/edit-plan") {
      return { payload: WS_PLAN };
    }
    return { status: 404, payload: { detail: `unexpected ${call.method} ${call.url}` } };
  });
  const screen = renderEditorial({ name: "editorial", param: null });
  await flush();
  assert(screen.querySelector(".ed-strip"), "the workspace still renders");
  assert(screen.textContent.includes("Preview is unavailable"), "cross-project preview URL surfaces the note");
  assert(!screen.querySelector("[data-ed-preview-toggle]"), "no toggle for an untrusted preview URL");
  assert(!screen.querySelector("iframe"), "no iframe is ever pointed at the untrusted URL");
  screen.remove();
  state.currentProjectId = null;
});

/* --- report -------------------------------------------------------------- */

const passed = results.filter((r) => r[1]).length;
const out = document.getElementById("out");
out.textContent = `LVSTESTS ${JSON.stringify({ passed, total: results.length, results })}`;
document.title = passed === results.length ? "OK" : "FAIL";
