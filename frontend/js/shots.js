/**
 * Shared multi-shot domain helpers (Phase 3 frontend groundwork).
 *
 * Mirrors backend/schemas/shots.py client-side so every screen reasons
 * identically about shots:
 *  - lane vocabulary and display chips;
 *  - transition / start-mode / overlay vocabularies;
 *  - the implicit legacy-shot projection (`effectiveShots`) matching
 *    `implicit_shot_from_scene()` for scenes that predate stored shots;
 *  - rendered-duration math: sum(durations) - sum(incoming overlaps).
 *
 * Nothing here fetches or mutates: API calls stay in api.js and the pages.
 */

import { el, fmtDate, shortId } from "./dom.js";
import { badge } from "./ui.js";

/** Editorial lanes (backend ShotLane): source policy, not implementation. */
export const SHOT_LANES = [
  { value: "real", label: "Real" },
  { value: "image", label: "Image" },
  { value: "h3", label: "H3" },
  { value: "html", label: "HTML" },
];

/** V1 shot transition kinds; `cut` always forces duration_seconds to 0. */
export const TRANSITION_KINDS = [
  { value: "cut", label: "Cut (no overlap)" },
  { value: "crossfade", label: "Crossfade" },
  { value: "dissolve", label: "Dissolve (alias of crossfade)" },
  { value: "fade_through_black", label: "Fade through black" },
  { value: "dip_to_white", label: "Dip to white" },
];

/** Start modes: only weighted shots may be retimed by the timing compiler. */
export const START_MODES = [
  { value: "fixed", label: "Fixed (never retimed)" },
  { value: "weighted", label: "Weighted (compiler may retime)" },
];

/** V1 overlay cue kinds. */
export const OVERLAY_KINDS = [
  { value: "exact_text", label: "Exact text" },
  { value: "graphic", label: "Graphic asset" },
  { value: "image", label: "Image asset" },
];

/** Nine-point anchor names in the canvas-pixel coordinate system. */
export const OVERLAY_ANCHORS = [
  ["top_left", "Top left"],
  ["top_center", "Top center"],
  ["top_right", "Top right"],
  ["center_left", "Center left"],
  ["center", "Center"],
  ["center_right", "Center right"],
  ["bottom_left", "Bottom left"],
  ["bottom_center", "Bottom center"],
  ["bottom_right", "Bottom right"],
];

/** @param {string} value @returns {string} */
export function laneLabel(value) {
  const found = SHOT_LANES.find((lane) => lane.value === value);
  return found ? found.label : String(value || "?");
}

/**
 * Incoming-overlap seconds a shot contributes to the scene (cut means 0).
 * @param {{transition_in?: {kind?: string, duration_seconds?: number}|null}} shot
 * @returns {number}
 */
export function transitionOverlap(shot) {
  const t = shot && shot.transition_in;
  if (!t || t.kind === "cut") return 0;
  const d = Number(t.duration_seconds);
  return Number.isFinite(d) && d > 0 ? d : 0;
}

/**
 * Scene rendered duration from ordered shots: Σduration − Σincoming overlap.
 * Matches backend `scene_rendered_duration`.
 * @param {Array<Record<string, any>>} shots
 * @returns {number}
 */
export function renderedDuration(shots) {
  let total = 0;
  for (const shot of shots || []) {
    const d = Number(shot && shot.duration_seconds);
    if (Number.isFinite(d)) total += d - transitionOverlap(shot);
  }
  return Math.max(0, total);
}

const LANE_BY_VISUAL_TYPE = {
  graphic_screen: "html",
  title_card: "html",
  diagram: "html",
  h3_audiovisual: "h3",
  h3_reference: "h3",
  wan_video: "h3",
  reused_media: "real",
};

/** Default lane for a legacy visual type (mirrors the backend lane resolver). */
export function defaultLane(visualType) {
  return LANE_BY_VISUAL_TYPE[visualType] || "image";
}

