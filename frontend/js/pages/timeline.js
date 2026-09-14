/**
 * Timeline screen: a zoomable, horizontally scrolling visualization of the
 * project's scenes and media tracks, built entirely from the
 * `GET /api/projects/{id}` snapshot.
 *
 *  - Scene filmstrip: one clip per scene, absolutely positioned by start time
 *    and sized proportionally to duration at the current zoom (px/second).
 *    Each clip shows the scene's generated visual via its project-scoped
 *    local URL (same trust rules as the storyboard); scenes without an asset
 *    get an honest dashed placeholder. Clicking a clip opens the Scene Editor.
 *  - Progressive disclosure (Phase 3): every readable clip carries a small
 *    disclosure control that expands exactly ONE selected scene into its
 *    ordered shots. Geometry comes from backend timing data via the shared
 *    corrected compiled layout (js/shots.js): each shot keeps its FULL
 *    duration, a later shot overlaps the previous shot's tail by its own
 *    incoming transition duration, and the first shot's incoming transition
 *    never consumes width — so the expanded span equals
 *    `scene_rendered_duration` exactly. Intra-scene transition labels and
 *    overlay markers sit at those same compiled starts; editing stays in
 *    the Scene Editor (shot clicks deep-link to the exact shot).
 *  - Overlay markers: a dedicated lane shows one marker per overlay cue,
 *    positioned at absolute scene time, but only under the expanded scene —
 *    never across the whole project.
 *  - Transitions: cut/dissolve/fade markers on scene boundaries.
 *  - Tracks: narration, music, captions (from completed pipeline stages).
 *  - Ruler: time ticks whose step adapts to the zoom level.
 *  - Zoom: slider + Fit button. The default fits the viewport but never
 *    shrinks a scene clip below a usable thumbnail width, so long projects
 *    scroll horizontally instead of collapsing into unreadable slivers.
 *
 * Editorial Mode (video_mode === "editorial"): the screen stays a
 * read-only composition and motion-event timeline built from the same
 * effective, narration-retimed plan preview and export use (the snapshot's
 * exact project-local timeline-plan URL; anything else is an honest
 * unavailable state, never a request). No Edit Plan yet renders an empty
 * state pointing at the Editorial workspace; editing stays owned by
 * #/editorial. A timing badge above the viewport says whether the current
 * plan is aligned to the active narration (word timings or the recorded
 * scene clock) or still on the authored plan's clock ("Planned timing").
 *
 * No data is invented; a track shows only what the backend reports.
 */

import { el, fmtDuration, shortId } from "../dom.js";
import { state, needsProject, latestAssetForScene } from "../state.js";
import { getProject, getEditorialTimelinePlan } from "../api.js";
import { loadingState, emptyState, errorPanel, badge, stageChip, icon } from "../ui.js";
import { registerLiveUpdate } from "../app.js";
import { navigate, parseRoute, sceneEditorHash } from "../router.js";
import { effectiveVideoMode } from "../video-mode.js";
import { compiledShotSpans, compiledSpanSeconds, fmtSecs, shotSummary, staleReason } from "../shots.js";

/** Width of the sticky label column: read from the shared CSS custom
 * property so the grid definition and the JS layout can never drift. */
const LABEL_COL_PX = (() => {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue("--tl-label-w").trim();
  const px = parseFloat(raw);
  return Number.isFinite(px) && px > 0 ? px : 96;
})();
/** Smallest useful on-screen width for a scene thumbnail. */
const MIN_CLIP_PX = 64;
/** Below this width a scene clip hides its caption text. */
const TINY_CLIP_PX = 46;
/** Below this width a scene clip also hides its scene number. */
const NARROW_CLIP_PX = 92;
/** Ruler steps (seconds) tried until one spans at least this many pixels. */
const MIN_TICK_PX = 72;
/** Smallest clip width that still shows the disclosure control. */
const MIN_EXPAND_PX = 34;

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderTimeline(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Timeline")),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject("Select a project in the top bar to see its timeline."));
    return screen;
  }
  screen.append(timelinePanel());
  return screen;
}

/**
 * @returns {HTMLElement}
 */
function timelinePanel() {
  const body = el("div", { class: "panel-body" });
  const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Refresh");
  refreshBtn.onclick = () => load(body);

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Timeline"),
      el("span", { class: "spacer" }),
      refreshBtn,
    ),
    body,
  );

  // Zoom and shot-disclosure survive live refreshes: the closure objects
  // outlive each rebuild.
  const zoom = { scale: null, userSet: false };
  /** Exactly one scene may be expanded into its shots at a time. */
  const view = { expandedSceneId: null };

  /**
   * @param {HTMLElement} region
   * @param {{skeleton?: boolean}} [opts] — omit the skeleton on live refreshes
   */
  let inflight = 0;
  async function load(region, { skeleton = true } = {}) {
    const token = ++inflight;
    if (skeleton) region.replaceChildren(loadingState(5));
    try {
      const snap = await getProject(state.config, state.currentProjectId);
      if (token !== inflight) return;
      if (effectiveVideoMode(snap.project) === "editorial") {
        await loadEditorialTimeline(region, snap, zoom, {
          isStale: () => token !== inflight,
          reload: () => load(region),
        });
        return;
      }
      const scenes = (snap.scenes || []).sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
      if (!scenes.length) {
        region.replaceChildren(emptyState("No scenes yet", "Run planning from the Script screen to draft scenes."));
        return;
      }
      const stages = (snap.stage_state && /** @type {any} */ (snap.stage_state).stages) || {};
      const assets = snap.assets || [];
      region.replaceChildren(
        buildTimeline(scenes, assets, stages, snap.project, zoom, view),
      );
    } catch (err) {
      if (token !== inflight) return;
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load(region) }, "Retry"),
      ));
    }
  }

  // Live path: tracks fill in as stages complete (no skeleton flash).
  registerLiveUpdate(() => load(body, { skeleton: false }));
  load(body);
  return panel;
}

