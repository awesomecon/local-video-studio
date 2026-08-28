/**
 * Music screen: background-music state for the current project.
 *
 *  - Shows ACE-Step 1.5 via ComfyUI readiness, controls, and generation.
 *  - Project-level mood, style, and instrumental preference persist to the
 *    portable project metadata and invalidate only music-dependent stages.
 *  - The soundtrack is produced as long musical "movements" (default ~60s)
 *    that follow the scenes' moods, stitched into background.wav; individual
 *    movements can be regenerated without re-paying GPU cost for the rest.
 *  - Generate/regenerate tracks the project's music job through the app-level
 *    live feed, so a running task stays visible across refreshes; regenerating
 *    sends force=true to bypass the unchanged-fingerprint reuse path.
 */

import { el, fmtDate, shortId } from "../dom.js";
import { state, needsProject } from "../state.js";
import { editProject, getProject, musicModels, generateMusic } from "../api.js";
import {
  loadingState, errorPanel, badge, jobStatusBadge, progress, toast, toastError,
  field, stageChip,
} from "../ui.js";
import { registerLiveUpdate } from "../app.js";
import { parseRoute } from "../router.js";

const TERMINAL = ["completed", "failed", "canceled"];
/** Bumped on every render; lets the live-feed callback know the screen is gone. */
let generation = 0;

export function renderMusic(_route) {
  generation += 1;
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Music")),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject("Select a project in the top bar to manage its music."));
    return screen;
  }
  screen.append(musicPanel());
  return screen;
}

function musicPanel() {
  const body = el("div", { class: "panel-body" });
  const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Refresh");
  refreshBtn.onclick = () => load(body);

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Music"),
      el("span", { class: "spacer" }),
      refreshBtn,
    ),
    body,
  );

  load(body);
  return panel;
}

async function load(region) {
  region.replaceChildren(loadingState(3));
  try {
    const [snap, models] = await Promise.all([
      getProject(state.config, state.currentProjectId),
      musicModels(state.config),
    ]);
    region.replaceChildren(build(snap, models, () => load(region)));
  } catch (err) {
    region.replaceChildren(errorPanel(err,
      el("button", { class: "btn", type: "button", onclick: () => load(region) }, "Retry"),
    ));
  }
}

function build(snap, models, reload) {
  const assets = snap.assets || [];
  const soundtracks = assets
    .filter((a) => (a.settings || {}).role === "music")
    .sort((a, b) => assetTime(a) - assetTime(b));
  const music = soundtracks.length ? soundtracks[soundtracks.length - 1] : null;
  const stages = (snap.stage_state && /** @type {any} */ (snap.stage_state).stages) || {};
  const musicSettings = (snap.project && snap.project.settings && snap.project.settings.music) || {};
  const readiness = models.readiness || {};
  const combo = readiness.combo_choices || {};
  const isReady = readiness.comfyui_healthy && readiness.turbo && readiness.turbo.ready;
  const turboMissing = (readiness.turbo && readiness.turbo.missing_files) || [];
  const sftReady = readiness.sft && readiness.sft.ready;
  const musicJobs = (snap.jobs || []).filter((j) => j.stage === "music");
  const movements = currentMovements(assets, soundtracks);
  const parts = [];

  parts.push(readinessPanel(models, readiness, turboMissing, sftReady));
  parts.push(controls(snap.project, musicSettings, combo, sftReady));
  parts.push(actionPanel(
    snap.project, isReady, turboMissing,
    Boolean(music), musicJobs, reload,
  ));
  parts.push(el("div", { class: "muted small" },
    "Music preferences are stored locally in project.json. Saving them invalidates music and dependent render stages without rebuilding visual assets.",
  ));

  if (music) {
    parts.push(assetPanel(music, stages.music));
  }
  if (movements.length) {
    parts.push(movementsPanel(movements));
  } else if (music) {
    parts.push(el("div", { class: "muted small" },
      "This soundtrack was generated as a single piece; re-run the pipeline to score it as mood-following movements.",
    ));
  }
  if (!music) {
    parts.push(el("div", { class: "muted small" },
      "No music has been generated yet — it is produced by the pipeline when the project is rendered (stage: music).",
    ));
  }

  return el("div", { class: "stack" }, ...parts);
}

