/**
 * Models & system status screen: what this machine can run, and what the
 * backend has registered.
 *
 *  - Everything is data-driven from GET /api/system/status and
 *    GET /api/models — no model IDs, GPU names, or versions are hardcoded.
 *  - Environment compatibility (classification, recommendations, version
 *    conflicts, FFmpeg/ffprobe/git, Python/PyTorch/CUDA, disks), live GPU
 *    snapshot, registered generator backends (with heavyweight flag and
 *    VRAM requirement), and the configured ports.
 *  - Port 1234 is the external local LLM service: shown as informational only.
 *    Nothing in this UI binds, starts, or stops any port.
 */

import { el } from "../dom.js";
import { state } from "../state.js";
import {
  health, systemStatus, models, projectModels, freeComfyMemory, unloadIdeogram4, llmModels, selectLlmModel,
} from "../api.js";
import { loadingState, errorPanel, badge, icon, toast, toastError } from "../ui.js";
import { parseRoute } from "../router.js";

const CLASSIFICATION = /** @type {Record<string, {kind: string, label: string}>} */ ({
  compatible_existing_environment: { kind: "good", label: "Compatible environment" },
  compatible_with_warnings: { kind: "warning", label: "Compatible with warnings" },
  incompatible_environment_requiring_isolation: { kind: "critical", label: "Isolation required" },
});

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderModels(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Models & System Status")),
  );
  if (state.connection !== "online") {
    screen.append(errorPanel(
      { kind: "offline", message: "Backend offline — system status is unavailable." },
      el("button", { class: "btn", type: "button", onclick: renderModelsRefresh }, "Retry"),
    ));
    return screen;
  }
  screen.append(modelsPanel());
  return screen;
}

/**
 * Re-render from fresh backend data (offline retry path): re-check health
 * first so the badge reflects the current backend state.
 */
async function renderModelsRefresh() {
  try {
    const h = await health(state.config);
    state.connection = "online";
    state.healthMode = h.mode;
  } catch {
    state.connection = "offline";
  }
  // The awaited health check may outlive this screen; never clobber whatever
  // route the user navigated to in the meantime.
  if (parseRoute().name !== "models") return;
  const content = document.querySelector(".content");
  if (content) content.replaceChildren(renderModels({ name: "models", param: null }));
}

/**
 * @returns {HTMLElement}
 */
function modelsPanel() {
  const body = el("div", { class: "panel-body" });
  const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Refresh");
  refreshBtn.onclick = () => load(body);

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "System diagnostics"),
      el("span", { class: "spacer" }),
      refreshBtn,
    ),
    body,
  );

  /**
   * @param {HTMLElement} region
   */
  async function load(region) {
    region.replaceChildren(loadingState(6));
    try {
      const [sys, list] = await Promise.all([
        systemStatus(state.config),
        state.currentProjectId
          ? projectModels(state.config, state.currentProjectId)
          : models(state.config),
      ]);
      region.replaceChildren(buildAll(sys, list));
    } catch (err) {
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => load(region) }, "Retry"),
      ));
    }
  }

  load(body);
  return panel;
}

/**
 * @param {import("../api.js").SystemStatus} sys
 * @param {import("../api.js").ModelList} list
 * @returns {HTMLElement}
 */
function buildAll(sys, list) {
  const env = sys.environment;
  const parts = [
    classificationPanel(env),
    runtimePanel(env),
    gpuPanel(sys),
    h3ReadinessPanel(sys),
    comfyMemoryPanel(sys, list),
    routerModelPanel(),
    backendsPanel(list, sys),
    portsPanel(sys),
  ];
  return el("div", { class: "stack" }, ...parts);
}

/** MiniMax H3 cold-load readiness from the dispatch policy's own threshold. */
function h3ReadinessPanel(sys) {
  const ready = sys.h3_readiness;
  if (!ready) return null;
  const free = ready.free_gib == null ? "unknown" : `${ready.free_gib.toFixed(1)} GiB`;
  const total = ready.total_gib == null ? "unknown" : `${ready.total_gib.toFixed(1)} GiB`;
  const stateBadge = ready.error
    ? badge("critical", "inspection failed")
    : ready.must_free_vram
      ? badge("warning", "free VRAM before H3")
      : badge("good", ready.cold_load_required ? "ready for cold load" : "resident H3 reusable");
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "MiniMax H3 readiness"),
      el("span", { class: "spacer" }), stateBadge,
    ),
    el("div", { class: "panel-body stack" },
      el("dl", { class: "kv" },
        el("dt", {}, "Free / total VRAM"), el("dd", {}, `${free} / ${total}`),
        el("dt", {}, "Cold-load threshold"), el("dd", {}, `${ready.threshold_gib} GiB`),
        el("dt", {}, "Resident ComfyUI family"),
        el("dd", { class: "mono" }, ready.resident_comfy_family || "none"),
      ),
      ready.error ? el("p", { class: "small critical" }, ready.error) : null,
      ready.must_free_vram
        ? el("p", { class: "small muted" },
            "Release cached ComfyUI models here or unload the external LLM in its own router UI, then retry.")
        : null,
    ),
  );
}