/* ============================================================================
 * Editorial Mode: read-only composition + motion-event timeline
 *
 * Built from the same effective, narration-retimed plan that preview,
 * captions, and export render (GET .../editorial/timeline-plan), so the
 * screen can never disagree with the actual render clock. Editing stays in
 * the Editorial workspace; this screen only explains it.
 * ==========================================================================*/

/** Frame-grid tolerance when validating the stored composition geometry. */
const TL_FRAME_TOL = 0.01;

/** Readable names for the renderer-owned composition templates. */
const COMP_TEMPLATE_LABELS = {
  archiveCanvas: "Archive canvas",
  documentReveal: "Document reveal",
  comparisonCanvas: "Comparison canvas",
  illustrationCanvas: "Illustration canvas",
  bigTextReveal: "Big text reveal",
};

/** Template → CSS tint class. Unknown templates get a plain clip. */
const COMP_TEMPLATE_CLASSES = {
  archiveCanvas: "tpl-archiveCanvas",
  documentReveal: "tpl-documentReveal",
  comparisonCanvas: "tpl-comparisonCanvas",
  illustrationCanvas: "tpl-illustrationCanvas",
  bigTextReveal: "tpl-bigTextReveal",
};

/** Readable labels for the motion-primitive event actions. */
const MOTION_ACTION_LABELS = {
  fade: "Fade",
  fadeUp: "Fade up",
  slideInLeft: "Slide in left",
  slideInRight: "Slide in right",
  scaleIn: "Scale in",
  slowPush: "Slow push",
  paperSlide: "Paper slide",
  underline: "Underline",
  highlight: "Highlight",
  drawLine: "Draw line",
  staggerIn: "Stagger in",
  dimOthers: "Dim others",
  focusOne: "Focus one",
  promoteNode: "Promote node",
  collapseToBlack: "Collapse to black",
  hardCut: "Hard cut",
};

/**
 * Readable label for a motion action; unknown actions pass through so a
 * malformed plan is still identifiable (they are rendered as text only and
 * never become class names).
 * @param {unknown} action
 * @returns {string}
 */
function motionActionLabel(action) {
  if (typeof action === "string" && MOTION_ACTION_LABELS[action]) return MOTION_ACTION_LABELS[action];
  return (typeof action === "string" && action) ? action : "unknown";
}

/**
 * The mounted project's timeline-plan URL, or null. Only the exact
 * project-local path `/api/projects/{id}/editorial/timeline-plan` is
 * trusted: whitespace, queries, fragments, backslashes, absolute URLs, and
 * cross-project paths all degrade to an honest unavailable state instead of
 * a request to an unknown target.
 * @param {unknown} value — the snapshot's timeline_plan_url
 * @param {string | null} projectId — the mounted project's id
 * @returns {string | null}
 */
export function safeEditorialTimelinePlanUrl(value, projectId) {
  if (typeof value !== "string" || value !== value.trim() || /\s/.test(value)
    || value.includes("\\") || value.includes("?") || value.includes("#")) return null;
  if (typeof projectId !== "string" || !projectId || /\s/.test(projectId)
    || projectId.includes("\\") || projectId.includes("/")
    || projectId.includes("?") || projectId.includes("#")) return null;
  const expected = `/api/projects/${encodeURIComponent(projectId)}/editorial/timeline-plan`;
  return value === expected ? value : null;
}

/** Timing bases the backend may report, in order of authority. */
const TIMING_BASIS_LABELS = {
  word_timings: "Narration aligned",
  recorded_scene_clock: "Recorded narration clock",
  authored_plan: "Planned timing",
};

/**
 * The badge that tells whether the effective plan is actually aligned to
 * the active narration. This is the authoritative statement: an active
 * take's badge on the Voice screen only describes what that take contains.
 * @param {"word_timings"|"recorded_scene_clock"|"authored_plan"} basis
 * @returns {HTMLElement}
 */
function timingBasisBadge(basis) {
  if (basis === "word_timings") {
    const b = badge("good", "Narration aligned");
    b.title = "Composition cuts follow the active narration's word timings - the same clock preview and export render.";
    return b;
  }
  if (basis === "recorded_scene_clock") {
    const b = badge("neutral", "Recorded narration clock");
    b.title = "Compositions follow the recorded scene boundaries of the active narration - real audio, coarser than word-level alignment.";
    return b;
  }
  const b = badge("warning", "Planned timing");
  b.title = "Narration alignment is not current: compositions sit on the authored plan's clock. Align the narration (or record a take) and the timeline follows the real narration clock.";
  return b;
}

/**
 * Strictly validate the timeline-plan envelope and reduce it to safe rows.
 * A composition row keeps only finite, non-negative start and positive,
 * finite duration; the stored order is preserved and the geometry must stay
 * contiguous (the domain requires contiguous compositions, so a break is an
 * invalid plan, not an expected gap). Events with malformed numeric fields
 * are ignored rather than emitted as invalid CSS; a valid event whose
 * duration runs past the composition end is clamped at the end for display
 * only - the source values are never mutated and no start is invented.
 * Unknown templates/actions degrade to escaped text labels.
 * @param {unknown} envelope — `{plan, timing_basis, narration_synced}`
 * @returns {{ok: true, compositions: any[], total: number, timingBasis: string, narrationSynced: boolean} | {ok: false, error: string}}
 */
