/** Local narration, reference-voice cloning, and expressive editing controls. */

import { el, fmtDate, fmtDuration } from "../dom.js";
import { state, needsProject } from "../state.js";
import {
  activateNarrationTake, clearPerformanceTags, editProject, generateNarration,
  generatePerformanceTags, getPerformanceTags, getProject,
  importRecordedNarration, listNarrationTakes, listVoiceProfiles,
  regenerateNarrationChunk, regeneratePerformanceSegment,
  savePerformanceTags,
  ttsModels, setNarrationTakeGain, unloadTtsProvider, uploadVoiceProfile,
} from "../api.js";
import { loadingState, errorPanel, badge, icon, toast, toastError, confirm, field as sharedField } from "../ui.js";
import { LANGUAGE_PAIRS, openVoiceRecorder } from "../voice-recorder.js";

export function renderVoice(_route) {
  const screen = el("div", { class: "screen" },
    el("div", { class: "screen-head" }, el("h1", {}, "Voice")));
  if (!state.currentProjectId) {
    screen.append(needsProject("Select a project in the top bar to manage narration."));
    return screen;
  }
  const body = el("div", { class: "panel-body" }, loadingState(4));
  const load = async () => {
    body.replaceChildren(loadingState(4));
    try {
      const [snapshot, voices, models, narrations, tags] = await Promise.all([
        getProject(state.config, state.currentProjectId),
        listVoiceProfiles(state.config, state.currentProjectId),
        ttsModels(state.config),
        listNarrationTakes(state.config, state.currentProjectId),
        getPerformanceTags(state.config, state.currentProjectId),
      ]);
      body.replaceChildren(build(
        snapshot, voices.voices || [], models.models || {}, narrations, tags, load,
      ));
    } catch (err) {
      body.replaceChildren(errorPanel(err,
        el("button", { class: "btn", type: "button", onclick: load }, "Retry")));
    }
  };
  screen.append(el("div", { class: "panel" },
    el("div", { class: "row" }, el("span", { class: "panel-title" }, "Local voice cloning"),
      el("span", { class: "spacer" }),
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: load }, "Refresh")),
    body));
  load();
  return screen;
}

