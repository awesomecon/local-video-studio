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
} from "../../js/pages/project.js";

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

/* --- report -------------------------------------------------------------- */

const passed = results.filter((r) => r[1]).length;
const out = document.getElementById("out");
out.textContent = `LVSTESTS ${JSON.stringify({ passed, total: results.length, results })}`;
document.title = passed === results.length ? "OK" : "FAIL";
