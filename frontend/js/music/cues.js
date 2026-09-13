/**
 * Score Studio cue model and editing helpers.
 *
 * A *cue* is one timed musical move over the generated soundtrack (the
 * portable `score-plan.json` schema, v1). Music-automation cues (build,
 * pull_back, silence, restore) reshape the bed from their time onward;
 * effect cues (impact, riser, end_sting) mix a one-shot project asset in at
 * the exact cue time. The renderer is sample-accurate; these helpers only
 * shape the plan payload — the backend validates every value on save.
 *
 * Guardrail bounds mirror backend/music/score.py (the single source of
 * truth the backend enforces): transition 0–5 s, gain −60…12 dB,
 * low-pass 200–20 000 Hz.
 */

import { el, clamp } from "../dom.js";
import { badge, field, icon } from "../ui.js";

/** @typedef {"build"|"pull_back"|"silence"|"restore"|"impact"|"riser"|"end_sting"} CueAction */

export const CUE_ACTIONS = [
  { value: "build", label: "Build", kind: "automation", hint: "Lift the bed's level from this moment." },
  { value: "pull_back", label: "Pull back", kind: "automation", hint: "Sit the bed down so narration leads." },
  { value: "silence", label: "Silence", kind: "automation", hint: "Drop the bed to silence (the narration keeps playing)." },
  { value: "restore", label: "Restore", kind: "automation", hint: "Bring the bed back to full level." },
  { value: "impact", label: "Impact", kind: "effect", hint: "Mix a hit/impact asset in at this exact time." },
  { value: "riser", label: "Riser", kind: "effect", hint: "Mix a rising one-shot under the moment." },
  { value: "end_sting", label: "End sting", kind: "effect", hint: "Land a short closing hit." },
];

export const CUE_ACTION_BY_VALUE = /** @type {Record<string, (typeof CUE_ACTIONS)[number]>} */
  (Object.fromEntries(CUE_ACTIONS.map((a) => [a.value, a])));

export const EFFECT_ACTIONS = new Set(["impact", "riser", "end_sting"]);

export const GAIN_DB_MIN = -60;
export const GAIN_DB_MAX = 12;
export const LOWPASS_MIN = 200;
export const LOWPASS_MAX = 20000;
export const TRANSITION_MAX = 5;

/** Default parameters per action, for cues the user creates in the Studio. */
export const ACTION_DEFAULTS = {
  build: { gain_db: 3, transition_seconds: 0.5, lowpass_hz: null },
  pull_back: { gain_db: -6, transition_seconds: 0.4, lowpass_hz: 3200 },
  silence: { gain_db: null, transition_seconds: 0.15, lowpass_hz: null },
  restore: { gain_db: null, transition_seconds: 0.25, lowpass_hz: null },
  impact: { effect_gain_db: -4, transition_seconds: 0, lowpass_hz: null },
  riser: { effect_gain_db: -6, transition_seconds: 0, lowpass_hz: null },
  end_sting: { effect_gain_db: -3, transition_seconds: 0, lowpass_hz: null },
};

/**
 * @param {number} time
 * @param {CueAction} action
 * @param {number} durationSeconds
 * @param {object} [extra] - optional label/source/locked/effect fields
 * @returns {object} a cue payload matching score-plan.json v1
 */
