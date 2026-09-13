/**
 * Music screen — Score Studio.
 *
 *  - Generation: ACE-Step settings (a dedicated music.direction leads the
 *    prompt; the project's visual style is only a fallback), plus readiness
 *    and the generate/regenerate job, exactly as before.
 *  - Studio: a five-lane score timeline (scene boundaries, narration
 *    waveform, music waveform, SFX clips, music automation) with one
 *    transport (play/pause, stop, seek, spacebar) and a fast
 *    narration-plus-score preview.
 *  - Cues: add at the playhead, drag on the automation lane (or edit the
 *    exact numbers in the inspector), lock against Auto Score, duplicate or
 *    delete. Changes stay local until Save, which persists the portable
 *    score plan and invalidates only the mix and its render descendants —
 *    never the generated soundtrack, visuals, or narration.
 *  - Auto Score: the configured local LLM proposes cues (deterministic
 *    recipe fallback); proposals are shown distinctly and only persisted
 *    after explicit acceptance. Locked manual cues survive every pass.
 *
 * Layout is responsive: desktop puts Generation / Timeline / Cues in three
 * columns, tablet stacks the cue inspector across the bottom, mobile
 * collapses to a single column in reading order.
 */

import { el, fmtDate, fmtDuration, shortId } from "../dom.js";
import { state, needsProject } from "../state.js";
import {
  editProject, musicModels, generateMusic,
  musicStudio, saveScorePlan, scorePreview, uploadScoreEffect, autoScore,
} from "../api.js";
import {
  loadingState, errorPanel, badge, jobStatusBadge, toast, toastError, field, stageChip, icon,
} from "../ui.js";
import { registerLiveUpdate } from "../app.js";
import { parseRoute } from "../router.js";
import { createScoreTimeline } from "../music/timeline.js";
import { destroyWaveformContext } from "../music/waveform.js";
import {
  CUE_ACTIONS, newCue, serializePlan, plansEqual, cueRow, cueInspector,
  EFFECT_ACTIONS, snapTime,
} from "../music/cues.js";

const TERMINAL = ["completed", "failed", "canceled"];
/** Bumped on every render; the live-feed callback and the transport check
 *  it before touching widgets, so leaving the screen cleans up. */
let generation = 0;

/** @type {{gen: number, screen: HTMLElement} | null} */
let active = null;

/**
 * @param {{name: string, param?: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderMusic(_route) {
  generation += 1;
  const gen = generation;
  // Tear down the previous screen's transport before rebuilding: the audio
  // element, spacebar listener, and decode context must not survive.
  if (active && active.timeline) {
    try { active.timeline.destroy(); } catch { /* already gone */ }
    active.timeline = null;
  }
  destroyWaveformContext();
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Music"),
      el("span", { class: "sub" }, "Score Studio")),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject("Select a project in the top bar to manage its music."));
    active = null;
    return screen;
  }
  const layout = el("div", { class: "score-layout" });
  const genPanel = el("div", { class: "score-col score-col-gen" });
  const studioPanel = el("div", { class: "score-col score-col-studio" });
  const cuePanel = el("div", { class: "score-col score-col-cues" });
  layout.append(genPanel, studioPanel, cuePanel);
  screen.append(layout);
  active = { gen, screen, timeline: null };

  load(gen, { genPanel, studioPanel, cuePanel });
  return screen;
}

/**
 * @param {number} gen
 * @param {{genPanel: HTMLElement, studioPanel: HTMLElement, cuePanel: HTMLElement}} region
 */
async function load(gen, region) {
  region.genPanel.replaceChildren(loadingState(2));
  region.studioPanel.replaceChildren(loadingState(3));
  region.cuePanel.replaceChildren(loadingState(2));
  try {
    const [snap, models] = await Promise.all([
      musicStudio(state.config, state.currentProjectId),
      musicModels(state.config),
    ]);
    if (gen !== generation) return; // navigated away mid-load
    region.genPanel.replaceChildren(generationPanel(snap, models, () => refreshAfterGeneration(gen, region)));
    buildStudio(gen, region, snap);
    buildCues(region, snap);
  } catch (err) {
    if (gen !== generation) return;
    const panel = errorPanel(err,
      el("button", { class: "btn", type: "button", onclick: () => load(gen, region) }, "Retry"),
    );
    region.genPanel.replaceChildren(panel);
    region.studioPanel.replaceChildren();
    region.cuePanel.replaceChildren();
  }
}

/**
 * After the generation job settles: re-fetch the snapshot and rebuild only
 * the studio and cues columns (fresh soundtrack/scored/preview assets, fresh
 * transport). The generation column — including any in-progress settings
 * edits — is left untouched.
 * @param {number} gen
 * @param {object} region
 */
