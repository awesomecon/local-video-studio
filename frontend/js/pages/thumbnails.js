/** Local thumbnail design, generation, and export selection. */

import { el } from "../dom.js";
import { state, needsProject } from "../state.js";
import {
  getThumbnails,
  saveThumbnailPlan,
  regenerateThumbnailMagicPrompt,
  createThumbnailCandidate,
  regenerateThumbnailCandidate,
  selectThumbnailCandidate,
  deleteThumbnailCandidate,
  cancelJob,
} from "../api.js";
import {
  loadingState, emptyState, errorPanel, field, badge, jobStatusBadge,
  toast, toastError, confirm,
} from "../ui.js";
import { registerLiveUpdate } from "../app.js";

const TERMINAL = ["completed", "failed", "canceled"];
let refreshCurrentCandidates = () => {};
let requireSavedPlan = () => true;

// Keep model labels in one place as more local generators become available.
const THUMBNAIL_MODELS = [
  { value: "krea", label: "Krea · artwork + exact text overlay" },
  { value: "qwen_image", label: "Qwen-Image-2512 · artwork + exact text overlay" },
  { value: "qwen_image_native_text", label: "Qwen-Image-2512 · native text rendering" },
  { value: "ideogram4_local", label: "Ideogram 4 · integrated text and image" },
];

export function renderThumbnails(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" },
      el("h1", {}, "Thumbnail Studio"),
      el("span", { class: "muted small" }, "Local-only · 1280×720 PNG"),
    ),
  );
  if (!state.currentProjectId) {
    screen.append(needsProject("Select a project to create and choose its thumbnail."));
    return screen;
  }
  screen.append(studio());
  return screen;
}

function studio() {
  requireSavedPlan = () => true;
  const root = el("div", { class: "stack" }, loadingState(5));
  const projectId = state.currentProjectId;
  let formHost = null;
  let candidateHost = null;
  let latest = null;

  async function initialLoad() {
    try {
      latest = await getThumbnails(state.config, projectId);
      formHost = buildPlanForm(latest.plan, refreshCandidates, latest.magic_prompt);
      candidateHost = el("div", { class: "stack" }, buildCandidateArea(latest));
      root.replaceChildren(formHost, candidateHost);
    } catch (err) {
      root.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: initialLoad }, "Retry"),
      ));
    }
  }

  async function refreshCandidates() {
    if (!candidateHost) return;
    try {
      latest = await getThumbnails(state.config, projectId);
      candidateHost.replaceChildren(buildCandidateArea(latest));
    } catch (err) {
      candidateHost.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: refreshCandidates }, "Retry"),
      ));
    }
  }

  refreshCurrentCandidates = refreshCandidates;

  // Only the candidate/job region changes; unsaved form inputs stay mounted.
  registerLiveUpdate(refreshCandidates);
  initialLoad();
  return root;
}