/**
 * Visual types offered for shots (mirrors backend.schemas.VisualType).
 * Shared by the scene form, shot form, and Add Shot chooser.
 * `wired` types have real local implementations; unwired ones are
 * mock-only placeholders and must never be picked silently as a default.
 */
export const VISUAL_TYPES = [
  {
    value: "text_overlay_still",
    wired: true,
    group: "Text and graphics",
    label: "Generated background + exact text",
    description: "Generate a background and overlay your exact wording. Use it for quotes, labels, or calls to action.",
  },
  {
    value: "graphic_screen",
    wired: true,
    group: "Text and graphics",
    label: "Graphic Screen",
    description: "Design a static graphic with exact text and a custom layout. Use it for title slides or explainer graphics.",
  },
  {
    value: "krea2_still",
    wired: true,
    group: "Still images",
    label: "Krea 2 Turbo still image",
    description: "Generate a still from a visual prompt with Krea 2 Turbo. Use it for a photo-style scene or illustration.",
  },
  {
    value: "ideogram4_still",
    wired: true,
    group: "Still images",
    label: "Ideogram 4 still image",
    description: "Generate a still with lettering in the image. Use Quick for a prompt or Precise to place text and objects in specific regions.",
  },
  {
    value: "qwen_image_still",
    wired: true,
    group: "Still images",
    label: "Qwen-Image-2512 text still",
    description: "Generate a still with text built into the scene, such as a storefront sign or a product label. Check the spelling in the result.",
  },
  {
    value: "qwen_image_21_still",
    wired: true,
    group: "Still images",
    label: "Qwen-Image-2.1 still image",
    description: "Generate a still with Qwen-Image-2.1. You can request lettering as part of the image.",
  },
  {
    value: "image_motion",
    wired: true,
    group: "Motion and video",
    label: "Image motion",
    description: "Turn a still into a moving shot with a camera effect such as a slow push-in or a pan.",
  },
  {
    value: "h3_audiovisual",
    wired: true,
    group: "Motion and video",
    label: "MiniMax H3 AV shot (audio + video)",
    description: "Generate a video clip with synchronized sound. Clip audio plays in preview only. Exports use Studio narration and music.",
  },
  {
    value: "reused_media",
    wired: true,
    group: "Existing media",
    label: "Reused media",
    description: "Import an image or video from your computer. Give it a source title so you can trace it later.",
  },
  {
    value: "flux_still",
    wired: false,
    label: "FLUX still image — unwired",
    description: "Placeholder for generating still images with FLUX.",
  },
  {
    value: "wan_video",
    wired: false,
    label: "Wan video — unwired",
    description: "Placeholder for generating video clips with Wan.",
  },
  {
    value: "h3_reference",
    wired: false,
    label: "MiniMax H3 reference video — unwired",
    description: "Placeholder for generating an H3 clip guided by reference media.",
  },
  {
    value: "title_card",
    wired: false,
    label: "Title card — unwired",
    description: "Placeholder for a dedicated title card.",
  },
  {
    value: "diagram",
    wired: false,
    label: "Diagram — unwired",
    description: "Placeholder for a diagram that explains a concept or process.",
  },
  {
    value: "transition_only",
    wired: false,
    label: "Transition only — unwired",
    description: "Placeholder for a transition without its own image or video.",
  },
  {
    value: "custom",
    wired: false,
    label: "Custom — unwired",
    description: "Placeholder for a custom local generation workflow.",
  },
];

/** Fallback visual type for new shots: wired, no special requirements. */
export const DEFAULT_SHOT_VISUAL_TYPE = "krea2_still";

export function isWiredVisualType(value) {
  const found = VISUAL_TYPES.find((mode) => mode.value === value);
  return Boolean(found && found.wired);
}

