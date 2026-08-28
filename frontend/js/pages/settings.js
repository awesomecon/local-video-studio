/**
 * Settings screen: API connection and configuration provenance.
 *
 * Model selection and runtime controls deliberately live on Models & System
 * Status, where their lifecycle and GPU effects can be shown together.
 */

import { el } from "../dom.js";
import { state } from "../state.js";
import { health } from "../api.js";
import { loadingState, badge } from "../ui.js";

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderSettings(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Settings")),
  );
  screen.append(
    el("div", { class: "stack" },
      connectionPanel(),
      el("div", { class: "panel" },
        el("div", { class: "panel-title" }, "Where configuration comes from"),
        el("div", { class: "panel-body stack" },
          el("p", { class: "small" },
            "This page talks to the backend at ",
            el("code", { class: "mono" }, state.config.apiBase ? state.config.apiBase : "the same origin as this page"),
            ". The base is resolved at boot from (in order): an injected ",
            el("code", { class: "mono" }, "window.LVS_CONFIG"),
            " object, a static ",
            el("code", { class: "mono" }, "config.json"),
            " served next to index.html, or the same-origin default. No build step is involved."),
          el("p", { class: "small muted" },
            "Backend behavior — LLM endpoint, ports, and GPU thresholds — is set in the backend process. The LLM API key, when required, lives only in that environment: this UI never reads, displays, or transmits it."),
          el("p", { class: "small muted" },
            "Choose script models and manage Studio-owned model memory on Models & System Status."),
        ),
      ),
    ),
  );
  return screen;
}

/** @returns {HTMLElement} */
function connectionPanel() {
  const region = el("div", { class: "panel-body" });
  const retryBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button",
  }, "Re-check");
  retryBtn.onclick = () => load();

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "API connection"),
      el("span", { class: "spacer" }), retryBtn,
    ),
    region,
  );

  async function load() {
    region.replaceChildren(loadingState(2));
    let connection = state.connection;
    let mode = state.healthMode;
    try {
      const response = await health(state.config);
      connection = "online";
      mode = response.mode;
      state.connection = connection;
      state.healthMode = mode;
    } catch {
      connection = "offline";
    }
    region.replaceChildren(
      el("dl", { class: "kv" },
        el("dt", {}, "API base"),
        el("dd", { class: "mono" }, state.config.apiBase || "(same origin)"),
        el("dt", {}, "Connection"),
        el("dd", {}, connection === "online" ? badge("good", "Connected") : badge("offline", "Backend offline")),
        el("dt", {}, "Pipeline mode"),
        el("dd", {},
          connection !== "online" ? el("span", { class: "muted" }, "—")
            : mode === "mock" ? badge("warning", "Mock pipeline") : badge("neutral", "Local pipeline")),
      ),
    );
  }

  load();
  return panel;
}