function build(snapshot, voices, models, narrations, tags, refresh) {
  const project = snapshot.project;
  const current = project.settings?.voice || {};

  const profileName = el("input", { type: "text", class: "input", placeholder: "My narrator" });
  const transcript = el("textarea", { class: "input", placeholder: "Exact words in the WAV" });
  const profileLanguage = languageSelect(current.language || "en");
  const consent = el("input", { type: "checkbox" });

  let sourceBlob = null;
  let sourceUrl = null;
  let lastPrompt = "";
  let previewContext = null;
  let previewGainNode = null;
  const sourceLabel = el("span", { class: "muted small" }, "No audio chosen yet.");
  const sourcePreview = el("audio", { controls: true, preload: "metadata" });
  const gainValue = el("output", { class: "mono small" }, "0 dB");
  const gain = el("input", {
    id: "voice-reference-boost",
    type: "range", class: "input", min: "0", max: "24", step: "1", value: "0",
  });
  const syncPreviewGain = () => {
    const db = Number(gain.value);
    gainValue.textContent = db ? `+${db} dB` : "0 dB";
    if (previewGainNode) previewGainNode.gain.value = 10 ** (db / 20);
  };
  gain.oninput = syncPreviewGain;
  const refBoostLabel = el("label", {}, "Reference boost");
  refBoostLabel.setAttribute("for", gain.id);
  sourcePreview.addEventListener("play", () => {
    if (!previewContext) {
      const Context = window.AudioContext || window.webkitAudioContext;
      if (Context) {
        try {
          previewContext = new Context();
          previewGainNode = previewContext.createGain();
          previewContext.createMediaElementSource(sourcePreview).connect(previewGainNode);
          previewGainNode.connect(previewContext.destination);
          syncPreviewGain();
        } catch {
          previewContext = null;
          previewGainNode = null;
        }
      }
    }
    if (previewContext?.state === "suspended") previewContext.resume().catch(() => {});
  });
  const previewBox = el("div", { class: "vp-preview", hidden: true },
    el("div", { class: "row" },
      el("strong", { class: "small" }, "Selected audio"),
      el("span", { class: "spacer" }),
      sourceLabel),
    sourcePreview,
    el("div", { class: "field" },
      el("div", { class: "row" }, refBoostLabel,
        el("span", { class: "spacer" }), gainValue),
      gain,
      el("div", { class: "hint" },
        "Boosts this preview and the saved cloning reference. Lower it if the voice sounds distorted.")));

  const recordCard = el("button", { class: "source-card", type: "button" },
    el("span", { class: "sc-icon" }, icon("mic", 20)),
    el("span", { class: "sc-body" },
      el("span", { class: "sc-title" }, "Record in browser"),
      el("span", { class: "sc-desc" },
        "Read a short guided passage with your microphone — nothing leaves this machine.")));
  const importCard = el("button", { class: "source-card", type: "button" },
    el("span", { class: "sc-icon" }, icon("folder", 20)),
    el("span", { class: "sc-body" },
      el("span", { class: "sc-title" }, "Import a WAV"),
      el("span", { class: "sc-desc" }, "Bring a clean PCM WAV you recorded elsewhere.")));

  const markSource = (active) => {
    for (const card of [recordCard, importCard]) card.classList.toggle("active", card === active);
  };
  const setSource = (blob, label, promptText) => {
    if (sourceUrl) URL.revokeObjectURL(sourceUrl);
    sourceBlob = blob;
    sourceUrl = URL.createObjectURL(blob);
    sourcePreview.src = sourceUrl;
    previewBox.hidden = false;
    sourceLabel.textContent = label;
    if (promptText) {
      const existing = transcript.value.trim();
      if (!existing || existing === lastPrompt) transcript.value = promptText;
      lastPrompt = promptText;
    }
  };

  recordCard.onclick = () => {
    openVoiceRecorder({
      language: profileLanguage.value,
      onUse: (take) => {
        profileLanguage.value = take.language;
        setSource(take.blob, `Recorded take · ${fmtDuration(take.seconds)}`, take.promptText);
        markSource(recordCard);
        if (!profileName.value.trim()) profileName.value = "My recorded voice";
      },
    });
  };

  const reference = el("input", {
    type: "file", accept: ".wav,audio/wav", class: "sr-only",
    "aria-label": "Choose a reference WAV",
  });
  reference.onchange = () => {
    const file = reference.files?.[0];
    if (!file) return;
    setSource(file, `${file.name} · ${fmtBytes(file.size)}`);
    markSource(importCard);
  };
  importCard.onclick = () => reference.click();

  const upload = el("button", { class: "btn btn-primary", type: "button" }, "Save voice profile");
  upload.onclick = async () => {
    if (!sourceBlob || !profileName.value.trim() || !consent.checked) {
      toast("critical", "Reference voice incomplete",
        "Record or import audio, name it, and confirm authorization.");
      return;
    }
    upload.disabled = true;
    try {
      await uploadVoiceProfile(state.config, project.id, sourceBlob, {
        name: profileName.value.trim(), transcript: transcript.value.trim(),
        language: profileLanguage.value, authorized: true, gain_db: Number(gain.value),
      });
      toast("good", "Voice profile saved", "The reference remains local to this project.");
      refresh();
    } catch (err) { toastError(err, "save voice profile"); }
    finally { upload.disabled = false; }
  };

  const provider = el("select", { class: "input" },
    modelOption("qwen_tts", "Qwen3-TTS 1.7B", models),
    modelOption("step_audio_editx", "Step-Audio-EditX", models),
    modelOption("chatterbox", "Chatterbox Multilingual V3", models),
    modelOption("index_tts_2_5", "IndexTTS 2.5", models),
    modelOption("voxcpm2", "VoxCPM2 2B", models),
    modelOption("omnivoice", "OmniVoice", models),
    modelOption("fish_s2_pro", "Fish Audio S2 Pro", models),
    modelOption("breeze_tts_2", "Breeze TTS 2 (≈3.5B)", models));
  provider.value = current.provider || "qwen_tts";
  const chatterboxBuiltIn = "__chatterbox_builtin__";
  const qwenBuiltInPrefix = "__qwen_builtin__:";
  const builtInOption = el("option", { value: chatterboxBuiltIn }, "Built-in Chatterbox voice");
  const qwenSpeakers = [
    ["Ryan", "Ryan — dynamic English male"],
    ["Aiden", "Aiden — sunny American male"],
    ["Vivian", "Vivian — bright young Chinese female"],
    ["Serena", "Serena — warm young Chinese female"],
    ["Uncle_Fu", "Uncle Fu — low, seasoned Chinese male"],
    ["Dylan", "Dylan — youthful Beijing male"],
    ["Eric", "Eric — lively Chengdu male"],
    ["Ono_Anna", "Ono Anna — playful Japanese female"],
    ["Sohee", "Sohee — warm Korean female"],
  ];
  const qwenOptions = qwenSpeakers.map(([value, label]) =>
    el("option", { value: `${qwenBuiltInPrefix}${value}` }, `Qwen built-in: ${label}`));
  const voice = el("select", { class: "input" },
    builtInOption,
    ...qwenOptions,
    el("option", { value: "" }, voices.length ? "Select a saved profile" : "No saved profiles"),
    ...voices.map((item) => el("option", { value: item.id }, item.name)));
  voice.value = current.voice_profile_id ||
    (provider.value === "chatterbox" ? chatterboxBuiltIn :
      provider.value === "qwen_tts" ? `${qwenBuiltInPrefix}${current.speaker || "Ryan"}` : "");
  const language = languageSelect(current.language || "en");
  const chunk = el("input", { type: "number", class: "input", min: "5", max: "180",
    step: "5", value: current.chunk_seconds || "", placeholder: "Model default" });
  const pause = el("input", { type: "number", class: "input", min: "0", max: "5000",
    step: "50", value: current.pause_ms ?? 350 });
  const script = el("textarea", { class: "input", rows: "9",
    placeholder: "Leave blank to use planned scene narration." });
  const hasPlannedNarration = (snapshot.scenes || []).some(
    (scene) => String(scene.narration || "").trim());
  const enhance = el("input", { type: "checkbox", checked: !!current.enhance_with_step });
  const editType = el("select", { class: "input" },
    ...["emotion", "style", "speed", "paralinguistic"].map(
      (value) => el("option", { value }, value)));
  const instruction = el("input", { type: "text", class: "input",
    value: current.step_instruction || "", placeholder: "e.g. warm, serious storytelling" });
  const voiceInstruction = el("input", { type: "text", class: "input", maxlength: "500",
    value: current.voice_instruction || "", placeholder: "e.g. calmly, like a documentary host" });
  const cfgValue = el("input", { type: "number", class: "input", min: "1", max: "10",
    step: "0.1", value: current.guidance_scale ?? "", placeholder: "Model default (2.0)" });
  const timesteps = el("input", { type: "number", class: "input", min: "1", max: "100",
    step: "1", value: current.inference_timesteps ?? "", placeholder: "Model default (10)" });
  const omniGuidance = el("input", { type: "number", class: "input", min: "0", max: "20",
    step: "0.1", value: current.guidance_scale ?? "", placeholder: "Model default (2.0)" });
  const omniSteps = el("input", { type: "number", class: "input", min: "1", max: "128",
    step: "1", value: current.num_steps ?? "", placeholder: "Model default (32)" });
  const omniSpeed = el("input", { type: "number", class: "input", min: "0.5", max: "2",
    step: "0.05", value: current.speed ?? "", placeholder: "Model default (1.0)" });
  const cloneProviders = ["fish_s2_pro", "voxcpm2", "omnivoice", "index_tts_2_5", "breeze_tts_2"];
  const breezeEngine = el("select", { class: "input" },
    el("option", { value: "eager" }, "Eager — default, lower VRAM (~7.7 GiB)"),
    el("option", { value: "fast" }, "Fast — experimental, ~14.4 GiB VRAM (needs ~20 GiB free)"));
  breezeEngine.value = current.breeze_mode || "eager";
  const breezeDirection = el("input", { type: "text", class: "input", maxlength: "500",
    value: current.voice_instruction || "",
    placeholder: "e.g. calm, documentary narrator pace (optional)" });
  const breezeCfg = el("input", { type: "number", class: "input", min: "0.1", max: "20",
    step: "0.1", value: "", placeholder: "Auto: 1.0 plain clone, 4.0 with direction" });
  const breezeGrid = el("div", { class: "pref-grid" },
    field("Breeze engine", breezeEngine,
      "Eager is the reliable default. Fast keeps the same model quality but uses optimized execution and needs ~20 GiB free VRAM."),
    field("Voice direction", breezeDirection,
      "Optional style instruction; keeps your cloned voice but steers tone, pace, and delivery."),
    field("CFG scale", breezeCfg,
      "Blank = auto (1.0 plain clone, 4.0 with voice direction). Raise to follow the direction more strictly."));
  const voxGrid = el("div", { class: "pref-grid" },
    field("CFG scale", cfgValue,
      "VoxCPM2 cfg_value: higher follows the text more strictly, lower sounds more natural."),
    field("Diffusion steps", timesteps, "VoxCPM2 inference_timesteps: more steps, better quality."));
  const omniGrid = el("div", { class: "pref-grid" },
    field("Guidance scale", omniGuidance, "OmniVoice diffusion guidance strength."),
    field("Sampling steps", omniSteps, "OmniVoice num_step."),
    field("Speed", omniSpeed, "OmniVoice speech speed factor."));
  const stepGrid = el("div", { class: "pref-grid" },
    field("Step edit type", editType, "Used only for Qwen → Step."),
    field("Step instruction", instruction, "A model-supported edit description."));
  const enhanceRow = el("label", { class: "check-row" }, enhance,
    " Preserve Qwen audio and run a Step expressive-edit pass");
  const performance = performancePanel(project, current, tags, provider, script, refresh);

  let recordedBlob = null;
  let recordedUrl = null;
  const recordedName = el("input", {
    type: "text", class: "input", maxlength: "100", value: "My recorded voiceover",
  });
  const recordedLabel = el("span", { class: "muted small" }, "No recording chosen yet.");
  const recordedPreview = el("audio", { controls: true, preload: "metadata", hidden: true });
  const setRecordedSource = (blob, label) => {
    if (recordedUrl) URL.revokeObjectURL(recordedUrl);
    recordedBlob = blob;
    recordedUrl = URL.createObjectURL(blob);
    recordedPreview.src = recordedUrl;
    recordedPreview.hidden = false;
    recordedLabel.textContent = label;
  };
  const plannedScript = (snapshot.scenes || [])
    .map((scene) => String(scene.narration || "").trim()).filter(Boolean).join("\n\n");
  const recordVoiceover = el("button", { class: "source-card", type: "button" },
    el("span", { class: "sc-icon" }, icon("mic", 20)),
    el("span", { class: "sc-body" },
      el("span", { class: "sc-title" }, "Record full voiceover"),
      el("span", { class: "sc-desc" },
        "Read the complete project narration and use the recording directly — no voice cloning.")));
  recordVoiceover.onclick = () => openVoiceRecorder({
    language: current.language || "en",
    purpose: "voiceover",
    promptText: plannedScript,
    maxSeconds: Math.max(300, Math.min(3600, Math.ceil(Number(project.target_duration || 0) * 2))),
    onUse: (take) => setRecordedSource(
      take.blob, `Recorded voiceover · ${fmtDuration(take.seconds)}`),
  });
  const recordedFile = el("input", {
    type: "file", accept: ".wav,audio/wav", class: "sr-only",
    "aria-label": "Choose a complete recorded voiceover WAV",
  });
  recordedFile.onchange = () => {
    const file = recordedFile.files?.[0];
    if (!file) return;
    setRecordedSource(file, `${file.name} · ${fmtBytes(file.size)}`);
    if (recordedName.value === "My recorded voiceover") {
      recordedName.value = file.name.replace(/\.wav$/i, "") || recordedName.value;
    }
  };
  const importVoiceover = el("button", { class: "source-card", type: "button" },
    el("span", { class: "sc-icon" }, icon("folder", 20)),
    el("span", { class: "sc-body" },
      el("span", { class: "sc-title" }, "Import full voiceover"),
      el("span", { class: "sc-desc" }, "Use a complete PCM WAV you recorded elsewhere.")));
  importVoiceover.onclick = () => recordedFile.click();
  const useRecording = el("button", { class: "btn btn-primary", type: "button" },
    "Use recorded voiceover");
  useRecording.onclick = async () => {
    if (!recordedBlob || !recordedName.value.trim()) {
      toast("critical", "Voiceover required", "Record or import a complete WAV and give it a name.");
      return;
    }
    useRecording.disabled = true;
    try {
      await importRecordedNarration(
        state.config, project.id, recordedBlob, recordedName.value.trim());
      toast("good", "Recorded voiceover selected",
        "Classic and Editorial renders will now use this recording. Rebuild captions for exact timing.");
      refresh();
    } catch (err) { toastError(err, "use recorded voiceover"); }
    finally { useRecording.disabled = false; }
  };
  const syncProviderControls = () => {
    const chatterbox = provider.value === "chatterbox";
    const qwen = provider.value === "qwen_tts";
    const cloneOnly = cloneProviders.includes(provider.value);
    builtInOption.disabled = !chatterbox;
    for (const option of qwenOptions) option.disabled = !qwen;
    if (chatterbox && (!voice.value || voice.value.startsWith(qwenBuiltInPrefix))) {
      voice.value = chatterboxBuiltIn;
    }
    if (qwen && (!voice.value || voice.value === chatterboxBuiltIn)) {
      voice.value = `${qwenBuiltInPrefix}${current.speaker || "Ryan"}`;
    }
    if ((!chatterbox && !qwen) &&
        (voice.value === chatterboxBuiltIn || voice.value.startsWith(qwenBuiltInPrefix))) {
      voice.value = voices[0]?.id || "";
    }
    const referenceFreeQwen = qwen && voice.value.startsWith(qwenBuiltInPrefix);
    enhance.disabled = !qwen || referenceFreeQwen;
    if (enhance.disabled) enhance.checked = false;
    voiceInstruction.disabled = !referenceFreeQwen;
    voxGrid.hidden = provider.value !== "voxcpm2";
    omniGrid.hidden = provider.value !== "omnivoice";
    breezeGrid.hidden = provider.value !== "breeze_tts_2";
    stepGrid.hidden = !qwen;
    enhanceRow.hidden = !qwen;
    // Delivery tags are a Fish S2 Pro feature: hide the panel for every other
    // provider and force the toggle off so cues can never be sent elsewhere.
    performance.hidden = provider.value !== "fish_s2_pro";
    if (provider.value !== "fish_s2_pro") performance.useTags.checked = false;
  };
  provider.onchange = syncProviderControls;
  voice.onchange = syncProviderControls;
  syncProviderControls();
  const generate = el("button", { class: "btn btn-primary", type: "button" }, "Generate narration");
  generate.onclick = async () => {
    const builtInChatterbox = voice.value === chatterboxBuiltIn;
    const builtInQwen = voice.value.startsWith(qwenBuiltInPrefix);
    const builtIn = builtInChatterbox || builtInQwen;
    if (!voice.value || (builtInChatterbox && provider.value !== "chatterbox") ||
        (builtInQwen && provider.value !== "qwen_tts")) {
      toast("critical", "Voice profile required", "Select or upload an authorized reference voice.");
      return;
    }
    if (!script.value.trim() && !hasPlannedNarration) {
      toast("critical", "Script required",
        "Run planning from the Script screen, or enter text in Script override.");
      return;
    }
    const settings = {
      provider: provider.value, voice_profile_id: builtIn ? null : voice.value,
      language: language.value,
      chunk_seconds: chunk.value ? Number(chunk.value) : null, pause_ms: Number(pause.value),
      enhance_with_step: enhance.checked, step_edit_type: editType.value,
      step_instruction: instruction.value.trim(),
      speaker: builtInQwen ? voice.value.slice(qwenBuiltInPrefix.length) : "Ryan",
      voice_instruction: builtInQwen ? voiceInstruction.value.trim()
        : (provider.value === "breeze_tts_2" ? breezeDirection.value.trim() : ""),
      guidance_scale: null, inference_timesteps: null, num_steps: null, speed: null,
      breeze_mode: "eager",
      // Fish S2 Pro delivery tags: only sent when the toggle is on and the
      // provider is S2 Pro (the panel forces it off otherwise).
      use_performance_tags: performance.useTags.checked && provider.value === "fish_s2_pro",
      intensity: performance.intensity.value,
      performance_notes: performance.notes.value.trim(),
    };
    if (provider.value === "breeze_tts_2") {
      settings.breeze_mode = breezeEngine.value;
      settings.guidance_scale = breezeCfg.value ? Number(breezeCfg.value) : null;
    } else if (provider.value === "voxcpm2") {
      settings.guidance_scale = cfgValue.value ? Number(cfgValue.value) : null;
      settings.inference_timesteps = timesteps.value ? Number(timesteps.value) : null;
    } else if (provider.value === "omnivoice") {
      settings.guidance_scale = omniGuidance.value ? Number(omniGuidance.value) : null;
      settings.num_steps = omniSteps.value ? Number(omniSteps.value) : null;
      settings.speed = omniSpeed.value ? Number(omniSpeed.value) : null;
    }
    generate.disabled = true;
    try {
      await editProject(state.config, project.id, { settings: { voice: settings } });
      // intensity / performance_notes are persisted settings, not NarrationRequest
      // fields, so they are stripped from the generation body.
      const { intensity: _intensity, performance_notes: _notes, ...requestSettings } = settings;
      const job = await generateNarration(state.config, project.id,
        { ...requestSettings, text: script.value.trim() || null });
      toast("good", "Narration queued", `Job ${job.id.slice(0, 8)} will run locally.`);
      refresh();
    } catch (err) { toastError(err, "generate narration"); }
    finally { generate.disabled = false; }
  };

  return el("div", { class: "stack" },
    el("div", { class: "consent-warning" }, icon("alert", 18),
      el("span", {}, "Only clone a voice you own or have permission to use. Audio remains local."),
      el("div", { class: "hint", style: { marginTop: "4px" } },
        "Breeze TTS 2 note: its model weights (and self-hosted outputs) are licensed for "
        + "non-commercial use — fine for this channel today; re-check before monetizing.")),
    section("1. Reference voice",
      el("div", { class: "source-grid" }, recordCard, importCard, reference),
      previewBox,
      el("div", { class: "pref-grid" },
        field("Profile name", profileName, "Reusable within this project."),
        field("Language", profileLanguage, "Language of the reference audio."),
        field("Exact transcript", transcript,
          "Filled in automatically after a recording; recommended for imports.")),
      el("label", { class: "check-row" }, consent,
        " I confirm I own or have permission to clone this voice."),
      el("div", { class: "row" }, upload,
        el("span", { class: "spacer" }),
        el("span", { class: "muted small" }, "Audio never leaves this machine.")),
      savedVoicesPanel(voices)),
    section("2. Use your recorded voiceover",
      el("p", { class: "muted small" },
        "This becomes the master narration without running TTS or cloning your voice. "
        + "Both Classic and Editorial modes follow its real duration."),
      el("div", { class: "source-grid" }, recordVoiceover, importVoiceover, recordedFile),
      el("div", { class: "vp-preview" },
        el("div", { class: "row" }, recordedLabel), recordedPreview),
      field("Take name", recordedName, "Shown in the narration-take library."),
      el("div", { class: "row" }, useRecording)),
    section("3. Generate narration with a local model",
      el("div", { class: "pref-grid" },
        field("TTS model", provider, "Worker readiness is shown in the label."),
        field("Voice", voice,
          cloneProviders.includes(provider.value)
            ? "This model clones an authorized saved profile."
            : "Qwen CustomVoice and Chatterbox include reference-free voices."),
        field("Language", language, "Generation language."),
        field("Qwen delivery", voiceInstruction, "Optional style instruction for a built-in Qwen voice."),
        field("Chunk seconds", chunk, "Defaults: Qwen 60, Step 20, Chatterbox 45, comparison models 30."),
        field("Minimum pause (ms)", pause,
          "Minimum silence between chunks; existing generated silence counts toward it."),
        field("Script override", script, hasPlannedNarration
          ? "Blank uses planned scene narration and enables exact picture sync. Overrides are not mapped to scenes."
          : "This project has no planned narration yet. Enter text here or run planning from Script.")),
      breezeGrid,
      voxGrid,
      omniGrid,
      enhanceRow,
      stepGrid,
      performance,
      el("div", { class: "row" }, generate)),
    workerControlsPanel(models, refresh),
    takeLibraryPanel(
      project.id, narrations.takes || [], narrations.active_asset_id || null,
      snapshot.stage_state?.stages?.narration, refresh,
    ));
}

