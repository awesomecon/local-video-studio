/**
 * Editorial screen: the first-class workspace for Editorial Mode projects
 * (video_mode === "editorial"), where the Project Details panel stays a
 * compact status surface. Classic and legacy projects are never shown the
 * workspace: they get an empty state pointing at the Project screen (where
 * the Video Style is set) instead.
 *
 * Data flow (explicit navigation / Refresh / plan generation):
 *  - one load issues at most two reads: the project snapshot
 *    (GET /api/projects/{id}) and, only when the snapshot reports an Edit
 *    Plan AND its edit_plan_url is the exact project-local
 *    /api/projects/{id}/editorial/edit-plan path, the full Edit Plan
 *    (GET). A plan without a usable URL degrades to an error state.
 *  - live job-feed ticks re-read the snapshot ONLY and update in place the
 *    plan-status badge, the stale-reasons banner, and the display-settings
 *    row. They never re-fetch the Edit Plan, never rebuild an in-flight
 *    mutation, and never disturb unsaved detail-panel edits. A plan
 *    appearing/disappearing or the video mode changing is the one state
 *    change that triggers a full reload.
 *
 * Workspace (plan present):
 *  - Sequence strip: the plan's compositions as a time-proportional
 *    horizontal card strip (index, readable template name, start–end,
 *    duration, asset/element/event counts, locked-asset indicator); card
 *    width grows with composition duration. Clicking a card selects it.
 *  - Composition detail: for the selected composition, the same strict
 *    inline controls as the Project Details panel (duration, template,
 *    deterministic text, event actions, per-asset lock / generate / local
 *    replace, per-composition Regenerate and AI revision), built with the
 *    builders exported from pages/project.js so both surfaces validate the
 *    same way: untrusted plan content renders as text nodes only, every
 *    mutation URL is built from the mounted project id plus validated plan
 *    ids, at most one mutation runs at a time per screen, a failed mutation
 *    restores the last good plan, and a successful one re-renders strip +
 *    detail from the returned plan payload (no extra Edit Plan GET).
 *  - Sequence AI revision: the same two-phase revision control scoped to
 *    the whole sequence (composition_id omitted).
 *  - Display settings: the snapshot-driven Captions / Editorial text /
 *    caption-style controls; a save updates the status row and settings
 *    from a fresh snapshot and never reads or writes the Edit Plan.
 *  - Preview: the deterministic backend preview document (the same HTML the
 *    "Open in new tab" link opens) embedded in a same-origin iframe behind
 *    an explicit Show/Hide toggle; the preview URL is used only when it is
 *    exactly the mounted project's project-local
 *    /api/projects/{id}/editorial/preview path. The frame reloads after a
 *    successful mutation so the preview tracks the edited plan.
 *
 * No Edit Plan yet: the guarded Generate Edit Plan button (bodyless POST
 * exactly once to the snapshot's generate_url) plus a short workflow
 * summary; on success the screen reloads into the workspace.
 */

import { el, fmtDuration } from "../dom.js";
import { state, needsProject } from "../state.js";
import { getProject, getEditPlan } from "../api.js";
import {
  badge,
  emptyState,
  errorPanel,
  loadingState,
  toastError,
} from "../ui.js";
import { navigate } from "../router.js";
import { registerLiveUpdate } from "../app.js";
import {
  effectiveVideoMode,
  editorialPlanState,
  projectEditorialApiPath,
  safeEditPlanDownloadUrl,
  summarizeEditPlanCompositions,
  parseCompositionEditor,
  buildEditorialDisplayControls,
  buildGeneratePlanButton,
  buildCompositionControls,
  buildRevisionControl,
  usableUrl,
  createEditorialController,
} from "./project.js";

/** Readable names for the five renderer-owned composition templates. */
const TEMPLATE_LABELS = {
  archiveCanvas: "Archive canvas",
  documentReveal: "Document reveal",
  comparisonCanvas: "Comparison canvas",
  illustrationCanvas: "Illustration canvas",
  bigTextReveal: "Big text reveal",
};

/**
 * Human label for a plan template value; unknown values pass through so a
 * malformed plan is still identifiable (its controls stay disabled).
 * @param {unknown} template
 * @returns {string}
 */
export function templateLabel(template) {
  if (typeof template === "string" && TEMPLATE_LABELS[template]) return TEMPLATE_LABELS[template];
  return (typeof template === "string" && template) ? template : "—";
}