/**
 * Defaults for a brand-new shot appended to `shots`: inherit the previous
 * shot's recipe when it is usable, otherwise fall back to a wired
 * lane/visual-type combination — never an unwired type by accident.
 * @param {Array<Record<string, any>>} shots — current ordered shots
 * @returns {{title: string, duration_seconds: number, lane: string, visual_type: string}}
 */
export function defaultNewShot(shots) {
  const last = shots && shots.length ? shots[shots.length - 1] : null;
  const lastType = last ? String(last.visual_type || "") : "";
  const visualType = lastType && isWiredVisualType(lastType)
    ? lastType
    : DEFAULT_SHOT_VISUAL_TYPE;
  const lastDur = Number(last && last.duration_seconds);
  return {
    title: "",
    duration_seconds: Number.isFinite(lastDur) && lastDur > 0 ? lastDur : 5,
    lane: defaultLane(visualType),
    visual_type: visualType,
  };
}

/**
 * True when a scene has stored (materialized) shots, making the legacy
 * scene-level visual recipe superseded: screens must then offer exactly one
 * editable source of truth (the shots), disabling scene-level visual and
 * generation controls.
 * @param {Record<string, any>|null|undefined} scene — snapshot Scene payload
 * @returns {boolean}
 */
export function sceneHasExplicitShots(scene) {
  if (!scene) return false;
  const s = scene.shot_summary;
  if (s && typeof s.materialized === "boolean") return s.materialized;
  const shots = Array.isArray(scene.shots) ? scene.shots : null;
  if (!shots) return false;
  return shots.some((shot) => shot && !shot.implicit);
}

/**
 * Corrected compiled layout for ordered shots, matching backend
 * `scene_rendered_duration`: every shot keeps its FULL duration and a later
 * shot starts early enough to overlap the previous shot's tail by its own
 * incoming transition duration. The first shot never consumes its incoming
 * overlap (there is no previous shot inside the scene), so its
 * `transition_in` is layout-neutral here.
 *
 * Starts are scene-relative seconds.
 * @param {Array<Record<string, any>>} shots
 * @returns {Array<{shot: Record<string, any>, start: number, duration: number, overlap: number}>}
 */
export function compiledShotSpans(shots) {
  const spans = [];
  for (const shot of shots || []) {
    const dur = Number(shot && shot.duration_seconds);
    if (!Number.isFinite(dur) || dur <= 0) continue;
    let overlap = 0;
    const prev = spans[spans.length - 1];
    if (prev) {
      const raw = Math.max(0, transitionOverlap(shot));
      // Defensive clamp mirroring the structural rule: strictly shorter than
      // both adjacent shots.
      overlap = Math.min(raw, prev.duration, dur);
    }
    const start = prev ? prev.start + prev.duration - overlap : 0;
    spans.push({ shot, start, duration: dur, overlap });
  }
  return spans;
}

/**
 * Total seconds an ordered shot list occupies once compiled (Σduration − Σ
 * non-first incoming overlaps). @param {Array} spans — from compiledShotSpans
 */
export function compiledSpanSeconds(spans) {
  const last = spans && spans.length ? spans[spans.length - 1] : null;
  if (!last) return 0;
  return last.start + last.duration;
}

const IMPLICIT_STATUS = {
  draft: "draft",
  queued: "queued",
  generating: "generating",
  generated: "ready",
  approved: "approved",
  locked: "approved",
  failed: "failed",
};

/**
 * Mirror of backend `implicit_shot_from_scene()`: project a legacy
 * single-visual scene as one deterministic implicit shot with id
 * `<scene-id>-implicit`. The projection never mutates anything.
 * @param {import("./api.js").Scene} scene
 * @returns {Record<string, any>}
 */