async function refreshAfterGeneration(gen, region) {
  try {
    const snap = await musicStudio(state.config, state.currentProjectId);
    if (gen !== generation) return;
    if (region._timeline) {
      region._timeline.destroy();
      region._timeline = null;
    }
    region.studioPanel.replaceChildren();
    region.cuePanel.replaceChildren();
    buildStudio(gen, region, snap);
    buildCues(region, snap);
  } catch { /* the next explicit reload recovers */ }
}

/* ============================================================================
 * Generation column
 * ==========================================================================*/

/**
 * @param {any} snap
 * @param {any} models
 * @param {() => void} reload
 * @returns {HTMLElement}
 */
function generationPanel(snap, models, reload) {
  const musicSettings = (snap.music && snap.music.settings) || {};
  const readiness = (models && models.readiness) || {};
  const combo = readiness.combo_choices || {};
  const isReady = readiness.comfyui_healthy && readiness.turbo && readiness.turbo.ready;
  const turboMissing = (readiness.turbo && readiness.turbo.missing_files) || [];
  const sftReady = readiness.sft && readiness.sft.ready;
  const ace = (snap.music && snap.music.ace) || null;
  const aceEnabled = ace && ace.enabled;

  const direction = el("textarea", {
    class: "input", rows: "4",
    "aria-label": "Music direction",
    placeholder: "e.g. tense documentary bed, slow pulsing synths, builds to a stark reveal",
  }, musicSettings.direction || "");
  const bpm = el("input", {
    type: "number", class: "input", min: "40", max: "220",
    value: String(musicSettings.bpm != null ? musicSettings.bpm : 90),
  });
  const keyScale = el("select", { class: "input" });
  (combo.key_scale || ["C major"]).forEach((ks) => {
    const opt = el("option", { value: ks }, ks);
    if (ks === (musicSettings.key_scale || "C major")) opt.selected = true;
    keyScale.append(opt);
  });
  const timeSig = el("select", { class: "input" });
  (combo.time_signature || ["4"]).forEach((ts) => {
    const opt = el("option", { value: ts }, ts);
    if (ts === (musicSettings.time_signature || "4")) opt.selected = true;
    timeSig.append(opt);
  });
  const seed = el("input", {
    type: "number", class: "input", min: "0",
    value: String(musicSettings.seed != null ? musicSettings.seed : 30001),
  });
  const randomize = el("button", { class: "btn btn-ghost btn-sm", type: "button" },
    icon("refresh", 13), "Random");
  randomize.onclick = () => { seed.value = String(Math.floor(Math.random() * 1_000_000)); };
  const modelSelect = el("select", { class: "input" });
  const turboOpt = el("option", { value: "xl_turbo" }, "XL Turbo — recommended");
  turboOpt.selected = (musicSettings.model || "xl_turbo") === "xl_turbo";
  modelSelect.append(turboOpt);
  const sftOpt = el("option", { value: "xl_sft" }, "XL SFT — maximum quality");
  sftOpt.selected = musicSettings.model === "xl_sft";
  sftOpt.disabled = !sftReady;
  modelSelect.append(sftOpt);
  const instrumental = el("input", {
    type: "checkbox", class: "input", checked: musicSettings.instrumental !== false,
    "aria-label": "Instrumental (no vocals)",
  });
  const enhanced = el("input", {
    type: "checkbox", class: "input", checked: musicSettings.generate_audio_codes !== false,
    "aria-label": "Enhanced audio planning",
  });
  const intensity = el("select", { class: "input" },
    el("option", { value: "subtle" }, "Subtle — gentle, broad moves"),
    el("option", { value: "balanced" }, "Balanced"),
    el("option", { value: "expressive" }, "Expressive — short, punchy moves"),
  );
  intensity.value = musicSettings.intensity || "balanced";

  const durationBox = el("input", {
    type: "text", class: "input", readonly: true,
    value: snap.music && snap.music.duration_seconds
      ? `${snap.music.duration_seconds}s` : "—",
    "aria-label": "Duration (derived from narration)",
  });

  const save = el("button", { class: "btn", type: "button" }, "Save settings");
  save.onclick = async () => {
    const chosen = modelSelect.selectedOptions[0];
    if (chosen && chosen.disabled) {
      toast("warning", "Model not installed",
        `${chosen.textContent} is missing from ComfyUI. Pick XL Turbo or install the SFT files first.`);
      return;
    }
    save.disabled = true;
    try {
      await editProject(state.config, state.currentProjectId, {
        settings: {
          music: {
            direction: direction.value.trim(),
            instrumental: instrumental.checked,
            backend: "ace_step_comfyui",
            model: modelSelect.value,
            generate_audio_codes: enhanced.checked,
            seed: parseInt(seed.value, 10) || 30001,
            bpm: parseInt(bpm.value, 10) || 90,
            key_scale: keyScale.value,
            time_signature: timeSig.value,
            intensity: intensity.value,
          },
        },
      });
      toast("good", "Music settings saved",
        "Music, the score mix, and dependent render stages will regenerate on the next render.");
    } catch (err) {
      toastError(err, "save music settings");
    } finally {
      save.disabled = false;
    }
  };

  const generate = el("button", { class: "btn btn-primary", type: "button" },
    snap.soundtrack ? "Regenerate music" : "Generate music");
  const jobStatus = el("div", { class: "stack" });
  let pendingJob = null;
  let hadActive = false;

  function musicJobs() {
    return (state.jobs || [])
      .filter((j) => j.stage === "music" && j.project_id === state.currentProjectId)
      .concat(pendingJob ? [pendingJob] : []);
  }

  function syncJobState() {
    if (!active || active.gen !== generation) return;
    const jobs = musicJobs();
    const activeJob = jobs.find((j) => !TERMINAL.includes(j.status)) || null;
    const failed = jobs.find((j) => j.status === "failed") || null;
    const parts = [];
    if (activeJob) {
      parts.push(el("div", { class: "row" },
        jobStatusBadge(activeJob.status),
        el("span", { class: "small muted mono" }, `job ${shortId(activeJob.id)}`),
      ));
    } else if (aceEnabled && !isReady) {
      parts.push(el("div", { class: "muted small" }, "ACE-Step ComfyUI is not ready."));
    } else if (aceEnabled && turboMissing.length) {
      parts.push(el("div", { class: "muted small" }, "Install missing Turbo files to enable generation."));
    }
    if (!activeJob && failed && failed.error) {
      parts.push(el("div", { class: "warning-list" },
        el("div", { class: "witem crit" }, el("span", { class: "small mono" }, failed.error)),
      ));
    }
    jobStatus.replaceChildren(...parts);
    const settled = hadActive && !activeJob;
    hadActive = Boolean(activeJob);
    generate.disabled = (aceEnabled && (!isReady || turboMissing.length > 0)) || Boolean(activeJob);
    generate.textContent = activeJob
      ? "Generating…"
      : (snap.soundtrack ? "Regenerate music" : "Generate music");
    if (settled) reload();
  }

  generate.onclick = async () => {
    generate.disabled = true;
    try {
      pendingJob = await generateMusic(state.config, state.currentProjectId, {
        force: Boolean(snap.soundtrack),
      });
      toast("good",
        snap.soundtrack ? "Music regeneration queued" : "Music generation queued",
        `Job ${pendingJob.id.substring(0, 8)}… is running.`,
      );
    } catch (err) {
      toastError(err, snap.soundtrack ? "regenerate music" : "start music generation");
    } finally {
      syncJobState();
    }
  };

  syncJobState();
  if (parseRoute().name === "music") {
    registerLiveUpdate(() => {
      if (generation !== active?.gen) return;
      syncJobState();
    });
  }

  return el("div", { class: "stack" },
    el("div", { class: "panel" },
      el("div", { class: "row" },
        el("span", { class: "panel-title" }, "Generation"),
        el("span", { class: "spacer" }),
        readinessReadinessBadge(ace, isReady),
      ),
      el("div", { class: "panel-body stack" },
        el("div", { class: "field" },
          el("label", {}, "Music direction"),
          direction,
          el("div", { class: "hint" },
            "What the score should feel like. ACE-Step composes the bed; cues shape the exact moves afterward."),
        ),
        el("div", { class: "pref-grid" },
          field({ label: "Duration (from narration)", input: durationBox, hint: "Read-only; the narration master sets the score length." }),
          field({ label: "BPM", input: bpm }),
          field({ label: "Key / scale", input: keyScale }),
          field({ label: "Time signature", input: timeSig }),
          el("div", { class: "field" },
            el("label", {}, "Seed"),
            el("div", { class: "row" }, seed, randomize),
          ),
          field({ label: "Model quality", input: modelSelect }),
          el("div", { class: "field" },
            el("label", { class: "check-row" }, instrumental, "Instrumental (no vocals)"),
          ),
          el("div", { class: "field" },
            el("label", { class: "check-row" }, enhanced, "Enhanced audio planning"),
          ),
          field({ label: "Intensity", input: intensity, hint: "Shapes Auto Score moves and the generation prompt." }),
        ),
        el("div", { class: "row" }, save, generate),
        jobStatus,
        aceNotes(readiness, turboMissing),
      ),
    ),
    soundtrackPanel(snap),
  );
}

