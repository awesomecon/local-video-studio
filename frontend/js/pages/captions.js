/**
 * Captions screen: subtitle state for the current project.
 *
 *  - The pipeline derives SRT + ASS cues from the narration and writes them
 *    into the project directory; both are recorded as assets with
 *    role="captions" (AssetType.SUBTITLE). In a real render the local Whisper
 *    alignment backend additionally produces word-timings.json, recorded as
 *    role="caption_timing" (AssetType.METADATA).
 *  - The "Alignment model" panel shows the configured caption-alignment model
 *    and its honest readiness (mock / disabled / ready / not configured /
 *    dependency missing) from GET /api/captions/models — fully data-driven,
 *    no model IDs or names are hardcoded here.
 *  - The Captions panel shows the real metadata (file, model, version,
 *    quantization, workflow, seed, created) for each caption file and the
 *    word-timings output, the subtitles-stage state, and links to open the
 *    local generated files.
 */

import { el, fmtDate } from "../dom.js";
import { state, needsProject } from "../state.js";
import { getProject, captionsModels, generateCaptions } from "../api.js";
import { loadingState, errorPanel, badge, toast, toastError, stageChip } from "../ui.js";
import { registerLiveUpdate } from "../app.js";

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
      const stages = (/** @type {any} */ (snap.stage_state) || {}).stages || {};
      modelInfo = fetchedModelInfo;
      alignmentBody.replaceChildren(buildAlignment(fetchedModelInfo));
      captionsBody.replaceChildren(build(captions, timings, stages.subtitles));
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
 * @param {import("../api.js").Asset[]} captions
 * @param {import("../api.js").Asset[]} timings
 * @param {{status?: string, outputs?: string[]}|undefined} stage
 * @returns {HTMLElement}
 */
function build(captions, timings, stage) {
  const audioDerived = captions.some((caption) => caption.settings?.audio_derived);
  const parts = [
    el("div", { class: "warning-list" },
      el("div", { class: "witem" },
        audioDerived
          ? "Captions are aligned to word timestamps from the generated narration audio. Generated SRT and ASS files can be opened from their local API links."
          : "Captions use deterministic mock timings until the local alignment model runs in a real render — see the alignment model panel above.",
      ),
    ),
  ];

  if (timings.length) {
    for (const asset of timings) parts.push(timingBlock(asset));
  }

  if (captions.length) {
    for (const asset of captions) parts.push(fileBlock(asset));
  } else {
    parts.push(el("div", { class: "muted small" },
      "No aligned captions yet. Generate or select narration, then choose Align from narration.",
    ));
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
 * The portable word-timings output of the alignment model — its provenance
 * (model, quantization, workflow, detected language, input audio) is what a
 * real render actually produced.
 * @param {import("../api.js").Asset} asset
 * @returns {HTMLElement}
 */
function timingBlock(asset) {
  const settings = asset.settings || {};
  const rows = [
    el("dt", {}, "File"), el("dd", { class: "mono" }, asset.filepath || "—"),
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
  if (settings.input_audio) {
    rows.push(
      el("dt", {}, "Input audio"),
      el("dd", { class: "mono" }, String(settings.input_audio)),
    );
  }
  if (settings.input_audio_sha256) {
    rows.push(
      el("dt", {}, "Audio SHA-256"),
      el("dd", { class: "mono" }, `${String(settings.input_audio_sha256).slice(0, 16)}…`),
    );
  }
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Word timings"),
      el("span", { class: "spacer" }),
      el("span", { class: "muted small mono" }, asset.hash || ""),
    ),
    el("div", { class: "panel-body" },
      el("dl", { class: "kv" }, ...rows),
      asset.url
        ? el("a", { class: "btn btn-ghost btn-sm", href: asset.url, target: "_blank", rel: "noopener" }, "Open word-timings.json")
        : el("span", { class: "muted small" }, "Timing file is not currently available."),
    ),
  );
}

/**
 * @param {import("../api.js").Asset} asset
 * @returns {HTMLElement}
 */
function fileBlock(asset) {
  const kind = (asset.filepath || "").toLowerCase().endsWith(".ass") ? "ASS" : "SRT";
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, `${kind} captions`),
      el("span", { class: "spacer" }),
      el("span", { class: "muted small mono" }, asset.hash || ""),
    ),
    el("div", { class: "panel-body" },
      el("dl", { class: "kv" },
        el("dt", {}, "File"), el("dd", { class: "mono" }, asset.filepath || "—"),
        el("dt", {}, "Model"), el("dd", {}, asset.model || "—"),
        el("dt", {}, "Version"), el("dd", {}, asset.model_version || "—"),
        el("dt", {}, "Quantization"), el("dd", {}, asset.quantization || "—"),
        el("dt", {}, "Workflow"), el("dd", { class: "mono" }, asset.workflow_version || "—"),
        el("dt", {}, "Seed"), el("dd", {}, asset.seed != null ? String(asset.seed) : "—"),
        el("dt", {}, "Created"), el("dd", {}, asset.created_at ? fmtDate(asset.created_at) : "—"),
      ),
      asset.url
        ? el("a", { class: "btn btn-ghost btn-sm", href: asset.url, target: "_blank", rel: "noopener" }, `Open ${kind}`)
        : el("span", { class: "muted small" }, "Caption file is not currently available."),
    ),
  );
}