function buildPlanForm(plan, afterSave, savedPrompt = null) {
  const proposedTitle = input("text", plan.proposed_title, { maxlength: "120" });
  const briefHook = input("text", plan.hook || "", { maxlength: "60", placeholder: "4–6 words recommended" });
  const audience = input("text", plan.audience || "", { maxlength: "120" });
  const topic = el("textarea", { class: "input", rows: "3", maxlength: "2000" }, plan.topic || "");
  const style = input("text", plan.style || "", { maxlength: "120" });
  const prompt = el("textarea", { class: "input", rows: "6", maxlength: "4000" }, plan.concept.prompt || "");
  const avoid = el("textarea", { class: "input", rows: "3", maxlength: "2000" }, plan.concept.avoid_prompt || "");
  const seed = input("number", String(plan.concept.seed ?? 0), { min: "0", step: "1" });
  const subject = select(["left", "center", "right"], plan.concept.subject_position);
  const textSide = select(["left", "right"], plan.concept.text_placement);
  const exactTitle = input("text", plan.text_layout.title, { maxlength: "120" });
  const exactHook = input("text", plan.text_layout.hook || "", { maxlength: "60" });
  const palette = select(["sunset", "electric", "midnight", "paper"], plan.text_layout.palette);
  const fontPreset = select(["impact", "clean", "editorial"], plan.text_layout.font_preset);
  const layoutPreset = select(["stacked", "split", "banner"], plan.text_layout.layout_preset);
  const outline = input("checkbox", "", { checked: !!plan.text_layout.outline });
  const shadow = input("checkbox", "", { checked: !!plan.text_layout.shadow });
  const imageModel = el("select", { class: "input" },
    ...THUMBNAIL_MODELS.map(({ value, label }) => el("option", { value }, label)),
  );
  imageModel.value = plan.image_model || "krea";
  const ideogramPromptMode = el("select", { class: "input" },
    el("option", { value: "quick" }, "Quick Generation"),
    el("option", { value: "precise" }, "Precise Text & Layout"),
  );
  ideogramPromptMode.value = plan.ideogram_prompt_mode
    || (savedPrompt?.prompt_mode === "precise" ? "precise" : "quick");
  const initialPrecisePrompt = plan.ideogram_prompt_json
    || (savedPrompt?.prompt_mode === "precise" ? savedPrompt.structured_prompt : null);
  const ideogramPromptJson = el("textarea", {
    class: "input mono small", rows: "18",
    placeholder: "Paste canonical Ideogram/KJNodes JSON",
  }, initialPrecisePrompt ? JSON.stringify(initialPrecisePrompt, null, 2) : "");
  const status = el("span", { class: "muted small", role: "status" }, "All changes saved");
  const save = el("button", { class: "btn btn-primary", type: "button" }, "Save thumbnail plan");
  let dirty = false;
  let editVersion = 0;
  function markDirty() {
    dirty = true;
    editVersion += 1;
    status.textContent = "Unsaved changes — save before generating or selecting a thumbnail.";
  }
  requireSavedPlan = () => {
    if (!dirty && !save.disabled) return true;
    toast("warning", "Save your design first", "The candidates below use the saved design.");
    save.scrollIntoView({ behavior: "smooth", block: "center" });
    save.focus();
    return false;
  };
  const isIdeogram = () => imageModel.value === "ideogram4_local";
  const isQwenNativeText = () => imageModel.value === "qwen_image_native_text";
  const artworkPanelHost = el("div");
  const avoidPromptHost = el("div");
  const artworkHint = el("p", { class: "muted small" });
  const typPanelHost = el("div");
  const typographyStyleHost = el("div", { class: "stack" });
  const typographyHint = el("p", { class: "muted small" });
  const ideogramModeHost = el("div", { class: "stack" });
  const preciseJsonHost = el("div");
  const preciseDetails = el("details", { open: !initialPrecisePrompt },
    el("summary", {}, "Edit Precise layout JSON"),
  );
  const directionControls = el("div", { class: "stack" });
  const modeHint = el("p", { class: "muted small" });
  function refreshPanels() {
    const ideogram = isIdeogram();
    const qwenNativeText = isQwenNativeText();
    const precise = ideogram && ideogramPromptMode.value === "precise";
    avoidPromptHost.style.display = (ideogram || qwenNativeText) ? "none" : "";
    ideogramModeHost.style.display = ideogram ? "" : "none";
    preciseJsonHost.style.display = precise ? "" : "none";
    directionControls.hidden = precise;
    typographyStyleHost.hidden = precise;
    modeHint.textContent = qwenNativeText
      ? "Qwen creates the artwork and lettering together for a more integrated design. It is creative rather than deterministic, so it may misspell, alter, or omit your wording; use the exact-overlay Qwen mode when copy accuracy matters."
      : !ideogram
      ? `${imageModel.value === "qwen_image" ? "Qwen-Image-2512" : "Krea"} creates the background. The studio adds your exact wording afterward, so spelling is deterministic.`
      : precise
        ? "Precise uses your saved layout JSON directly. Edit it below; it controls the image, lettering, colors, and positions."
        : "Quick turns your artwork direction and exact wording into a detailed prompt using your local LLM.";
    artworkHint.replaceChildren(ideogram
      ? "Describe one concrete visual subject and environment. Avoid topic summaries, prose, documents, collages, and lists of ideas."
      : qwenNativeText
        ? "Describe the complete visual scene. The studio automatically tells Qwen to render only the headline and supporting text entered here."
        : `Artwork is generated by local ${imageModel.value === "qwen_image" ? "Qwen-Image-2512" : "Krea 2 Turbo"} with lettering explicitly prohibited.`);
    typographyHint.replaceChildren(ideogram
      ? (precise
        ? "These phrases must also appear exactly in the layout JSON. Changing them here does not rewrite your JSON."
        : "Quick mode protects these exact strings, expands the concept with the local Ideogram Magic Prompt, then applies a collision-safe layout and renders the text natively.")
      : qwenNativeText
        ? "Qwen receives these as exact-copy instructions, but generative lettering is not guaranteed. Palette, font, outline, shadow, and layout are visual guidance rather than pixel-exact controls in this mode."
        : "These exact strings are rendered locally and deterministically.");
  }
  imageModel.onchange = refreshPanels;
  ideogramPromptMode.onchange = refreshPanels;
  refreshPanels();
  save.onclick = async () => {
    const savingVersion = editVersion;
    save.disabled = true;
    status.replaceChildren("Saving…");
    let precisePrompt = null;
    if (isIdeogram() && ideogramPromptMode.value === "precise") {
      try {
        precisePrompt = JSON.parse(ideogramPromptJson.value);
        const text = precisePrompt?.compositional_deconstruction?.elements
          ?.filter((element) => element.type === "text").map((element) => element.text) || [];
        const missing = [exactTitle.value.trim(), exactHook.value.trim()]
          .filter((value) => value && !text.includes(value));
        if (missing.length) throw new Error(`Add these exact phrases to the JSON text elements: ${missing.join(" · ")}`);
      } catch (_err) {
        status.replaceChildren(_err instanceof SyntaxError ? "Enter valid layout JSON before saving." : _err.message);
        toast("warning", "Check the Precise layout", status.textContent);
        preciseDetails.open = true;
        ideogramPromptJson.focus();
        save.disabled = false;
        return;
      }
    }
    const body = {
      schema_version: 1,
      project_id: plan.project_id,
      proposed_title: proposedTitle.value.trim(),
      hook: briefHook.value.trim(),
      audience: audience.value.trim(),
      topic: topic.value.trim(),
      style: style.value.trim(),
      canvas: [1280, 720],
      concept: {
        prompt: prompt.value.trim(),
        avoid_prompt: avoid.value.trim(),
        seed: Number(seed.value),
        subject_position: subject.value,
        text_placement: textSide.value,
      },
      text_layout: {
        title: exactTitle.value.trim(),
        hook: exactHook.value.trim(),
        palette: palette.value,
        font_preset: fontPreset.value,
        outline: outline.checked,
        shadow: shadow.checked,
        layout_preset: layoutPreset.value,
      },
      image_model: imageModel.value,
      ideogram_prompt_mode: isIdeogram() ? ideogramPromptMode.value : "quick",
      ideogram_prompt_json: precisePrompt,
      auto_derived_title: exactTitle.value.trim() === plan.text_layout.title && plan.auto_derived_title,
      auto_derived_hook: exactHook.value.trim() === plan.text_layout.hook && plan.auto_derived_hook,
      updated_at: plan.updated_at,
    };
    try {
      const saved = await saveThumbnailPlan(state.config, state.currentProjectId, body);
      plan = saved;
      dirty = editVersion !== savingVersion;
      status.replaceChildren(dirty
        ? "New edits are still unsaved. Save again before generating."
        : "Design saved. Generate a candidate below, then select your favorite.");
      toast("good", "Thumbnail plan saved");
      await afterSave();
    } catch (err) {
      status.replaceChildren("");
      toastError(err, "save thumbnail plan");
    } finally {
      save.disabled = false;
    }
  };

  avoidPromptHost.append(field({ label: "Avoid prompt", input: avoid }));
  directionControls.append(
    artworkHint,
    field({
      label: "Concept prompt", input: prompt,
      hint: "Use a concrete person, object, place, lighting, and composition—not a synopsis.",
    }),
    avoidPromptHost,
    field({ label: "Subject position", input: subject }),
    field({ label: "Text placement", input: textSide }),
  );
  artworkPanelHost.append(panel("Artwork direction", directionControls));
  typographyStyleHost.append(
    field({ label: "Palette", input: palette }),
    field({ label: "Font preset", input: fontPreset }),
    field({ label: "Layout preset", input: layoutPreset }),
    el("label", { class: "check-row" }, outline, "High-contrast outline"),
    el("label", { class: "check-row" }, shadow, "Text shadow"),
  );
  ideogramModeHost.append(field({
    label: "Ideogram prompt mode", input: ideogramPromptMode,
    hint: "Choose Quick for a written description, or Precise for a custom layout.",
  }));
  const useSaved = action("Copy saved prompt into editor", async () => {
    useSaved.disabled = true;
    try {
      const snapshot = await getThumbnails(state.config, plan.project_id);
      if (snapshot.magic_prompt?.status !== "saved") {
        toast("warning", "No saved prompt yet", "Use Quick mode to save a design and generate its Magic Prompt first, or paste your own layout JSON.");
        return;
      }
      ideogramPromptJson.value = JSON.stringify(snapshot.magic_prompt.structured_prompt, null, 2);
      markDirty();
    } catch (err) {
      toastError(err, "copy saved thumbnail prompt");
    } finally {
      useSaved.disabled = false;
    }
  });
  preciseDetails.append(
    el("p", { class: "muted small" }, "Already have a Quick prompt? Copy it here as a starting point. Keep lettering away from the image edges; generated layouts can vary."),
    useSaved,
    field({
    label: "Precise Ideogram JSON", input: ideogramPromptJson,
    hint: "Text is literal. Coordinates use Ideogram's 0–1000 [y_min, x_min, y_max, x_max] order.",
  }));
  preciseJsonHost.append(preciseDetails);
  // Exact copy, styling direction, and Save apply to both models. Ideogram
  // treats styling controls as prompt guidance rather than pixel-exact rules.
  typPanelHost.append(panel("Thumbnail wording",
    typographyHint,
    field({ label: "Headline on image", input: exactTitle, hint: "Short phrases are easier to read on a phone." }),
    field({
      label: "Supporting text (optional)", input: exactHook,
      hint: "Use a second short phrase only if it adds something to the headline.",
    }),
    typographyStyleHost,
  ));
  const form = el("div", { class: "stack thumbnail-editor" },
    panel("1 · Choose your image model",
      field({ label: "Image model", input: imageModel }),
      ideogramModeHost,
      modeHint,
    ),
    panel("2 · Edit your design",
      el("div", { class: "thumbnail-plan-grid" }, typPanelHost, artworkPanelHost),
      preciseJsonHost,
      el("details", {},
        el("summary", {}, "Project brief & advanced settings"),
        el("div", { class: "thumbnail-plan-grid mt" },
          el("div", { class: "stack" },
            field({ label: "Proposed video title", input: proposedTitle }),
            field({ label: "Brief hook", input: briefHook, hint: "Planning context; use Headline on image for visible text." }),
            field({ label: "Audience", input: audience }),
          ),
          el("div", { class: "stack" },
            field({ label: "Topic", input: topic }),
            field({ label: "Style", input: style }),
            field({
              label: "Seed", input: seed,
              hint: "Each candidate and retry varies this base seed automatically.",
            }),
          ),
        ),
      ),
      el("div", { class: "thumbnail-save-bar" }, save, status),
    ),
  );
  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);
  // Hide the empty direction panel as well as its controls in Precise mode.
  const updateVisibility = () => {
    refreshPanels();
    artworkPanelHost.hidden = isIdeogram() && ideogramPromptMode.value === "precise";
  };
  imageModel.onchange = updateVisibility;
  ideogramPromptMode.onchange = updateVisibility;
  updateVisibility();
  return form;
}