/** Router model selection belongs with model operation, not general settings. */
function routerModelPanel() {
  const region = el("div", { class: "panel-body" });
  const refresh = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Re-check");
  refresh.onclick = () => load();
  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Script model (local LLM router)"),
      el("span", { class: "spacer" }), refresh,
    ),
    region,
  );

  async function load() {
    region.replaceChildren(loadingState(2));
    try {
      const info = await llmModels(state.config, state.currentProjectId);
      const select = el("select", { class: "input" },
        el("option", { value: "" }, "Choose a model…"),
        ...info.models.map((model) =>
          el("option", { value: model.id, selected: model.id === info.selected_model }, model.id)),
      );
      const save = el("button", {
        class: "btn btn-primary btn-sm",
        type: "button",
        disabled: !state.currentProjectId || !info.models.length || !info.selected_model,
      }, "Use for this project");
      select.onchange = () => { save.disabled = !state.currentProjectId || !select.value; };
      save.onclick = async () => {
        save.disabled = true;
        try {
          await selectLlmModel(state.config, {
            model: select.value,
            project_id: state.currentProjectId,
          });
          toast("good", "Script model saved for this project");
          load();
        } catch (err) {
          toastError(err, "select local script model");
          save.disabled = false;
        }
      };
      region.replaceChildren(
        el("div", { class: "stack" },
          el("dl", { class: "kv" },
            el("dt", {}, "Router"), el("dd", { class: "mono" }, info.endpoint),
            el("dt", {}, "Project selection"), el("dd", {},
              info.selected_model ? badge("good", info.selected_model) : badge("warning", "required")),
            el("dt", {}, "Router currently resolves"), el("dd", { class: "mono" }, info.resolved_model || "—"),
          ),
          state.currentProjectId
            ? el("div", { class: "field" },
                el("label", {}, "Use this router model for script and planning"), select,
                el("div", { class: "hint" },
                  "A model must be explicitly selected before Studio sends any script-generation request."),
                save,
              )
            : el("p", { class: "small muted" },
                "Select a project first to choose and persist its script model."),
          el("p", { class: "small muted" },
            "The router owns loading and unloading its model. Studio only lists and selects models; "
            + "it never starts, stops, or unloads the router service."),
        ),
      );
    } catch (err) {
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: load }, "Retry"),
      ));
    }
  }

  load();
  return panel;
}

/** ComfyUI residency policy and explicit safe release control. */
function comfyMemoryPanel(sys, list) {
  const resident = sys.comfyui_resident_backend;
  const release = el("button", {
    class: "btn btn-sm",
    type: "button",
  }, "Release ComfyUI VRAM");
  release.onclick = async () => {
    release.disabled = true;
    release.textContent = "Releasing…";
    try {
      await freeComfyMemory(state.config);
      toast("good", "ComfyUI VRAM released");
      renderModelsRefresh();
    } catch (err) {
      toastError(err, "release ComfyUI VRAM");
      release.disabled = false;
      release.textContent = "Release ComfyUI VRAM";
    }
  };
  const hasIdeogram = Boolean(list.models?.ideogram4_local_comfyui);
  const unloadIdeogram = el("button", {
    class: "btn btn-sm",
    type: "button",
    disabled: !hasIdeogram,
    title: hasIdeogram
      ? "Release Ideogram 4 model memory and stop only the worker started by this Studio."
      : "Ideogram 4 is not registered in this Studio instance.",
  }, "Unload Ideogram 4");
  unloadIdeogram.onclick = async () => {
    unloadIdeogram.disabled = true;
    unloadIdeogram.textContent = "Unloading…";
    try {
      const result = await unloadIdeogram4(state.config);
      toast("good", result.stopped_owned_worker
        ? "Ideogram 4 unloaded and its Studio-owned worker stopped"
        : "Ideogram 4 model memory released");
      renderModelsRefresh();
    } catch (err) {
      toastError(err, "unload Ideogram 4");
      unloadIdeogram.disabled = false;
      unloadIdeogram.textContent = "Unload Ideogram 4";
    }
  };
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Model memory"),
      el("span", { class: "spacer" }),
      resident
        ? badge("accent", `Resident: ${resident}`)
        : badge("neutral", "No Studio-managed model"),
    ),
    el("div", { class: "panel-body stack" },
      el("p", { class: "small" },
        "Consecutive scenes using the same ComfyUI model reuse it. Switching model families "
        + "releases the current family before loading the next one."),
      el("p", { class: "small muted" },
        "The Ideogram action also stops the worker only when this Studio started it. "
        + "The script LLM on port 1234 remains externally managed."),
      el("div", { class: "row" }, release, hasIdeogram ? unloadIdeogram : null),
    ),
  );
}

