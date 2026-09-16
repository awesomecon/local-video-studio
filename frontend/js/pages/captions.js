/**
 * Captions screen: subtitle state for the current project.
 *
 *  - The pipeline derives SRT + ASS cues from the narration and writes them
 *    into the project directory; both are recorded as assets with
 *    role="captions" (AssetType.SUBTITLE). In a real render the local Whisper
 *    alignment backend additionally produces word-timings.json, recorded as
 *    role="caption_timing" (AssetType.METADATA).
 *  - Every alignment run rewrites the same live files and archives the
 *    previous run's (the superseded asset records are re-pointed at the
 *    archive files with settings.archived_at), so each run leaves a
 *    fingerprint: settings.input_audio_sha256, the hash of the narration
 *    audio the run aligned against. The screen groups the assets into those
 *    runs — "generations" — and shows the most recent one (the files a render
 *    consumes) as the current captions plus a collapsed history. Each run is
 *    labelled with the narration take it was aligned against (matched by the
 *    take asset's hash) so it is clear which captions belong to which voice.
 *  - The "Alignment model" panel shows the configured caption-alignment model
 *    and its honest readiness (mock / disabled / ready / not configured /
 *    dependency missing) from GET /api/captions/models — fully data-driven,
 *    no model IDs or names are hardcoded here.
 */

import { el, fmtDate } from "../dom.js";
import { state, needsProject } from "../state.js";
import { getProject, captionsModels, generateCaptions } from "../api.js";
import { loadingState, errorPanel, badge, toast, toastError, stageChip } from "../ui.js";
import { registerLiveUpdate } from "../app.js";
import { providerLabel } from "./voice.js";

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderCaptions(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Captions")),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject("Select a project in the top bar to see its captions."));
    return screen;
  }
  screen.append(captionsScreenBody());
  return screen;
}

/**
 * Both panels share one fetch pair (project snapshot + alignment model) and
 * one live hook, so a render run refreshes them together.
 * @returns {HTMLElement}
 */
function captionsScreenBody() {
  const alignmentBody = el("div", { class: "panel-body" });
  const captionsBody = el("div", { class: "panel-body" });

  /**
   * @param {{skeleton?: boolean}} [opts] — omit the skeleton on live refreshes
   */
  let inflight = 0; // last-write-wins sequence, same pattern as timeline.js
  /** True once both panels have rendered; a transient error must not replace
   *  them, but a first-load failure (only skeletons on screen) still shows
   *  the error panels with their Retry action. */
  let hasContent = false;
  let modelInfo = null;
  async function load({ skeleton = true } = {}) {
    const token = ++inflight;
    if (skeleton) {
      alignmentBody.replaceChildren(loadingState(2));
      captionsBody.replaceChildren(loadingState(3));
    }
    try {
      const [snap, fetchedModelInfo] = await Promise.all([
        getProject(state.config, state.currentProjectId),
        captionsModels(state.config),
      ]);
      if (token !== inflight) return;
      const assets = /** @type {import("../api.js").Asset[]} */ (snap.assets || []);
      const captions = assets.filter((a) => (a.settings || {}).role === "captions");
      const timings = assets.filter((a) => (a.settings || {}).role === "caption_timing");
      const takes = assets.filter((a) => (a.settings || {}).role === "narration_take");
      const stages = (/** @type {any} */ (snap.stage_state) || {}).stages || {};
      modelInfo = fetchedModelInfo;
      alignmentBody.replaceChildren(buildAlignment(fetchedModelInfo));
      captionsBody.replaceChildren(build(captions, timings, takes, stages.subtitles));
      hasContent = true;
    } catch (err) {
      if (token !== inflight || hasContent) return;
      const retry = el("button", { class: "btn", type: "button", onclick: () => load() }, "Retry");
      alignmentBody.replaceChildren(errorPanel(err, retry));
      captionsBody.replaceChildren(errorPanel(err, retry));
    }
  }

  const alignmentPanel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Alignment model"),
      el("span", { class: "spacer" }),
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => load() }, "Refresh"),
    ),
    alignmentBody,
  );
  const captionsPanel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Captions"),
      el("span", { class: "spacer" }),
      el("button", {
        class: "btn btn-primary btn-sm",
        type: "button",
        onclick: async (event) => {
          const button = /** @type {HTMLButtonElement} */ (event.currentTarget);
          button.disabled = true;
          try {
            const job = await generateCaptions(state.config, state.currentProjectId);
            toast("good", "Caption alignment queued", `Local ${modelInfo?.descriptor?.model_name || "Whisper"} · job ${job.id.slice(0, 8)}`);
            await load({ skeleton: false });
          } catch (err) {
            toastError(err, "align captions");
          } finally {
            button.disabled = false;
          }
        },
      }, "Align from narration"),
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => load() }, "Refresh"),
    ),
    captionsBody,
  );

  // Live path: the alignment readiness, subtitle assets, and stage state all
  // appear as the render runs.
  registerLiveUpdate(() => load({ skeleton: false }));
  load();
  return el("div", { class: "stack" }, alignmentPanel, captionsPanel);
}