function buildCandidateArea(snapshot) {
  const activeBySlot = new Map(
    (snapshot.jobs || [])
      .filter((job) => !TERMINAL.includes(job.status))
      .map((job) => [job.stage.replace("thumbnail:", ""), job]),
  );
  const candidateById = new Map((snapshot.candidates || []).map((item) => [item.candidate_id, item]));
  const cards = [1, 2, 3].map((number) => {
    const id = `candidate-${String(number).padStart(2, "0")}`;
    const lastJob = (snapshot.jobs || []).filter((job) => job.stage === `thumbnail:${id}`)
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
    return candidateCard(id, candidateById.get(id), activeBySlot.get(id), lastJob);
  });
  const legacy = (snapshot.legacy_frames || []).length
    ? el("div", { class: "thumbnail-candidate-grid" },
        ...snapshot.legacy_frames.map(frameCard))
    : emptyState("No extracted frames yet", "Run a final render to create low-cost fallback frames.");
  return el("div", { class: "stack" },
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "3 · Generate & choose"),
      el("p", { class: "muted small" }, "Generate up to three options from your saved design, then select your favorite for export. Jobs run one at a time. A failed retry keeps the previous image."),
      buildMagicPromptArea(snapshot),
      el("div", { class: "thumbnail-candidate-grid" }, ...cards),
    ),
    el("details", { class: "panel" },
      el("summary", {}, "Use a frame from your final video instead"),
      el("p", { class: "muted small" }, "Promote a local extracted frame as artwork, then apply the typography settings above."),
      legacy,
    ),
  );
}