/* --- environment classification ------------------------------------------- */

/**
 * @param {import("../api.js").EnvironmentReport} env
 * @returns {HTMLElement}
 */
function classificationPanel(env) {
  const c = CLASSIFICATION[env.classification] || { kind: "neutral", label: env.classification };
  const items = [];
  for (const conflict of env.version_conflicts || []) {
    items.push(el("div", { class: "witem crit" }, icon("alert", 16), el("span", {}, `Version conflict: ${conflict}`)));
  }
  for (const warning of env.warnings || []) {
    items.push(el("div", { class: "witem" }, icon("alert", 16), el("span", {}, warning)));
  }
  for (const rec of env.recommendations || []) {
    items.push(el("div", { class: "witem" }, icon("info", 16), el("span", { class: "small" }, `Suggested: ${rec}`)));
  }

  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Environment compatibility"),
      el("span", { class: "spacer" }),
      badge(c.kind, c.label),
    ),
    items.length
      ? el("div", { class: "panel-body" }, el("div", { class: "warning-list" }, ...items))
      : el("div", { class: "panel-body" },
          el("span", { class: "muted small" }, "No warnings, conflicts, or recommendations."),
        ),
  );
}

/* --- runtime environment --------------------------------------------------- */

/**
 * @param {import("../api.js").EnvironmentReport} env
 * @returns {HTMLElement}
 */
function runtimePanel(env) {
  const torch = env.torch;
  const tool = (label, toolInfo) => {
    const ok = toolInfo && toolInfo.available;
    return [
      el("dt", {}, label),
      el("dd", {},
        ok ? badge("good", toolInfo.version ? `v${toolInfo.version}` : "available")
          : badge("critical", toolInfo && toolInfo.error ? `missing — ${toolInfo.error}` : "not found"),
        toolInfo && toolInfo.path ? el("span", { class: "small muted mono" }, ` (${toolInfo.path})`) : null,
        toolInfo && toolInfo.source && toolInfo.source !== "system"
          ? el("span", { class: "small muted" }, ` [${toolInfo.source}]`)
          : null,
      ),
    ];
  };

  return el("div", { class: "panel" },
    el("div", { class: "panel-title" }, "Runtime environment"),
    el("div", { class: "panel-body" },
      el("dl", { class: "kv" },
        el("dt", {}, "Python"),
        el("dd", {}, `${env.python_version} `, el("span", { class: "small muted mono" }, env.python_executable)),
        el("dt", {}, "Operating system"), el("dd", {}, env.operating_system),
        el("dt", {}, "System RAM"), el("dd", {}, `${env.system_ram_gb.toFixed(1)} GiB`),
        el("dt", {}, "PyTorch"),
        el("dd", {},
          torch.installed
            ? el("div", { class: "stack" },
                el("span", {}, `${torch.version} · CUDA build ${torch.cuda_runtime || "n/a"}`),
                torch.cuda_probed
                  ? torch.cuda_available
                    ? badge("good", `CUDA device available (${torch.cuda_device_name || "device 0"})`)
                    : badge("warning", `CUDA device unavailable to this Studio process${torch.cuda_error ? ` (${torch.cuda_error})` : ""}`)
                  : badge("neutral", "CUDA device was not probed"),
              )
            : badge("critical", torch.import_error ? `not importable — ${torch.import_error}` : "not installed"),
        ),
        ...tool("FFmpeg", env.ffmpeg),
        ...tool("ffprobe", env.ffprobe),
        ...tool("git", env.git),
        ...(env.nvidia_gpus || []).length
          ? [
              el("dt", {}, "NVIDIA GPUs"),
              el("dd", {},
                el("div", { class: "diagnostic-list" },
                  ...env.nvidia_gpus.map((g) =>
                    el("div", { class: "drow" },
                      el("span", { class: "dk" }, g.name),
                      el("span", {},
                        `driver ${g.driver_version || "?"} · ${g.total_vram_gb.toFixed(1)} GiB`,
                        g.free_vram_gb != null ? ` · ${g.free_vram_gb.toFixed(1)} GiB free` : "",
                      ),
                    ))),
              ),
            ]
          : [el("dt", {}, "NVIDIA GPUs"), el("dd", { class: "muted" }, "none detected")],
      ),
    ),
  );
}

