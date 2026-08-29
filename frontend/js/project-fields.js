/**
 * Pure value pipeline for the Project Details brief form: baseline reading,
 * change detection, PATCH body construction, and input reset.
 *
 * Moved out of `pages/project.js` so the headless logic tests can exercise the
 * exact code the form uses without importing the page (which would boot the
 * whole app shell). These helpers take plain values / control registries and
 * never touch globals, timers, or the network.
 */

import { effectiveVideoMode } from "./video-mode.js";

/**
 * Brief fields: changing any of them re-routes generation from the script
 * plan onward, so they invalidate planning and everything downstream.
 * `video_mode` switches the whole generator (classic vs. editorial
 * compositions), so it belongs here too.
 */
export const BRIEF_FIELDS = new Set([
  "title", "topic", "style", "audience", "visual_quality", "instructions",
  "duration_mode", "video_mode",
]);

/** Dimension fields: change the timeline/render math, not the script. */
export const DIMENSION_FIELDS = new Set([
  "target_duration", "aspect_ratio", "fps", "resolution",
]);

/**
 * Baseline of editable values read from a backend project payload.
 * An omitted or unrecognized `video_mode` means classic.
 * @param {any} p
 */
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

/**
 * Build the PATCH body from the changed fields only (the backend
 * `ProjectEdit` uses extra="forbid", and a mode that didn't change must not
 * be resent).
 * @param {Set<string>} changed
 * @param {Record<string, any>} values
 */
export function buildPatchBody(changed, values) {
  const body = {};
  for (const key of changed) body[key] = values[key];
  return body;
}

function fieldsEqual(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => x === b[i]);
  return a === b;
}