function buildMagicPromptArea(snapshot) {
  if (snapshot.plan?.image_model !== "ideogram4_local") {
    return el("div", { hidden: true });
  }
  const saved = snapshot.magic_prompt;
  const preciseMode = snapshot.plan?.ideogram_prompt_mode === "precise";
  const valid = saved?.status === "saved";
  const stateBadge = !saved
    ? badge("neutral", "Not generated yet")
    : !valid
      ? badge("warning", "Saved prompt invalid")
      : saved.stale
        ? badge("warning", "Saved prompt is stale")
        : badge("good", preciseMode ? "Precise prompt saved" : "Magic Prompt saved");
  const regenerate = action(
    preciseMode
      ? "Validate & Save Precise Prompt"
      : (valid ? "Regenerate Magic Prompt" : "Generate Magic Prompt"),
    async () => {
      if (!requireSavedPlan()) return;
      regenerate.disabled = true;
      regenerate.textContent = preciseMode
        ? "Validating Precise prompt…"
        : "Generating with local LLM…";
      try {
        const result = await regenerateThumbnailMagicPrompt(
          state.config, state.currentProjectId,
        );
        toast(
          "good",
          preciseMode ? "Precise prompt saved" : "Magic Prompt saved",
          result.same_as_previous
            ? "The regenerated prompt matched the previous prompt and was saved again."
            : "Saved before any Ideogram model load or VRAM check.",
        );
        await refreshCurrentCandidates();
      } catch (err) {
        toastError(err, "regenerate thumbnail Magic Prompt");
        regenerate.disabled = false;
        regenerate.textContent = preciseMode
          ? "Validate & Save Precise Prompt"
          : (valid ? "Regenerate Magic Prompt" : "Generate Magic Prompt");
      }
    },
    "btn btn-primary btn-sm",
  );
  const children = [
    el("div", { class: "row" },
      el("div", { class: "panel-title" }, preciseMode ? "Precise layout" : "Quick prompt"),
      el("span", { class: "spacer" }),
      stateBadge,
      regenerate,
    ),
    el("p", { class: "muted small" },
      preciseMode
        ? "Generate validates your saved layout automatically. You can also validate it here without creating an image."
        : "Generate prepares the prompt automatically. You can also prepare and inspect it here before creating an image.",
    ),
  ];
  if (valid) {
    const pretty = JSON.stringify(saved.structured_prompt, null, 2);
    children.push(el("details", {},
      el("summary", {}, "Inspect saved prompt & generation details"),
      el("div", { class: "muted small mono" },
        `${saved.path || "thumbnails/ideogram-magic-prompt.json"}`
        + `${saved.updated_at ? ` · ${saved.updated_at}` : ""}`
        + `${saved.same_as_previous ? " · same as previous regeneration" : ""}`),
      el("textarea", {
        class: "input mono small", rows: "18", readonly: true,
        "aria-label": "Saved Ideogram Magic Prompt JSON",
      }, pretty),
      el("details", {},
        el("summary", {}, "Exact serialized prompt sent to Ideogram"),
        el("textarea", {
          class: "input mono small", rows: "6", readonly: true,
          "aria-label": "Exact serialized Ideogram prompt",
        }, saved.serialized_prompt || ""),
      ),
    ));
    if (Array.isArray(saved.protected_text) && saved.protected_text.length) {
      children.push(el("p", { class: "muted small" },
        `Protected exact text: ${saved.protected_text.map((item) => JSON.stringify(item)).join(", ")}`));
    }
    if (Array.isArray(saved.warnings) && saved.warnings.length) {
      children.push(el("div", { class: "readonly-note" }, saved.warnings.join(" ")));
    }
  } else if (saved?.error) {
    children.push(el("div", { class: "readonly-note" }, saved.error));
  }
  return el("section", { class: "panel" }, ...children);
}