function languageSelect(value) {
  const select = el("select", { class: "input" },
    ...LANGUAGE_PAIRS.map(([code, label]) => el("option", { value: code }, label)));
  select.value = value;
  return select;
}

function savedVoicesPanel(voices) {
  if (!voices.length) {
    return el("p", { class: "muted small" },
      "No saved voices yet. Record or import your first reference above — it then appears in the Voice dropdown below.");
  }
  return el("div", { class: "stack" },
    el("div", { class: "panel-title" }, "Saved voices"),
    el("div", { class: "voice-profile-list" },
      ...voices.map((item) => el("div", { class: "voice-profile-row saved-voice" },
        icon("mic", 15),
        el("div", { class: "stack saved-voice-body" },
          el("div", { class: "row" },
            el("span", { class: "vp-name" }, item.name),
            el("span", { class: "tag" }, String(item.language || "en").toUpperCase()),
            item.authorized ? badge("good", "authorized", false)
              : badge("warning", "unauthorized", false),
            item.gain_db ? el("span", { class: "tag" }, `+${item.gain_db} dB boost`) : "",
            el("span", { class: "spacer" }),
            el("span", { class: "muted small" }, item.created_at ? fmtDate(item.created_at) : "")),
          item.url ? el("audio", {
            controls: true, preload: "metadata", src: item.url,
            "aria-label": `Play reference voice ${item.name}`,
          }) : el("span", { class: "muted small" }, "Reference audio unavailable."))))));
}

