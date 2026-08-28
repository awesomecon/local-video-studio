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

/* --- report -------------------------------------------------------------- */

const passed = results.filter((r) => r[1]).length;
const out = document.getElementById("out");
out.textContent = `LVSTESTS ${JSON.stringify({ passed, total: results.length, results })}`;
document.title = passed === results.length ? "OK" : "FAIL";