/**
 * Compact ACE readiness line (full details live on the Models screen).
 * @param {any} ace @param {boolean} isReady
 */
function readinessReadinessBadge(ace, isReady) {
  if (!ace) return null;
  if (!ace.enabled) return badge("offline", "ACE-Step off");
  return isReady ? badge("good", "ready") : badge("warning", "not ready");
}

function aceNotes(readiness, turboMissing) {
  const parts = [];
  if (turboMissing.length) {
    parts.push(el("div", { class: "warning-list" },
      el("div", { class: "witem" }, `Missing Turbo files: ${turboMissing.join(", ")}`),
    ));
  }
  if (readiness.turbo && readiness.turbo.missing_nodes && readiness.turbo.missing_nodes.length) {
    parts.push(el("div", { class: "warning-list" },
      el("div", { class: "witem" }, `Missing Turbo nodes: ${readiness.turbo.missing_nodes.join(", ")}`),
    ));
  }
  return parts;
}

function soundtrackPanel(snap) {
  const rows = [];
  const entries = [
    ["Soundtrack (master)", snap.soundtrack],
    ["Scored mix", snap.scored],
    ["Score preview", snap.preview],
  ];
  for (const [label, item] of entries) {
    if (!item) continue;
    const url = item.url || null;
    const versioned = url ? versionedAudioUrl(url, item.hash) : null;
    rows.push(el("div", { class: "row" },
      el("span", { class: "small" }, label),
      el("span", { class: "muted small mono" }, item.duration_seconds != null ? `${item.duration_seconds}s` : ""),
      el("span", { class: "spacer" }),
      versioned
        ? el("audio", { controls: true, preload: "metadata", src: versioned, style: { width: "220px" } })
        : el("span", { class: "muted small" }, "unavailable"),
    ));
  }
  const planHash = snap.score_plan_hash;
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Soundtracks"),
      el("span", { class: "spacer" }),
      snap.stages ? stageChip("score_mix", snap.stages.score_mix) : null,
    ),
    el("div", { class: "panel-body stack" },
      rows.length
        ? rows
        : el("div", { class: "muted small" },
          "No music yet — generate it above. The master is never modified by scoring; cues are mixed into a separate scored file."
        ),
      planHash
        ? el("div", { class: "muted small mono" }, `score plan ${planHash}`)
        : null,
    ),
  );
}