function fmtBytes(bytes) {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function modelOption(value, label, models) {
  const ready = models[value]?.health?.status === "healthy";
  const suffix = ready ? "ready" : models[value]?.managed ? "starts automatically" : "offline";
  return el("option", { value },
    `${label} — ${suffix}`);
}

function modelStatusLine(provider, models) {
  const entry = models[provider] || null;
  if (!entry) {
    return el("span", { class: "muted small" },
      "This provider is not registered on the backend.");
  }
  if (entry.health?.status === "healthy") {
    return el("span", { class: "muted small" }, "Loaded and ready.");
  }
  if (entry.managed) {
    return el("span", { class: "muted small" },
      "Configured for automatic worker startup — first use will load the model.");
  }
  return el("span", { class: "muted small" },
    "Not configured. Set managed=true and the worker details in config.");
}

function workerControlsPanel(models, refresh) {
  const provider = el("select", { class: "input" },
    ...Object.keys(models).map((name) =>
      el("option", { value: name }, providerLabel(name))));
  const status = el("div", { class: "hint" }, modelStatusLine(provider.value, models));
  const unload = el("button", {
    class: "btn btn-ghost btn-sm", type: "button",
  }, "Unload model from memory");
  const refreshStatus = () => {
    status.replaceChildren(modelStatusLine(provider.value, models));
  };
  provider.onchange = refreshStatus;
  unload.onclick = async () => {
    if (!models[provider.value]) {
      toast("critical", "Unknown provider",
        `${providerLabel(provider.value)} is not available on this backend.`);
      return;
    }
    unload.disabled = true;
    try {
      const result = await unloadTtsProvider(state.config, provider.value);
      const note = result.stopped_owned_worker
        ? "Worker process stopped; weights and memory released."
        : "Weights released; worker stays running for the next job.";
      toast("good", `${providerLabel(provider.value)} unloaded`, note);
      refresh();
    } catch (err) {
      toastError(err, `unload ${providerLabel(provider.value)}`);
    } finally {
      unload.disabled = false;
    }
  };
  return section("3. Worker controls",
    el("p", { class: "muted small" },
      "Free VRAM between jobs. Stops only the worker process owned by this Studio; ",
      "an externally run worker is left running."),
    el("div", { class: "pref-grid" },
      field("TTS worker", provider,
        "Unload the loaded model weights and stop this Studio's worker process."),
    ),
    el("div", { class: "row" }, status, el("span", { class: "spacer" }), unload));
}

/**
 * Positional wrapper over the shared labeled-field helper (ui.js), which
 * associates the label with its control (for/aria-describedby).
 * @param {string} label
 * @param {HTMLElement} input
 * @param {string} [hint]
 * @returns {HTMLElement}
 */
function field(label, input, hint) {
  return sharedField({ label, input, hint });
}

/**
 * Fish S2 Pro delivery-tags panel. Hidden for every other provider; the
 * narration toggle is forced off there so cues can never reach another model.
 * Returns the panel element with `useTags`, `intensity`, and `notes` attached
 * so the generate handler can read them.
 */
function performancePanel(project, current, tags, provider, script, refresh) {
  const scriptData = tags?.script || null;
  const stale = !!tags?.stale;
  const tagCount = tags?.tag_count || 0;
  const llm = tags?.llm || { available: false, model: null };

  const intensity = el("select", { class: "input" },
    el("option", { value: "subtle" }, "Subtle — a few cues"),
    el("option", { value: "balanced" }, "Balanced — moderate"),
    el("option", { value: "expressive" }, "Expressive — frequent"));
  intensity.value = current.intensity || scriptData?.intensity || "balanced";
  const notes = el("input", { type: "text", class: "input", maxlength: "2000",
    value: current.performance_notes || "",
    placeholder: "e.g. keep it grounded, documentary pace" });
  const useTags = el("input", { type: "checkbox",
    checked: !!(current.use_performance_tags && provider.value === "fish_s2_pro") });

  const statusLine = el("div", { class: "hint" });
  const renderStatus = () => {
    const parts = [];
    parts.push(llm.available
      ? `Local LLM: ${llm.model || "auto"}.`
      : "Local LLM is not available — start it (or disable mock mode) to add tags.");
    if (scriptData) {
      parts.push(`${tagCount} cue${tagCount === 1 ? "" : "s"} across ` +
        `${scriptData.segments.length} segment${scriptData.segments.length === 1 ? "" : "s"}.`);
      if (stale) parts.push("The narration changed since these tags were generated — regenerate for a fresh pass.");
    } else {
      parts.push("No delivery tags yet.");
    }
    statusLine.replaceChildren(parts.join(" "));
  };
  renderStatus();

  const generateTags = el("button", { class: "btn", type: "button" },
    scriptData ? "Regenerate tags" : "Add delivery tags with local LLM");
  generateTags.disabled = !llm.available;
  generateTags.onclick = async () => {
    generateTags.disabled = true;
    try {
      const result = await generatePerformanceTags(state.config, project.id, {
        intensity: intensity.value,
        notes: notes.value.trim(),
        force: !!scriptData,
        text: script.value.trim() || null,
      });
      for (const warning of result.warnings || []) {
        toast("warning", "Delivery tags", warning);
      }
      toast("good", "Delivery tags ready",
        `${result.tag_count} cue${result.tag_count === 1 ? "" : "s"} added.`);
      refresh();
    } catch (err) { toastError(err, "add delivery tags"); }
    finally { generateTags.disabled = false; }
  };

  // Per-segment editors, shown only when a script exists.
  const segmentEditors = (scriptData?.segments || []).map((seg, segIndex) => {
    const label = seg.scene_index != null
      ? `Scene ${seg.scene_index + 1}${seg.scene_title ? ` · ${seg.scene_title}` : ""}`
      : "Script override";
    const textarea = el("textarea", { id: `perfseg-${segIndex}`, class: "input", rows: "3" }, seg.tagged);
    const segLabel = el("label", {}, label);
    segLabel.setAttribute("for", textarea.id);
    const clearTags = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Clear tags");
    clearTags.onclick = () => { textarea.value = seg.source; };
    const regenSeg = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Regenerate");
    regenSeg.disabled = !llm.available;
    regenSeg.onclick = async () => {
      regenSeg.disabled = true;
      try {
        const result = await regeneratePerformanceSegment(state.config, project.id, {
          key: seg.key,
          intensity: intensity.value,
          notes: notes.value.trim(),
        });
        for (const warning of result.warnings || []) {
          toast("warning", "Delivery tags", warning);
        }
        toast("good", "Segment re-tagged", `${label} was re-tagged with the local LLM.`);
        refresh();
      } catch (err) { toastError(err, "regenerate segment tags"); }
      finally { regenSeg.disabled = !llm.available; }
    };
    return {
      key: seg.key, textarea,
      node: el("div", { class: "field" },
        el("div", { class: "row" }, segLabel,
          el("span", { class: "spacer" }), regenSeg, clearTags),
        textarea,
        el("div", { class: "hint" }, `Clean source: ${seg.source}`)),
    };
  });

  const saveEdits = el("button", { class: "btn", type: "button" }, "Save edits");
  const saveAnyway = el("button", { class: "btn btn-ghost", type: "button", hidden: true },
    "Save anyway (keep my edit)");
  const doSave = async (accept) => {
    const button = accept ? saveAnyway : saveEdits;
    button.disabled = true;
    try {
      await savePerformanceTags(state.config, project.id, {
        segments: segmentEditors.map((s) => ({ key: s.key, tagged: s.textarea.value })),
      }, { accept });
      toast("good", "Delivery tags saved",
        accept ? "Kept your hand edit even though the validator flagged it." : "");
      refresh();
    } catch (err) {
      if (!accept && err.status === 422) {
        // Validation failed: surface the reason and offer the escape hatch.
        statusLine.replaceChildren(String(err.message || err));
        saveAnyway.hidden = false;
      } else {
        toastError(err, "save delivery tags");
      }
    } finally { button.disabled = false; }
  };
  saveEdits.onclick = () => doSave(false);
  saveAnyway.onclick = () => doSave(true);

  const removeTags = el("button", { class: "btn btn-ghost", type: "button" }, "Remove all tags");
  removeTags.onclick = async () => {
    if (!scriptData) return;
    const ok = await confirm({
      title: "Remove delivery tags",
      message: "Remove all delivery tags for this project? The clean narration is untouched.",
      confirmLabel: "Remove",
    });
    if (!ok) return;
    try {
      await clearPerformanceTags(state.config, project.id);
      toast("good", "Delivery tags removed", "");
      refresh();
    } catch (err) { toastError(err, "remove delivery tags"); }
  };

  const useTagsRow = el("label", { class: "check-row" }, useTags,
    " Use delivery tags for this narration (Fish S2 Pro only)");

  const panel = el("div", { class: "stack", hidden: provider.value !== "fish_s2_pro" },
    el("div", { class: "panel-title" }, "Delivery tags (Fish S2 Pro)"),
    el("p", { class: "muted small" },
      "Adds [square bracket] delivery cues — tone, emotion, and sound effects — that S2 Pro "
      + "interprets without speaking them. Cues never reach captions or any other model."),
    statusLine,
    el("div", { class: "pref-grid" },
      field("Intensity", intensity, "How many cues the local LLM should add."),
      field("Focus notes", notes, "Optional direction for the tagger, e.g. pacing or mood.")),
    el("div", { class: "row" }, generateTags, el("span", { class: "spacer" }),
      ...(scriptData ? [removeTags] : [])),
    ...(segmentEditors.length ? [
      el("div", { class: "panel-title" }, "Tagged segments"),
      ...segmentEditors.map((s) => s.node),
      el("div", { class: "row" }, saveEdits, saveAnyway),
    ] : []),
    useTagsRow);
  return Object.assign(panel, { useTags, intensity, notes });
}

function section(title, ...children) {
  return el("div", { class: "panel" }, el("div", { class: "panel-title" }, title),
    el("div", { class: "panel-body stack" }, ...children));
}

function takeLibraryPanel(projectId, takes, activeId, stage, refresh) {
  if (!takes.length) {
    return el("div", { class: "panel" },
      el("div", { class: "panel-title" }, "Narration takes"),
      el("div", { class: "panel-body" },
        el("p", { class: "muted small" }, "No narration takes yet.")));
  }
  const groups = new Map();
  for (const take of [...takes].reverse()) {
    const key = take.settings?.provider || take.backend || "Unknown model";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(take);
  }
  return el("div", { class: "panel" },
    el("div", { class: "row" },
      el("span", { class: "panel-title" }, "Narration takes"),
      el("span", { class: "spacer" }),
      badge(stage?.status === "completed" ? "good" : "neutral", stage?.status || "pending")),
    el("div", { class: "panel-body stack" },
      ...[...groups.entries()].map(([provider, providerTakes]) =>
        el("div", { class: "stack" },
          el("div", { class: "row" },
            el("strong", {}, providerLabel(provider)),
            el("span", { class: "muted small" }, `${providerTakes.length} take${providerTakes.length === 1 ? "" : "s"}`)),
          ...providerTakes.map((take) => takeCard(projectId, take, take.id === activeId, refresh))))));
}

function takeCard(projectId, take, active, refresh) {
  const settings = take.settings || {};
  const request = settings.request || {};
  const choose = el("button", {
    class: active ? "btn btn-sm btn-ghost" : "btn btn-sm",
    type: "button", disabled: active,
  }, active ? "Currently in use" : "Use this narration");
  choose.onclick = async () => {
    choose.disabled = true;
    try {
      await activateNarrationTake(state.config, projectId, take.id);
      toast("good", "Narration selected", "Captions and render outputs will rebuild from this take.");
      refresh();
    } catch (err) { toastError(err, "select narration take"); }
    finally { choose.disabled = false; }
  };
  const voice = settings.voice_profile_id ||
    settings.display_name ||
    (settings.built_in_voice ? `${settings.speaker || "built-in"}` : "—");
  const takeAudio = take.url
    ? el("audio", { controls: true, preload: "metadata", src: take.url })
    : null;
  const gain = el("input", {
    id: `narration-take-gain-${take.id}`,
    type: "range", class: "input", min: "0", max: "24", step: "1",
    value: String(take.gain_db || 0),
  });
  const gainValue = el("output", { class: "mono small" });
  const gainLabel = el("label", { class: "small", style: { fontWeight: "650" } }, "Full narration boost");
  gainLabel.setAttribute("for", gain.id);
  let previewContext = null;
  let previewGainNode = null;
  const syncGain = () => {
    const db = Number(gain.value);
    gainValue.textContent = db ? `+${db} dB` : "0 dB";
    if (previewGainNode) previewGainNode.gain.value = 10 ** (db / 20);
  };
  gain.oninput = syncGain;
  syncGain();
  if (takeAudio) {
    takeAudio.addEventListener("play", () => {
      if (!previewContext) {
        const Context = window.AudioContext || window.webkitAudioContext;
        if (Context) {
          try {
            previewContext = new Context();
            previewGainNode = previewContext.createGain();
            previewContext.createMediaElementSource(takeAudio).connect(previewGainNode);
            previewGainNode.connect(previewContext.destination);
            syncGain();
          } catch {
            previewContext = null;
            previewGainNode = null;
          }
        }
      }
      if (previewContext?.state === "suspended") previewContext.resume().catch(() => {});
    });
  }
  const saveGain = el("button", { class: "btn btn-sm", type: "button" }, "Save boost");
  saveGain.onclick = async () => {
    saveGain.disabled = true;
    try {
      const db = Number(gain.value);
      await setNarrationTakeGain(state.config, projectId, take.id, db);
      toast("good", "Narration boost saved",
        active
          ? "Preview and final renders will use this boost."
          : "This boost will be used when you select this take.");
      refresh();
    } catch (err) { toastError(err, "save narration boost"); }
    finally { saveGain.disabled = false; }
  };
  return el("div", { class: "voice-profile-row narration-take" },
    el("div", { class: "stack narration-take-body" },
      el("div", { class: "row" },
        el("strong", {}, take.model || providerLabel(take.backend)),
        active ? badge("good", "active", false) : "",
        settings.timing_mode === "scene_audio_v1"
          ? badge("good", "scene synced", false)
          : settings.timing_mode === "recorded_master_v1"
            ? badge("neutral", "recorded master", false)
            : badge("warning", "legacy timing", false),
        el("span", { class: "spacer" }),
        el("span", { class: "muted small" }, take.created_at ? fmtDate(take.created_at) : "—")),
      el("dl", { class: "kv" },
        el("dt", {}, "File"), el("dd", { class: "mono" }, take.filepath || "—"),
        el("dt", {}, "Voice"), el("dd", {}, voice),
        el("dt", {}, "Seed"), el("dd", {}, String(take.seed ?? "—")),
        el("dt", {}, "Language"), el("dd", {}, request.language || "—"),
        ...(request.breeze_mode
          ? [el("dt", {}, "Engine"), el("dd", {}, request.breeze_mode)]
          : [])),
      takeAudio || "",
      takeAudio ? el("div", { class: "narration-gain" },
        el("div", { class: "row" },
          gainLabel,
          el("span", { class: "spacer" }), gainValue),
        gain,
        el("div", { class: "row" },
          el("span", { class: "hint" },
            "Boosts this full-take player and the active take in final renders. Lower it if audio distorts."),
          el("span", { class: "spacer" }), saveGain)) : "",
      chunkList(projectId, take, refresh),
      el("div", { class: "row" }, choose)));
}

function chunkList(projectId, take, refresh) {
  const chunks = take.chunks || [];
  if (!chunks.length) {
    return el("p", { class: "muted small" },
      "This older take has no recoverable chunk files. Generate a new take for scene-level syncing.");
  }
  return el("details", { class: "narration-chunks" },
    el("summary", {}, `${chunks.length} regenerable chunk${chunks.length === 1 ? "" : "s"}`),
    take.settings?.timing_mode !== "scene_audio_v1"
      ? el("p", { class: "muted small" },
        "These chunks can be repaired individually, but generate one new full narration take to enable exact scene timing.")
      : "",
    el("div", { class: "narration-chunk-list" },
      ...chunks.map((chunk) => chunkRow(
        projectId, take.id, chunk, take.settings?.timing_mode, refresh))));
}

function chunkRow(projectId, takeId, chunk, timingMode, refresh) {
  const scene = Number.isInteger(chunk.scene_index)
    ? `Scene ${chunk.scene_index + 1}${chunk.scene_title ? ` · ${chunk.scene_title}` : ""}`
    : timingMode === "override" ? "Script override" : "Legacy / unmapped";
  const regenerate = el("button", { class: "btn btn-sm", type: "button" }, "Regenerate chunk");
  regenerate.onclick = async () => {
    regenerate.disabled = true;
    try {
      const job = await regenerateNarrationChunk(
        state.config, projectId, takeId, Number(chunk.index));
      toast("good", "Chunk regeneration queued",
        `Job ${job.id.slice(0, 8)} will create and activate a new take. Use Refresh when it completes.`);
      window.setTimeout(refresh, 1500);
    } catch (err) {
      toastError(err, "regenerate narration chunk");
      regenerate.disabled = false;
    }
  };
  return el("div", { class: "narration-chunk" },
    el("div", { class: "row" },
      el("strong", {}, `Chunk ${chunk.index}`),
      el("span", { class: "tag" }, scene),
      el("span", { class: "spacer" }),
      el("span", { class: "muted small" }, fmtDuration(chunk.duration || 0))),
    el("p", { class: "small narration-chunk-text" }, chunk.text || "(no saved text)"),
    chunk.url ? el("audio", { controls: true, preload: "metadata", src: chunk.url,
      "aria-label": `Play narration chunk ${chunk.index}` }) : "",
    el("div", { class: "row" }, regenerate));
}

function providerLabel(provider) {
  return ({
    qwen_tts: "Qwen3-TTS",
    step_audio_editx: "Step-Audio-EditX",
    chatterbox: "Chatterbox",
    fish_s2_pro: "Fish Audio S2 Pro",
    voxcpm2: "VoxCPM2",
    omnivoice: "OmniVoice",
    index_tts_2_5: "IndexTTS 2.5",
    breeze_tts_2: "Breeze TTS 2",
    recorded_voiceover: "Recorded voiceover",
  })[provider] || String(provider || "Unknown model");
}