export function implicitShotFromScene(scene) {
  const normalized = String(scene.transition || "cut").trim().toLowerCase();
  const aliases = { dissolve: "crossfade", fade: "crossfade" };
  const kind = aliases[normalized]
    || (TRANSITION_KINDS.some((k) => k.value === normalized) ? normalized : "cut");
  const duration = Number(scene.duration) || 0;
  const overlap = kind === "cut" ? 0 : Math.min(0.35, duration / 4);
  let status = IMPLICIT_STATUS[scene.status] || "draft";
  if (scene.locked && status !== "approved") status = "approved";
  return {
    id: `${scene.id}-implicit`,
    project_id: scene.project_id,
    scene_id: scene.id,
    index: 0,
    title: scene.title || "",
    duration_seconds: duration,
    start_mode: "fixed",
    lane: defaultLane(scene.visual_type),
    visual_type: scene.visual_type || "flux_still",
    selected_backend: scene.selected_backend || "automatic",
    visual_prompt: scene.visual_prompt || "",
    negative_prompt: scene.negative_prompt || "",
    camera_instruction: scene.camera_instruction || "",
    source_asset_id: null,
    source_in_seconds: null,
    source_out_seconds: null,
    transition_in: { kind, duration_seconds: overlap },
    references: Array.isArray(scene.references) ? scene.references : [],
    seed: scene.seed != null ? scene.seed : 0,
    status,
    locked: !!scene.locked,
    overlays: [],
    audio_cues: [],
    implicit: true,
    stale: false,
    staleness: null,
  };
}

/**
 * The shots a screen should reason about: the snapshot's stored/projected
 * list when present, otherwise a client-side implicit projection.
 * @param {Record<string, any>} scene — snapshot Scene payload (may carry shots)
 * @returns {Array<Record<string, any>>}
 */
export function effectiveShots(scene) {
  if (Array.isArray(scene && scene.shots) && scene.shots.length) return scene.shots;
  if (!scene) return [];
  return [implicitShotFromScene(/** @type {import("./api.js").Scene} */ (scene))];
}

/**
 * Human-readable explanation for a shot's staleness marker.
 * `staleness` carries provenance from the backend: which upstream shot
 * regenerated, why, and when — surfaced verbatim in the UI ("…tell them why").
 * @param {Record<string, any>} shot
 * @param {Array<Record<string, any>>} [shots] — optional known shots used to
 *   turn the producer's id into a friendly name; falls back to a short id.
 * @returns {string|null}
 */
export function staleReason(shot, shots = []) {
  const marker = shot && shot.staleness;
  if (!marker || typeof marker !== "object") return null;
  let source = "an upstream shot";
  if (marker.source_shot_id) {
    const producer = (Array.isArray(shots) ? shots : [])
      .find((x) => x && x.id === marker.source_shot_id);
    if (producer) {
      source = producer.title ? `shot “${producer.title}”` : `shot #${(Number(producer.index) || 0) + 1}`;
    } else {
      source = shortId(marker.source_shot_id);
    }
  }
  const when = marker.marked_at ? ` on ${fmtDate(marker.marked_at)}` : "";
  return `${source} regenerated after this shot's media was produced${when}`;
}

/**
 * Completion summary preferring the backend's per-scene `shot_summary`
 * block and falling back to local computation from effective shots.
 * `pending` counts shots that are neither ready nor failed; `stale` counts
 * shots carrying a staleness marker (upstream regeneration after their media).
 * @param {Record<string, any>} scene
 * @returns {{count: number, ready: number, approved: number, failed: number, pending: number, stale: number, materialized: boolean, rendered: number}}
 */