function versionedAudioUrl(url, version) {
  if (!url) return null;
  if (!version) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}v=${encodeURIComponent(version)}`;
}

/* ============================================================================
 * Studio column: transport + timeline + preview + auto score
 * ==========================================================================*/

/**
 * @param {number} gen
 * @param {{genPanel: HTMLElement, studioPanel: HTMLElement, cuePanel: HTMLElement}} region
 * @param {any} snap
 */
function buildStudio(gen, region, snap) {
  const { studioPanel } = region;
  const duration = (snap.music && snap.music.duration_seconds) || 0;

  // ---- working state shared with the cues column -------------------------
  const cuesState = createCuesState(snap, duration);

  const timelinePanel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Score timeline"),
      el("span", { class: "spacer" }),
      snap.stages ? stageChip("score_mix", snap.stages.score_mix) : null,
    ),
  );
  const timelineHost = el("div", {});
  timelinePanel.append(timelineHost);

  const previewBtn = el("button", { class: "btn", type: "button" },
    icon("play", 14), "Preview score mix");
  const previewNote = el("div", { class: "muted small" },
    "Mixes the scored music with the narration as a fast audio check — no video render, no re-scoring.");
  const previewRow = el("div", { class: "stack" }, previewBtn, previewNote);

  const autoTrigger = el("button", { class: "btn", type: "button" },
    icon("music", 14), "Propose cues");
  const autoPanel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Auto Score"),
      el("span", { class: "spacer" }),
      autoTrigger,
    ),
    el("div", { class: "panel-body stack" },
      el("div", { class: "muted small" },
        "The local LLM reads your narration, scene boundaries, and caption emphasis (nothing leaves the machine) and proposes cues. Suggestions never touch the saved plan until you accept them; locked cues are always preserved."),
    ),
  );
  const autoBody = autoPanel.querySelector(".panel-body");

  studioPanel.replaceChildren(timelinePanel, previewRow, autoPanel);

  // ---- timeline -----------------------------------------------------------
  const timeline = createScoreTimeline(timelineHost, {
    getDuration: () => cuesState.duration(),
    getData: () => ({
      scenes: snap.scenes || [],
      cues: cuesState.cues().map((c) => ({
        id: c.id,
        time_seconds: c.time_seconds,
        action: c.action,
        label: c.label,
        locked: !!c.locked,
        duration_seconds: effectDuration(snap, c),
      })),
      snapPoints: snapPointsOf(snap, duration),
      narration_url: versionedAudioUrl(
        (snap.narration && snap.narration.url) || null,
        (snap.narration && snap.narration.active_asset_id) || null,
      ),
      music_url: primaryMusicUrl(snap),
    }),
    onCueCommit: (cueId, time) => {
      if (generation !== gen) return;
      cuesState.commitTime(cueId, time, region);
    },
    onSelect: (cueId) => {
      if (generation !== gen) return;
      cuesState.select(cueId, region);
    },
  });
  if (active) active.timeline = timeline;
  region._cuesState = cuesState;
  region._timeline = timeline;

  // Playback source: the mixed preview when available, else the scored bed,
  // else the untouched master. Without any audio the transport runs a
  // silent clock so cues can still be placed and moved.
  const initialUrl =
    (snap.preview && snap.preview.url)
    || (snap.scored && snap.scored.url)
    || (snap.soundtrack && snap.soundtrack.url)
    || null;
  if (initialUrl) timeline.setAudioSource(versionedAudioUrl(initialUrl, (snap.preview && snap.preview.hash) || (snap.scored && snap.scored.hash) || (snap.soundtrack && snap.soundtrack.hash)));
  timeline.setDuration(duration);

  previewBtn.onclick = async () => {
    previewBtn.disabled = true;
    try {
      const result = await scorePreview(state.config, state.currentProjectId, { force: false });
      if (generation !== gen) return;
      snap.preview = { url: result.url, hash: result.hash, duration_seconds: result.duration_seconds };
      timeline.setAudioSource(versionedAudioUrl(result.url, result.hash));
      toast("good", "Score preview ready",
        result.reused ? "Reused the cached preview." : "Narration mixed over the current score.");
    } catch (err) {
      toastError(err, "render score preview");
    } finally {
      previewBtn.disabled = false;
    }
  };

  // ---- auto score ----------------------------------------------------------
  autoTrigger.onclick = async () => {
    autoTrigger.disabled = true;
    autoBody.replaceChildren(loadingState(1));
    try {
      const settings = (snap.music && snap.music.settings) || {};
      const result = await autoScore(state.config, state.currentProjectId, {
        musicDirection: settings.direction || null,
        intensity: settings.intensity || null,
      });
      if (generation !== gen) return;
      autoBody.replaceChildren(autoSuggestions(result, cuesState, () => {
        if (gen !== generation) return;
        refreshRegion(cuesState, snap, region);
      }));
    } catch (err) {
      if (generation === gen) {
        autoBody.replaceChildren(errorPanel(err,
          el("button", { class: "btn", type: "button", onclick: () => autoTrigger.click() }, "Retry"),
        ));
      }
    } finally {
      if (generation === gen) autoTrigger.disabled = false;
    }
  };
}

/**
 * @param {any} result
 * @param {object} cuesState
 * @param {(change: string) => void} notify
 */
function autoSuggestions(result, cuesState, notify) {
  const list = el("div", { class: "stack" });
  const sourceBadge = result.source === "local_llm"
    ? badge("good", result.model ? `local LLM · ${result.model}` : "local LLM")
    : badge("neutral", "deterministic recipe");
  const rows = (result.suggestions || []).map((s) => {
    const accept = el("button", { class: "btn btn-sm", type: "button" }, "Accept");
    accept.onclick = () => {
      cuesState.acceptSuggestion(s);
      accept.replaceChildren("Accepted");
      accept.disabled = true;
      accept.classList.remove("btn");
      accept.classList.add("badge", "badge-good");
      notify();
    };
    return el("div", { class: "auto-suggestion row" },
      el("span", { class: "mono small" }, `${s.time_seconds.toFixed(3)}s`),
      badge("warning", s.action, false),
      el("span", { class: "small" }, s.label || s.action),
      el("span", { class: "muted small" }, s.reason || ""),
      el("span", { class: "spacer" }),
      accept,
    );
  });
  const all = el("button", { class: "btn btn-primary btn-sm", type: "button" }, "Accept all");
  const discard = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Discard all");
  all.onclick = () => {
    const added = cuesState.acceptAllSuggestions(result.suggestions || []);
    list.replaceChildren(el("div", { class: "muted small" },
      `Added ${added} cue${added === 1 ? "" : "s"} (locked collisions skipped). Review, then Save the plan.`));
    notify();
  };
  discard.onclick = () => {
    list.replaceChildren(el("div", { class: "muted small" }, "Suggestions discarded; nothing was changed."));
  };
  return el("div", { class: "stack" },
    el("div", { class: "row" }, sourceBadge,
      el("span", { class: "muted small" }, result.note || ""),
    ),
    el("div", { class: "row" }, all, discard),
    rows.length ? rows : el("div", { class: "muted small" }, "No suggestions returned."),
  );
}

function effectDuration(snap, cue) {
  if (!EFFECT_ACTIONS.has(cue.action)) return null;
  const fx = (snap.effects || []).find((e) => e.asset_id === cue.effect_asset_id);
  return fx && fx.duration_seconds != null ? fx.duration_seconds : 0.25;
}

function primaryMusicUrl(snap) {
  if (snap.scored && snap.scored.url) return versionedAudioUrl(snap.scored.url, snap.scored.hash);
  if (snap.soundtrack && snap.soundtrack.url) {
    return versionedAudioUrl(snap.soundtrack.url, snap.soundtrack.hash);
  }
  return null;
}

function snapPointsOf(snap, duration) {
  const points = new Set([0, duration]);
  for (const scene of snap.scenes || []) {
    points.add(scene.start_seconds);
    points.add(scene.end_seconds);
  }
  for (const caption of snap.captions || []) {
    points.add(caption.start_seconds);
    points.add(caption.end_seconds);
  }
  return [...points].filter((t) => t >= 0 && t <= duration).sort((a, b) => a - b);
}

/* ============================================================================
 * Cues column: list + inspector + save
 * ==========================================================================*/

/**
 * Working (unsaved) cue state. The saved plan is the base; edits accumulate
 * here until Save persists them (or Discard restores the base).
 * @param {any} snap
 * @param {number} duration
 */
function createCuesState(snap, duration) {
  const base = snap.score_plan
    ? {
        duration_seconds: snap.score_plan.duration_seconds || duration,
        source_music_hash: snap.score_plan.source_music_hash || null,
        cues: (snap.score_plan.cues || []).map((c) => ({ ...c })),
      }
    : { duration_seconds: duration, source_music_hash: null, cues: [] };
  // `cs` = the local (unsaved) cue state. The module-level `state` import is
  // the application state and is NOT shadowed here.
  const cs = {
    baseRevision: snap.score_plan_revision || 0,
    base,
    working: { duration_seconds: base.duration_seconds, source_music_hash: base.source_music_hash, cues: base.cues.map((c) => ({ ...c })) },
    selectedId: null,
    dirty: false,
  };

  cs.cues = () => cs.working.cues;
  cs.duration = () => cs.working.duration_seconds;
  cs.isDirty = () => cs.dirty;
  cs.markDirty = (value = true) => { cs.dirty = value; };

  cs.select = (cueId, region) => {
    cs.selectedId = cueId;
    if (region) {
      region._timeline && region._timeline.selectCue(cueId);
      refreshRegion(cs, snap, region);
    }
  };

  cs.commitTime = (cueId, time, region) => {
    const cue = cs.working.cues.find((c) => c.id === cueId);
    if (!cue) return;
    const newTime = Math.round(Math.min(Math.max(time, 0), cs.working.duration_seconds) * 1000) / 1000;
    if (cue.time_seconds === newTime) return; // no-op (e.g. nudged at 0:00)
    cue.time_seconds = newTime;
    sortCues(cs.working);
    cs.markDirty(true);
    if (region) refreshRegion(cs, snap, region);
  };

  cs.setField = (cueId, field, value, region) => {
    const cue = cs.working.cues.find((c) => c.id === cueId);
    if (!cue) return;
    if (field === "time_seconds") {
      cue.time_seconds = Math.round(Math.min(Math.max(value, 0), cs.working.duration_seconds) * 1000) / 1000;
    } else if (field === "action") {
      applyActionDefaults(cue, value);
    } else {
      cue[field] = value;
    }
    sortCues(cs.working);
    cs.markDirty(true);
    if (region) refreshRegion(cs, snap, region);
  };

  cs.toggleLock = (cueId, region) => {
    const cue = cs.working.cues.find((c) => c.id === cueId);
    if (!cue) return;
    cue.locked = !cue.locked;
    cs.markDirty(true);
    if (region) refreshRegion(cs, snap, region);
  };

  cs.duplicate = (cueId, region) => {
    const cue = cs.working.cues.find((c) => c.id === cueId);
    if (!cue) return;
    const copy = { ...cue, id: newCueId() };
    copy.time_seconds = Math.min(cue.time_seconds + 0.1, cs.working.duration_seconds);
    cs.working.cues.push(copy);
    sortCues(cs.working);
    cs.selectedId = copy.id;
    cs.markDirty(true);
    if (region) refreshRegion(cs, snap, region);
  };

  cs.remove = (cueId, region) => {
    cs.working.cues = cs.working.cues.filter((c) => c.id !== cueId);
    if (cs.selectedId === cueId) cs.selectedId = null;
    cs.markDirty(true);
    if (region) refreshRegion(cs, snap, region);
  };

  cs.addAt = (time, region) => {
    const cue = newCue(time, "pull_back", cs.working.duration_seconds);
    cs.working.cues.push(cue);
    sortCues(cs.working);
    cs.selectedId = cue.id;
    cs.markDirty(true);
    if (region) refreshRegion(cs, snap, region);
  };

  cs.acceptSuggestion = (s) => {
    if (lockedCollides(cs.working.cues, s)) return 0;
    cs.working.cues.push(suggestionToCue(s));
    sortCues(cs.working);
    cs.markDirty(true);
    return 1;
  };

  cs.acceptAllSuggestions = (suggestions) => {
    const locked = cs.working.cues.filter((c) => c.locked);
    const taken = [...cs.working.cues];
    let added = 0;
    for (const s of suggestions) {
      if (lockedCollides(locked, s) || sameActionNear(taken, s)) continue;
      taken.push(suggestionToCue(s));
      added += 1;
    }
    cs.working.cues = taken;
    sortCues(cs.working);
    cs.markDirty(true);
    return added;
  };

  cs.discard = (region) => {
    cs.working = {
      duration_seconds: cs.base.duration_seconds,
      source_music_hash: cs.base.source_music_hash,
      cues: cs.base.cues.map((c) => ({ ...c })),
    };
    cs.selectedId = null;
    cs.markDirty(false);
    if (region) refreshRegion(cs, snap, region);
  };

  /** Persist the working plan; on success the base advances to the saved revision. */
  cs.save = async (region, afterSave) => {
    const plan = serializePlan(cs.working.duration_seconds, cs.working.cues, cs.working.source_music_hash);
    const result = await saveScorePlan(state.config, state.currentProjectId, {
      plan,
      expectedRevision: cs.baseRevision,
    });
    cs.baseRevision = result.revision;
    cs.base = {
      duration_seconds: plan.duration_seconds,
      source_music_hash: plan.source_music_hash,
      cues: plan.cues.map((c) => ({ ...c })),
    };
    cs.markDirty(false);
    snap.score_plan = result.plan;
    snap.score_plan_revision = result.revision;
    snap.score_plan_hash = result.plan_hash;
    if (afterSave) afterSave(result.invalidated_stages || []);
  };

  return cs;
}

function newCueId() {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `cue-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function suggestionToCue(s) {
  const cue = newCue(s.time_seconds, s.action, 1e9, { label: s.label });
  cue.time_seconds = Math.round(Number(s.time_seconds) * 1000) / 1000;
  if (s.transition_seconds != null) cue.transition_seconds = s.transition_seconds;
  if (s.gain_db != null) cue.gain_db = s.gain_db;
  if (s.lowpass_hz != null) cue.lowpass_hz = s.lowpass_hz;
  cue.source = "auto";
  return cue;
}

