from pathlib import Path


VOICE_JS = Path(__file__).parents[1] / "frontend" / "js" / "pages" / "voice.js"


def test_narration_chunks_start_collapsed_and_full_take_gain_is_wired() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")

    assert 'el("details", { class: "narration-chunks" }' in source
    assert "open: take.active" not in source
    assert "setNarrationTakeGain" in source
    assert '"Full narration boost"' in source


def test_breeze_controls_are_attached_to_the_generation_panel() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")

    assert 'const breezeGrid = el("div", { class: "pref-grid" }' in source
    panel_start = source.index('section("3. Generate narration with a model"')
    panel_end = source.index("workerControlsPanel(models, refresh)", panel_start)
    assert "breezeGrid," in source[panel_start:panel_end]


def test_scene_chunk_combining_is_saved_and_sent() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")

    assert 'sceneGrouping.value = current.combine_scene_chunks ? "combined" : "scene"' in source
    assert 'combine_scene_chunks: sceneGrouping.value === "combined"' in source
    assert "Separately — one request per scene" in source
    assert "Together — combine scenes into longer requests" in source
    assert "How should planned scenes be sent?" in source
    assert "Maximum text per TTS request" in source
    assert "If the whole short fits" in source
    assert "six short scenes become six requests" in source
    assert "estimated from word count" in source
    assert "updateChunkingExplanation();" in source


def test_delivery_tags_panel_is_provider_scoped_and_wired_into_generation() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")

    # The panel is built and attached to the narration-generation section.
    assert "performancePanel(project, current, tags, provider, script, refresh)" in source
    panel_start = source.index('section("3. Generate narration with a model"')
    panel_end = source.index("workerControlsPanel(models, refresh)", panel_start)
    assert "performance," in source[panel_start:panel_end]

    # It is shown only for providers with native controls and forced off elsewhere.
    assert 'new Set(["fish_s2_pro", "higgs_tts_3"])' in source
    assert "performance.syncProvider(provider.value)" in source

    # Each artifact is identified by its stored provider. A mismatch
    # cannot look enabled or expose the wrong provider's tag editor.
    assert "const storedProvider = scriptData?.provider || null" in source
    assert "useTags.disabled = !!(scriptData && !matchesStored)" in source
    assert "if (!supported || !matchesStored) useTags.checked = false" in source
    assert "if (taggedEditors) taggedEditors.hidden = !matchesStored" in source
    assert "tags?.providers?.[activeProvider]" in source
    assert "provider: panelProvider" in source
    assert "Replace with ${label} tags" not in source
    assert "official <|category:value|> control-token vocabulary" in source
    assert "free-form natural-language delivery cues in [square brackets]" in source

    # The toggle is only sent for supported providers; intensity/notes are persisted
    # settings and are stripped from the NarrationRequest body.
    assert "&& PERFORMANCE_TAG_PROVIDERS.has(provider.value)" in source
    assert "provider: provider.value" in source
    assert "intensity: _intensity, performance_notes: _notes, gemini_style: _style, ...requestSettings" in source
    assert "generatePerformanceTags" in source
    assert "savePerformanceTags" in source
    assert "clearPerformanceTags" in source
    # Each stored segment gets its own per-segment regenerate button.
    assert "regeneratePerformanceSegment" in source
    assert '"Regenerate"' in source
    assert "result.tag_count" in source
    assert "result.script.tag_count" not in source


def test_narration_timing_badges_are_mode_and_format_aware() -> None:
    """Known timing formats get honest labels; only unknown ones read legacy.

    Classic projects describe what the take provides (scene synced /
    recorded master / combined scenes / script override). Editorial
    projects describe what the mode does with the take (scene timed /
    Editorial narration), and the combined-scene help explains the
    follow-the-narration behavior without promising every take is word-level
    synced. The authoritative alignment statement lives on the Timeline
    screen, so the take badge never claims narration alignment itself.
    """
    source = VOICE_JS.read_text(encoding="utf-8")
    # The pure badge builder covers every known timing_mode in both modes.
    assert "export function narrationTimingBadge(videoMode, timingMode" in source
    badge = source.split("export function narrationTimingBadge(", 1)[1].split(
        "\n}", 1,
    )[0]
    for mode in ("scene_audio_v1", "recorded_master_v1", "script_audio_v1", "override"):
        assert f'"{mode}"' in badge, f"{mode} handled by the badge builder"
    # Known formats: mode-appropriate labels, never a "legacy timing" warning.
    assert 'badge("good", "scene timed", false)' in badge
    assert '"Editorial narration"' in badge
    assert 'badge("neutral", "script override", false)' in badge
    # The warning is reserved for genuinely unknown/missing values.
    assert 'badge("warning", "legacy timing", false)' in badge
    # Classic wording is preserved (compatibility literals).
    assert 'badge("good", "scene synced", false)' in badge
    assert 'badge("neutral", "recorded master", false)' in badge
    assert 'badge("neutral", "combined scenes", false)' in badge
    # The take card renders the mode-aware badge instead of a hardcoded chain.
    assert "narrationTimingBadge(videoMode, settings.timing_mode, active)" in source
    # The combined-scene help explains the Editorial follow behavior...
    assert "grouping only changes the TTS request boundaries" in source
    assert "follow the active recorded narration" in source
    assert "the Timeline shows" in source
    assert "Planned timing" in source
    # ...without promising every take is synced, and the classic wording
    # (timing estimated) is preserved.
    assert "This often sounds smoother, but scene timing is estimated" in source