function candidateCard(id, candidate, job, lastJob) {
  const body = el("article", { class: `thumbnail-card${candidate?.selected ? " selected" : ""}` });
  const statusBadge = candidate?.selected
    ? badge("good", "Selected export thumbnail")
    : candidate?.stale
      ? badge("warning", "Previous design · regenerate")
      : badge("neutral", candidate ? "Ready" : "Empty");
  body.append(el("div", { class: "row" },
    el("strong", {}, id.replace("candidate-", "Candidate ")),
    el("span", { class: "spacer" }),
    statusBadge,
  ));
  if (candidate && localMedia(candidate.file_url)) {
    const img = el("img", {
      class: "thumbnail-preview",
      src: `${candidate.file_url}?v=${encodeURIComponent(candidate.composite_hash || "")}`,
      alt: `${id}${candidate.selected ? ", selected export thumbnail" : ""}`,
    });
    // A recorded candidate whose composite file disappeared (manual cleanup,
    // archived project) must not sit behind a "Ready" badge with a broken image.
    img.onerror = () => {
      img.replaceWith(el("div", { class: "thumbnail-placeholder" },
        "Preview file missing on disk — regenerate this slot"));
      statusBadge.className = "badge badge-warning";
      statusBadge.replaceChildren(el("span", { class: "dot" }), "File missing · regenerate");
    };
    body.append(img);
    const provenance = candidate.provenance || {};
    body.append(el("details", { class: "thumbnail-provenance" },
      el("summary", {}, "Generation details"),
      el("span", {}, `${provenance.image_model === "ideogram4_local" ? "Ideogram 4" : provenance.model || "local"} · seed ${provenance.seed ?? "—"}`),
      el("span", {}, provenance.workflow_version || "thumbnail-v1"),
      el("span", { class: "mono" }, `${String(candidate.composite_hash || "").slice(0, 12)}…`),
    ));
  } else {
    body.append(el("div", { class: "thumbnail-placeholder" }, "No candidate generated"));
  }
  if (!job && lastJob?.status === "failed") {
    body.append(el("p", { class: "readonly-note", role: "status" },
      `Last attempt failed: ${lastJob.error || "See Jobs for details."}`));
  }
  if (job) {
    const cancel = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Cancel queued job");
    cancel.onclick = () => cancelJob(state.config, job.id).catch((err) => toastError(err, "cancel thumbnail job"));
    body.append(el("div", { class: "row" }, jobStatusBadge(job.status), cancel));
  } else {
    const actions = [];
    if (!candidate) {
      actions.push(action("Generate", () => queue(id, false), "btn btn-primary btn-sm"));
    } else {
      actions.push(action("Regenerate", async () => {
        const ok = await confirm({
          title: `Regenerate ${id}?`,
          message: "The completed version is archived only after the replacement succeeds.",
          confirmLabel: "Regenerate",
        });
        if (ok) queue(id, true);
      }));
      actions.push(action("Duplicate", () => queue(null, false, null, id)));
      if (!candidate.stale && !candidate.selected) {
        actions.push(action("Use this thumbnail", () => choose(id), "btn btn-primary btn-sm"));
      }
      if (localMedia(candidate.file_url)) {
        actions.push(el("a", {
          class: "btn btn-ghost btn-sm", href: `${candidate.file_url}?download=true`,
        }, "Download PNG"));
      }
      actions.push(action(
        "Delete",
        async () => {
          const ok = await confirm({
            title: `Delete ${id}?`,
            message:
              "The files move to the project archive, the export selection clears if it "
              + "pointed here, and the slot becomes free for a new candidate.",
            confirmLabel: "Delete",
          });
          if (ok) deleteCandidate(id);
        },
        "btn btn-danger btn-sm",
      ));
    }
    body.append(el("div", { class: "row" }, ...actions));
  }
  return body;
}