export function summarizeEditorialTimeline(envelope) {
  if (!envelope || typeof envelope !== "object") {
    return { ok: false, error: "The timeline-plan response is not an object." };
  }
  const plan = envelope.plan;
  if (!plan || typeof plan !== "object" || !Array.isArray(plan.compositions)) {
    return { ok: false, error: "The timeline-plan response has no composition list." };
  }
  const basis = envelope.timing_basis;
  if (typeof basis !== "string" || !(basis in TIMING_BASIS_LABELS)) {
    return { ok: false, error: `Unknown timing basis ${JSON.stringify(basis)}.` };
  }
  if (typeof envelope.narration_synced !== "boolean") {
    return { ok: false, error: "The timeline-plan response is missing a strict narration_synced flag." };
  }
  const raw = /** @type {any[]} */ (plan.compositions);
  const compositions = [];
  let expectedStart = 0; // the first composition starts at zero on the frame grid
  for (let i = 0; i < raw.length; i += 1) {
    const c = raw[i];
    if (!c || typeof c !== "object") {
      return { ok: false, error: `Composition ${i + 1} is not an object.` };
    }
    const start = c.start;
    const duration = c.duration;
    if (typeof start !== "number" || !Number.isFinite(start) || start < 0) {
      return { ok: false, error: `Composition ${i + 1} has an invalid start time.` };
    }
    if (typeof duration !== "number" || !Number.isFinite(duration) || duration <= 0) {
      return { ok: false, error: `Composition ${i + 1} has an invalid duration.` };
    }
    if (i > 0 && Math.abs(start - expectedStart) > TL_FRAME_TOL) {
      return { ok: false, error: `Composition ${i + 1} breaks the contiguous timeline geometry.` };
    }
    expectedStart = start + duration;
    const events = [];
    for (const e of (Array.isArray(c.events) ? c.events : [])) {
      if (!e || typeof e !== "object") continue; // ignore malformed events
      const time = e.time;
      const dur = e.duration;
      if (typeof time !== "number" || !Number.isFinite(time) || time < 0) continue;
      if (typeof dur !== "number" || !Number.isFinite(dur) || dur < 0) continue;
      if (time > duration + TL_FRAME_TOL) continue; // past the composition end
      events.push({
        time,
        duration: Math.min(dur, Math.max(0, duration - time)), // display clamp only
        action: (typeof e.action === "string" && e.action) || "unknown",
        target: (typeof e.target === "string" && e.target) || "",
      });
    }
    const template = (typeof c.template === "string" && c.template) || "unknown";
    compositions.push({
      index: i,
      id: (typeof c.id === "string" && c.id) || `composition ${i + 1}`,
      start,
      duration,
      template,
      templateLabel: (typeof template === "string" && COMP_TEMPLATE_LABELS[template]) || template,
      templateClass: COMP_TEMPLATE_CLASSES[template] || "",
      assetCount: Array.isArray(c.assets) ? c.assets.length : 0,
      elementCount: Array.isArray(c.elements) ? c.elements.length : 0,
      eventCount: events.length,
      events,
    });
  }
  const total = compositions.reduce(
    (max, c) => Math.max(max, c.start + c.duration), 0,
  );
  return {
    ok: true,
    compositions,
    total,
    timingBasis: /** @type {keyof typeof TIMING_BASIS_LABELS} */ (basis),
    narrationSynced: /** @type {boolean} */ (envelope.narration_synced),
  };
}

/**
 * Load (or re-load) the Editorial timeline for one already-validated
 * snapshot: empty state without a plan, honest unavailable state for an
 * untrusted URL, then one strictly validated timeline-plan read. Stale
 * loads are dropped via the `isStale` guard, so a slow first response can
 * never paint over a later project or mode selection.
 * @param {HTMLElement} region
 * @param {import("../api.js").ProjectSnapshot} snap
 * @param {{scale: number | null, userSet: boolean}} zoom — mutated in place
 * @param {{isStale: () => boolean, reload: () => void}} hooks — the caller's
 *   liveness check and the same load function (so Retry re-runs the panel
 *   load from a fresh snapshot)
 */
async function loadEditorialTimeline(region, snap, zoom, hooks) {
  const editorial = (snap.editorial && typeof snap.editorial === "object")
    ? snap.editorial : {};
  if (editorial.has_edit_plan !== true) {
    region.replaceChildren(emptyState(
      "No Edit Plan yet",
      "The Editorial Edit Plan will define this timeline. Generate it in the Editorial workspace first - until then there are no compositions to lay out.",
      [el("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("#/editorial") },
        "Open Editorial workspace")],
    ));
    return;
  }
  const url = safeEditorialTimelinePlanUrl(editorial.timeline_plan_url, state.currentProjectId);
  if (!url) {
    region.replaceChildren(errorPanel(new Error(
      "The snapshot reports an Edit Plan, but its timeline-plan URL is not a usable project-local path, so this timeline cannot be read safely.",
    )));
    return;
  }
  let envelope;
  try {
    envelope = await getEditorialTimelinePlan(state.config, url);
  } catch (err) {
    if (hooks.isStale()) return;
    region.replaceChildren(errorPanel(err, el("div", { class: "row" },
      el("button", { class: "btn", type: "button", onclick: hooks.reload }, "Retry"),
      el("button", { class: "btn btn-ghost", type: "button", onclick: () => navigate("#/editorial") },
        "Open Editorial workspace"),
    )));
    return;
  }
  if (hooks.isStale()) return;
  const summary = summarizeEditorialTimeline(envelope);
  if (!summary.ok) {
    region.replaceChildren(errorPanel(new Error(summary.error),
      el("button", { class: "btn", type: "button", onclick: hooks.reload }, "Retry"),
    ));
    return;
  }
  if (!summary.compositions.length) {
    region.replaceChildren(emptyState(
      "No compositions",
      "The Edit Plan has no readable compositions yet. Open the Editorial workspace to review it.",
      [el("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("#/editorial") },
        "Open Editorial workspace")],
    ));
    return;
  }
  region.replaceChildren(buildEditorialTimeline(summary, snap, zoom));
}

/**
 * The read-only Editorial timeline: a timing-basis badge, a compositions
 * lane tinted by renderer-owned template, a motion-events lane, and the
 * same stage lanes as the Classic view (Narration / Music / Captions), all
 * on the effective plan's clock. Composition clips are clickable
 * read-only affordances pointing at the Editorial workspace - nothing
 * here mutates the plan.
 * @param {{ok: true, compositions: any[], total: number, timingBasis: string, narrationSynced: boolean}} summary
 * @param {import("../api.js").ProjectSnapshot} snap
 * @param {{scale: number | null, userSet: boolean}} zoom — mutated in place
 * @returns {HTMLElement}
 */
