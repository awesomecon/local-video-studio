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


def test_delivery_tags_panel_is_fish_only_and_wired_into_generation() -> None:
    source = VOICE_JS.read_text(encoding="utf-8")

    # The panel is built and attached to the local-model narration section.
    assert "performancePanel(project, current, tags, provider, script, refresh)" in source
    panel_start = source.index('section("3. Generate narration with a local model"')
    panel_end = source.index("workerControlsPanel(models, refresh)", panel_start)
    assert "performance," in source[panel_start:panel_end]

    # It is hidden for every non-Fish provider and the toggle is forced off.
    assert 'performance.hidden = provider.value !== "fish_s2_pro"' in source
    assert 'if (provider.value !== "fish_s2_pro") performance.useTags.checked = false' in source

    # The toggle is only sent for Fish S2 Pro; intensity/notes are persisted
    # settings and are stripped from the NarrationRequest body.
    assert 'use_performance_tags: performance.useTags.checked && provider.value === "fish_s2_pro"' in source
    assert "intensity: _intensity, performance_notes: _notes, ...requestSettings" in source
    assert "generatePerformanceTags" in source
    assert "savePerformanceTags" in source
    assert "clearPerformanceTags" in source
    # Each stored segment gets its own per-segment regenerate button.
    assert "regeneratePerformanceSegment" in source
    assert '"Regenerate"' in source
    assert "result.tag_count" in source
    assert "result.script.tag_count" not in source


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