/* --- live GPU snapshot ------------------------------------------------------ */

/**
 * @param {import("../api.js").SystemStatus} sys
 * @returns {HTMLElement}
 */
function gpuPanel(sys) {
  const gpu = sys.gpu || /** @type {any} */ ({});
  const devices = gpu.devices || [];
  const minFree = gpu.minimum_free_vram_gb;

  if (gpu.error) {
    return el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "GPU (live)"),
      el("div", { class: "panel-body" },
        el("div", { class: "warning-list" },
          el("div", { class: "witem crit" },
            icon("alert", 16),
            el("span", {}, `GPU inspection failed: ${gpu.error.message || "unknown error"}`),
          ),
        ),
      ),
    );
  }

  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "GPU (live)"),
      el("span", { class: "spacer" }),
      gpu.active_backend
        ? badge("accent", `active: ${gpu.active_backend}`)
        : el("span", { class: "muted small" }, "no active backend"),
    ),
    el("div", { class: "panel-body" },
      devices.length
        ? el("div", { class: "table-wrap" },
            el("table", { class: "data" },
              el("thead", {}, el("tr", {},
                el("th", {}, "GPU"), el("th", {}, "Total"), el("th", {}, "Used"), el("th", {}, "Free"), el("th", {}, "Heavy jobs"))),
              el("tbody", {},
                ...devices.map((d) => {
                  const tight = typeof minFree === "number" && d.free_gb < minFree;
                  return el("tr", {},
                    el("td", {}, d.name),
                    el("td", { class: "num" }, `${d.total_gb.toFixed(1)} GiB`),
                    el("td", { class: "num" }, `${d.used_gb.toFixed(1)} GiB`),
                    el("td", { class: "num" }, `${d.free_gb.toFixed(1)} GiB`),
                    el("td", {},
                      typeof minFree !== "number" ? el("span", { class: "muted" }, "—")
                      : tight ? badge("warning", `${minFree} GiB required`)
                      : badge("good", `needs ${minFree} GiB`),
                    ),
                  );
                })),
            ),
          )
        : el("span", { class: "muted small" }, "No GPUs reported (nvidia-smi unavailable)."),
    ),
  );
}

/* --- registered backends ---------------------------------------------------- */

/**
 * @param {import("../api.js").ModelList} list
 * @param {import("../api.js").SystemStatus} sys
 * @returns {HTMLElement}
 */