/**
 * The mounted project's preview URL, or null. Only the exact project-local
 * path `/api/projects/{id}/editorial/preview` is trusted; anything else
 * (remote hosts, other projects, query/fragment junk) hides the embedded
 * preview rather than navigating the frame somewhere unknown.
 * @param {unknown} value — the snapshot's preview_url
 * @param {string | null} projectId — the mounted project's id
 * @returns {string | null}
 */
export function safeEditorialPreviewUrl(value, projectId) {
  if (typeof value !== "string" || value !== value.trim() || /\s/.test(value)
    || value.includes("\\") || value.includes("?") || value.includes("#")) return null;
  if (typeof projectId !== "string" || !projectId || /\s/.test(projectId)) return null;
  const expected = `/api/projects/${encodeURIComponent(projectId)}/editorial/preview`;
  return value === expected ? value : null;
}

/**
 * Aspect ratio (CSS `w / h`) for the preview frame: the plan's own
 * width/height when both are positive finite numbers, then the project's
 * aspect_ratio, then the Editorial default (portrait).
 * @param {unknown} plan
 * @param {unknown} project
 * @returns {{css: string, number: number}}
 */
export function previewAspectRatio(plan, project) {
  const width = (plan && typeof plan === "object") ? plan.width : null;
  const height = (plan && typeof plan === "object") ? plan.height : null;
  if (typeof width === "number" && typeof height === "number"
    && Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
    return { css: `${width} / ${height}`, number: width / height };
  }
  const ratio = (project && typeof project.aspect_ratio === "string") ? project.aspect_ratio : null;
  if (ratio) {
    const m = ratio.match(/^(\d+):(\d+)$/);
    if (m && Number(m[1]) > 0 && Number(m[2]) > 0) {
      return { css: `${m[1]} / ${m[2]}`, number: Number(m[1]) / Number(m[2]) };
    }
  }
  return { css: "9 / 16", number: 9 / 16 };
}

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderEditorial(_route) {
  const refreshBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button",
  }, "Refresh");
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" },
      el("h1", {}, "Editorial"),
      el("span", { class: "sub" }, "Motion-graphics composition workspace"),
      el("div", { class: "screen-actions" }, refreshBtn),
    ),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject(
      "Select a project in the top bar (or create one) to open its Editorial workspace.",
    ));
    screen.append(el("div", { class: "mt" },
      el("button", { class: "btn btn-primary", type: "button", onclick: () => navigate("#/new") }, "New Project"),
    ));
    return screen;
  }
  const projectId = state.currentProjectId;
  const body = el("div", {});
  refreshBtn.addEventListener("click", () => loadBody(body, projectId, { skeleton: true }));
  screen.append(body);
  loadBody(body, projectId, { skeleton: true });
  return screen;
}

/**
 * Mount the workspace (or an empty/error state) for one project and wire
 * its live-update hook. All state that must survive re-renders (in-flight
 * mutation, selected composition, preview toggle, last good plan) lives in
 * this closure, so replacing `body`'s children never loses it.
 *
 * @param {HTMLElement} body
 * @param {string} projectId
 * @param {{skeleton?: boolean}} [opts]
 */