/**
 * @param {import("../api.js").CaptionsModels} info
 * @returns {HTMLElement}
 */
function buildAlignment(info) {
  const descriptor = info.descriptor || /** @type {any} */ ({});
  const health = info.health || /** @type {any} */ ({});
  const modelLabel = [descriptor.model_name, descriptor.model_version]
    .filter(Boolean)
    .join(" · ");

  const parts = [
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, modelLabel || "No alignment model registered"),
      el("span", { class: "spacer" }),
      descriptor.quantization ? badge("neutral", descriptor.quantization) : null,
      descriptor.device ? badge("neutral", descriptor.device) : null,
    ),
  ];

  const vramNote = descriptor.device === "cuda" && descriptor.vram_required_gb
    ? `≈${descriptor.vram_required_gb} GiB free VRAM required while aligning`
    : "No VRAM required (CPU alignment).";

  let status;
  if (info.mock_mode) {
    status = badge("neutral", "mock — deterministic timings until a real render");
  } else if (!info.enabled) {
    status = badge("neutral", "disabled (backends.whisper.enabled=false)");
  } else if (health.status === "healthy") {
    status = badge("good", "ready");
  } else {
    status = badge(
      "warning",
      health.status === "incompatible" ? "dependency missing" : "not configured",
    );
  }

  parts.push(
    el("div", { class: "row" },
      status,
      el("span", { class: "spacer" }),
      el("span", { class: "muted small" }, vramNote),
    ),
  );

  const rows = [];
  const modelPath = info.model_path || health.model_path;
  if (modelPath) {
    rows.push(
      el("dt", {}, "Model path"),
      el("dd", { class: "mono" }, modelPath),
    );
  }
  if (rows.length) parts.push(el("dl", { class: "kv" }, ...rows));

  if (!info.mock_mode && info.enabled && health.status !== "healthy" && health.install_guidance) {
    parts.push(
      el("div", { class: "warning-list" },
        el("div", { class: "witem" }, health.install_guidance),
      ),
    );
  }

  parts.push(
    el("p", { class: "small muted" },
      "Loaded only while the subtitles stage aligns the narration, then released; Studio never downloads its weights."),
  );

  return el("div", { class: "stack" }, ...parts);
}

/**
 * One alignment run: the SRT/ASS/word-timings assets written together plus
 * the identity of the narration audio the run aligned against.
 * @typedef {Object} CaptionGeneration
 * @property {string} key
 * @property {import("../api.js").Asset | null} srt
 * @property {import("../api.js").Asset | null} ass
 * @property {import("../api.js").Asset | null} timings
 * @property {string | null} audioSha256 — fingerprint of the narration audio
 * @property {string | null} inputAudio — project-relative narration path
 * @property {string} createdAt — newest asset timestamp of the run
 * @property {boolean} archived — any asset re-pointed into the archive
 * @property {{label: string, createdAt: string} | null} take — matched narration take
 */

/**
 * Group caption assets into alignment runs ("generations").
 *
 * One run writes SRT + ASS (and, in a real render, word timings) against the
 * same live paths and archives the previous run's files; every asset of a run
 * records the fingerprint of the narration audio it aligned against
 * (settings.input_audio_sha256), which is the run's identity. Assets without
 * a fingerprint (mock timings) each form their own generation. The most
 * recent generation is the current one — its files are the live caption
 * files a render consumes — the rest are history, newest first.
 *
 * @param {import("../api.js").Asset[]} captions — role "captions" (SRT/ASS)
 * @param {import("../api.js").Asset[]} timings — role "caption_timing"
 * @param {import("../api.js").Asset[]} [narrationTakes] — role "narration_take"
 * @returns {{current: CaptionGeneration | null, history: CaptionGeneration[]}}
 */
