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
} from "../../js/pages/project.js";
import {
  renderExport,
  exportVideoMode,
  exportDescriptionText,
  exportWorkflowText,
  exportForceConfirmMessage,
  editorialPlanSummary,
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
    assert(!node.querySelector("button"), "no buttons at all once a plan exists");
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

/* --- report -------------------------------------------------------------- */

const passed = results.filter((r) => r[1]).length;
const out = document.getElementById("out");
out.textContent = `LVSTESTS ${JSON.stringify({ passed, total: results.length, results })}`;
document.title = passed === results.length ? "OK" : "FAIL";