function loadBody(body, projectId, { skeleton = true } = {}) {
  const ctrl = createEditorialController();
  /** Shared inline failure surface (sibling of the state content). */
  const errors = el("div", { class: "ed-errors" });

  /** Last good plan payload; failure recovery re-renders from it. */
  let planRef = null;
  /** Last snapshot the workspace was painted from. */
  let snapRef = null;
  let selectedId = null;
  let previewOn = false;
  /** @type {HTMLIFrameElement | null} */
  let previewFrame = null;
  /** @type {HTMLElement | null} the preview well the frame mounts into. */
  let previewWell = null;
  /** @type {HTMLElement | null} plan-status badge host (workspace only). */
  let statusHost = null;
  /** @type {HTMLElement | null} stale/untracked note host (workspace only). */
  let noteHost = null;
  /** @type {HTMLElement | null} sequence summary host (workspace only). */
  let summaryHost = null;
  /** @type {HTMLElement | null} display-settings host (workspace only). */
  let settingsHost = null;
  /** @type {HTMLElement | null} composition detail host (workspace only). */
  let detailHost = null;
  /** @type {HTMLElement | null} root of the interactive workspace. */
  let workspaceRoot = null;

  /** True once any state (including empty states) has rendered. */
  let hasContent = false;
  /** "classic" | "editorial" of the last rendered snapshot. */
  let modeRef = "unknown";
  let hasPlanRef = false;
  let inflight = 0;
  /** Last validated strip/detail rows the workspace was painted from. */
  let lastSums = null;
  let lastRows = null;

  /* --- state renderers -------------------------------------------------- */

  /**
   * Classic / legacy projects: no workspace, a pointer to where the video
   * style is chosen.
   * @returns {HTMLElement}
   */
  function classicState() {
    return el("section", { class: "panel" },
      el("div", { class: "panel-title" }, "Editorial canvas"),
      emptyState(
        "Classic project",
        "This project uses the Classic scene-based pipeline, so it has no Editorial composition workspace. Switch the Video Style to Editorial on the Project screen to build motion-graphics compositions.",
        [
          el("button", { class: "btn", type: "button", onclick: () => navigate("#/project") }, "Open Project screen"),
        ],
      ),
    );
  }

  /**
   * No Edit Plan yet: the guarded Generate action (only for a usable
   * generate_url) plus the workflow summary the workspace implements.
   * @param {Object} editorial — the snapshot's editorial block
   * @returns {HTMLElement}
   */
  function noPlanState(editorial) {
    const actions = [];
    const generateUrl = usableUrl(editorial.generate_url);
    if (generateUrl) {
      actions.push(buildGeneratePlanButton(generateUrl, errors, () => load(), ctrl));
    }
    return el("section", { class: "panel" },
      el("div", { class: "panel-title" }, "Editorial canvas"),
      emptyState(
        "No Edit Plan yet",
        "This editorial project has no Edit Plan. Generating one plans the whole sequence from the script and narration — it can take a while and changes nothing else.",
        actions,
      ),
      el("div", { class: "panel-body" },
        el("div", { class: "small", style: { fontWeight: "650" } }, "How it works"),
        el("ol", { class: "ed-workflow" },
          el("li", {}, "Generate the Edit Plan from the script and narration."),
          el("li", {}, "Review the sequence: tune timings, templates, text, motion, and assets."),
          el("li", {}, "Preview the deterministic canvas right here, or in a new tab."),
          el("li", {}, "Render the final video from the Export screen."),
        ),
      ),
    );
  }

  /**
   * The snapshot reports a plan but its edit-plan URL is not the exact
   * project-local path: the plan cannot be read safely, so say so instead
   * of guessing a URL.
   * @returns {HTMLElement}
   */
  function unusablePlanUrlState() {
    const retry = el("button", { class: "btn", type: "button" }, "Retry");
    retry.addEventListener("click", () => load());
    return el("section", { class: "panel" },
      el("div", { class: "panel-title" }, "Editorial canvas"),
      errorPanel(new Error(
        "The snapshot reports an Edit Plan, but its edit-plan URL is not a usable project-local path, so it cannot be read safely.",
      ), retry),
    );
  }

  /**
   * The Edit Plan read failed.
   * @param {Error} err
   * @returns {HTMLElement}
   */
  function planErrorState(err) {
    const retry = el("button", { class: "btn", type: "button" }, "Retry");
    retry.addEventListener("click", () => load());
    return el("section", { class: "panel" },
      el("div", { class: "panel-title" }, "Editorial canvas"),
      errorPanel(err, retry),
    );
  }

  /* --- workspace -------------------------------------------------------- */

  /**
   * Plan-status badge row + the stale/untracked note beneath it. Both hosts
   * exist only while the workspace is mounted; the live tick calls this to
   * keep them current without touching the strip, detail, or preview.
   * @param {Object} editorial — the snapshot's editorial block
   */
  function paintStatusAndNotes(editorial) {
    const planState = editorialPlanState(editorial);
    if (statusHost) {
      statusHost.replaceChildren(
        planState.kind === "stale"
          ? badge("warning", "Edit Plan is stale")
          : planState.kind === "untracked"
            ? badge("neutral", "Edit Plan available")
            : badge("good", "Edit Plan available"),
        planState.kind === "current" ? el("span", { class: "muted small" }, "Current") : null,
      );
    }
    if (noteHost) {
      if (planState.kind === "stale") {
        noteHost.replaceChildren(el("div", { class: "banner banner-warning" },
          badge("warning", "Stale", false),
          el("div", {},
            el("div", {}, planState.reasons.length
              ? "The plan was generated before recent changes:"
              : "Tracked inputs changed since this plan was generated:"),
            ...planState.reasons.map((reason) => el("div", { class: "muted small" }, reason)),
            el("div", { class: "muted small" },
              "Stale plans are preserved on purpose — the preview still shows the last generated plan."),
          ),
        ));
      } else if (planState.kind === "untracked") {
        noteHost.replaceChildren(el("p", { class: "muted small" },
          "This plan may predate provenance tracking, so its freshness can't be verified. It is still usable — the preview shows it as-is."));
      } else {
        noteHost.replaceChildren();
      }
    }
  }

  /**
   * Display settings row from the snapshot, through the strict shared
   * builder (project-local settings_url + strict-boolean values only).
   * @param {Object} editorial
   */
  function paintSettingsHost(editorial) {
    if (!settingsHost) return;
    const controls = buildEditorialDisplayControls(
      editorial, ctrl, errors, onSettingsSaved, projectId,
    );
    settingsHost.replaceChildren(
      controls
        ? controls
        : el("span", { class: "muted small" },
          "No display settings are available for this plan."),
    );
  }

  /**
   * Fresh-snapshot refresh after a display-setting save: the plan on disk
   * changed, so the status row and the settings row re-read from the
   * snapshot (the plan itself is never touched on this path).
   */
  async function onSettingsSaved() {
    try {
      const fresh = await getProject(state.config, projectId);
      if (fresh.project && fresh.project.id === projectId) snapRef = fresh;
      paintStatusAndNotes((fresh.editorial && typeof fresh.editorial === "object") ? fresh.editorial : {});
      paintSettingsHost((fresh.editorial && typeof fresh.editorial === "object") ? fresh.editorial : {});
    } catch (err) {
      // The setting itself saved; only the confirmation refresh failed.
      errors.replaceChildren(errorPanel(err));
      toastError(err, "Display setting saved, but the project refresh failed");
    }
  }

  /**
   * The time-proportional composition strip.
   * @param {ReturnType<typeof summarizeEditPlanCompositions>["compositions"]} sums
   * @returns {HTMLElement}
   */
  function buildStrip(sums) {
    const cards = sums.map((sum, i) => {
      const startText = sum.start != null ? fmtDuration(sum.start) : "—";
      const endText = (sum.start != null && sum.duration != null)
        ? fmtDuration(sum.start + sum.duration)
        : "—";
      const card = el("button", {
        class: `ed-card${sum.id === selectedId ? " selected" : ""}`
          + (typeof sum.template === "string" && TEMPLATE_LABELS[sum.template]
            ? ` tpl-${sum.template}` : ""),
        type: "button",
        "data-ed-comp-card": sum.id,
        // Time-proportional width with a readable floor; CSS clamps it.
        style: { flexGrow: String(Math.max(0.6, sum.duration != null ? sum.duration : 1)) },
        title: `${sum.id} — ${templateLabel(sum.template)}`,
        onclick: () => {
          if (ctrl.busy !== "") return; // one mutation in flight at a time
          selectedId = sum.id;
          syncStripSelection();
          paintDetailHost();
        },
      },
        el("span", { class: "ed-card-head" },
          el("span", { class: "ed-card-num" }, String(i + 1)),
          el("span", { class: "ed-card-title" }, templateLabel(sum.template)),
        ),
        el("span", { class: "ed-card-time" }, `${startText}–${endText}`),
        el("span", { class: "ed-card-meta" },
          `${sum.assetCount} assets · ${sum.elementCount} elements · ${sum.eventCount} events`),
        sum.lockedAssetCount > 0
          ? badge("neutral", `${sum.lockedAssetCount} locked`, false)
          : null,
      );
      return card;
    });
    return el("div", { class: "ed-strip", role: "list", "aria-label": "Sequence" }, ...cards);
  }

  /** Keep the strip's selected-card highlight in sync after a selection. */
  function syncStripSelection() {
    if (!workspaceRoot) return;
    for (const card of /** @type {NodeListOf<HTMLElement>} */ (workspaceRoot.querySelectorAll("[data-ed-comp-card]"))) {
      card.classList.toggle("selected", card.getAttribute("data-ed-comp-card") === selectedId);
    }
  }

  /**
   * The selected composition's detail block: header (index, id, template,
   * timing, counts) plus the strict shared inline controls. Reads the last
   * validated rows painted by paintWorkspace, so strip clicks can repaint
   * the block without re-parsing the plan.
   */
  function paintDetailHost() {
    if (!detailHost || !lastSums || !lastRows) return;
    const sums = lastSums;
    const rows = lastRows;
    const index = sums.findIndex((s) => s.id === selectedId);
    if (index === -1) {
      detailHost.replaceChildren(el("span", { class: "muted small" },
        "Select a composition from the sequence strip."));
      return;
    }
    const sum = sums[index];
    const data = rows[index];
    const startText = sum.start != null ? fmtDuration(sum.start) : "—";
    const endText = (sum.start != null && sum.duration != null)
      ? fmtDuration(sum.start + sum.duration)
      : "—";
    const counts =
      `${sum.assetCount} assets · ${sum.elementCount} elements · ${sum.eventCount} events · ` +
      `${sum.narrationRefCount} narration refs`;
    detailHost.replaceChildren(
      el("div", { class: "ed-detail-head" },
        el("span", { class: "badge neutral" }, String(index + 1)),
        el("span", { class: "mono small", title: sum.id }, sum.id),
        data.templateKnown
          ? badge("accent", templateLabel(data.template), false)
          : el("span", { class: "badge badge-neutral" },
            `${data.template || "unknown"} (not recognized)`),
        el("span", { class: "muted small" }, `${startText}–${endText}`),
      ),
      el("div", { class: "muted small" }, counts),
      el("div", { class: "mt" }, buildCompositionControls(data, mutationCtx())),
    );
  }

  /**
   * Shared mutation context for the reused builders: one in-flight guard
   * per screen, a shared inline error surface, and a runMutation that
   * re-renders the workspace from the response plan on success (no extra
   * Edit Plan GET) or restores the last good plan on failure.
   * @returns {{projectId: string, ctrl: import("./project.js").EditorialController, errors: HTMLElement, runMutation: (call: () => Promise<any>, context: string) => Promise<any>}}
   */
  function mutationCtx() {
    return {
      projectId,
      ctrl,
      errors,
      runMutation: async (call, context) => {
        if (ctrl.busy !== "") return null; // one mutation in flight at a time
        const prior = ctrl.plan;
        ctrl.busy = "composition";
        if (workspaceRoot) {
          for (const node of /** @type {NodeListOf<HTMLElement>} */ (workspaceRoot.querySelectorAll("button, input, select, textarea"))) {
            node.disabled = true;
          }
        }
        errors.replaceChildren();
        try {
          const updated = await call();
          ctrl.busy = "";
          paintWorkspace(updated);
          return updated;
        } catch (err) {
          ctrl.busy = "";
          errors.replaceChildren(errorPanel(err));
          toastError(err, context);
          if (prior) paintWorkspace(prior);
          return null;
        }
      },
    };
  }

  /**
   * Preview frame: created on demand behind the Show/Hide toggle, loaded
   * with a cache-busting stamp, and reloaded after successful mutations so
   * it tracks the edited plan. Only a validated project-local preview URL
   * is ever pointed at.
   */
  function mountPreviewFrame() {
    if (!previewWell) return;
    const previewUrl = safeEditorialPreviewUrl(
      (snapRef && snapRef.editorial && typeof snapRef.editorial === "object")
        ? snapRef.editorial.preview_url : null,
      projectId,
    );
    if (!previewUrl || !previewOn) return;
    const ratio = previewAspectRatio(planRef, snapRef && snapRef.project);
    previewFrame = el("iframe", {
      class: "ed-preview-frame",
      title: "Editorial preview",
      style: {
        width: `min(100%, calc(66vh * ${ratio.number.toFixed(4)}))`,
        aspectRatio: ratio.css,
      },
    });
    previewFrame.src = `${previewUrl}?ts=${Date.now()}`;
    previewWell.replaceChildren(previewFrame);
  }

  /**
   * Build (or rebuild) the full workspace from a validated plan payload.
   * Selection and preview toggle are kept across rebuilds.
   * @param {unknown} planPayload
   */
  function paintWorkspace(planPayload) {
    if (ctrl.busy !== "") return;
    const summary = summarizeEditPlanCompositions(planPayload);
    const editor = parseCompositionEditor(planPayload);
    if (!summary.ok || !editor.ok) {
      resetWorkspaceHosts();
      body.replaceChildren(errors,
        el("section", { class: "panel" },
          el("div", { class: "panel-title" }, "Editorial canvas"),
          errorPanel(new Error("The Edit Plan is not a readable composition list.")),
        ));
      return;
    }
    ctrl.plan = planPayload;
    // Detach any previously mounted hosts before building the new ones.
    resetWorkspaceHosts();
    lastSums = summary.compositions;
    lastRows = editor.compositions;
    // Keep the selection when the plan still contains it; otherwise select
    // the first composition with a usable id.
    if (!selectedId || !editor.compositions.some((c) => c.id === selectedId)) {
      const first = editor.compositions.find((c) => c.id != null) || {};
      selectedId = (typeof first.id === "string" && first.id) ? first.id : null;
    }

    const plan = (planPayload && typeof planPayload === "object") ? planPayload : {};
    const project = (snapRef && snapRef.project) ? snapRef.project : null;
    const width = (typeof plan.width === "number" && Number.isFinite(plan.width)) ? plan.width : null;
    const height = (typeof plan.height === "number" && Number.isFinite(plan.height)) ? plan.height : null;
    const fps = (typeof plan.fps === "number" && Number.isFinite(plan.fps)) ? plan.fps : null;
    const end = summary.compositions.reduce(
      (max, s) => (s.start != null && s.duration != null
        ? Math.max(max, s.start + s.duration) : max), 0,
    );
    const summaryBits = [
      `${summary.compositions.length} composition${summary.compositions.length === 1 ? "" : "s"}`,
      fmtDuration(end),
      width != null && height != null ? `${width}×${height}` : null,
      fps != null ? `${fps} fps` : null,
    ].filter(Boolean).join(" · ");

    const previewUrl = safeEditorialPreviewUrl(
      (snapRef && snapRef.editorial && typeof snapRef.editorial === "object")
        ? snapRef.editorial.preview_url : null,
      projectId,
    );
    const showPreview = el("button", {
      class: "btn btn-sm", type: "button", "data-ed-preview-toggle": "1",
    }, previewOn ? "Hide preview" : "Show preview");
    showPreview.addEventListener("click", () => {
      if (ctrl.busy !== "" || !previewUrl) return;
      previewOn = !previewOn;
      showPreview.textContent = previewOn ? "Hide preview" : "Show preview";
      previewWell.hidden = !previewOn;
      if (previewOn) mountPreviewFrame();
    });
    const downloadHref = safeEditPlanDownloadUrl(
      (snapRef && snapRef.editorial && typeof snapRef.editorial === "object")
        ? snapRef.editorial.edit_plan_url : null,
      projectId,
    );

    const ctx = mutationCtx();
    const leftCol = el("div", { class: "ed-col" },
      el("section", { class: "panel" },
        el("div", { class: "row" },
          el("span", { class: "panel-title" }, "Sequence"),
          (statusHost = el("span", { class: "row" })),
          el("span", { class: "spacer" }),
          (summaryHost = el("span", { class: "muted small" }, summaryBits)),
        ),
        (noteHost = el("div", {})),
        buildStrip(summary.compositions),
      ),
      el("section", { class: "panel" },
        el("div", { class: "row" },
          el("span", { class: "panel-title" }, "Preview"),
          previewUrl ? showPreview : null,
          previewUrl
            ? el("a", {
              class: "btn btn-ghost btn-sm", href: previewUrl, target: "_blank", rel: "noopener",
            }, "Open in new tab")
            : null,
          !previewUrl
            ? el("span", { class: "muted small" },
              "Preview is unavailable for this project's snapshot metadata.")
            : null,
        ),
        (previewWell = el("div", { class: "ed-preview-well", hidden: !previewOn })),
      ),
      el("section", { class: "panel" },
        el("div", { class: "panel-title" }, "AI revision"),
        el("p", { class: "muted small" },
          "Describe a sequence-level change — adding, splitting, or retiming compositions. Nothing is applied until you preview and accept it."),
        buildRevisionControl(null, ctx),
      ),
    );
    const rightCol = el("div", { class: "ed-col" },
      el("section", { class: "panel" },
        el("div", { class: "panel-title" }, "Display settings"),
        (settingsHost = el("div", { class: "stack" })),
      ),
      el("section", { class: "panel" },
        el("div", { class: "panel-title" }, "Composition"),
        (detailHost = el("div", { class: "stack" })),
      ),
    );
    workspaceRoot = el("div", { class: "ed-layout" }, leftCol, rightCol);
    body.replaceChildren(errors, workspaceRoot);
    paintStatusAndNotes((snapRef && snapRef.editorial && typeof snapRef.editorial === "object")
      ? snapRef.editorial : {});
    paintSettingsHost((snapRef && snapRef.editorial && typeof snapRef.editorial === "object")
      ? snapRef.editorial : {});
    paintDetailHost();
    if (previewOn) mountPreviewFrame();
  }

  /**
   * Detach the persistent workspace hosts so a live tick can never paint
   * into a region that is no longer mounted.
   */
  function resetWorkspaceHosts() {
    statusHost = null;
    noteHost = null;
    summaryHost = null;
    settingsHost = null;
    detailHost = null;
    previewWell = null;
    previewFrame = null;
    workspaceRoot = null;
  }

  /* --- load / live refresh ---------------------------------------------- */

  /**
   * One explicit load: snapshot first, then the Edit Plan (only for a plan
   * with a usable project-local URL). Never re-issues a request for a
   * superseded load (token guard).
   */
  async function load({ skeleton = true } = {}) {
    const token = ++inflight;
    if (skeleton) body.replaceChildren(loadingState(4));
    /** @type {import("../api.js").ProjectSnapshot | null} */
    let snap = null;
    try {
      snap = await getProject(state.config, state.currentProjectId);
    } catch (err) {
      if (token !== inflight) return;
      if (!hasContent) {
        body.replaceChildren(errorPanel(err,
          el("button", { class: "btn", type: "button", onclick: () => load() }, "Retry"),
        ));
      }
      return;
    }
    if (token !== inflight) return;
    if (!snap || !snap.project || snap.project.id !== projectId) {
      body.replaceChildren(needsProject(
        "This project is no longer selected. Pick it in the top bar to continue."));
      return;
    }
    snapRef = snap;
    const mode = effectiveVideoMode(snap.project);
    const editorial = (snap.editorial && typeof snap.editorial === "object") ? snap.editorial : {};
    const nowHasPlan = editorial.has_edit_plan === true;
    modeRef = mode;
    hasPlanRef = nowHasPlan;
    hasContent = true;

    if (mode !== "editorial") {
      resetWorkspaceHosts();
      body.replaceChildren(errors, classicState());
      return;
    }
    if (!nowHasPlan) {
      resetWorkspaceHosts();
      body.replaceChildren(errors, noPlanState(editorial));
      return;
    }
    const planUrl = projectEditorialApiPath(editorial.edit_plan_url, projectId, "edit-plan");
    if (!planUrl) {
      resetWorkspaceHosts();
      body.replaceChildren(errors, unusablePlanUrlState());
      return;
    }
    let planPayload;
    try {
      planPayload = await getEditPlan(state.config, planUrl);
    } catch (err) {
      if (token !== inflight) return;
      resetWorkspaceHosts();
      body.replaceChildren(errors, planErrorState(err));
      return;
    }
    if (token !== inflight) return;
    paintWorkspace(planPayload);
  }

  /**
   * Live job-feed tick: snapshot only. In-flight mutations are never
   * disturbed; plan/mode state changes trigger a full reload; otherwise the
   * status badge, note, and display-settings row update in place.
   */
  async function tick() {
    if (ctrl.busy !== "") return;
    if (!state.currentProjectId) return;
    let snap;
    try {
      snap = await getProject(state.config, state.currentProjectId);
    } catch {
      return; // keep last-known state; the next tick retries
    }
    if (state.currentProjectId !== projectId || !snap || !snap.project
      || snap.project.id !== projectId) return;
    const mode = effectiveVideoMode(snap.project);
    const nowHasPlan = mode === "editorial"
      && !!(snap.editorial && snap.editorial.has_edit_plan === true);
    if (nowHasPlan !== hasPlanRef || mode !== modeRef) {
      await load({ skeleton: false });
      return;
    }
    if (!nowHasPlan) return; // classic or no-plan states render nothing live
    snapRef = snap;
    const editorial = (snap.editorial && typeof snap.editorial === "object") ? snap.editorial : {};
    paintStatusAndNotes(editorial);
    paintSettingsHost(editorial);
  }

  registerLiveUpdate(() => { void tick(); });
  load({ skeleton });
}
