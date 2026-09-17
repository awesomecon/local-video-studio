/**
 * System status screen: the machine and environment the studio runs on.
 *
 *  - Everything is data-driven from GET /api/system/status and
 *    GET /api/projects — no tool names or versions are hardcoded.
 *  - Core studio capabilities, environment compatibility (classification,
 *    recommendations, version conflicts, FFmpeg/ffprobe/git,
 *    Python/PyTorch/CUDA), the persistent on-disk project recovery report,
 *    the runtime environment, and the configured ports.
 *  - Port 1234 is the external local LLM service: shown as informational
 *    only. Nothing in this UI binds, starts, or stops any port.
 *
 * The `Page` suffix on renderSystemPage is deliberate: app.js already has a
 * local renderSystem(sys) helper for the top-bar model/GPU summary, and
 * importing the same identifier would break the module graph.
 */

import { el } from "../dom.js";
import { state } from "../state.js";
import { health, systemStatus, listProjects } from "../api.js";
import { loadingState, errorPanel, badge, icon } from "../ui.js";
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
export function renderSystemPage(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "System status")),
  );
  if (state.connection !== "online") {
    screen.append(errorPanel(
      { kind: "offline", message: "Backend offline — system status is unavailable." },
      el("button", { class: "btn", type: "button", onclick: renderSystemPageRefresh }, "Retry"),
    ));
    return screen;
  }
  screen.append(systemPanel());
  return screen;
}

/**
 * Re-render from fresh backend data (offline retry path): re-check health
 * first so the badge reflects the current backend state.
 */
async function renderSystemPageRefresh() {
  try {
    const h = await health(state.config);
    state.connection = "online";
    state.healthMode = h.mode;
  } catch {
    state.connection = "offline";
  }
  // The awaited health check may outlive this screen; never clobber whatever
  // route the user navigated to in the meantime.
  if (parseRoute().name !== "system") return;
  const content = document.querySelector(".content");
  if (content) content.replaceChildren(renderSystemPage({ name: "system", param: null }));
}

/**
 * @returns {HTMLElement}
 */
function systemPanel() {
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
      const [sys, projects] = await Promise.all([
        systemStatus(state.config),
        listProjects(state.config),
      ]);
      region.replaceChildren(buildAll(sys, projects.recovery));
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
 * Persistent on-disk recovery entries from `GET /api/projects` (recovered /
 * orphaned / conflict / unreadable project directories). The boot toast
 * reports the same entries transiently; this panel keeps a persistent
 * conflict visible across boots and is the destination of the toast's View
 * button. Nothing is mutated here — the backend already left the files
 * untouched, we only read its report.
 * @param {Array<{type: string, slug?: string, project_id?: string, detail: string}> | null | undefined} recovery
 * @returns {HTMLElement | null}
 */
function recoveryPanel(recovery) {
  if (!Array.isArray(recovery) || !recovery.length) return null;
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Project recovery"),
      el("span", { class: "spacer" }),
      badge("warning", `${recovery.length} ${recovery.length === 1 ? "issue" : "issues"}`),
    ),
    el("div", { class: "panel-body" },
      el("div", { class: "warning-list" },
        ...recovery.map((r) =>
          el("div", { class: "witem" }, icon("alert", 16),
            el("span", {}, `${r.slug || r.type}: ${r.detail || "no detail"}`),
          ),
        ),
      ),
      el("p", { class: "small muted" },
        "The backend reconciled on-disk project state at startup and left the files untouched. "
        + "These entries reappear on every boot until the underlying mismatch is resolved, "
        + "for example by renaming the on-disk directory to the slug stored in its project.json."),
    ),
  );
}

/**
 * @param {import("../api.js").SystemStatus} sys
 * @param {Array<{type: string, slug?: string, project_id?: string, detail: string}> | null | undefined} recovery
 * @returns {HTMLElement}
 */
function buildAll(sys, recovery) {
  const env = sys.environment;
  const parts = [
    capabilityPanel(env.capabilities),
    classificationPanel(env),
    recoveryPanel(recovery),
    runtimePanel(env),
    portsPanel(sys),
  ];
  return el("div", { class: "stack" }, ...parts);
}

/** Core requirements are independent of optional local model runtimes. */
export function capabilityPanel(report) {
  if (!report) return null;
  const labels = {
    python: "Python", ffmpeg: "FFmpeg", media_inspection: "Detailed media inspection",
    browser_rendering: "Editorial and graphic rendering", pytorch: "PyTorch",
    cuda: "CUDA through PyTorch", nvidia_gpu_inventory: "NVIDIA inventory",
    managed_ideogram_launch: "Managed Ideogram launcher",
    managed_tts_launch: "Managed TTS worker source", managed_higgs_launch: "Managed Higgs launcher",
    external_http_services: "External model services",
  };
  const rows = (items) => Object.entries(items || {}).map(([name, item]) =>
    el("div", { class: "stack" },
      el("div", { class: "row" }, el("strong", {}, labels[name] || name),
        badge(item.status === "available" ? "good" : item.status === "probe_failed" ? "warning" : "neutral",
          item.status.replaceAll("_", " "), false)),
      el("p", { class: "small muted" }, item.detail)));
  return el("section", { class: "panel", "aria-label": "Core readiness" },
    el("div", { class: "row" }, el("h2", { class: "panel-title" }, "Core studio"),
      badge(report.core.ready ? "good" : "critical", report.core.ready ? "Ready" : "Needs attention")),
    el("div", { class: "panel-body stack" },
      el("p", {}, report.core.detail),
      ...rows(report.core.requirements),
      el("h3", {}, "Additional rendering features"), ...rows(report.features),
      el("details", {}, el("summary", {}, "Optional AI runtimes and launchers"),
        el("p", { class: "small muted" }, "These do not affect core mock mode. A reachable service does not establish local model or GPU support."),
        ...rows(report.optional)),
      el("p", { class: "small muted" }, "Runtime discovery is not native platform qualification.")));
}

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
