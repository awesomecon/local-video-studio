/**
 * Script screen: the project's real, persisted script.
 *
 *  - Scene narration text comes from `GET /api/projects/{id}` (the snapshot
 *    includes every scene's `narration`) — nothing is invented or cached.
 *  - The plan (title, strategy notes, outline) is shown after an explicit
 *    "Run planning" action (`POST /api/projects/{id}/plan`), which is
 *    non-idempotent: the plan is only requested when the user asks, and a
 *    force re-plan asks for confirmation first.
 *
 * There is no client-side persistence here: reload the page and the screen
 * shows exactly what the backend currently has.
 */

import { el, fmtDuration } from "../dom.js";
import { state, needsProject } from "../state.js";
import { getProject, planProject } from "../api.js";
import { badge, loadingState, emptyState, errorPanel, toast, confirm } from "../ui.js";
import { registerLiveUpdate } from "../app.js";

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderScript(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Script")),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject(
      "Select a project in the top bar (or create one) to see its script.",
    ));
    return screen;
  }
  screen.append(scriptPanel());
  return screen;
}

/**
 * The main panel: header actions, the narration document, and the plan
 * region (populated by an explicit planning action).
 * @returns {HTMLElement}
 */
function scriptPanel() {
  const scriptRegion = el("div");
  const planRegion = el("div", { class: "mt" });
  const modelGate = el("div", { class: "mt" });

  const planBtn = el("button", { class: "btn", type: "button" }, "Run planning");
  const forceBtn = el("button", { class: "btn btn-ghost", type: "button" }, "Force re-plan");
  const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Refresh");
  let scriptModelRequired = false;

  /**
   * @param {HTMLElement} region
   * @param {{skeleton?: boolean}} [opts] — omit the skeleton on live refreshes
   */
  let inflight = 0; // last-write-wins sequence, same pattern as timeline.js
  /** True once the script has rendered; a transient error must not replace
   *  it, but a first-load failure (only the skeleton on screen) still shows
   *  the error panel with its Retry action. */
  let hasContent = false;
  async function loadScript(region, { skeleton = true } = {}) {
    const token = ++inflight;
    if (skeleton) region.replaceChildren(loadingState(3));
    try {
      const snap = await getProject(state.config, state.currentProjectId);
      if (token !== inflight) return;
      updateModelGate(snap);
      buildScript(region, snap);
      hasContent = true;
    } catch (err) {
      if (token !== inflight || hasContent) return;
      region.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => loadScript(region) }, "Retry"),
      ));
    }
  }

  /** Disable planning before the backend guard needs to reject a request. */
  function updateModelGate(snap) {
    const selectionRequired = state.healthMode !== "mock"
      && (!snap.project.selected_llm_model || snap.project.selected_llm_model === "auto");
    scriptModelRequired = selectionRequired;
    planBtn.disabled = selectionRequired;
    forceBtn.disabled = selectionRequired;
    modelGate.replaceChildren(
      selectionRequired
        ? el("div", { class: "warning-list" },
            el("div", { class: "witem" },
              el("span", {}, "Choose a script model before planning. "),
              el("a", { href: "#/models" }, "Open Models & System Status"),
              el("span", {}, " to select a router model for this project."),
            ),
          )
        : el("div", { class: "small muted" },
            `Script model: ${snap.project.selected_llm_model || "mock"}.`),
    );
  }

  /**
   * Render the narration document from a fresh snapshot.
   * @param {HTMLElement} region
   * @param {import("../api.js").ProjectSnapshot} snap
   */
  function buildScript(region, snap) {
    const scenes = snap.scenes || [];
    if (!scenes.length) {
      region.replaceChildren(emptyState(
        "No scenes yet",
        "Run planning to have the backend draft the scene script for this project.",
      ));
      return;
    }
    const doc = el("div", { class: "script-doc" });
    scenes.forEach((s, i) => {
      doc.append(
        el("h3", {}, `${i + 1}. ${s.title}`),
        s.narration
          ? el("p", {}, s.narration)
          : el("p", { class: "muted small" }, "(no narration yet)"),
        el("div", { class: "muted small" },
          `${fmtDuration(s.duration || 0)} · ${s.selected_backend || "automatic"} · ${s.visual_type || "still"}`,
        ),
      );
    });
    region.replaceChildren(doc);
  }

  /**
   * Request the plan from the backend and display it. Non-idempotent:
   * force re-plan invalidates downstream stages (confirmed by the caller).
   * @param {boolean} force
   */
  async function doPlan(force) {
    planBtn.disabled = true;
    forceBtn.disabled = true;
    planBtn.textContent = force ? "Re-planning…" : "Planning…";
    planRegion.replaceChildren(loadingState(2));
    try {
      const plan = await planProject(state.config, state.currentProjectId, { force });
      renderPlan(plan, planRegion);
      toast("good", force ? "Project re-planned" : "Planning complete", plan.title);
      loadScript(scriptRegion); // narration may have changed
    } catch (err) {
      planRegion.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: () => doPlan(force) }, "Retry"),
      ));
    } finally {
      planBtn.disabled = scriptModelRequired;
      forceBtn.disabled = scriptModelRequired;
      planBtn.textContent = "Run planning";
    }
  }

  async function onForce() {
    const ok = await confirm({
      title: "Force re-plan?",
      message: "The backend will draft a new scene plan. Existing scenes are replaced and downstream stages (narration, visuals, music, timeline, render) are invalidated.",
      confirmLabel: "Re-plan",
    });
    if (ok) doPlan(true);
  }

  planBtn.onclick = () => doPlan(false);
  forceBtn.onclick = onForce;
  refreshBtn.onclick = () => loadScript(scriptRegion);

  const panel = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Project script"),
      el("span", { class: "spacer" }),
      planBtn,
      forceBtn,
      refreshBtn,
    ),
    el("p", { class: "muted small" },
      "The script is the narration text the planner assigned to each scene. It is persisted on the backend and refreshes automatically as planning and narration jobs complete."),
    el("div", { class: "panel-body" }, modelGate, scriptRegion, planRegion),
  );
  // Live path: narration updates as plan/narration jobs complete. The plan
  // region is populated only by an explicit planning action, so it is not
  // touched by the live refresh.
  registerLiveUpdate(() => loadScript(scriptRegion, { skeleton: false }));
  loadScript(scriptRegion);
  return panel;
}

