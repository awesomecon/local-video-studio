/**
 * Notification sound engine: WebAudio synthesis, no binary assets.
 *
 * The frontend is zero-build and never downloads anything, so every tone is
 * synthesized from the selected preset (oscillator + gain nodes) at play
 * time. Policy:
 *  - warning / critical toasts and modals play the preset at full volume;
 *  - good / info toasts play the same preset as a soft variant (reduced
 *    gain, shorter envelope) — a little beep, clearly quieter than an alert.
 *
 * Browsers keep a page muted until the first user gesture. `initAlertSound()`
 * registers a one-time capturing pointerdown/keydown unlock that creates and
 * resumes the shared AudioContext; alerts that fire before any interaction
 * are silently skipped — never queued or replayed.
 *
 * Preferences persist per browser under the `lvs-alert-sound` key (it is
 * whitelisted in frontend/tests/static_checks.py).
 */

const STORAGE_KEY = "lvs-alert-sound";

/**
 * Selectable presets: id -> {label, recipe}. A recipe is a list of tones
 * scheduled on the shared AudioContext: {freq Hz, type waveform, start s,
 * dur s, gain 0..1 relative peak}.
 * @type {Record<string, {label: string, tones: {freq: number, type: string, start: number, dur: number, gain: number}[]}>}
 */
export const SOUND_PRESETS = {
  /** Short two-tone rising beep, ~250 ms. */
  chirp: { label: "Chirp", tones: [
    { freq: 740, type: "sine", start: 0, dur: 0.08, gain: 0.8 },
    { freq: 1180, type: "sine", start: 0.09, dur: 0.13, gain: 0.6 },
  ] },
  /** Square double beep. */
  beep: { label: "Beep", tones: [
    { freq: 990, type: "square", start: 0, dur: 0.05, gain: 0.2 },
    { freq: 990, type: "square", start: 0.08, dur: 0.05, gain: 0.2 },
  ] },
  /** Sine with a soft partial, ~400 ms decay. */
  chime: { label: "Chime", tones: [
    { freq: 1047, type: "sine", start: 0, dur: 0.4, gain: 0.7 },
    { freq: 1568, type: "sine", start: 0, dur: 0.28, gain: 0.16 },
  ] },
  /** Low sine thump. */
  pulse: { label: "Pulse", tones: [
    { freq: 170, type: "sine", start: 0, dur: 0.16, gain: 0.9 },
  ] },
};

const DEFAULT_PREFS = Object.freeze({ enabled: true, sound: "chirp", volume: 50 });

/** Soft (good/info) variant: clearly quieter, shorter envelope. */
const SOFT_GAIN = 0.4;
const SOFT_ENV = 0.6;

/** @type {any|null} the shared AudioContext (suspended until the unlock). */
let context = null;
let unlockRegistered = false;

/* ============================================================================
 * Preferences (persisted per browser)
 * ==========================================================================*/

/**
 * Read and validate the stored preferences. Corrupt or absent stored JSON
 * falls back to defaults (on, chirp, 50); wrong-typed fields are ignored.
 * @returns {{enabled: boolean, sound: string, volume: number}}
 */
export function getAlertPrefs() {
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  } catch {
    stored = null;
  }
  const prefs = {
    enabled: DEFAULT_PREFS.enabled,
    sound: DEFAULT_PREFS.sound,
    volume: DEFAULT_PREFS.volume,
  };
  if (stored && typeof stored === "object") {
    if (typeof stored.enabled === "boolean") prefs.enabled = stored.enabled;
    if (typeof stored.sound === "string" && SOUND_PRESETS[stored.sound]) {
      prefs.sound = stored.sound;
    }
    if (typeof stored.volume === "number" && Number.isFinite(stored.volume)) {
      prefs.volume = Math.min(100, Math.max(0, Math.round(stored.volume)));
    }
  }
  return prefs;
}

/**
 * Merge a validated patch into the stored preferences and persist it.
 * Wrong-typed fields are ignored.
 * @param {{enabled?: boolean, sound?: string, volume?: number}} [patch]
 * @returns {{enabled: boolean, sound: string, volume: number}} the new prefs
 */
export function setAlertPrefs(patch = {}) {
  const current = getAlertPrefs();
  const next = { ...current };
  if (typeof patch.enabled === "boolean") next.enabled = patch.enabled;
  if (typeof patch.sound === "string" && SOUND_PRESETS[patch.sound]) {
    next.sound = patch.sound;
  }
  if (typeof patch.volume === "number" && Number.isFinite(patch.volume)) {
    next.volume = Math.min(100, Math.max(0, Math.round(patch.volume)));
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable — the merged prefs still apply for this page load */
  }
  return next;
}

/* ============================================================================
 * Policy (pure predicates — testable without a browser AudioContext)
 * ==========================================================================*/

