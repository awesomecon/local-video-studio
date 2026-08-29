"""Regression coverage for rendering already-generated real-mode scenes."""

from __future__ import annotations

from pathlib import Path

from backend.core import load_config
from backend.models import GenerationResult
from backend.pipeline import PipelineService
from backend.rendering.mock_media import create_placeholder_audio, create_placeholder_image
from backend.schemas import (
    AssetType,
    ProjectCreate,
    ProjectPlan,
    ProjectStatus,
    Scene,
    SceneStatus,
    VisualType,
)


def test_real_render_reuses_visuals_and_avoids_mock_only_stages(
    tmp_path: Path, monkeypatch,
) -> None:
    pipeline = PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=False,
    )
    project = pipeline.create_project(
        ProjectCreate(
            title="Existing Krea Scene",
            topic="render generated local assets",
            target_duration=1,
            resolution=(320, 180),
            fps=12,
        )
    )
    scene = Scene(
        project_id=project.id,
        index=0,
        title="Generated still",
        duration=1,
        narration="A completed local scene.",
        visual_prompt="A finished Krea still",
        visual_type=VisualType.IMAGE_MOTION,
        camera_instruction="static",
        selected_backend="krea2_comfyui",
        seed=42,
        status=SceneStatus.GENERATED,
    )
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)

    plan = ProjectPlan(
        project_id=project.id,
        title=project.title,
        outline=[scene.title],
        scenes=[scene],
        target_duration=project.target_duration,
    )
    root = pipeline.store.project_path(project)
    pipeline.store.save_plan(project.slug, plan)
    outline = root / "script" / "outline.md"
    script = root / "script" / "script.md"
    outline.parent.mkdir(parents=True, exist_ok=True)
    outline.write_text("# Existing Krea Scene\n", encoding="utf-8")
    script.write_text("A completed local scene.\n", encoding="utf-8")
    pipeline._mark_stage(project, "plan", [outline, script, root / "plan.json"], "existing-plan")

    narration = create_placeholder_audio(
        root / "narration" / "master.wav",
        duration_seconds=1,
        binaries=pipeline.renderer.binaries,
    )
    pipeline._mark_stage(project, "narration", [narration], "existing-narration")
    # This regression covers reusing and rendering existing visuals, not GPU
    # caption alignment. Treat subtitles as an already-resolved optional stage.
    pipeline._mark_stage(project, "subtitles", [], "existing-subtitles")

    visual = create_placeholder_image(
        root / "scenes" / "001" / "visual.png",
        width=320,
        height=180,
        seed=scene.seed,
        binaries=pipeline.renderer.binaries,
    )
    pipeline._record_asset(
        project,
        scene,
        visual,
        AssetType.IMAGE,
        GenerationResult(
            outputs=(visual,),
            metadata={
                "backend": "krea2_comfyui",
                "model": "Krea 2 Turbo",
                "model_version": "open-v1.0",
                "workflow_version": "krea2-turbo-fp8-v1",
                "seed": scene.seed,
            },
        ),
        role="visual",
    )

    def unexpected_regeneration(*_args, **_kwargs):
        raise AssertionError("normal render must reuse the completed scene visual")

    monkeypatch.setattr(pipeline, "_generate_visual", unexpected_regeneration)

    final = pipeline.run_project(project.id)

    assert final.is_file() and final.stat().st_size > 0
    assert pipeline._project(project.id).status is ProjectStatus.COMPLETED
    stages = pipeline.project_snapshot(project.id)["stage_state"]["stages"]
    assert stages["references"]["outputs"] == []
    assert stages["music"]["outputs"] == []
    assert stages["visuals"]["outputs"] == ["scenes/001/visual.png"]
    assert len(stages["thumbnails"]["outputs"]) == 3
    assert all((root / path).stat().st_size > 0 for path in stages["thumbnails"]["outputs"])
    thumbnail_assets = [
        asset for asset in pipeline.database.list_assets(project.id)
        if asset.settings.get("role") == "thumbnail"
    ]
    assert len(thumbnail_assets) == 3
    assert {asset.backend for asset in thumbnail_assets} == {"ffmpeg"}


def test_render_only_never_calls_content_generation_even_when_forced(
    tmp_path: Path, monkeypatch,
) -> None:
    pipeline = PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )
    project = pipeline.create_project(
        ProjectCreate(
            title="Render Only",
            topic="reuse finished local content",
            target_duration=1,
            resolution=(320, 180),
            fps=12,
        )
    )
    pipeline.run_project(project.id)
    root = pipeline.store.project_path(project)
    narration = root / "narration" / "master.wav"
    visual_paths = sorted((root / "scenes").glob("*/visual.png"))
    original_inputs = {
        path: path.read_bytes()
        for path in [narration, *visual_paths]
    }

    # A stale/missing plan marker used to make POST /render contact the LLM.
    pipeline._invalidate_stages(project, {"plan"})

    def unexpected_generation(*_args, **_kwargs):
        raise AssertionError("render-only workflow called a content-generation stage")

    for method in (
        "ensure_plan",
        "_ensure_narration",
        "_ensure_references",
        "_ensure_visuals",
        "_ensure_music",
        "_ensure_subtitles",
        "_ensure_metadata",
        "_ensure_editorial_visual",
    ):
        monkeypatch.setattr(pipeline, method, unexpected_generation)

    job = pipeline.queue_render(project.id, force=True)
    final = pipeline.run_render(project.id, force=True, parent_job_id=job.id)

    assert job.stage == "render"
    assert job.backend == "ffmpeg"
    assert final.is_file() and final.stat().st_size > 0
    assert pipeline.jobs.get(job.id).status.value == "completed"
    assert "plan" not in pipeline.project_snapshot(project.id)["stage_state"]["stages"]
    assert all(path.read_bytes() == content for path, content in original_inputs.items())
    thumbnail_assets = [
        asset for asset in pipeline.database.list_assets(project.id)
        if asset.settings.get("role") == "thumbnail"
    ]
    assert thumbnail_assets
    assert {asset.backend for asset in thumbnail_assets} == {"ffmpeg"}