def test_saved_voices_can_be_deleted_from_the_shared_library() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")
    api = (VOICE_JS.parent.parent / "api.js").read_text(encoding="utf-8")

    # The API helper issues a DELETE against the shared voice-profile library.
    assert "export function deleteVoiceProfile" in api
    assert 'method: "DELETE"' in api
    assert "/tts/voices/${encodeURIComponent(profileId)}" in api

    # Each saved-voice row gets a delete button wired through a confirm dialog,
    # and the page refreshes so the row disappears from the list and dropdown.
    assert "deleteVoiceProfile" in source
    assert "savedVoicesPanel(voices, project.id, refresh)" in source
    assert 'confirmLabel: "Delete"' in source
    assert 'toast("good", "Voice profile deleted"' in source
    assert 'toastError(err, "delete voice profile")' in source


def test_voice_settings_can_be_saved_without_generating_or_reloading() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")
    handler = source.split("saveVoice.onclick = async () => {", 1)[1].split(
        "generate.onclick = async () => {", 1,
    )[0]
    assert "await editProject" in handler
    assert "const settings = voiceSettings();" in handler
    assert "if (!settings) return;" in handler
    assert "voice: settings" in handler
    assert "generateNarration(" not in handler
    assert "script.value" not in handler
    assert "refresh()" not in handler  # Keep unsaved script and recording inputs.
    assert '"Save voice settings"' in source
    assert '(voice.value || null)' in source


def test_complete_recorded_voiceover_can_be_recorded_imported_and_activated() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")
    api = (VOICE_JS.parent.parent / "api.js").read_text(encoding="utf-8")
    recorder = (VOICE_JS.parent.parent / "voice-recorder.js").read_text(encoding="utf-8")

    assert 'section("2. Use your recorded voiceover"' in source
    assert 'purpose: "voiceover"' in source
    assert '"Record full voiceover"' in source
    assert '"Import full voiceover"' in source
    assert "importRecordedNarration" in source
    assert "/tts/narrations/import" in api
    assert 'voiceover ? "Record your complete voiceover"' in recorder
    assert "Math.min(3600" in recorder


def test_gemini_model_picker_is_backend_driven_with_fallback() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")

    # The picker prefers the backend-advertised gallery, with a hardcoded
    # fallback for older backends; all three curated models are present.
    assert "GEMINI_MODEL_FALLBACK" in source
    assert "models.gemini_tts?.gemini_models" in source
    assert "gemini-3.1-flash-tts-preview" in source
    assert "gemini-2.5-flash-preview-tts" in source
    assert "gemini-2.5-pro-preview-tts" in source
    assert 'field("Gemini model", geminiModel,' in source
    assert "settings.gemini_model = geminiModel.value || null;" in source


def test_gemini_style_prompt_reaches_the_backend_as_voice_instruction() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")

    # The style field is sent as voice_instruction; the backend incorporates it
    # into the text prompt because prebuiltVoiceConfig accepts voiceName only.
    assert '(provider.value === "gemini_tts" ? geminiStyle.value.trim() : "")' in source


def test_frontend_model_fallback_matches_backend_gallery() -> None:
    from backend.models.gemini_tts import GEMINI_TTS_MODELS

    source = VOICE_JS.read_text(encoding="utf-8")
    for model_id, _ in GEMINI_TTS_MODELS:
        assert model_id in source


