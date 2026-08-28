from pathlib import Path

from backend.schemas import VisualType


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
# The visual-mode catalog lives in js/shots.js: one source shared by the
# scene editor's legacy form and the Add Shot chooser.
SHOTS_HELPERS = FRONTEND / "js" / "shots.js"
SCENE_EDITOR = FRONTEND / "js" / "pages" / "scene-editor.js"
THUMBNAILS_PAGE = FRONTEND / "js" / "pages" / "thumbnails.js"
MODELS_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "js" / "pages" / "models.js"


def test_scene_editor_describes_every_visual_mode_and_its_readiness() -> None:
    catalog = SHOTS_HELPERS.read_text(encoding="utf-8")
    source = SCENE_EDITOR.read_text(encoding="utf-8")

    for visual_type in VisualType:
        assert catalog.count(f'value: "{visual_type.value}"') == 1
    # The scene editor keeps exactly one other "custom" literal: the H3
    # quality preset selector.
    assert source.count('value: "custom"') == 1

    assert 'label: "Image motion"' in catalog
    assert "Generates a Krea 2 or Qwen-Image-2512 still locally" in catalog
    assert 'label: "Qwen-Image-2512 text still"' in catalog
    assert 'label: "Ideogram 4 still image"' in catalog
    assert 'id: "se-sh-ideogram-mode"' in source
    # Automatic routing keeps the legacy Image Motion source selector as its
    # fallback, while the primary selector exposes all routed image models.
    assert 'label: "Automatic motion source"' in source
    assert 'value: "krea2" }, "Krea 2 Turbo"' in source
    assert 'value: "qwen_image_2512" }, "Qwen-Image-2512"' in source
    assert 'value: "ideogram4_local" }, "Ideogram 4 (local)"' in source
    assert 'label: "Image model"' in source
    assert "Select Krea, local Ideogram 4, or Qwen" in source
    assert "preferred_image_model: preferredImageModel.value" in source
    assert "needs_embedded_text: needsEmbeddedText.checked" in source
    assert "text_in_image: textInImage.value" in source
    assert 'value: "quick" }, "Quick Generation"' in source
    assert 'value: "precise" }, "Precise Text & Layout"' in source
    assert "ideogram_prompt_mode:" in source
    assert "ideogram_prompt_json:" in source
    assert "[y_min, x_min, y_max, x_max]" in source
    assert (
        'image_motion_source: visualType.value === "image_motion" ? imageMotionSource.value'
        in source
    )
    assert "Requested in-image text" in source
    for camera_move in ("slow push in", "slow pull out", "pan left", "pan right", "drift up", "drift down"):
        assert f'value: "{camera_move}"' in source
    assert 'camera_instruction: cameraInstruction.value' in source
    assert 'label: "MiniMax H3 AV shot (audio + video)"' in catalog
    assert "Native H3 audio is preview-only" in catalog
    assert 'return currentType === "h3_audiovisual" ? "high" : "standard"' in source
    assert source.index("let h3Policy = null;") < source.index("initializeH3Fields();")
    assert 'label: "MiniMax H3 reference video — unwired"' in catalog
    assert 'label: "Wan video — unwired"' in catalog
    assert "for (const wired of [true, false])" in source
    assert "Wan video (audio + video)" not in source
    assert "const saved = await persistChanges();" in source
    assert source.index("const saved = await persistChanges();") < source.index(
        "if (currentVisual) await regenerateScene"
    )
    assert 'id: "se-sh-reused-media-file"' in source
    assert '"Save shot & import local media"' in source
    assert "await importShotReusedMedia(state.config, shot.id, reusedFile" in source
    assert 'id: "se-generated-image-file"' in source
    assert 'id: "se-sh-generated-image-file"' in source
    assert '"Save & import AI image"' in source
    assert '"Save shot & import AI image"' in source
    assert "await importSceneGeneratedImage(state.config, scene.id, generatedFile)" in source
    assert "await importShotGeneratedImage(state.config, shot.id, generatedFile)" in source


def test_models_page_surfaces_h3_cold_load_readiness() -> None:
    source = MODELS_PAGE.read_text(encoding="utf-8")

    assert "function h3ReadinessPanel(sys)" in source
    assert "ready.must_free_vram" in source
    assert "ready.cold_load_required" in source
    assert "Resident ComfyUI family" in source


def test_thumbnail_studio_exposes_quick_and_precise_ideogram_modes() -> None:
    source = THUMBNAILS_PAGE.read_text(encoding="utf-8")

    assert 'value: "quick" }, "Quick Generation"' in source
    assert 'value: "precise" }, "Precise Text & Layout"' in source
    assert 'label: "Precise Ideogram JSON"' in source
    assert "ideogram_prompt_mode:" in source
    assert "ideogram_prompt_json:" in source
    assert "[y_min, x_min, y_max, x_max]" in source
    assert "Validate & Save Precise Prompt" in source


def test_models_page_can_unload_a_studio_owned_ideogram_worker() -> None:
    source = MODELS_PAGE.read_text(encoding="utf-8")

    assert "unloadIdeogram4" in source
    assert '"Unload Ideogram 4"' in source
    assert "stopped_owned_worker" in source
    assert "only when this Studio started it" in source


def test_scene_editor_exposes_generated_background_exact_text_mode() -> None:
    source = SCENE_EDITOR.read_text(encoding="utf-8")
    catalog = SHOTS_HELPERS.read_text(encoding="utf-8")

    assert 'visualType.value === "text_overlay_still"' in source
    assert "Generated background + exact text" in catalog
    assert "text_overlay_background_model" in source
    assert "text_overlay_layout" in source
    assert "Quotation — quote and citation" in source