function buildEditorialTimeline(summary, snap, zoom) {
  const total = summary.total;
  const project = snap.project;
  const stages = (snap.stage_state && /** @type {any} */ (snap.stage_state).stages) || {};
  const target = (typeof project?.target_duration === "number" && project.target_duration > 0)
    ? project.target_duration : 0;

  const ruler = el("div", { class: "tl-ruler" });
  const compLane = el("div", { class: "tl-lane tl-lane-scenes" });
  const eventsLane = el("div", { class: "tl-lane tl-lane-overlays" });
  const narrationLane = el("div", { class: "tl-lane tl-lane-audio" });
  const musicLane = el("div", { class: "tl-lane tl-lane-audio" });
  const captionsLane = el("div", { class: "tl-lane tl-lane-audio" });
  const sizedLanes = [ruler, compLane, eventsLane, narrationLane, musicLane, captionsLane];

  let rendered = false;

  /** Apply one zoom level to every lane. @param {number} scale px per second */
  function render(scale) {
    zoom.scale = scale;
    const width = Math.max(1, Math.round(total * scale));
    for (const lane of sizedLanes) lane.style.width = `${width}px`;
    renderRuler(ruler, total, scale);
    renderCompositionLane(compLane, summary, scale);
    renderEventsLane(eventsLane, summary, scale);
    renderStageLane(narrationLane, stages, "narration", total, scale);
    renderStageLane(musicLane, stages, "music", total, scale,
      hasRecordedInput(snap.assets || [], "music"));
    renderStageLane(captionsLane, stages, "subtitles", total, scale);
    rendered = true;
  }

  /** Scale that fills the viewport but keeps composition clips readable. */
  function fitScale() {
    const viewportEl = /** @type {HTMLElement} */ (grid.parentElement);
    const available = Math.max(240, viewportEl.clientWidth - LABEL_COL_PX - 2);
    const fit = total > 0 ? available / total : 8;
    const durations = summary.compositions.map((c) => c.duration).filter((d) => d > 0.25);
    const minDur = durations.length ? Math.min(...durations) : 1;
    return Math.max(fit, MIN_CLIP_PX / minDur, 0.5);
  }

  function applyFit() {
    render(fitScale());
    syncZoomControls();
  }

  function syncZoomControls() {
    if (zoom.scale != null) {
      zoomSlider.value = String(Math.round(zoom.scale * 2) / 2);
      zoomValue.textContent = `${zoom.scale.toFixed(1)} px/s`;
    }
  }

  const zoomSlider = el("input", {
    type: "range", min: "0.5", max: "200", step: "0.5",
    "aria-label": "Timeline zoom (pixels per second)",
  });
  zoomSlider.addEventListener("input", () => {
    zoom.userSet = true;
    render(parseFloat(zoomSlider.value));
    syncZoomControls();
  });
  const zoomValue = el("span", { class: "tl-zoom-value" }, "");
  const fitBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button",
    title: "Reset the zoom so the whole timeline fits while composition clips stay readable.",
  }, "Fit");
  fitBtn.addEventListener("click", () => {
    zoom.userSet = false;
    applyFit();
  });

  const grid = el("div", { class: "tl-grid" },
    el("span", { class: "tl-corner", "aria-hidden": "true" }),
    ruler,
    el("span", { class: "tl-label" }, "Compositions"),
    compLane,
    el("span", { class: "tl-label" }, "Motion events"),
    eventsLane,
    el("span", { class: "tl-label" }, "Narration"),
    narrationLane,
    el("span", { class: "tl-label" }, "Music"),
    musicLane,
    el("span", { class: "tl-label" }, "Captions"),
    captionsLane,
  );
  const viewport = el("div", { class: "tl-viewport" }, grid);

  // The viewport only has a real width once mounted; lay out on first measure
  // and refit on container resizes until the user picks a zoom manually.
  const ro = new ResizeObserver(() => {
    if (zoom.userSet && rendered) {
      syncZoomControls();
      return;
    }
    if (zoom.userSet && zoom.scale) render(zoom.scale);
    else applyFit();
    syncZoomControls();
  });
  ro.observe(viewport);

  /* --- totals + timing-basis QC ------------------------------------------ */
  const gap = total - target;
  const gapBadge = target <= 0
    ? badge("neutral", "no target set")
    : Math.abs(gap) < 0.5
      ? badge("good", "matches target duration")
      : gap < 0
        ? badge("warning", `gap of ${fmtDuration(Math.abs(gap))} vs target`)
        : badge("warning", `over target by ${fmtDuration(gap)}`);

  return el("div", { class: "stack" },
    el("div", { class: "row", style: { alignItems: "center" } },
      el("span", { class: "small" },
        `${summary.compositions.length} compositions · ${fmtDuration(total)} total`),
      timingBasisBadge(summary.timingBasis),
      gapBadge,
      stageChip(stages.timeline, "Timeline"),
      stageChip(stages.quality_control, "QC"),
      el("span", { class: "spacer" }),
      el("span", { class: "tl-zoom" },
        el("span", { class: "muted small" }, "Zoom"),
        zoomSlider,
        zoomValue,
        fitBtn,
      ),
    ),
    viewport,
    el("p", { class: "muted small" },
      "Scroll horizontally to move through time. "
      + "Compositions and motion events come from the same effective plan that preview and export render; open the Editorial workspace to edit them."),
  );
}

/**
 * Composition lane: one clip per validated composition, tinted by the
 * renderer-owned template, clickable to the Editorial workspace (read-only
 * here). Clips keep at least MIN_CLIP_PX of width where the layout allows.
 * @param {HTMLElement} lane
 * @param {{compositions: any[]}} summary
 * @param {number} scale px per second
 */