function frameCard(asset) {
  const card = el("article", { class: "thumbnail-card" });
  if (localMedia(asset.url)) {
    const img = el("img", { class: "thumbnail-preview", src: asset.url, alt: "Extracted final-render frame" });
    img.onerror = () => img.replaceWith(
      el("div", { class: "thumbnail-placeholder" }, "Frame file missing on disk"));
    card.append(img);
  }
  card.append(
    el("span", { class: "muted small mono" }, asset.filepath || "local frame"),
    action("Use as candidate background", () => queue(null, false, asset.id), "btn btn-sm"),
  );
  return card;
}

async function queue(candidateId, regenerate, sourceAssetId = null, sourceCandidateId = null) {
  if (!requireSavedPlan()) return;
  try {
    const body = sourceAssetId
      ? { source_asset_id: sourceAssetId }
      : sourceCandidateId ? { source_candidate_id: sourceCandidateId } : {};
    if (regenerate) {
      await regenerateThumbnailCandidate(state.config, state.currentProjectId, candidateId, body);
    } else {
      if (candidateId) body.candidate_id = candidateId;
      await createThumbnailCandidate(state.config, state.currentProjectId, body);
    }
    toast("good", "Thumbnail queued", candidateId || "next available slot");
    await refreshCurrentCandidates();
  } catch (err) {
    toastError(err, "queue thumbnail");
  }
}