export function shotSummary(scene) {
  const s = scene && scene.shot_summary;
  const shots = effectiveShots(scene);
  const count = s && Number.isFinite(s.count) ? s.count : shots.length;
  const readyOf = (list) => list.filter((x) => x.status === "ready" || x.status === "approved").length;
  const approvedOf = (list) => list.filter((x) => x.status === "approved").length;
  const failedOf = (list) => list.filter((x) => x.status === "failed").length;
  const staleOf = (list) => list.filter((x) => x.stale).length;
  const ready = s ? s.ready || 0 : readyOf(shots);
  const approved = s ? s.approved || 0 : approvedOf(shots);
  const failed = s ? s.failed || 0 : failedOf(shots);
  const stale = s ? s.stale || 0 : staleOf(shots);
  const rendered = s && Number.isFinite(s.rendered_duration_seconds)
    ? s.rendered_duration_seconds
    : renderedDuration(shots);
  return {
    count,
    ready,
    approved,
    failed,
    pending: Math.max(0, count - ready - failed),
    stale,
    materialized: Boolean(s && s.materialized),
    rendered,
  };
}

/**
 * Unique lane chips ("Real ×2") for a shot list.
 * @param {Array<Record<string, any>>} shots
 * @returns {HTMLElement[]}
 */
export function laneChips(shots) {
  const counts = new Map();
  for (const shot of shots || []) {
    const key = (shot && shot.lane) || "image";
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()].map(([lane, n]) => el("span", {
    class: `lane-chip lane-${lane}`,
    title: `${laneLabel(lane)} lane`,
  }, n > 1 ? `${laneLabel(lane)} ×${n}` : laneLabel(lane)));
}

/**
 * Badge for a shot status value (locked wins over everything).
 * @param {string} status
 * @param {boolean} [locked]
 * @returns {HTMLElement}
 */
export function shotStatusBadge(status, locked) {
  if (locked) return badge("warning", "Locked");
  const map = {
    draft: ["neutral", "Draft"],
    queued: ["neutral", "Queued"],
    generating: ["accent", "Generating"],
    ready: ["good", "Ready"],
    approved: ["good", "Approved"],
    failed: ["critical", "Failed"],
  };
  const pair = map[status] || ["neutral", status || "Unknown"];
  return badge(pair[0], pair[1]);
}

/**
 * Imperative phrase describing how a shot's stale marker is cleared.
 * Reused-media shots are refreshed by re-importing the local file —
 * regeneration is a no-op for them and never clears the marker — while
 * every other lane clears by regenerating.
 * @param {Record<string, any>} shot
 * @returns {string} e.g. "Regenerate this shot" / "Re-import this shot's media"
 */
export function staleClearAction(shot) {
  return shot && shot.visual_type === "reused_media"
    ? "Re-import this shot's media"
    : "Regenerate this shot";
}

/**
 * Warning badge for a shot carrying a staleness marker. Approving a stale
 * shot does NOT clear the marker (approval = explicit acceptance; preflight
 * still says "regenerate before export"), so approved + stale reads
 * "Approved but stale" — and the tooltip explains why from the marker's
 * provenance (which shot regenerated, when).
 * @param {Record<string, any>} shot
 * @param {Array<Record<string, any>>} [shots] — known shots for a friendly name
 * @returns {HTMLElement}
 */
export function shotStaleBadge(shot, shots = []) {
  const why = staleReason(shot, shots)
    || "an upstream shot regenerated after this shot's media was produced";
  const label = shot && shot.status === "approved" ? "Approved but stale" : "Stale";
  const b = badge("warning", label);
  b.title = `${why}. ${staleClearAction(shot)} to clear the flag; export preflight keeps warning until you do.`;
  return b;
}

/**
 * Seconds formatted compactly and precisely ("4.5 s"); em-dash when not finite.
 * @param {number|null|undefined} value
 * @returns {string}
 */
export function fmtSecs(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${Math.round(n * 100) / 100} s`;
}

/**
 * Parse raw input text into a finite number; null for blank input, NaN when
 * unparseable. Callers decide which cases are errors.
 * @param {string|null|undefined} raw
 * @returns {number|null|NaN}
 */
export function numOrNull(raw) {
  const text = String(raw == null ? "" : raw).trim();
  if (text === "") return null;
  const n = Number(text);
  return Number.isFinite(n) ? n : NaN;
}