/** A locked cue of the same action within 0.5 s protects its region. */
function lockedCollides(lockedCues, s) {
  return lockedCues.some(
    (lock) => lock.locked && lock.action === s.action
      && Math.abs(lock.time_seconds - Number(s.time_seconds)) <= 0.5,
  );
}

/** Skip a proposal that would sit on top of an already-placed cue. */
function sameActionNear(cues, s) {
  return cues.some(
    (c) => c.action === s.action && Math.abs(c.time_seconds - Number(s.time_seconds)) <= 0.5,
  );
}

function applyActionDefaults(cue, action) {
  const defaults = {
    build: { gain_db: 3, transition_seconds: 0.5, lowpass_hz: null },
    pull_back: { gain_db: -6, transition_seconds: 0.4, lowpass_hz: 3200 },
    silence: { gain_db: null, transition_seconds: 0.15, lowpass_hz: null },
    restore: { gain_db: null, transition_seconds: 0.25, lowpass_hz: null },
    impact: { gain_db: undefined, lowpass_hz: undefined, effect_gain_db: -4, transition_seconds: 0 },
    riser: { gain_db: undefined, lowpass_hz: undefined, effect_gain_db: -6, transition_seconds: 0 },
    end_sting: { gain_db: undefined, lowpass_hz: undefined, effect_gain_db: -3, transition_seconds: 0 },
  }[action] || {};
  cue.action = action;
  if (defaults.gain_db !== undefined) cue.gain_db = defaults.gain_db;
  if (defaults.lowpass_hz !== undefined) cue.lowpass_hz = defaults.lowpass_hz;
  if (defaults.effect_gain_db !== undefined) cue.effect_gain_db = defaults.effect_gain_db;
  if (defaults.transition_seconds !== undefined) cue.transition_seconds = defaults.transition_seconds;
  if (action === "silence") cue.gain_db = null;
  if (EFFECT_ACTIONS.has(action) && cue.effect_asset_id == null) {
    // keep the last chosen asset when switching between effect actions
  }
}