function renderCompositionLane(lane, summary, scale) {
  const parts = [];
  for (const comp of summary.compositions) {
    const left = comp.start * scale + 1;
    const width = Math.max(2, comp.duration * scale - 2);
    const tips = [
      `C${comp.index + 1} · ${comp.templateLabel}`,
      `${fmtDuration(comp.duration)} · ${comp.assetCount} assets · ${comp.elementCount} elements · ${comp.eventCount} motion events`,
      "Opens the composition in the Editorial workspace.",
    ];
    const clip = el("div", {
      class: `tl-clip tl-comp ${comp.templateClass}`.trim(),
      role: "button",
      tabindex: "0",
      "aria-label": tips.join(" "),
      title: tips.join("\n"),
      style: { left: `${left}px`, width: `${width}px` },
    },
      el("span", { class: "tl-clip-info" },
        el("span", { class: "tl-clip-name" }, `C${comp.index + 1}`),
        el("span", { class: "tl-clip-dur" }, fmtDuration(comp.duration)),
      ),
    );
    const openEditorial = () => navigate("#/editorial");
    clip.addEventListener("click", openEditorial);
    clip.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        openEditorial();
      }
    });
    parts.push(clip);
  }
  // Boundary markers between compositions (not at the left edge).
  for (let i = 1; i < summary.compositions.length; i += 1) {
    const prev = summary.compositions[i - 1];
    parts.push(el("div", {
      class: "tl-clip-boundary",
      style: { left: `${(prev.start + prev.duration) * scale}px` },
      "aria-hidden": "true",
    }));
  }
  // replaceChildren keeps re-renders (zoom / resize) from accumulating clips.
  lane.replaceChildren(...parts);
}

/**
 * Motion-events lane: one marker per validated event (action on target),
 * positioned inside its composition and clamped at the composition end for
 * display only. Events render as escaped text labels - malformed actions
 * degrade to the raw string or "unknown", never to a class name.
 * @param {HTMLElement} lane
 * @param {{compositions: any[]}} summary
 * @param {number} scale px per second
 */
function renderEventsLane(lane, summary, scale) {
  const parts = [];
  for (const comp of summary.compositions) {
    for (const ev of comp.events) {
      const left = (comp.start + ev.time) * scale;
      const width = Math.max(3, ev.duration * scale);
      const label = motionActionLabel(ev.action);
      const caption = `${label} on ${ev.target || "composition"} · inside C${comp.index + 1} at ${fmtDuration(ev.time)}`;
      parts.push(el("div", {
        class: "tl-event",
        title: caption,
        "aria-label": caption,
        style: { left: `${left}px`, width: `${width}px` },
      }, el("span", { class: "tl-event-label" }, label)));
    }
  }
  // replaceChildren keeps re-renders (zoom / resize) from accumulating markers.
  lane.replaceChildren(...parts);
}

/* ============================================================================
 * Timeline build
 * ==========================================================================*/

/**
 * @param {import("../api.js").Scene[]} scenes
 * @param {import("../api.js").Asset[]} assets
 * @param {Record<string, {status?: string, outputs?: string[], completed_at?: string}>} stages
 * @param {import("../api.js").Project} project
 * @param {{scale: number | null, userSet: boolean}} zoom — mutated in place
 * @param {{expandedSceneId: string | null}} view — mutated in place
 * @returns {HTMLElement}
 */
