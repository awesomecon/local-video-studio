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
    panel_start = source.index('section("3. Generate narration with a local model"')
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

    # The panel is built and attached to the local-model narration section.
    assert "performancePanel(project, current, tags, provider, script, refresh)" in source
    panel_start = source.index('section("3. Generate narration with a local model"')
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

    # The style field is sent as voice_instruction (the backend's stylePrompt
    # input); without this mapping the prompt would be saved but never spoken.
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

    # The panel is built and attached to the local-model narration section.
    assert 'const geminiGrid = el("div", { class: "pref-grid" }' in source
    panel_start = source.index('section("3. Generate narration with a local model"')
    panel_end = source.index("workerControlsPanel(models, refresh)", panel_start)
    panel = source[panel_start:panel_end]
    assert "geminiGrid," in panel
    assert "geminiNote," in panel

    # The profile selector is suspended for Gemini and the generate button
    # stays disabled until a key is present.
    assert "voice.disabled = gemini;" in source
    assert 'provider.value === "gemini_tts" && !geminiKeyReady()' in source
    assert "Gemini API key required" in source
    # Saving/removing the key talks to the dedicated endpoints.
    api_js = VOICE_JS.parent.parent / "api.js"
    api_source = api_js.read_text(encoding="utf-8")
    assert "export function saveGeminiKey(config" in api_source
    assert "export function clearGeminiKey(config" in api_source