function backendsPanel(list, sys) {
  const entries = Object.entries(list.models || {});
  const readiness = heavyReadiness(entries, sys);
  const runtime = list.runtime || {};
  const rows = entries.map(([key, descriptor]) => {
    const stateInfo = runtime[key] || {};
    const stateKind = stateInfo.state === "resident" || stateInfo.state === "selected"
      ? "good"
      : stateInfo.state === "selection_required" || stateInfo.state === "disabled"
        ? "warning" : "neutral";
    const action = (stateInfo.actions || []).includes("release")
      ? el("span", { class: "small muted" }, "Release above")
      : (stateInfo.actions || []).includes("select")
        ? el("span", { class: "small muted" }, "Choose above")
        : el("span", { class: "small muted" }, "Automatic");
    return el("tr", {},
      el("td", {},
        el("div", { class: "mono" }, key),
        descriptor.backend_name !== key
          ? el("div", { class: "small muted" }, descriptor.backend_name)
          : null,
        descriptor.heavyweight
          ? el("span", { class: "small" }, badge("serious", "heavyweight"))
          : null,
      ),
      el("td", {}, descriptor.model_name || "—"),
      el("td", {},
        descriptor.model_version || "—",
        descriptor.quantization
          ? el("span", { class: "small muted" }, ` (${descriptor.quantization})`)
          : null,
      ),
      el("td", {}, descriptor.device || "—"),
      el("td", { class: "num" },
        descriptor.vram_required_gb > 0 ? `${descriptor.vram_required_gb} GiB` : "—"),
      el("td", {},
        badge(stateKind, stateInfo.state || "unknown"),
        stateInfo.detail ? el("div", { class: "small muted" }, stateInfo.detail) : null,
      ),
      el("td", { class: "small" }, stateInfo.ownership || "—"),
      el("td", {}, action),
      el("td", {},
        (descriptor.capabilities || []).length
          ? el("div", { class: "badge-row" },
              ...descriptor.capabilities.map((capability) =>
                badge("neutral", capability, false)))
          : el("span", { class: "muted" }, "—"),
      ),
    );
  });

  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Model backends and runtime state"),
      el("span", { class: "spacer" }),
      readiness,
    ),
    entries.length
      ? el("div", { class: "table-wrap" },
          el("table", { class: "data" },
            el("thead", {}, el("tr", {},
              el("th", {}, "Backend"), el("th", {}, "Model"), el("th", {}, "Version"),
              el("th", {}, "Device"), el("th", {}, "VRAM"), el("th", {}, "State"),
              el("th", {}, "Owner"), el("th", {}, "Action"), el("th", {}, "Capabilities"))),
            el("tbody", {}, ...rows),
          ),
        )
      : el("div", { class: "panel-body" },
          el("span", { class: "muted small" }, "No generator backends registered."),
        ),
  );
}

/**
 * Heavy-job readiness: can the heaviest registered backend run on this
 * machine right now?
 * @param {[string, import("../api.js").BackendDescriptor][]} entries
 * @param {import("../api.js").SystemStatus} sys
 * @returns {HTMLElement}
 */
function heavyReadiness(entries, sys) {
  const heavy = entries
    .map(([, d]) => d)
    .filter((d) => d.heavyweight)
    .sort((a, b) => (b.vram_required_gb || 0) - (a.vram_required_gb || 0));
  if (!heavy.length) {
    return badge("neutral", "no heavyweight backends");
  }
  const required = Math.max(heavy[0].vram_required_gb || 0, 0);
  const devices = (sys.gpu && sys.gpu.devices) || [];
  if (!devices.length) {
    return badge("critical", `GPU required (${required} GiB)`);
  }
  const maxFree = Math.max(...devices.map((d) => d.free_gb));
  return maxFree >= required
    ? badge("good", `ready — ${maxFree.toFixed(1)} GiB free of ${required} GiB needed`)
    : badge("warning", `not ready — ${maxFree.toFixed(1)} GiB free, ${required} GiB needed`);
}

/* --- ports & mode ------------------------------------------------------------ */

/**
 * @param {import("../api.js").SystemStatus} sys
 * @returns {HTMLElement}
 */
function portsPanel(sys) {
  const ports = sys.ports || /** @type {any} */ ({});
  const row = (label, port, note) => el("tr", {},
    el("td", {}, el("span", { class: "mono" }, label)),
    el("td", { class: "num mono" }, typeof port === "number" ? String(port) : "—"),
    el("td", { class: "small muted" }, note),
  );
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Ports & pipeline mode"),
      el("span", { class: "spacer" }),
      sys.mock_mode ? badge("warning", "mock mode") : badge("neutral", "local mode"),
    ),
    el("div", { class: "table-wrap" },
      el("table", { class: "data" },
        el("tbody", {},
          row("LLM server (external)", ports.llm_external,
            "External local LLM service — informational only; this app never binds, starts, or stops it."),
          row("Studio API (this connection)", ports.backend_effective ?? ports.backend_configured,
            "Observed from this request; FastAPI is bound to 127.0.0.1."),
          row("Studio API (configured default)", ports.backend_configured,
            "Used by the documented local startup command."),
          row("Frontend", ports.frontend_configured, "Static file server (bound to 127.0.0.1)."),
          row("ComfyUI (external)", ports.comfyui_external, "Optional local ComfyUI service."),
        )),
    ),
    el("div", { class: "panel-body" },
      el("span", { class: "muted small" },
        `Queued jobs: ${sys.queued_jobs ?? 0}.`,
      ),
    ),
  );
}