function buildTimeline(scenes, assets, stages, project, zoom, view) {
  const total = scenes.reduce((n, s) => n + (s.duration || 0), 0);
  const target = project.target_duration || 0;

  /** Scenes with their absolute start times and latest visual asset. */
  let cursor = 0;
  const layout = scenes.map((scene) => {
    const start = cursor;
    cursor += scene.duration || 0;
    return {
      scene,
      start,
      duration: scene.duration || 0,
      asset: latestAssetForScene(assets, scene.id, "visual"),
    };
  });

  /**
   * Compiled shot spans for a layout item, from the shared corrected
   * geometry (full durations; later shots overlap the prior tail by their
   * incoming transition; the first shot's incoming transition is ignored).
   * Starts are scene-relative seconds.
   * @param {{scene: Record<string, any>, start: number}} item
   * @returns {Array<{shot: Record<string, any>, start: number, duration: number, overlap: number}>}
   */
  function shotSpans(item) {
    const shots = Array.isArray(item.scene.shots) ? item.scene.shots : [];
    return compiledShotSpans(shots);
  }

  /** Seconds an expanded scene actually occupies on screen. */
  function expandedSpanSeconds(item) {
    const planned = item.duration || 0;
    return Math.max(planned, compiledSpanSeconds(shotSpans(item)));
  }

  const ruler = el("div", { class: "tl-ruler" });
  const sceneLane = el("div", { class: "tl-lane tl-lane-scenes" });
  const overlayLane = el("div", { class: "tl-lane tl-lane-overlays" });
  const narrationLane = el("div", { class: "tl-lane tl-lane-audio" });
  const musicLane = el("div", { class: "tl-lane tl-lane-audio" });
  const captionsLane = el("div", { class: "tl-lane tl-lane-audio" });
  const sizedLanes = [ruler, sceneLane, overlayLane, narrationLane, musicLane, captionsLane];

  let rendered = false;

  /** Re-render at the current zoom after an in-place disclosure toggle. */
  function rerender() {
    if (zoom.scale != null) render(zoom.scale);
    else applyFit();
    syncZoomControls();
  }

  /** Apply one zoom level to every lane. @param {number} scale px per second */
  function render(scale) {
    zoom.scale = scale;
    const width = Math.max(1, Math.round(total * scale));
    for (const lane of sizedLanes) lane.style.width = `${width}px`;
    renderRuler(ruler, total, scale);
    renderSceneLane(sceneLane, layout, scale, view, shotSpans, expandedSpanSeconds, rerender);
    renderOverlayLane(overlayLane, layout, scale, view, shotSpans);
    renderStageLane(narrationLane, stages, "narration", total, scale);
    // A recorded music input is not the generation stage: name it as such so
    // a pending stage doesn't read as "no music" (the Export page says
    // "music recorded" for the same input).
    renderStageLane(musicLane, stages, "music", total, scale, hasRecordedInput(assets, "music"));
    renderStageLane(captionsLane, stages, "subtitles", total, scale);
    rendered = true;
  }

  /** Scale that fills the viewport but keeps thumbnails readable. */
  function fitScale() {
    const viewport = /** @type {HTMLElement} */ (grid.parentElement);
    const available = Math.max(240, viewport.clientWidth - LABEL_COL_PX - 2);
    const fit = total > 0 ? available / total : 8;
    const durations = layout.map((i) => i.duration).filter((d) => d > 0.25);
    // When a scene is expanded, its shots must stay readable too.
    const expandedItem = layout.find((i) => i.scene.id === view.expandedSceneId);
    if (expandedItem) {
      for (const span of shotSpans(expandedItem)) {
        if (span.duration > 0.25) durations.push(span.duration);
      }
    }
    const minDur = durations.length ? Math.min(...durations) : 1;
    return Math.max(fit, MIN_CLIP_PX / minDur, 0.5);
  }

  function applyFit() {
    render(fitScale());
    syncZoomControls();
  }

  function syncZoomControls() {
    if (zoom.scale != null) {
      zoomSlider.value = String(Math.round(zoom.scale * 2) / 2);
      zoomValue.textContent = `${zoom.scale.toFixed(1)} px/s`;
    }
  }

  const zoomSlider = el("input", {
    type: "range", min: "0.5", max: "200", step: "0.5",
    "aria-label": "Timeline zoom (pixels per second)",
  });
  zoomSlider.addEventListener("input", () => {
    zoom.userSet = true;
    render(parseFloat(zoomSlider.value));
    syncZoomControls();
  });
  const zoomValue = el("span", { class: "tl-zoom-value" }, "");
  const fitBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button",
    title: "Reset the zoom so the whole timeline fits while thumbnails stay readable.",
  }, "Fit");
  fitBtn.addEventListener("click", () => {
    zoom.userSet = false;
    applyFit();
  });

  const grid = el("div", { class: "tl-grid" },
    el("span", { class: "tl-corner", "aria-hidden": "true" }),
    ruler,
    el("span", { class: "tl-label" }, "Scenes"),
    sceneLane,
    el("span", { class: "tl-label" }, "Overlays"),
    overlayLane,
    el("span", { class: "tl-label" }, "Narration"),
    narrationLane,
    el("span", { class: "tl-label" }, "Music"),
    musicLane,
    el("span", { class: "tl-label" }, "Captions"),
    captionsLane,
  );
  const viewport = el("div", { class: "tl-viewport" }, grid);

  // The viewport only has a real width once mounted; lay out on first measure
  // and refit on container resizes until the user picks a zoom manually.
  const ro = new ResizeObserver(() => {
    if (zoom.userSet && rendered) {
      syncZoomControls();
      return;
    }
    if (zoom.userSet && zoom.scale) render(zoom.scale);
    else applyFit();
    syncZoomControls();
  });
  ro.observe(viewport);

  /* --- totals + gap QC --------------------------------------------------- */
  const gap = total - target;
  const gapBadge = target <= 0
    ? badge("neutral", "no target set")
    : Math.abs(gap) < 0.5
      ? badge("good", "matches target duration")
      : gap < 0
        ? badge("warning", `gap of ${fmtDuration(Math.abs(gap))} vs target`)
        : badge("warning", `over target by ${fmtDuration(gap)}`);

  return el("div", { class: "stack" },
    el("div", { class: "row", style: { alignItems: "center" } },
      el("span", { class: "small" }, `${scenes.length} scenes · ${fmtDuration(total)} total · target ${fmtDuration(target)}`),
      el("span", { class: "spacer" }),
      el("span", { class: "tl-zoom" },
        el("span", { class: "muted small" }, "Zoom"),
        zoomSlider,
        zoomValue,
        fitBtn,
      ),
      gapBadge,
    ),
    viewport,
    el("p", { class: "muted small" },
      "Scroll horizontally to move through time. Click a scene clip to open it in the Scene Editor; use the arrow control on a clip to expand that one scene into its shots, transitions, and overlay markers."),
    el("div", { class: "row" },
      stageChip("timeline", stages.timeline),
      el("span", { class: "spacer" }),
      stageChip("quality_control", stages.quality_control),
    ),
  );
}

/**
 * Time ruler with an adaptive tick step.
 * @param {HTMLElement} ruler
 * @param {number} total
 * @param {number} scale
 */
function renderRuler(ruler, total, scale) {
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 3600];
  const step = steps.find((s) => s * scale >= MIN_TICK_PX) ?? 7200;
  const ticks = [];
  const lastMultiple = Math.floor(total / step + 1e-6) * step;
  for (let t = 0; t <= lastMultiple; t += step) {
    ticks.push(el("span", { class: "tl-tick", style: { left: `${t * scale}px` } }, fmtDuration(t)));
  }
  if (total > 0 && total - lastMultiple > 1e-6) {
    ticks.push(el("span", { class: "tl-tick", style: { left: `${total * scale}px` } }, fmtDuration(total)));
  }
  ruler.replaceChildren(...ticks);
}

/**
 * The scene filmstrip: thumbnails sized by duration, transition markers on
 * boundaries, honest placeholders for scenes without visuals — plus the
 * Phase 3 disclosure: one selected scene expands into its ordered shots
 * using the shared corrected compiled geometry (see js/shots.js), with
 * intra-scene transition labels and overlay-marker data feeding the
 * overlays lane.
 * @param {HTMLElement} lane
 * @param {Array<{scene: Record<string, any>, start: number, duration: number, asset: import("../api.js").Asset | null}>} layout
 * @param {number} scale
 * @param {{expandedSceneId: string | null}} view
 * @param {Function} shotSpans — closure helper from buildTimeline
 * @param {Function} expandedSpanSeconds — closure helper from buildTimeline
 * @param {() => void} rerender — re-render at the current zoom
 */