export function newCue(time, action, durationSeconds, extra = {}) {
  const def = ACTION_DEFAULTS[action] || {};
  const cue = {
    id: typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `cue-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
    time_seconds: roundTime(clamp(time, 0, durationSeconds)),
    action,
    label: extra.label || CUE_ACTION_BY_VALUE[action]?.label || action,
    transition_seconds: def.transition_seconds ?? 0.25,
    source: "manual",
    locked: false,
  };
  if (action === "silence") {
    cue.gain_db = null;
  } else if (def.gain_db != null) {
    cue.gain_db = def.gain_db;
  }
  if (def.lowpass_hz != null) cue.lowpass_hz = def.lowpass_hz;
  if (EFFECT_ACTIONS.has(action)) {
    cue.effect_gain_db = def.effect_gain_db ?? -4;
    if (extra.effect_asset_id) cue.effect_asset_id = extra.effect_asset_id;
  }
  return cue;
}

export function roundTime(seconds) {
  return Math.round(seconds * 1000) / 1000;
}

/**
 * Snap a time to the nearest strong point (scene boundaries, caption
 * starts, and the start/end of the piece) within `radius` seconds.
 * @param {number} time
 * @param {number[]} points
 * @param {number} [radius]
 * @returns {number}
 */
export function snapTime(time, points, radius = 0.35) {
  let best = time;
  let bestDist = radius;
  for (const point of points) {
    const dist = Math.abs(point - time);
    if (dist <= bestDist) {
      bestDist = dist;
      best = point;
    }
  }
  return roundTime(best);
}

/**
 * The plan payload for PUT /music/score-plan. Cues are re-sorted by time
 * (the backend re-sorts too; keeping the payload stable makes dirty
 * detection honest).
 * @param {number} durationSeconds
 * @param {object[]} cues
 * @param {string | null} [sourceMusicHash]
 * @returns {object}
 */
export function serializePlan(durationSeconds, cues, sourceMusicHash = null) {
  const ordered = [...cues].sort((a, b) => a.time_seconds - b.time_seconds);
  return {
    version: 1,
    duration_seconds: roundTime(durationSeconds),
    source_music_hash: sourceMusicHash,
    cues: ordered,
  };
}

/**
 * Stable comparison of two plan payloads (ignoring revision/updated_at,
 * mirroring the backend's score_plan_hash content set).
 * @param {object} a
 * @param {object} b
 * @returns {boolean}
 */
export function plansEqual(a, b) {
  if (!a || !b) return a === b;
  if (Math.abs(a.duration_seconds - b.duration_seconds) > 1e-6) return false;
  if ((a.source_music_hash || null) !== (b.source_music_hash || null)) return false;
  if (a.cues.length !== b.cues.length) return false;
  const sort = (cues) => cues
    .map((c) => JSON.stringify({
      id: c.id, t: c.time_seconds, a: c.action, l: c.label,
      tr: c.transition_seconds, g: c.gain_db ?? null, f: c.lowpass_hz ?? null,
      ea: c.effect_asset_id || null, ep: c.effect_path || null,
      eg: c.effect_gain_db ?? null, s: c.source, lk: !!c.locked,
    }))
    .sort();
  return sort(a.cues || []).join("\n") === sort(b.cues || []).join("\n");
}

/** @param {string} action @returns {string} "automation" | "effect" */
export function cueKind(action) {
  return EFFECT_ACTIONS.has(action) ? "effect" : "automation";
}

/**
 * One row in the cue list: time, action badge, label, and per-row actions
 * (lock against Auto Score, duplicate, delete).
 * @param {object} cue
 * @param {object} [state]
 * @param {boolean} [state.selected]
 * @param {boolean} [state.suggested]
 * @param {(cueId: string) => void} onSelect
 * @param {(cueId: string) => void} onToggleLock
 * @param {(cueId: string) => void} onDuplicate
 * @param {(cueId: string) => void} onDelete
 */
export function cueRow(cue, state, onSelect, onToggleLock, onDuplicate, onDelete) {
  const action = CUE_ACTION_BY_VALUE[cue.action] || { label: cue.action, kind: "automation" };
  const timeText = `${cue.time_seconds.toFixed(3)}s`;
  const lockBtn = el("button", {
    class: "btn btn-ghost btn-sm cue-lock-btn",
    type: "button",
    title: cue.locked ? "Unlock from Auto Score" : "Lock against Auto Score",
    "aria-pressed": String(!!cue.locked),
  }, cue.locked ? icon("lock", 13) : icon("unlock", 13));
  lockBtn.onclick = (ev) => {
    ev.stopPropagation();
    onToggleLock(cue.id);
  };
  const dup = el("button", {
    class: "btn btn-ghost btn-sm", type: "button", "aria-label": "Duplicate cue",
  }, "Duplicate");
  dup.onclick = (ev) => { ev.stopPropagation(); onDuplicate(cue.id); };
  const del = el("button", {
    class: "btn btn-ghost btn-sm", type: "button", "aria-label": "Delete cue",
  }, "Delete");
  del.onclick = (ev) => { ev.stopPropagation(); onDelete(cue.id); };

  const row = el("div", {
    class: [
      "cue-row",
      state?.selected ? "selected" : "",
      cue.locked ? "locked" : "",
      state?.suggested ? "suggested" : "",
    ].filter(Boolean).join(" "),
    role: "button",
    tabindex: "0",
    "aria-label": `Cue at ${timeText}: ${action.label}${cue.label ? `, ${cue.label}` : ""}${cue.locked ? ", locked" : ""}`,
  },
    el("span", { class: "cue-time mono" }, timeText),
    badge(action.kind === "effect" ? "warning" : "accent", action.label, false),
    cue.label ? el("span", { class: "cue-label" }, cue.label) : null,
    state?.suggested ? badge("warning", "proposed", false) : null,
    el("span", { class: "spacer" }),
    lockBtn, dup, del,
  );
  row.onclick = () => onSelect(cue.id);
  row.onkeydown = (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      onSelect(cue.id);
    }
  };
  return row;
}

/**
 * The inspector form for the selected cue. Every numeric control has a
 * plain input, which is the accessible alternative to dragging on the
 * timeline (drag updates the same values; save commits either way).
 * @param {object} cue
 * @param {object} ctx
 * @param {{label: string, input: HTMLElement, hint?: string}[]} ctx.fields - built by the caller
 * @returns {HTMLElement}
 */
export function cueInspector(cue, { effects, onField, onAction, onToggleLock, onDuplicate, onDelete, durationSeconds }) {
  const action = CUE_ACTION_BY_VALUE[cue.action];
  const isEffect = EFFECT_ACTIONS.has(cue.action);

  const timeInput = el("input", {
    type: "number", class: "input", step: "0.001", min: "0",
    max: String(durationSeconds), value: String(cue.time_seconds),
    "aria-label": "Cue time in seconds",
  });
  timeInput.onchange = () => {
    const value = parseFloat(timeInput.value);
    if (Number.isFinite(value)) onField("time_seconds", roundTime(clamp(value, 0, durationSeconds)));
    else timeInput.value = String(cue.time_seconds);
  };

  const actionSelect = el("select", { class: "input", "aria-label": "Cue action" });
  for (const a of CUE_ACTIONS) {
    const opt = el("option", { value: a.value }, `${a.label} — ${a.hint}`);
    if (a.value === cue.action) opt.selected = true;
    actionSelect.append(opt);
  }
  actionSelect.onchange = () => onAction(actionSelect.value);

  const transition = el("input", {
    type: "number", class: "input", step: "0.05", min: "0", max: String(TRANSITION_MAX),
    value: String(cue.transition_seconds ?? 0.25), "aria-label": "Transition in seconds",
  });
  transition.onchange = () => {
    const value = parseFloat(transition.value);
    if (Number.isFinite(value)) onField("transition_seconds", clamp(value, 0, TRANSITION_MAX));
  };

  const parts = [
    field({ label: "Time (s)", input: timeInput, hint: "Exact sample-accurate position; drag on the timeline or type here." }),
    field({ label: "Action", input: actionSelect }),
  ];

  if (!isEffect) {
    const gain = el("input", {
      type: "number", class: "input", step: "0.5", min: String(GAIN_DB_MIN), max: String(GAIN_DB_MAX),
      value: cue.gain_db != null ? String(cue.gain_db) : "",
      "aria-label": "Music gain in decibels (blank uses the action default)",
    });
    gain.onchange = () => {
      const raw = gain.value.trim();
      if (raw === "") { onField("gain_db", null); return; }
      const value = parseFloat(raw);
      if (Number.isFinite(value)) onField("gain_db", clamp(value, GAIN_DB_MIN, GAIN_DB_MAX));
    };
    const gainField = field({
      label: "Gain (dB)", input: gain,
      hint: cue.action === "silence"
        ? "Silence goes to the digital floor; gain here is ignored."
        : "Blank = action default (build +3, pull back −6, restore 0).",
    });
    if (cue.action === "silence") gain.disabled = true;
    parts.push(gainField);

    if (cue.action === "pull_back") {
      const filter = el("input", {
        type: "number", class: "input", step: "100", min: String(LOWPASS_MIN), max: String(LOWPASS_MAX),
        value: cue.lowpass_hz != null ? String(cue.lowpass_hz) : "",
        "aria-label": "Low-pass filter cutoff in hertz (blank disables)",
      });
      filter.onchange = () => {
        const raw = filter.value.trim();
        if (raw === "") { onField("lowpass_hz", null); return; }
        const value = parseFloat(raw);
        if (Number.isFinite(value)) onField("lowpass_hz", clamp(value, LOWPASS_MIN, LOWPASS_MAX));
      };
      parts.push(field({
        label: "Low-pass (Hz)", input: filter,
        hint: "Dulls the bed during the pullback; released at the next cue.",
      }));
    }
  } else {
    const effectSelect = el("select", { class: "input", "aria-label": "Effect asset" });
    effectSelect.append(el("option", { value: "" }, "— no asset (cue renders silently) —"));
    for (const fx of effects) {
      const opt = el("option", { value: fx.asset_id }, `${fx.name} (${fx.format || "?"}, ${fx.duration_seconds != null ? `${fx.duration_seconds}s` : "?"})`);
      if (fx.asset_id === cue.effect_asset_id) opt.selected = true;
      effectSelect.append(opt);
    }
    effectSelect.onchange = () => onField("effect_asset_id", effectSelect.value || null);

    const fxGain = el("input", {
      type: "number", class: "input", step: "0.5", min: String(GAIN_DB_MIN), max: String(GAIN_DB_MAX),
      value: cue.effect_gain_db != null ? String(cue.effect_gain_db) : "",
      "aria-label": "Effect gain in decibels",
    });
    fxGain.onchange = () => {
      const raw = fxGain.value.trim();
      if (raw === "") { onField("effect_gain_db", null); return; }
      const value = parseFloat(raw);
      if (Number.isFinite(value)) onField("effect_gain_db", clamp(value, GAIN_DB_MIN, GAIN_DB_MAX));
    };
    parts.push(
      field({ label: "Effect asset", input: effectSelect, hint: "Local impacts/stings uploaded to this project." }),
      field({ label: "Effect gain (dB)", input: fxGain }),
    );
  }

  const labelInput = el("input", {
    type: "text", class: "input", value: cue.label || "", maxlength: "200",
    "aria-label": "Cue label",
  });
  labelInput.onchange = () => onField("label", labelInput.value.slice(0, 200));
  parts.push(field({ label: "Label", input: labelInput }));

  const lockBtn = el("button", {
    class: "btn btn-sm", type: "button", "aria-pressed": String(!!cue.locked),
  }, cue.locked ? "Unlocked from Auto Score" : "Locked against Auto Score");
  lockBtn.onclick = () => onToggleLock();
  const dupBtn = el("button", { class: "btn btn-sm", type: "button" }, "Duplicate");
  dupBtn.onclick = () => onDuplicate();
  const delBtn = el("button", { class: "btn btn-danger btn-sm", type: "button" }, "Delete cue");
  delBtn.onclick = () => onDelete();

  return el("div", { class: "cue-inspector stack" },
    el("div", { class: "row" },
      el("strong", {}, action ? action.label : String(cue.action)),
      cue.locked ? badge("warning", "locked", false) : null,
      cue.source === "auto" ? badge("neutral", "auto", false) : null,
    ),
    parts,
    el("div", { class: "row" },
      lockBtn, dupBtn, delBtn,
    ),
  );
}