def test_gemini_provider_is_listed_key_gated_and_attached() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")

    # The provider is offered in the model list and labelled as remote.
    assert 'modelOption("gemini_tts", "Gemini TTS (Google, cloud)", models)' in source
    assert "gemini_tts: 30" in source
    assert "Gemini TTS (Google)" in source
    # Key-required state is surfaced in the option labels.
    assert 'needsKey ? "needs API key"' in source

    # The panel is built and attached to the narration-generation section.
    assert 'const geminiGrid = el("div", { class: "pref-grid" }' in source
    panel_start = source.index('section("3. Generate narration with a model"')
    panel_end = source.index("workerControlsPanel(models, refresh)", panel_start)
    panel = source[panel_start:panel_end]
    assert "geminiGrid," in panel
    assert "geminiNote," in panel

    # The profile selector is suspended for Gemini and the generate button
    # stays disabled until a key is present.
    assert "voice.disabled = gemini;" in source
    assert 'provider.value === "gemini_tts" && !geminiReady()' in source
    assert 'badge("good", "Ready to generate")' in source
    assert 'badge("warning", "API key needed")' in source
    assert 'badge("offline", "Gemini disabled")' in source
    assert 'entry?.health?.remote !== true' in source
    assert "Gemini API key required" in source
    # Saving/removing the key talks to the dedicated endpoints.
    api_js = VOICE_JS.parent.parent / "api.js"
    api_source = api_js.read_text(encoding="utf-8")
    assert "export function saveGeminiKey(config" in api_source
    assert "export function clearGeminiKey(config" in api_source


def test_worker_controls_report_honest_loaded_states() -> None:
    """The Worker controls never claim "loaded" without a strict report.

    Isolated TTS workers (and the mock backend) report `loaded` in their
    /health payload; ComfyUI-backed adapters omit it. The status line must
    distinguish loaded / running-without-weights / reachable-but-unreported /
    worker-down. Reachable ComfyUI adapters with unreported residency remain
    unloadable so their `/free` endpoint can release cached weights; confirmed
    unloaded or unreachable workers stay disabled (a title explains why).
    """
    source = VOICE_JS.read_text(encoding="utf-8")

    # The status logic is an exported pure function (table-shaped; the full
    # state table is exercised by frontend/tests/js/frontend.test.js).
    assert "export function ttsWorkerStatus(" in source
    block = source.split("export function ttsWorkerStatus(", 1)[1].split("\n}", 1)[0]
    # Healthy is split on a strict `loaded` boolean…
    assert 'health.loaded === true' in block
    assert '"Loaded and ready."' in block
    assert 'health.loaded === false' in block
    assert '"Worker running — model loads on first use."' in block
    # …and a healthy entry without the flag is never claimed as loaded, while
    # remaining unloadable because ComfyUI may still hold cached weights.
    assert '"Worker reachable."' in block
    # A downed worker is "not reachable", not "Loaded and ready".
    assert '"Worker not reachable."' in block
    # The managed auto-start and not-configured branches are kept.
    assert "Configured for automatic worker startup — first use will load the model." in block
    assert "Not configured. Set managed=true and the worker details in config." in block
    # Confirmed and potentially cached weights are unloadable.
    assert block.count("canUnload: true") == 2
    assert "health.loaded === true" in block.split("canUnload: true", 1)[0]
    assert "Request release of any cached model weights" in block
    assert block.count("canUnload: false") == 5

    # The panel binds the status line and the Unload button to the same view,
    # re-applies it on provider change, and re-reads the snapshot after the
    # post-unload refresh() rebuilds the page.
    panel = source.split("function workerControlsPanel(", 1)[1].split("\n}", 1)[0]
    assert "ttsWorkerStatus(localModels[provider.value] || null)" in panel
    assert "unload.disabled = !view.canUnload" in panel
    assert "unload.title = view.unloadTitle" in panel
    assert "provider.onchange = applyWorkerStatus" in panel
    assert "applyWorkerStatus();" in panel
    # The post-unload toast keeps the owned-worker distinction.
    assert "result.stopped_owned_worker" in panel
    assert '"Worker process stopped; weights and memory released."' in panel
    assert '"Weights released; worker stays running for the next job."' in panel


def test_voice_consent_note_is_neutral_and_provider_free() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")
    # The clone-consent sentence stays…
    assert "Only clone a voice you own or have permission to use. Audio remains local." in source
    # …without per-provider licensing commentary.
    assert "Breeze TTS 2 note" not in source
    assert "non-commercial use" not in source