function renderSceneLane(lane, layout, scale, view, shotSpans, expandedSpanSeconds, rerender) {
  const parts = [];
  layout.forEach((item, i) => {
    const { scene, start, duration, asset } = item;
    const num = (scene.index ?? 0) + 1;
    const expanded = scene.id === view.expandedSceneId;

    if (i > 0) {
      const prevSpan = layout[i - 1].duration * scale;
      if (prevSpan >= 8) {
        parts.push(el("span", { class: "tl-boundary", style: { left: `${start * scale}px` } }));
      }
      if (prevSpan >= NARROW_CLIP_PX) {
        parts.push(el("span", {
          class: "tl-transition",
          style: { left: `${start * scale}px` },
          title: `Transition into S${num}: ${scene.transition || "cut"}`,
        }, scene.transition || "cut"));
      }
    }

    const seconds = expanded ? expandedSpanSeconds(item) : duration;
    const width = Math.max(2, seconds * scale - 2);
    const sizeClass = width < TINY_CLIP_PX ? " tiny" : width < NARROW_CLIP_PX ? " narrow" : "";
    const staleCount = shotSummary(scene).stale;
    const narrationExcerpt = scene.narration
      ? ` · ${(scene.narration.length > 140 ? `${scene.narration.slice(0, 140)}…` : scene.narration)}`
      : "";
    const tooltip = `S${num} · ${scene.title} · ${fmtDuration(expanded ? seconds : duration)} · ${scene.transition || "cut"}`
      + (staleCount ? ` · ${staleCount} stale shot(s) — upstream regeneration` : "")
      + (asset ? "" : " · no visual generated yet") + narrationExcerpt;

    const clip = el("div", {
      class: `tl-clip tl-scene${expanded ? " expanded" : ""}${!asset && !expanded ? " missing" : ""}${sizeClass}`,
      style: { left: `${start * scale + 1}px`, width: `${width}px` },
      title: tooltip,
      role: "button",
      tabindex: 0,
      "aria-label": `Scene ${num}: ${scene.title}, ${fmtDuration(duration)}. Opens in the Scene Editor.`,
    });
    const open = () => navigate(`#/scene/${scene.id}`);
    clip.addEventListener("click", open);
    clip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });

    // Disclosure control: expand exactly one selected scene into its shots.
    if (width >= MIN_EXPAND_PX) {
      const isExpanded = expanded;
      const expandBtn = el("button", {
        class: "tl-expand",
        type: "button",
        title: isExpanded
          ? "Collapse back to the scene-level view"
          : "Expand this scene into its shots",
        "aria-expanded": String(isExpanded),
        "aria-label": `${isExpanded ? "Collapse" : "Expand"} scene S${num} shots`,
      });
      expandBtn.append(icon("chevron", 12));
      expandBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        view.expandedSceneId = isExpanded ? null : scene.id;
        rerender();
      });
      clip.append(expandBtn);
    }

    if (expanded) {
      appendExpandedShots(clip, lane, item, scale, shotSpans);
    } else if (asset) {
      const url = mediaUrl(asset);
      if (url) {
        const thumb = asset.type === "video"
          ? el("video", {
              class: "tl-thumb", src: `${url}#t=0.5`, muted: true,
              preload: "metadata", tabindex: -1,
            })
          : el("img", {
              class: "tl-thumb", src: url, alt: "", loading: "lazy",
              decoding: "async", tabindex: -1,
            });
        thumb.addEventListener("error", () => {
          thumb.remove();
          clip.classList.add("thumb-failed");
        });
        clip.append(thumb, el("span", { class: "tl-shade" }));
      }
    }

    clip.append(el("span", { class: "tl-scene-info" },
      el("span", { class: "tl-scene-name" }, `S${num}`),
      el("span", { class: "tl-scene-title" }, scene.title),
      el("span", { class: "tl-scene-dur" }, fmtDuration(expanded ? seconds : duration)),
      staleCount ? el("span", {
        class: "tl-scene-stale",
        title: `${staleCount} shot(s) marked stale: an upstream shot regenerated after their media was produced. Regenerate before final export (re-import for reused-media shots).`,
      }, "stale") : null,
    ));
    parts.push(clip);
  });
  lane.replaceChildren(...parts);
}

/**
 * Fill an expanded scene clip with its shot segments and intra-scene
 * transition labels, positioned from the corrected compiled starts (full
 * shot durations; a later segment's left edge sits one incoming-overlap
 * earlier than the previous segment's right edge). Segment clicks deep-link
 * into the Scene Editor with that exact shot preselected.
 * @param {HTMLElement} clip — the expanded scene clip
 * @param {HTMLElement} _lane — the scenes lane (labels are appended here)
 * @param {{scene: Record<string, any>, start: number}} item
 * @param {number} scale
 * @param {Function} shotSpans
 */