export function groupCaptionGenerations(captions, timings, narrationTakes = []) {
  /** @type {Map<string, CaptionGeneration>} */
  const byKey = new Map();
  /** @type {Map<string, import("../api.js").Asset>} */
  const takeByHash = new Map(
    narrationTakes
      .filter((take) => typeof take.hash === "string" && take.hash)
      .map((take) => [take.hash, take]));
  const place = (asset) => {
    const settings = asset.settings || {};
    const sha = typeof settings.input_audio_sha256 === "string" && settings.input_audio_sha256
      ? settings.input_audio_sha256
      : null;
    const key = sha || `asset:${asset.id}`;
    let generation = byKey.get(key);
    if (!generation) {
      generation = {
        key,
        srt: null, ass: null, timings: null,
        audioSha256: sha,
        inputAudio: typeof settings.input_audio === "string" ? settings.input_audio : null,
        createdAt: "",
        archived: false,
        take: null,
      };
      byKey.set(key, generation);
      if (sha) {
        const take = takeByHash.get(sha);
        generation.take = take
          ? { label: providerLabel((take.settings || {}).provider || take.backend), createdAt: take.created_at }
          : null;
      }
    }
    const kind = settings.role === "caption_timing" ? "timings"
      : (asset.filepath || "").toLowerCase().endsWith(".srt") ? "srt"
      : (asset.filepath || "").toLowerCase().endsWith(".ass") ? "ass"
      : null;
    if (kind) generation[kind] = asset;
    if ((asset.created_at || "") > generation.createdAt) generation.createdAt = asset.created_at || "";
    generation.archived = generation.archived || Boolean(settings.archived_at);
  };
  for (const asset of [...captions, ...timings]) place(asset);
  const generations = [...byKey.values()];
  generations.sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1));
  const [current, ...history] = generations;
  return { current: current || null, history };
}

/**
 * @param {import("../api.js").Asset[]} captions
 * @param {import("../api.js").Asset[]} timings
 * @param {import("../api.js").Asset[]} narrationTakes
 * @param {{status?: string, outputs?: string[]}|undefined} stage
 * @returns {HTMLElement}
 */
function build(captions, timings, narrationTakes, stage) {
  const audioDerived = captions.some((caption) => caption.settings?.audio_derived);
  const { current, history } = groupCaptionGenerations(captions, timings, narrationTakes);
  const parts = [
    el("div", { class: "warning-list" },
      el("div", { class: "witem" },
        audioDerived
          ? "Captions are aligned to word timestamps from the generated narration audio. Generated SRT and ASS files can be opened from their local API links."
          : "Captions use deterministic mock timings until the local alignment model runs in a real render — see the alignment model panel above.",
      ),
    ),
  ];

  if (!current) {
    parts.push(el("div", { class: "muted small" },
      "No aligned captions yet. Generate or select narration, then choose Align from narration.",
    ));
  } else {
    parts.push(currentGenerationCard(current));
    if (history.length) parts.push(historyPanel(history));
  }

  parts.push(
    el("div", { class: "row" },
      stageChip("subtitles", stage),
      el("span", { class: "spacer" }),
      el("span", { class: "muted small" }, "Alignment writes SRT, styled ASS, and portable word timings into the project."),
    ),
  );

  return el("div", { class: "stack" }, ...parts);
}

/**
 * The current (most recent) generation: its files are the live caption files
 * a render consumes. Provenance (model, version, quantization, workflow,
 * seed) is shared by every file of the run, so it is shown once.
 * @param {CaptionGeneration} generation
 * @returns {HTMLElement}
 */
function currentGenerationCard(generation) {
  const source = generation.srt || generation.ass || generation.timings;
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Current captions"),
      badge("good", "current", false),
      el("span", { class: "spacer" }),
      el("span", { class: "muted small" }, generation.createdAt ? fmtDate(generation.createdAt) : ""),
    ),
    el("div", { class: "panel-body" },
      el("p", { class: "muted small" }, alignedToLine(generation)),
      el("div", { class: "stack caption-files" },
        captionFileRow("SRT", generation.srt),
        captionFileRow("ASS", generation.ass),
        captionFileRow("Word timings", generation.timings),
      ),
      provenanceRows(source),
    ),
  );
}

