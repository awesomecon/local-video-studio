/** Local Krea artwork plus deterministic exact-text Thumbnail Studio. */

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
  const imageModel = select(["krea", "ideogram4_local"], plan.image_model || "krea");
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
  const status = el("span", { class: "muted small", role: "status" });
  const save = el("button", { class: "btn btn-primary", type: "button" }, "Save thumbnail plan");
  const isIdeogram = () => imageModel.value === "ideogram4_local";
  const artworkPanelHost = el("div");
  const avoidPromptHost = el("div");
  const artworkHint = el("p", { class: "muted small" });
  const typPanelHost = el("div");
  const typographyStyleHost = el("div", { class: "stack" });
  const typographyHint = el("p", { class: "muted small" });
  const ideogramModeHost = el("div", { class: "stack" });
  const preciseJsonHost = el("div");
  function refreshPanels() {
    const ideogram = isIdeogram();
    const precise = ideogram && ideogramPromptMode.value === "precise";
    avoidPromptHost.style.display = ideogram ? "none" : "";
    ideogramModeHost.style.display = ideogram ? "" : "none";
    preciseJsonHost.style.display = precise ? "" : "none";
    artworkHint.replaceChildren(ideogram
      ? "Describe one concrete visual subject and environment. Avoid topic summaries, prose, documents, collages, and lists of ideas."
      : "Artwork is generated by local Krea 2 Turbo with lettering explicitly prohibited.");
    typographyHint.replaceChildren(ideogram
      ? (precise
        ? "Precise mode validates and sends this native JSON unchanged; bbox order is [y_min, x_min, y_max, x_max]."
        : "Quick mode protects these exact strings, expands the concept with the local Ideogram Magic Prompt, then applies a collision-safe layout and renders the text natively.")
      : "These exact strings are rendered locally and deterministically.");
  }
  imageModel.onchange = refreshPanels;
  ideogramPromptMode.onchange = refreshPanels;
  refreshPanels();
  save.onclick = async () => {
    save.disabled = true;
    status.replaceChildren("Saving…");
    let precisePrompt = null;
    if (isIdeogram() && ideogramPromptMode.value === "precise") {
      try {
        precisePrompt = JSON.parse(ideogramPromptJson.value);
      } catch (_err) {
        status.replaceChildren("Precise Ideogram JSON is not valid JSON.");
        toast("warning", "Invalid Precise JSON", "Correct the JSON before saving.");
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
      status.replaceChildren("Saved. Existing candidate files are retained; selection was cleared.");
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
  artworkPanelHost.append(panel("Artwork direction",
    artworkHint,
    field({
      label: "Concept prompt", input: prompt,
      hint: "Use a concrete person, object, place, lighting, and composition—not a synopsis.",
    }),
    avoidPromptHost,
    field({
      label: "Seed", input: seed,
      hint: "Base seed — each slot and every regenerate attempt shifts it automatically",
    }),
    field({ label: "Subject position", input: subject }),
    field({ label: "Text placement", input: textSide }),
  ));
  typographyStyleHost.append(
    field({ label: "Palette", input: palette }),
    field({ label: "Font preset", input: fontPreset }),
    field({ label: "Layout preset", input: layoutPreset }),
    el("label", { class: "check-row" }, outline, "High-contrast outline"),
    el("label", { class: "check-row" }, shadow, "Text shadow"),
  );
  ideogramModeHost.append(field({
    label: "Ideogram prompt mode", input: ideogramPromptMode,
    hint: "Quick uses your local LLM; Precise uses canonical native/KJNodes JSON without Magic Prompt.",
  }));
  preciseJsonHost.append(field({
    label: "Precise Ideogram JSON", input: ideogramPromptJson,
    hint: "Text is literal. Coordinates use Ideogram's 0–1000 [y_min, x_min, y_max, x_max] order.",
  }));
  // Exact copy, styling direction, and Save apply to both models. Ideogram
  // treats styling controls as prompt guidance rather than pixel-exact rules.
  typPanelHost.append(panel("Typography",
    typographyHint,
    ideogramModeHost,
    preciseJsonHost,
    field({ label: "Exact title", input: exactTitle }),
    field({
      label: "Exact hook", input: exactHook,
      hint: "Rendered as a small kicker above the title · skipped when it repeats the title",
    }),
    typographyStyleHost,
    el("div", { class: "row mt" }, save, status),
  ));
  return el("div", { class: "thumbnail-plan-grid" },
    panel("Brief",
      field({ label: "Proposed video title", input: proposedTitle }),
      field({ label: "Thumbnail hook", input: briefHook, hint: "Keep it to 4–6 words for mobile." }),
      field({ label: "Audience", input: audience }),
      field({ label: "Topic", input: topic }),
      field({ label: "Style", input: style }),
      field({
        label: "Image model", input: imageModel,
        hint: "Krea: local art + Pillow text overlay · Ideogram: model renders text natively",
      }),
      el("div", { class: "thumbnail-safe-demo", "aria-label": "Mobile safe-area preview" },
        el("span", {}, "Mobile-safe copy area")),
    ),
    artworkPanelHost,
    typPanelHost,
  );
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
    return candidateCard(id, candidateById.get(id), activeBySlot.get(id));
  });
  const legacy = (snapshot.legacy_frames || []).length
    ? el("div", { class: "thumbnail-candidate-grid" },
        ...snapshot.legacy_frames.map(frameCard))
    : emptyState("No extracted frames yet", "Run a final render to create low-cost fallback frames.");
  return el("div", { class: "stack" },
    buildMagicPromptArea(snapshot),
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "Three candidate slots"),
      el("p", { class: "muted small" }, "GPU generations are queued and run sequentially. Completed candidates survive failed regenerations. Deleting a candidate frees its slot for Duplicate or frame promotion."),
      el("div", { class: "thumbnail-candidate-grid" }, ...cards),
    ),
    el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "Final-render frame sources"),
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
      el("div", { class: "panel-title" }, "Ideogram Structured Prompt"),
      el("span", { class: "spacer" }),
      stateBadge,
      regenerate,
    ),
    el("p", { class: "muted small" },
      preciseMode
        ? "Precise mode validates the canonical JSON from the plan and bypasses the local LLM. Candidate generation reuses it unchanged."
        : "Quick mode persists Magic Prompt before Ideogram checks VRAM. Candidate generation reuses it while the plan is unchanged.",
    ),
  ];
  if (valid) {
    const pretty = JSON.stringify(saved.structured_prompt, null, 2);
    children.push(
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
    );
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

function candidateCard(id, candidate, job) {
  const body = el("article", { class: `thumbnail-card${candidate?.selected ? " selected" : ""}` });
  const statusBadge = candidate?.selected
    ? badge("good", "Selected export thumbnail")
    : candidate?.stale
      ? badge("warning", "Stale · regenerate")
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
    body.append(el("div", { class: "thumbnail-provenance" },
      el("span", {}, `${provenance.image_model === "ideogram4_local" ? "Ideogram 4" : provenance.model || "local"} · seed ${provenance.seed ?? "—"}`),
      el("span", {}, provenance.workflow_version || "thumbnail-v1"),
      el("span", { class: "mono" }, `${String(candidate.composite_hash || "").slice(0, 12)}…`),
    ));
  } else {
    body.append(el("div", { class: "thumbnail-placeholder" }, "No candidate generated"));
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
      if (!candidate.stale) {
        actions.push(action("Set as export thumbnail", () => choose(id), "btn btn-primary btn-sm"));
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