function appendExpandedShots(clip, _lane, item, scale, shotSpans) {
  const spans = shotSpans(item);
  if (!spans.length) {
    clip.append(el("span", {
      class: "tl-shot-info-empty",
      title: "This scene has no readable shots yet",
    }, "no shots"));
    return;
  }
  spans.forEach((span, j) => {
    const { shot } = span;
    const segWidth = Math.max(3, span.duration * scale - 2);
    const overlap = span.overlap;
    const kind = (shot.transition_in && shot.transition_in.kind) || "cut";

    // Intra-scene transition label at the boundary where this shot begins
    // overlapping the previous one (= its compiled start).
    if (j > 0 && kind !== "cut" && overlap > 0) {
      const boundaryPx = span.start * scale;
      if (segWidth >= 8) {
        clip.append(el("span", {
          class: "tl-transition tl-transition-inner",
          style: { left: `${boundaryPx}px` },
          title: `Shot #${shot.index + 1} enters via ${kind}, overlapping the previous shot by ${fmtSecs(overlap)}`,
        }, kind === "crossfade" || kind === "dissolve" ? `× ${fmtSecs(overlap)}` : kind));
      }
    }

    const overlayCount = Array.isArray(shot.overlays) ? shot.overlays.length : 0;
    const staleTag = shot.stale
      ? ` · STALE (${staleReason(shot, item.scene.shots || []) || "upstream regeneration"})`
      : "";
    const segTooltip = `Shot #${shot.index + 1} · ${shot.title || "(untitled)"}`
      + ` · ${fmtSecs(shot.duration_seconds)}`
      + (j === 0 ? "" : ` · in: ${kind}${overlap > 0 ? ` ${fmtSecs(overlap)}` : ""}`)
      + ` · starts ${fmtSecs(span.start)} into the scene`
      + ` · ${shot.lane} lane · status ${shot.status}`
      + staleTag
      + (overlayCount ? ` · ${overlayCount} overlay(s)` : "")
      + ". Click to edit this shot in the Scene Editor.";

    const seg = el("div", {
      class: `tl-shot lane-${shot.lane || "image"}${shot.stale ? " stale" : ""}`,
      style: { left: `${span.start * scale + 1}px`, width: `${segWidth}px` },
      title: segTooltip,
      role: "button",
      tabindex: 0,
      "aria-label": `Shot ${shot.index + 1}: ${shot.title || "untitled"}, ${fmtSecs(shot.duration_seconds)}, ${shot.lane} lane. Opens in the Scene Editor with this shot selected.`,
    });
    const openShotEditor = () => navigate(sceneEditorHash(item.scene.id, shot.id));
    seg.addEventListener("click", openShotEditor);
    seg.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openShotEditor();
      }
    });
    if (segWidth >= TINY_CLIP_PX) {
      seg.append(el("span", { class: "tl-shot-info" },
        el("span", {}, `#${shot.index + 1}`),
        el("span", {}, shot.title || "(untitled)"),
        shot.stale ? el("span", { class: "tl-shot-stale" }, "stale") : null,
        el("span", { class: "tl-shot-dur" }, fmtSecs(shot.duration_seconds)),
      ));
    }
    clip.append(seg);
  });
}

/**
 * Overlay-markers lane: one marker per overlay cue of the expanded scene,
 * positioned at absolute project time from the corrected compiled starts
 * (scene start + compiled shot start + cue start). Every other scene leaves
 * this lane honestly empty.
 * @param {HTMLElement} lane
 * @param {Array<{scene: Record<string, any>, start: number}>} layout
 * @param {number} scale
 * @param {{expandedSceneId: string | null}} view
 * @param {Function} shotSpans
 */
function renderOverlayLane(lane, layout, scale, view, shotSpans) {
  const parts = [];
  const item = layout.find((it) => it.scene.id === view.expandedSceneId);
  if (item && Array.isArray(item.scene.shots)) {
    for (const span of shotSpans(item)) {
      const cues = Array.isArray(span.shot.overlays) ? span.shot.overlays : [];
      for (const cue of cues) {
        const cueStart = Number(cue.start_seconds);
        const cueDur = Number(cue.duration_seconds);
        if (!Number.isFinite(cueStart) || !Number.isFinite(cueDur)) continue;
        const left = (item.start + span.start + Math.max(0, cueStart)) * scale;
        const width = Math.max(3, cueDur * scale);
        const label = cue.kind === "exact_text"
          ? String(cue.exact_text || "")
          : `asset ${shortId(cue.asset_id)}`;
        const marker = el("span", {
          class: `tl-overlay-marker k-${cue.kind || "exact_text"}`,
          style: { left: `${left}px`, width: `${width}px` },
          title: `Overlay ${cue.kind}`
            + (label ? `: "${label.slice(0, 80)}"` : "")
            + ` · ${fmtSecs(cueStart)}–${fmtSecs(cueStart + cueDur)} in shot #${span.shot.index + 1}`
            + (cue.anchor ? ` · anchored ${cue.anchor.replace(/_/g, " ")}` : ""),
        });
        parts.push(marker);
      }
    }
  }
  lane.replaceChildren(...parts);
}

/**
 * Does the snapshot carry a recorded input asset for a role (e.g. a music
 * track the user uploaded instead of generating)? Same check the Export
 * page's readiness summary uses.
 * @param {import("../api.js").Asset[]} assets
 * @param {string} role
 * @returns {boolean}
 */
function hasRecordedInput(assets, role) {
  return assets.some((a) => a.settings && a.settings.role === role);
}

/**
 * A single full-length clip for a stage: solid when the stage has completed,
 * a dashed placeholder (honest "not yet") when it has not.
 * @param {Record<string, {status?: string}>} stages
 * @param {string} stage
 * @param {number} total
 * @param {number} scale
 * @param {boolean} [recordedInput] - a recorded input for this stage's role
 *   exists; a pending stage then says so instead of "not generated yet"
 */
function renderStageLane(lane, stages, stage, total, scale, recordedInput = false) {
  const record = stages[stage];
  const status = (record && record.status) || "pending";
  const done = status === "completed";
  const label = done ? "complete"
    : recordedInput ? "recorded input in use; generation stage pending"
    : `not generated yet (${status})`;
  lane.replaceChildren(el("div", {
    class: `tl-clip tl-stage ${done ? "done" : "pending"}`,
    style: { left: "1px", width: `${Math.max(2, total * scale - 2)}px` },
    title: `${stage}: ${label}`,
  }, el("span", { class: "tl-stage-label" }, `${stage} · ${label}`)));
}

/**
 * Build a media URL from a stored project-relative path. Only a localhost
 * media base (config.json `media_base`) is trusted per the security rules;
 * anything else yields null so the clip falls back to the placeholder.
 * Mirrors storyboard.js.
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