function assetTime(asset) {
  const parsed = Date.parse(asset && asset.created_at ? asset.created_at : "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function versionedAudioUrl(asset) {
  if (!asset || !asset.url) return null;
  const version = asset.hash || asset.id || asset.created_at;
  if (!version) return asset.url;
  const separator = asset.url.includes("?") ? "&" : "?";
  return `${asset.url}${separator}v=${encodeURIComponent(version)}`;
}

function currentMovements(assets, soundtracks) {
  if (!soundtracks.length) return [];
  const music = soundtracks[soundtracks.length - 1];
  const all = assets.filter(
    (asset) => (asset.settings || {}).role === "music_movement"
      && assetTime(asset) <= assetTime(music),
  );
  const recordedIds = new Set((music.settings || {}).movement_asset_ids || []);
  let candidates;
  if (recordedIds.size) {
    candidates = all.filter((asset) => recordedIds.has(asset.id));
  } else {
    // Legacy soundtrack rows did not record their movement IDs. Recover the
    // active set from the newest movement's plan identity, then keep only the
    // newest asset for each slot. The timestamp window is a fallback for
    // older mock projects that predate plan hashes.
    const newest = all.reduce(
      (latest, asset) => (!latest || assetTime(asset) > assetTime(latest) ? asset : latest),
      null,
    );
    const settings = (newest && newest.settings) || {};
    if (settings.plan_hash && settings.fingerprint) {
      candidates = all.filter((asset) => {
        const current = asset.settings || {};
        return current.plan_hash === settings.plan_hash
          && current.fingerprint === settings.fingerprint;
      });
    } else {
      const previous = soundtracks.length > 1
        ? assetTime(soundtracks[soundtracks.length - 2]) : -Infinity;
      candidates = all.filter((asset) => assetTime(asset) > previous);
    }
  }

  const latestByIndex = new Map();
  candidates.forEach((asset) => {
    const index = Number((asset.settings || {}).movement_index || 0);
    const existing = latestByIndex.get(index);
    if (!existing || assetTime(asset) > assetTime(existing)) latestByIndex.set(index, asset);
  });
  return [...latestByIndex.values()].sort((a, b) => (
    Number((a.settings || {}).movement_index || 0)
      - Number((b.settings || {}).movement_index || 0)
  ));
}

function readinessPanel(models, readiness, turboMissing, sftReady) {
  const panel = el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "ComfyUI + ACE readiness"),
    el("div", { class: "panel-body" },
      el("dl", { class: "kv" },
        el("dt", {}, "Provider"), el("dd", {}, models.provider || "comfyui"),
        el("dt", {}, "ComfyUI"), el("dd", {}, badge(
          readiness.comfyui_healthy ? "good" : "critical",
          readiness.comfyui_healthy ? "healthy" : "unhealthy",
        )),
        el("dt", {}, "XL Turbo"), el("dd", {}, badge(
          readiness.turbo && readiness.turbo.ready ? "good" : "critical",
          readiness.turbo && readiness.turbo.ready ? "ready" : "not ready",
        )),
        el("dt", {}, "XL SFT"), el("dd", {}, badge(
          sftReady ? "good" : "neutral",
          sftReady ? "ready" : "not ready",
        )),
        el("dt", {}, "Resident family"), el("dd", {}, models.comfyui_resident || "none"),
      ),
      turboMissing.length ? el("div", { class: "warning-list" },
        el("div", { class: "witem" },
          "Missing Turbo files: " + turboMissing.join(", "),
        ),
      ) : null,
      (readiness.turbo && readiness.turbo.missing_nodes && readiness.turbo.missing_nodes.length) ? el("div", { class: "warning-list" },
        el("div", { class: "witem" },
          "Missing Turbo nodes: " + readiness.turbo.missing_nodes.join(", "),
        ),
      ) : null,
    ),
  );
  return panel;
}