/**
 * "Aligned to …" line: the narration take a run aligned against (the take
 * asset whose hash matches the run's audio fingerprint), falling back to the
 * recorded narration path. The short fingerprint keeps runs distinct when
 * several aligned to takes of the same model.
 * @param {CaptionGeneration} generation
 * @returns {string}
 */
function alignedToLine(generation) {
  const label = generation.take
    ? `${generation.take.label} take · ${generation.take.createdAt ? fmtDate(generation.take.createdAt) : "date unknown"}`
    : generation.inputAudio || "the project narration";
  const sha = generation.audioSha256 ? ` · audio ${generation.audioSha256.slice(0, 8)}…` : "";
  return `Aligned to: ${label}${sha}`;
}

/**
 * One file row of a generation: kind tag, path, created, Open link.
 * @param {string} label — "SRT" | "ASS" | "Word timings"
 * @param {import("../api.js").Asset | null} asset
 * @returns {HTMLElement}
 */
function captionFileRow(label, asset) {
  if (!asset) {
    return el("div", { class: "caption-file-row" },
      el("span", { class: "tag" }, label),
      el("span", { class: "muted small" }, "not produced by this run"),
    );
  }
  return el("div", { class: "caption-file-row" },
    el("span", { class: "tag" }, label),
    el("span", { class: "mono small" }, asset.filepath || "—"),
    el("span", { class: "muted small" }, asset.created_at ? fmtDate(asset.created_at) : ""),
    el("span", { class: "spacer" }),
    asset.url
      ? el("a", { class: "btn btn-ghost btn-sm", href: asset.url, target: "_blank", rel: "noopener" }, `Open ${label}`)
      : el("span", { class: "muted small" }, "file not available"),
  );
}

/**
 * Shared provenance of one run (identical across its files).
 * @param {import("../api.js").Asset | null} asset — any asset of the run
 * @returns {HTMLElement}
 */
function provenanceRows(asset) {
  if (!asset) return el("span");
  const settings = asset.settings || {};
  const rows = [
    el("dt", {}, "Model"), el("dd", {}, asset.model || "—"),
    el("dt", {}, "Version"), el("dd", {}, asset.model_version || "—"),
    el("dt", {}, "Quantization"), el("dd", {}, asset.quantization || "—"),
    el("dt", {}, "Workflow"), el("dd", { class: "mono" }, asset.workflow_version || "—"),
  ];
  if (settings.language) {
    const probability = settings.language_probability;
    rows.push(
      el("dt", {}, "Language"),
      el("dd", {}, typeof probability === "number"
        ? `${settings.language} (${(probability * 100).toFixed(0)}%)`
        : String(settings.language)),
    );
  }
  if (asset.seed != null) rows.push(el("dt", {}, "Seed"), el("dd", {}, String(asset.seed)));
  return el("dl", { class: "kv" }, ...rows);
}

/**
 * Collapsed history of previous generations: one slim row per run with its
 * date, the narration take it aligned against, and its files (still
 * openable through their archive links, or marked archived when not).
 * @param {CaptionGeneration[]} history
 * @returns {HTMLElement}
 */
function historyPanel(history) {
  const rows = history.map((generation) => {
    const files = [["SRT", generation.srt], ["ASS", generation.ass], ["Word timings", generation.timings]];
    return el("div", { class: "caption-history-row" },
      el("span", { class: "muted small" }, generation.createdAt ? fmtDate(generation.createdAt) : "—"),
      el("span", { class: "small" }, generation.take
        ? generation.take.label
        : generation.inputAudio ? generation.inputAudio.split("/").pop() : "narration"),
      generation.audioSha256 ? el("span", { class: "tag" }, generation.audioSha256.slice(0, 8)) : null,
      el("span", { class: "spacer" }),
      ...files.map(([name, asset]) => !asset
        ? null
        : asset.url
          ? el("a", { class: "btn btn-ghost btn-sm", href: asset.url, target: "_blank", rel: "noopener" }, `Open ${name}`)
          : el("span", { class: "muted small" }, `${name} archived`)),
      );
  });
  return el("div", { class: "panel" },
    el("details", { class: "caption-history" },
      el("summary", {},
        `${history.length} previous generation${history.length === 1 ? "" : "s"} — aligned to earlier narration takes`),
      ...rows,
    ),
  );
}