async function choose(candidateId) {
  if (!requireSavedPlan()) return;
  try {
    await selectThumbnailCandidate(state.config, state.currentProjectId, candidateId);
    toast("good", "Export thumbnail selected", candidateId);
    await refreshCurrentCandidates();
  } catch (err) {
    toastError(err, "select thumbnail");
  }
}

async function deleteCandidate(candidateId) {
  try {
    await deleteThumbnailCandidate(state.config, state.currentProjectId, candidateId);
    toast("good", "Candidate deleted", candidateId);
    await refreshCurrentCandidates();
  } catch (err) {
    toastError(err, "delete thumbnail candidate");
  }
}

function panel(title, ...children) {
  return el("section", { class: "panel" },
    el("div", { class: "panel-title" }, title),
    el("div", { class: "panel-body stack" }, ...children),
  );
}

function input(type, value, attrs = {}) {
  return el("input", { class: "input", type, value, ...attrs });
}

function select(options, value) {
  const node = el("select", { class: "input" }, ...options.map((item) => el("option", { value: item }, item)));
  node.value = value;
  return node;
}

function action(label, onclick, className = "btn btn-sm") {
  return el("button", { class: className, type: "button", onclick }, label);
}

function localMedia(url) {
  return typeof url === "string" && url.startsWith("/api/projects/");
}