function controls(project, musicSettings, combo, sftReady) {
  const style = el("input", {
    type: "text", class: "input",
    value: musicSettings.style || project.style || "documentary",
  });
  const mood = el("select", { class: "input" },
    el("option", { value: "" }, "follow project style"),
    el("option", { value: "uplifting" }, "uplifting"),
    el("option", { value: "tense" }, "tense"),
    el("option", { value: "curious" }, "curious"),
    el("option", { value: "none" }, "none"),
  );
  mood.value = musicSettings.mood || "";
  const instrumental = el("input", {
    type: "checkbox", checked: musicSettings.instrumental !== false,
  });
  const seed = el("input", {
    type: "number", class: "input",
    value: String(musicSettings.seed != null ? musicSettings.seed : 30001),
  });
  const bpm = el("input", {
    type: "number", class: "input",
    value: String(musicSettings.bpm != null ? musicSettings.bpm : 90),
  });
  const movementSeconds = el("input", {
    type: "number", class: "input", min: "30", max: "180", step: "5",
    value: String(musicSettings.movement_seconds != null ? musicSettings.movement_seconds : 60),
  });
  const keyScale = el("select", { class: "input" });
  (combo.key_scale || ["C major"]).forEach((ks) => {
    const opt = el("option", { value: ks }, ks);
    if (ks === (musicSettings.key_scale || "C major")) opt.selected = true;
    keyScale.appendChild(opt);
  });
  const timeSig = el("select", { class: "input" });
  (combo.time_signature || ["4"]).forEach((ts) => {
    const opt = el("option", { value: ts }, ts);
    if (ts === (musicSettings.time_signature || "4")) opt.selected = true;
    timeSig.appendChild(opt);
  });
  const language = el("select", { class: "input" });
  (combo.language || ["en"]).forEach((lang) => {
    const opt = el("option", { value: lang }, lang);
    if (lang === (musicSettings.language || "en")) opt.selected = true;
    language.appendChild(opt);
  });
  const generateAudioCodes = el("input", {
    type: "checkbox", checked: musicSettings.generate_audio_codes !== false,
  });
  const modelSelect = el("select", { class: "input" });
  const turboOpt = el("option", { value: "xl_turbo" }, "XL Turbo — recommended");
  turboOpt.selected = (musicSettings.model || "xl_turbo") === "xl_turbo";
  modelSelect.appendChild(turboOpt);
  const sftOpt = el("option", { value: "xl_sft" }, "XL SFT — maximum quality");
  sftOpt.selected = musicSettings.model === "xl_sft";
  sftOpt.disabled = !sftReady;
  modelSelect.appendChild(sftOpt);

  const save = el("button", { class: "btn btn-primary", type: "button" }, "Save music settings");
  save.onclick = async () => {
    // A disabled selected <option> still reports its value — refuse to
    // persist a preset whose files are not installed in ComfyUI.
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
            style: style.value.trim() || project.style || "documentary",
            mood: mood.value,
            instrumental: instrumental.checked,
            backend: "ace_step_comfyui",
            model: modelSelect.value,
            generate_audio_codes: generateAudioCodes.checked,
            seed: parseInt(seed.value, 10) || 30001,
            bpm: parseInt(bpm.value, 10) || 90,
            movement_seconds: Math.min(180, Math.max(30, parseInt(movementSeconds.value, 10) || 60)),
            key_scale: keyScale.value,
            time_signature: timeSig.value,
            language: language.value,
          },
        },
      });
      toast("good", "Music settings saved", "Music and dependent render stages will regenerate on the next render.");
    } catch (err) {
      toastError(err, "save music settings");
    } finally {
      save.disabled = false;
    }
  };

  const sftSavedUnavailable = musicSettings.model === "xl_sft" && !sftReady;
  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "Music preferences"),
    el("div", { class: "panel-body" },
      sftSavedUnavailable ? el("div", { class: "warning-list" },
        el("div", { class: "witem crit" },
          "The saved model XL SFT is not installed in ComfyUI. Music generation would fail with a validation error — switch Model quality to XL Turbo or install acestep_v1.5_xl_sft_bf16.safetensors.",
        ),
      ) : null,
      el("div", { class: "pref-grid" },
        fieldBlock("Style", style, "Instrumental background in this style."),
        fieldBlock("Mood", mood, "Project-wide default; per-scene moods remain visible below."),
        fieldBlock("Instrumental (no vocals)", instrumental, "Pipeline default: instrumental, restrained, no vocals."),
        fieldBlock("Seed", seed, "Regenerate with unchanged inputs retains this seed."),
        fieldBlock("BPM", bpm, "Beats per minute."),
        fieldBlock("Movement length", movementSeconds, "Target length of each scored section in seconds (30–180). Scenes are grouped into movements this long; moods shift the boundaries."),
        fieldBlock("Key / scale", keyScale, "Musical key and scale."),
        fieldBlock("Time signature", timeSig, "Time signature."),
        fieldBlock("Language", language, "Lyrics language (instrumental tracks ignore this)."),
        fieldBlock("Enhanced audio planning", generateAudioCodes, "Higher-quality ACE LM/code-generation path."),
        fieldBlock("Model quality", modelSelect, "XL Turbo is the daily driver; SFT is slower."),
      ),
      el("div", { class: "row" }, save),
    ),
  );
}