/**
 * Display a fetched ProjectPlan: strategy notes, outline, scene count.
 * @param {import("../api.js").ProjectPlan} plan
 * @param {HTMLElement} region
 */
function renderPlan(plan, region) {
  const parts = [];
  if (plan.strategy_notes && plan.strategy_notes.length) {
    parts.push(
      el("h3", {}, "Strategy notes"),
      el("ul", { class: "outline-list" }, plan.strategy_notes.map((n) => el("li", {}, n))),
    );
  }
  if (plan.outline && plan.outline.length) {
    parts.push(
      el("h3", {}, "Outline"),
      el("ol", { class: "outline-list" }, plan.outline.map((o) => el("li", {}, o))),
    );
  }
  parts.push(
    el("h3", {}, "Scenes"),
    el("div", { class: "muted small" },
      `${(plan.scenes || []).length} scenes · target ${fmtDuration(plan.target_duration || 0)}`,
    ),
  );
  region.replaceChildren(
    el("div", { class: "panel" },
      el("div", { class: "row" },
        el("span", { class: "panel-title" }, "Plan"),
        badge("good", "planned"),
        el("span", { class: "spacer" }),
        plan.created_at ? el("span", { class: "muted small mono" }, String(plan.created_at)) : null,
      ),
      el("div", { class: "panel-body" }, el("div", { class: "stack" }, ...parts)),
    ),
  );
}