/**
 * Play at all? Enabled, non-zero volume, and the audio context unlocked and
 * running (browsers mute pages until the first user gesture).
 * @param {string} _kind - "good"|"info"|"warning"|"critical"|"modal"
 *   (kept in the signature so per-kind muting can extend this predicate
 *   without changing call sites)
 * @param {{enabled?: boolean, volume?: number}} prefs
 * @param {boolean} audioReady
 * @returns {boolean}
 */
export function shouldPlay(_kind, prefs, audioReady) {
  return !!(prefs && prefs.enabled)
    && (Number(prefs.volume) || 0) > 0
    && !!audioReady;
}

/**
 * full (alerts) vs soft (acknowledgements); anything else defaults to soft.
 * @param {string} kind - "good"|"info"|"warning"|"critical"|"modal"
 * @returns {"full"|"soft"}
 */
export function volumeProfile(kind) {
  return kind === "warning" || kind === "critical" || kind === "modal" ? "full" : "soft";
}

/* ============================================================================
 * AudioContext (created lazily, unlocked by the first gesture)
 * ==========================================================================*/

/**
 * @returns {any|null} the shared AudioContext (still suspended before the
 *   first gesture), or null when WebAudio is unavailable.
 */
function ensureContext() {
  if (context) return context;
  const Ctor = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (typeof Ctor !== "function") return null;
  try {
    context = new Ctor();
  } catch {
    context = null;
  }
  return context;
}

/** A context is ready only once the browser's autoplay policy released it. */
function audioReady(ctx) {
  return !!(ctx && ctx.state === "running");
}

/**
 * Register the one-time capturing pointerdown/keydown unlock. The first
 * gesture creates the context and resumes it; both listeners then remove
 * themselves. Safe to call repeatedly; only the first call binds.
 */
export function initAlertSound() {
  if (unlockRegistered) return;
  unlockRegistered = true;
  const unlock = () => {
    window.removeEventListener("pointerdown", unlock, true);
    window.removeEventListener("keydown", unlock, true);
    const ctx = ensureContext();
    if (ctx && ctx.state !== "running") ctx.resume().catch(() => {});
  };
  window.addEventListener("pointerdown", unlock, true);
  window.addEventListener("keydown", unlock, true);
}

/* ============================================================================
 * Playback
 * ==========================================================================*/

/**
 * Schedule one preset on a running context.
 * @param {any} ctx
 * @param {string} presetId
 * @param {"full"|"soft"} profile
 * @param {number} volume - 0..100
 */
function playPreset(ctx, presetId, profile, volume) {
  const preset = SOUND_PRESETS[presetId] || SOUND_PRESETS.chirp;
  const soft = profile === "soft";
  const clamped = Math.min(100, Math.max(0, volume));
  const master = (clamped / 100) * (soft ? SOFT_GAIN : 1);
  const envelope = soft ? SOFT_ENV : 1;
  const t0 = ctx.currentTime;
  for (const tone of preset.tones) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = tone.type;
    osc.frequency.value = tone.freq;
    const peak = Math.max(0.0001, tone.gain * master);
    const start = t0 + tone.start;
    const dur = Math.max(0.02, tone.dur * envelope);
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.linearRampToValueAtTime(peak, start + 0.006);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + dur);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(start);
    osc.stop(start + dur + 0.02);
  }
}

/**
 * Play the selected preset for a notification kind, honoring the persisted
 * preferences. No-ops when disabled, volume 0, or the context is still
 * locked by the browser's autoplay policy (silently skipped, never queued).
 * @param {string} kind - "good"|"info"|"warning"|"critical"|"modal"
 */
export function playNotification(kind) {
  const prefs = getAlertPrefs();
  const ctx = ensureContext();
  if (!shouldPlay(kind, prefs, audioReady(ctx))) return;
  try {
    playPreset(ctx, prefs.sound, volumeProfile(kind), prefs.volume);
  } catch {
    /* an audio failure must never break the notification itself */
  }
}

/**
 * Explicit user-gesture preview (the Settings "Test sound" button): create
 * or resume the context, await the resume, then play once if preferences
 * allow it. It does not queue notifications that arrived while audio was
 * locked — those were already skipped.
 * @param {string} kind
 * @returns {Promise<void>}
 */
export async function unlockAndPlayNotification(kind) {
  const ctx = ensureContext();
  if (ctx && ctx.state !== "running") {
    try {
      await ctx.resume();
    } catch {
      /* the context may refuse to resume; the readiness check decides */
    }
  }
  const prefs = getAlertPrefs();
  if (!shouldPlay(kind, prefs, audioReady(ctx))) return;
  try {
    playPreset(ctx, prefs.sound, volumeProfile(kind), prefs.volume);
  } catch {
    /* an audio failure must never break the preview */
  }
}

/** Test hook: drop the cached context so the next call re-resolves the global AudioContext constructor. */
export function resetAudioContextForTests() {
  context = null;
}