/**
 * Generate/regenerate controls plus a live view of this project's music job.
 *
 * The panel tracks the newest non-terminal `music` job so the task survives a
 * page refresh, keeps the button disabled while it runs, and shows the last
 * failure's error. Regenerating sends `force: true` so the backend bypasses
 * the unchanged-fingerprint reuse path and actually submits to ComfyUI again.
 */
function actionPanel(project, isReady, turboMissing, hasMusic, initialJobs, reload) {
  const gen = generation;
  const generate = el("button", { class: "btn btn-primary", type: "button" },
    hasMusic ? "Regenerate music" : "Generate music");
  const status = el("div", { class: "stack" });

  /** Job kept between the queue response and the feed's first delivery. */
  let pendingJob = null;
  let hadActive = false;

  function syncJobState(jobs) {
    const mine = ((jobs || []).concat(pendingJob ? [pendingJob] : []))
      .filter((j) => j.stage === "music" && j.project_id === project.id);
    if (pendingJob && mine.some((j) => j.id === pendingJob.id && j !== pendingJob)) {
      pendingJob = null;
    }
    const active = mine.find((j) => !TERMINAL.includes(j.status)) || null;
    const failed = mine.find((j) => j.status === "failed") || null;

    const parts = [];
    if (active) {
      parts.push(el("div", { class: "row" },
        jobStatusBadge(active.status),
        el("span", { class: "small muted mono" }, `job ${shortId(active.id)}`),
      ));
      parts.push(progress(active.progress || 0));
      parts.push(el("div", { class: "muted small" },
        "The ACE-Step workflow was submitted to ComfyUI; generation runs until the audio is rendered.",
      ));
    } else if (!isReady) {
      parts.push(el("div", { class: "muted small" }, "ACE-Step ComfyUI is not ready."));
    } else if (turboMissing.length) {
      parts.push(el("div", { class: "muted small" },
        "Install missing Turbo files to enable generation.",
      ));
    }
    if (!active && failed && failed.error) {
      parts.push(el("div", { class: "warning-list" },
        el("div", { class: "witem crit" },
          el("span", { class: "small mono" }, failed.error),
        ),
      ));
    }
    status.replaceChildren(...parts);

    const settled = hadActive && !active;
    hadActive = Boolean(active);
    generate.disabled = !isReady || turboMissing.length > 0 || Boolean(active);
    generate.textContent = active
      ? "Generating…"
      : (hasMusic ? "Regenerate music" : "Generate music");
    // A tracked job just finished or failed — refresh once so the new audio
    // (or failure detail) replaces the stale asset panel.
    if (settled && reload) reload();
  }

  syncJobState(initialJobs);
  // The awaited fetches above may have outlasted this screen; only claim the
  // live-update hook if Music is still the active route (the generation
  // counter inside the hook then covers later navigations).
  if (parseRoute().name === "music") {
    registerLiveUpdate(() => {
      if (gen !== generation) return;
      syncJobState(state.jobs);
    });
  }

  generate.onclick = async () => {
    generate.disabled = true;
    try {
      pendingJob = await generateMusic(
        state.config, state.currentProjectId, { force: hasMusic },
      );
      toast(
        "good",
        hasMusic ? "Music regeneration queued" : "Music generation queued",
        `Job ${pendingJob.id.substring(0, 8)}… is running.`,
      );
    } catch (err) {
      toastError(err, hasMusic ? "regenerate music" : "start music generation");
    } finally {
      syncJobState(state.jobs);
    }
  };

  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, hasMusic ? "Regenerate" : "Generate"),
    el("div", { class: "panel-body stack" },
      el("div", { class: "row" }, generate),
      status,
    ),
  );
}