function sortCues(plan) {
  plan.cues.sort((a, b) => a.time_seconds - b.time_seconds);
}

/**
 * Rebuild the cues column and repaint the timeline from working state.
 * @param {object} state @param {any} snap
 * @param {{genPanel: HTMLElement, studioPanel: HTMLElement, cuePanel: HTMLElement, _timeline?: object}} region
 */
function refreshRegion(state, snap, region) {
  region.cuePanel.replaceChildren(cuesPanel(state, snap, region));
  region._timeline && region._timeline.refresh();
}

/**
 * @param {object} state @param {any} snap
 * @param {{genPanel: HTMLElement, studioPanel: HTMLElement, cuePanel: HTMLElement}} region
 */
function cuesPanel(state, snap, region) {
  const cues = state.cues();
  const selected = cues.find((c) => c.id === state.selectedId) || null;
  const addBtn = el("button", { class: "btn btn-sm", type: "button" },
    icon("plus", 13), "Add at playhead");
  const saveBtn = el("button", { class: "btn btn-primary", type: "button", disabled: !state.dirty }, "Save plan");
  const discardBtn = el("button", { class: "btn btn-ghost", type: "button", disabled: !state.dirty }, "Discard");

  addBtn.onclick = () => {
    const time = region._timeline ? region._timeline.getTime() : 0;
    state.addAt(time, region);
  };
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    try {
      await state.save(region, (invalidated) => {
        toast("good", "Score plan saved",
          invalidated.length
            ? `Invalidated: ${invalidated.join(", ")}. Music generation was not re-run.`
            : "No downstream stages needed re-running.");
        load(generation, region);
      });
    } catch (err) {
      if (err.kind === "conflict") {
        toast("warning", "Score plan conflict",
          "The plan changed while you were editing. Reloading the studio — unsaved local edits are discarded.");
        load(generation, region);
      } else {
        toastError(err, "save score plan");
        saveBtn.disabled = false;
      }
    }
  };
  discardBtn.onclick = () => state.discard(region);

  const listRows = cues.length
    ? cues.map((cue) => cueRow(cue,
      { selected: cue.id === state.selectedId },
      (id) => state.select(id, region),
      (id) => state.toggleLock(id, region),
      (id) => state.duplicate(id, region),
      (id) => state.remove(id, region),
    ))
    : el("div", { class: "muted small" },
      "No cues yet. Add one at the playhead, or let Auto Score propose a starting plan.");

  const inspector = selected
    ? cueInspector(selected, {
        effects: snap.effects || [],
        durationSeconds: state.duration(),
        onField: (f, v) => state.setField(selected.id, f, v, region),
        onAction: (action) => state.setField(selected.id, "action", action, region),
        onToggleLock: () => state.toggleLock(selected.id, region),
        onDuplicate: () => state.duplicate(selected.id, region),
        onDelete: () => state.remove(selected.id, region),
      })
    : el("div", { class: "muted small" }, "Select a cue to edit its exact time, action, gain, filter, and effect.");

  return el("div", { class: "stack" },
    el("div", { class: "panel" },
      el("div", { class: "row" },
        el("span", { class: "panel-title" }, "Cues"),
        state.dirty ? badge("warning", "unsaved") : null,
        el("span", { class: "spacer" }),
        addBtn,
      ),
      el("div", { class: "panel-body stack" }, listRows),
    ),
    el("div", { class: "panel" },
      el("div", { class: "row" },
        el("span", { class: "panel-title" }, "Inspector"),
      ),
      el("div", { class: "panel-body" }, inspector),
    ),
    effectsPanel(snap, region),
    el("div", { class: "row" },
      saveBtn, discardBtn,
      el("span", { class: "spacer" }),
      el("span", { class: "muted small" },
        "Saving re-mixes the score and its render descendants only — never the generated soundtrack."),
    ),
  );
}

