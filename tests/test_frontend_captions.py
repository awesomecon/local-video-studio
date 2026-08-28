"""Frontend tests for the Captions screen's alignment-model surfacing.

Source-text assertions (the repo is zero-build; the JS itself is validated by
frontend/tests/static_checks.py). They pin the contracts that make the local
Whisper alignment model visible on the Captions screen:

  - the screen fetches GET /api/captions/models and renders the model
    identity and readiness entirely from the response (no hardcoded model
    names, versions, or paths);
  - the word-timings output (role "caption_timing") and its provenance are
    shown;
  - the API helper and typedef exist.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "frontend"


def _js(rel: str) -> str:
    return (ROOT / "js" / rel).read_text(encoding="utf-8")


def test_captions_screen_fetches_and_renders_the_alignment_model() -> None:
    source = _js("pages/captions.js")
    assert 'getProject, captionsModels, generateCaptions' in source
    assert "captionsModels(state.config)" in source
    # Identity (name/version/quantization/device) comes from the descriptor…
    assert "buildAlignment(info)" in source
    assert "descriptor.model_name" in source
    assert "descriptor.model_version" in source
    assert "descriptor.quantization" in source
    assert "descriptor.vram_required_gb" in source
    # …and readiness/guidance come from health(), verbatim…
    assert "health.status" in source
    assert "health.install_guidance" in source
    # …while no model identity is baked into the screen itself.
    assert "Whisper large-v3-turbo" not in source
    assert "large-v3-turbo" not in source
    assert "ct2" not in source


def test_captions_screen_shows_the_word_timings_output() -> None:
    source = _js("pages/captions.js")
    # The alignment output is recorded with role "caption_timing" and must be
    # collected and rendered, not filtered out with the other assets.
    assert 'role === "caption_timing"' in source
    assert "timingBlock(" in source
    # Provenance rows are data-driven from the recorded asset settings.
    assert "settings.input_audio" in source
    assert "settings.input_audio_sha256" in source
    assert "settings.language_probability" in source


def test_captions_assets_show_quantization_and_workflow() -> None:
    source = _js("pages/captions.js")
    # SRT/ASS blocks surface the alignment model's quantization and workflow.
    assert source.count('el("dt", {}, "Quantization")') >= 2
    assert source.count('el("dt", {}, "Workflow")') >= 2


def test_captions_models_api_helper_exists() -> None:
    api = _js("api.js")
    assert "export function captionsModels(config, opts = {})" in api
    assert '"/api/captions/models"' in api
    assert "@typedef {Object} CaptionsModels" in api
    assert "@property {BackendDescriptor} descriptor" in api


def test_captions_can_be_aligned_directly_from_the_screen() -> None:
    source = _js("pages/captions.js")
    api = _js("api.js")
    assert "Align from narration" in source
    assert "generateCaptions(state.config, state.currentProjectId)" in source
    assert "export function generateCaptions(config, projectId" in api
    assert "/captions/generate" in api
