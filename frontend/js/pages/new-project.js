/**
 * New Project screen: creates a project via `POST /api/projects`.
 *
 * Client-side validation mirrors the backend `ProjectCreate` contract
 * (backend/schemas/models.py). Backend errors (422 validation, 5xx) are
 * displayed verbatim below the form; the form is never cleared on failure.
 */

import { el } from "../dom.js";
import { state, persistCurrentProject, upsertProject } from "../state.js";
import { createProject } from "../api.js";
import { field, setFieldError, errorPanel, toast } from "../ui.js";
import { navigate } from "../router.js";

/**
 * @param {{name: string, param: string | null}} _route
 * @returns {HTMLElement}
 */
export function renderNewProject(_route) {
  /* --- inputs --------------------------------------------------------- */
  const titleInput = el("input", { id: "np-title", type: "text", maxlength: "1000", placeholder: "e.g. How Local LLMs Work" });
  const topicInput = el("input", { id: "np-topic", type: "text", placeholder: "What the video is about" });
  const durationInput = el("input", { id: "np-duration", type: "number", min: "1", step: "1", value: "120" });
  const durationModeSelect = el("select", { id: "np-duration-mode" },
    el("option", { value: "fixed" }, "Fixed — scenes match the target"),
    el("option", { value: "llm" }, "AI decides — sized to the script"),
  );
  const aspectSelect = el("select", { id: "np-aspect" },
    el("option", { value: "16:9" }, "16:9 — landscape"),
    el("option", { value: "9:16" }, "9:16 — portrait"),
    el("option", { value: "1:1" }, "1:1 — square"),
  );
  const fpsInput = el("input", { id: "np-fps", type: "number", min: "1", max: "240", step: "1", value: "24" });
  const widthInput = el("input", { id: "np-width", type: "number", min: "1", step: "1", value: "1920", style: { width: "110px" } });
  const heightInput = el("input", { id: "np-height", type: "number", min: "1", step: "1", value: "1080", style: { width: "110px" } });
  const resolutionRow = el("div", { class: "row", style: { gap: "6px" } }, widthInput, "×", heightInput);
  const styleInput = el("input", { id: "np-style", type: "text", value: "documentary" });
  const audienceInput = el("input", { id: "np-audience", type: "text", value: "general" });
  const narratorInput = el("input", { id: "np-narrator", type: "text", placeholder: "optional" });
  const qualityInput = el("input", { id: "np-quality", type: "text", value: "balanced" });
  const instructionsInput = el("textarea", { id: "np-instructions", rows: "4", placeholder: "Optional instructions for the planner" });

  /* --- field wrappers (kept for setFieldError) ------------------------ */
  const fTitle = field({ label: "Title", input: titleInput, hint: "Required, up to 1000 characters. The slug is generated from this." });
  const fTopic = field({ label: "Topic", input: topicInput, hint: "Required." });
  const fDuration = field({ label: "Target duration (seconds)", input: durationInput, hint: "Greater than 0." });
  const fDurationMode = field({
    label: "Duration control",
    input: durationModeSelect,
    hint: "With AI decides, this number is ignored entirely; the planner sizes the runtime from the script it writes.",
  });
  const fAspect = field({ label: "Aspect ratio", input: aspectSelect });
  const fFps = field({ label: "FPS", input: fpsInput, hint: "1–240." });
  const fResolution = field({ label: "Resolution (width × height)", input: resolutionRow, hint: "Positive pixels; 1920×1080 for 16:9, 1080×1920 for 9:16." });
  const fStyle = field({ label: "Style", input: styleInput, hint: "e.g. documentary, cinematic." });
  const fAudience = field({ label: "Audience", input: audienceInput, hint: "e.g. general, technical." });
  const fNarrator = field({ label: "Narrator preference", input: narratorInput, hint: "Optional free-form preference for the voice stage." });
  const fQuality = field({ label: "Visual quality", input: qualityInput, hint: "Backend default: balanced." });
  const fInstructions = field({ label: "Instructions", input: instructionsInput, hint: "Optional." });

  const submitBtn = el("button", { class: "btn btn-primary", type: "submit" }, "Create project");
  const errRegion = el("div", { class: "mt" });

  // Keep the duration hint honest about what the chosen control mode does.
  const durationHint = fDuration.querySelector(".hint");
  function refreshDurationHint() {
    if (!durationHint) return;
    durationHint.textContent = durationModeSelect.value === "llm"
      ? "Ignored; the AI decides the final runtime from its script alone."
      : "Greater than 0. Scenes are scaled to match exactly.";
  }
  durationModeSelect.addEventListener("change", refreshDurationHint);
  refreshDurationHint();

  /* --- validation (mirrors ProjectCreate) ------------------------------ */
  function validate() {
    let ok = true;
    const fail = (wrap, msg) => { setFieldError(wrap, msg); ok = false; };
    const pass = (wrap) => setFieldError(wrap, null);

    const title = titleInput.value.trim();
    if (!title) fail(fTitle, "Title is required.");
    else if (title.length > 1000) fail(fTitle, "Title must be 1000 characters or fewer.");
    else pass(fTitle);

    if (!topicInput.value.trim()) fail(fTopic, "Topic is required.");
    else pass(fTopic);

    const duration = Number(durationInput.value);
    if (!Number.isFinite(duration) || duration <= 0) fail(fDuration, "Duration must be a number greater than 0.");
    else pass(fDuration);

    const fps = Number(fpsInput.value);
    if (!Number.isInteger(fps) || fps < 1 || fps > 240) fail(fFps, "FPS must be an integer from 1 to 240.");
    else pass(fFps);

    const w = Number(widthInput.value);
    const h = Number(heightInput.value);
    if (!Number.isInteger(w) || w <= 0) fail(fResolution, "Width must be a positive integer.");
    else if (!Number.isInteger(h) || h <= 0) fail(fResolution, "Height must be a positive integer.");
    else pass(fResolution);
    return ok;
  }

  /* --- submit ----------------------------------------------------------- */
  let submitting = false;
  async function doCreate() {
    if (submitting) return;
    errRegion.replaceChildren();
    if (!validate()) return;
    submitting = true;
    submitBtn.disabled = true;
    submitBtn.textContent = "Creating…";
    const body = {
      title: titleInput.value.trim(),
      topic: topicInput.value.trim(),
      target_duration: Number(durationInput.value),
      duration_mode: durationModeSelect.value,
      aspect_ratio: aspectSelect.value,
      fps: Number(fpsInput.value),
      resolution: [Number(widthInput.value), Number(heightInput.value)],
      style: styleInput.value.trim() || "documentary",
      audience: audienceInput.value.trim() || "general",
      narrator_preference: narratorInput.value.trim() || null,
      visual_quality: qualityInput.value.trim() || "balanced",
      instructions: instructionsInput.value,
    };
    try {
      const snap = await createProject(state.config, body);
      state.currentProjectId = snap.project.id;
      persistCurrentProject(snap.project.id);
      upsertProject(snap.project);
      toast("good", "Project created", snap.project.title);
      navigate("#/project");
    } catch (err) {
      errRegion.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: doCreate }, "Try again"),
      ));
      submitting = false;
      submitBtn.disabled = false;
      submitBtn.textContent = "Create project";
    }
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    await doCreate();
  }

  const form = el("form", { onsubmit: onSubmit },
    el("div", { class: "grid-2" }, fTitle, fTopic),
    el("div", { class: "grid-2" }, fDuration, fAspect),
    fDurationMode,
    el("div", { class: "grid-2" }, fFps, fResolution),
    el("div", { class: "grid-2" }, fStyle, fAudience),
    el("div", { class: "grid-2" }, fNarrator, fQuality),
    fInstructions,
    el("div", { class: "row mt" }, submitBtn),
    errRegion,
  );

  return el("div", { class: "screen" },
    el("div", { class: "screen-head" },
      el("h1", {}, "New Project"),
    ),
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "Project details"),
      el("p", { class: "muted small" }, "Title and topic are required — the backend plans scenes from them. Leave the other fields at their defaults unless you know better."),
      form,
    ),
  );
}
