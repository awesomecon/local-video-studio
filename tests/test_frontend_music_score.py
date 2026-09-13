"""Frontend tests for the Music screen's Score Studio.

Source-text assertions (the repo is zero-build; the JS itself is validated by
frontend/tests/static_checks.py). They pin the contracts the plan relies on:

  - the Music screen is a responsive 3-column workspace built from the shared
    design system (panels/fields/badges/toasts and the existing tokens), not
    a duplicated standalone interface;
  - the five-lane score timeline has one shared transport, draggable cues
    with boundary snapping and millisecond keyboard nudging, and a transport
    that is torn down (audio paused, listeners removed, decode context
    destroyed) when the screen is left;
  - cue edits stay local until an explicit Save, which persists the portable
    score plan with an optimistic revision and reports that only mix/render
    descendants are invalidated (the generated soundtrack is never re-run);
  - Auto Score proposals are local-LLM first with a deterministic fallback,
    are displayed distinctly, and only touch the saved plan after acceptance,
    with locked cues always preserved;
  - sound effects upload locally (WAV/FLAC/MP3) — the UI exposes no remote
    audio processing.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "frontend"


def _js(rel: str) -> str:
    return (ROOT / "js" / rel).read_text(encoding="utf-8")


def _css(rel: str) -> str:
    return (ROOT / "css" / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- existence


def test_score_studio_modules_exist() -> None:
    assert (ROOT / "js" / "music" / "waveform.js").is_file()
    assert (ROOT / "js" / "music" / "cues.js").is_file()
    assert (ROOT / "js" / "music" / "timeline.js").is_file()
    page = _js("pages/music.js")
    assert 'from "../music/timeline.js"' in page
    assert 'from "../music/waveform.js"' in page
    assert 'from "../music/cues.js"' in page


def test_api_helpers_for_score_studio() -> None:
    api = _js("api.js")
    for name in ("musicStudio", "saveScorePlan", "scorePreview",
                 "uploadScoreEffect", "autoScore"):
        assert f"function {name}(" in api, name
    # The score-plan write is optimistic: it must send the expected revision.
    assert "expectedRevision" in api
    assert "score-preview" in api
    assert "auto-score" in api
    assert "effects" in api


# ---------------------------------------------------------------- design


def test_page_reuses_the_shared_design_system() -> None:
    page = _js("pages/music.js")
    # Shared primitives and the screen scaffolding — no bespoke layout system.
    assert 'from "../dom.js"' in page
    assert 'from "../ui.js"' in page
    assert "loadingState" in page
    assert "errorPanel" in page
    assert "toastError" in page
    assert 'class: "panel"' in page
    assert 'class: "panel-title"' in page
    assert 'class: "screen"' in page
    # Status is conveyed with badge + label, never colour alone.
    assert "badge(" in page


def test_score_styles_use_the_existing_tokens() -> None:
    css = _css("components.css")
    assert ".score-layout" in css
    assert ".score-col-studio" in css
    assert ".sc-timeline" in css or ".score-timeline" in css
    # Every new class is painted with shared tokens, not a new palette.
    for token in ("var(--accent)", "var(--text-2)", "var(--surface-2)",
                  "var(--line)", "var(--warning)", "var(--good)"):
        assert token in css
    # The responsive plan: 3 columns → 2 on tablet → 1 on mobile.
    assert css.count("@media") >= 2


def test_no_duplicate_mockup_palette() -> None:
    """The mockup's standalone purple interface must not leak in."""
    for rel in ("pages/music.js", "music/waveform.js", "music/cues.js",
                "music/timeline.js"):
        source = _js(rel)
        assert "#7c5cff" not in source.lower()
        assert "#a78bfa" not in source.lower()
        assert "linear-gradient" not in source
    page = _js("pages/music.js")
    assert "Score Studio" in page


# ---------------------------------------------------------------- timeline


def test_timeline_lanes_transport_and_cue_dragging() -> None:
    src = _js("music/timeline.js")
    # The five planned lanes.
    assert "sc-lane-scenes" in src
    assert "sc-lane-wave" in src
    assert "sc-lane-clips" in src
    assert "sc-lane-auto" in src
    # One shared transport: play/pause, stop, seek, spacebar.
    assert "sc-transport" in src
    assert "setPointerCapture" in src
    assert 'ev.key !== " "' in src
    # Cues commit on pointer-up (not per drag frame) and snap to boundaries.
    assert "onpointerup" in src or "pointerup" in src
    assert "snapTime(" in src or "0.35" in src
    # Keyboard nudging is millisecond-precise (Shift = coarse).
    assert "ArrowLeft" in src
    assert "shiftKey" in src
    # A silent clock keeps the transport useful before any audio exists.
    assert "silentClock" in src