/**
 * Positional wrapper over the shared labeled-field helper (ui.js), which
 * associates the label with its control (for/aria-describedby).
 * @param {string} label
 * @param {HTMLElement} input
 * @param {string} [hint]
 * @returns {HTMLElement}
 */
function fieldBlock(label, input, hint) {
  return field({ label, input, hint });
}

function assetPanel(asset, stage) {
  const audioUrl = versionedAudioUrl(asset);
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Generated music"),
      el("span", { class: "spacer" }),
      stageChip("music", stage),
    ),
    el("div", { class: "panel-body" },
      el("dl", { class: "kv" },
        el("dt", {}, "File"), el("dd", { class: "mono" }, asset.filepath || "—"),
        el("dt", {}, "Backend"), el("dd", {}, asset.backend || "—"),
        el("dt", {}, "Model"), el("dd", {}, asset.model || "—"),
        el("dt", {}, "Workflow"), el("dd", {}, asset.workflow_version || "—"),
        el("dt", {}, "Seed"), el("dd", {}, asset.seed != null ? String(asset.seed) : "—"),
        el("dt", {}, "Created"), el("dd", {}, asset.created_at ? fmtDate(asset.created_at) : "—"),
      ),
      audioUrl
        ? el("audio", { controls: true, preload: "metadata", src: audioUrl })
        : el("p", { class: "muted small" }, "The music file is not currently available through the local API."),
    ),
  );
}

/**
 * Per-movement listing with individual regeneration.
 *
 * Each row plays its movement clip and offers a regenerate button that posts
 * movement_index to the generate endpoint: the backend reuses every other
 * movement's audio and only pays GPU cost for this one, then re-stitches
 * background.wav.
 */
function movementsPanel(movements) {
  function clock(seconds) {
    const total = Math.max(0, Math.round(seconds));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }

  const rows = movements.map((asset) => {
    const s = asset.settings || {};
    const index = s.movement_index != null ? Number(s.movement_index) : 0;
    const start = Number(s.start_seconds || 0);
    const duration = Number(s.duration_seconds || 0);
    const energy = Number(s.energy != null ? s.energy : 0.5);
    const audioUrl = versionedAudioUrl(asset);
    const regen = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Regenerate");
    regen.onclick = async () => {
      regen.disabled = true;
      try {
        const job = await generateMusic(state.config, state.currentProjectId, {
          force: true,
          movement_index: index,
        });
        toast(
          "good",
          `Movement ${index + 1} regeneration queued`,
          `Job ${job.id.substring(0, 8)}… is running; the stitched track updates when it finishes.`,
        );
      } catch (err) {
        toastError(err, `regenerate movement ${index + 1}`);
        regen.disabled = false;
      }
    };
    return el("div", { class: "row" },
      el("strong", {}, `#${index + 1}`),
      el("span", { class: "small muted mono" },
        `${clock(start)}–${clock(start + duration)} · ${Math.round(duration)}s`),
      el("span", { class: "small" }, s.mood || "—"),
      el("span", { class: "badge neutral" }, `${Math.round(energy * 100)}% energy`),
      audioUrl
        ? el("audio", { controls: true, preload: "none", src: audioUrl })
        : null,
      el("span", { class: "spacer" }),
      regen,
    );
  });

  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "Movements"),
    el("div", { class: "panel-body stack" },
      el("div", { class: "muted small" },
        "The soundtrack is scored as long musical movements that follow each scene's mood, then stitched with short dips into background.wav. Regenerating one movement reuses the others.",
      ),
      ...rows,
    ),
  );
}