/**
 * Local sound-effect library. Uploads stay on this machine; effect cues in
 * the score plan reference an uploaded asset by id.
 * @param {any} snap
 * @param {object} region
 */
function effectsPanel(snap, region) {
  const items = snap.effects || [];
  const fileInput = el("input", { type: "file", accept: ".wav,.flac,.mp3", hidden: true });
  const uploadBtn = el("label", { class: "btn btn-sm", style: "cursor:pointer" },
    icon("upload", 13), "Upload SFX");
  uploadBtn.append(fileInput);
  fileInput.onchange = async () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    uploadBtn.disabled = true;
    try {
      await uploadScoreEffect(state.config, state.currentProjectId, file);
      toast("good", "Sound effect uploaded", file.name);
      load(generation, region);
    } catch (err) {
      toastError(err, "upload sound effect");
    } finally {
      uploadBtn.disabled = false;
    }
  };

  const rows = items.length
    ? items.map((fx) => el("div", { class: "row sfx-row" },
        el("span", { class: "small" }, fx.name),
        fx.duplicate_of ? badge("neutral", "duplicate", false) : null,
        el("span", { class: "spacer" }),
        el("span", { class: "muted small mono" },
          fx.duration_seconds != null ? `${fx.duration_seconds}s` : ""),
      ))
    : el("div", { class: "muted small" },
      "No local sound effects yet. Upload WAV/FLAC/MP3 stingers to give impact, riser, and end-sting cues their own audio.");

  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Sound effects"),
      el("span", { class: "spacer" }),
      uploadBtn,
    ),
    el("div", { class: "panel-body stack" }, rows),
  );
}

/* ============================================================================
 * Column bootstrap (shared load) + cleanup
 * ==========================================================================*/

/**
 * @param {{genPanel: HTMLElement, studioPanel: HTMLElement, cuePanel: HTMLElement}} region
 * @param {any} snap
 */
function buildCues(region, snap) {
  // cuesState lives with the studio (shared by both columns); the cues column
  // is rebuilt from it whenever anything changes.
  const cuesState = region._cuesState;
  if (cuesState) {
    region.cuePanel.replaceChildren(cuesPanel(cuesState, snap, region));
  }
}