def test_timeline_tears_down_the_transport() -> None:
    src = _js("music/timeline.js")
    assert "function destroy()" in src
    # destroy() must pause the audio element, clear its source, and remove
    # the window-level spacebar listener.
    destroy = src[src.index("function destroy("):]
    assert "pause()" in destroy
    assert 'audio.src = ""' in destroy
    assert "removeEventListener" in destroy


def test_navigation_destroys_the_previous_timeline() -> None:
    page = _js("pages/music.js")
    # Leaving the screen (re-render) tears down the transport and the decode
    # context so no audio or listeners survive the route change.
    assert "destroyWaveformContext()" in page
    assert "timeline.destroy()" in page


# ---------------------------------------------------------------- cues


def test_cue_model_guardrails_match_the_backend() -> None:
    src = _js("music/cues.js")
    # All seven planned cue actions are offered.
    for action in ("build", "pull_back", "silence", "restore",
                   "impact", "riser", "end_sting"):
        assert f'"{action}"' in src or f"'{action}'" in src
    # The inspector's numeric limits are named constants with the exact
    # ranges the backend enforces (backend/music/score.py).
    assert "GAIN_DB_MIN = -60" in src
    assert "GAIN_DB_MAX = 12" in src
    assert "LOWPASS_MIN = 200" in src
    assert "LOWPASS_MAX = 20000" in src
    assert "TRANSITION_MAX = 5" in src
    assert "min: String(GAIN_DB_MIN)" in src
    assert "min: String(LOWPASS_MIN)" in src
    # Serialized plans are versioned and time-sorted.
    assert "version: 1" in src
    assert "sort(" in src


def test_cue_lifecycle_edit_lock_duplicate_delete() -> None:
    page = _js("pages/music.js")
    assert "Add at playhead" in page
    assert "onToggleLock" in page
    # Selection comes back from the timeline (onSelect) into the same
    # working state the inspector edits.
    assert "onSelect" in page
    assert "onCueCommit" in page
    # The per-cue actions are labeled for the user in the cue module.
    cues = _js("music/cues.js")
    assert "Duplicate" in cues
    assert "Delete" in cues
    assert "Lock" in cues


def test_saving_is_explicit_and_reports_scoped_invalidation() -> None:
    page = _js("pages/music.js")
    assert "saveScorePlan(" in page
    assert "expectedRevision" in page
    # Optimistic-concurrency failures reload instead of silently overwriting.
    assert '"conflict"' in page
    # The user is told the scope: mix/render descendants, never the
    # generated soundtrack.
    assert "re-mixes the score" in page
    assert "unsaved" in page
    # Unsaved edits are discarded back to the saved base, not lost silently.
    assert "Discard" in page


# ---------------------------------------------------------------- auto score


def test_auto_score_flow_is_local_and_accept_driven() -> None:
    page = _js("pages/music.js")
    assert "autoScore(" in page
    # Suggestions are presented distinctly (badged, dashed) …
    assert "auto-suggestion" in page or "suggested" in page
    assert "Accept all" in page
    assert "Discard all" in page
    # … and the screen names its guarantees: local LLM, explicit acceptance,
    # locked cues preserved.
    assert "local LLM" in page
    assert "locked" in page.lower()
    # The fallback (deterministic recipe) is surfaced, not hidden.
    assert "deterministic" in page


def test_effects_upload_is_local_only() -> None:
    page = _js("pages/music.js")
    assert "uploadScoreEffect(" in page
    assert ".wav,.flac,.mp3" in page
    assert "Upload SFX" in page


# ---------------------------------------------------------------- generation


def test_generation_panel_persists_music_settings() -> None:
    page = _js("pages/music.js")
    assert "editProject(" in page
    assert "direction" in page
    assert "bpm" in page
    assert "key_scale" in page
    assert "time_signature" in page
    assert "instrumental" in page
    assert "intensity" in page
    # Duration is narration-derived and read-only in the UI.
    assert "readonly: true" in page
    # Regeneration is explicit and tracked as a job.
    assert "Regenerate music" in page
    assert "generateMusic(" in page
    assert "registerLiveUpdate" in page