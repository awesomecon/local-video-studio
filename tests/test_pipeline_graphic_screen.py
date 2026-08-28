from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.core import load_config
from backend.models.errors import BackendError, BackendErrorCode
from backend.pipeline import PipelineService
from backend.rendering.mock_media import create_placeholder_audio
from backend.schemas import ProjectCreate, VisualType


def service(tmp_path: Path) -> PipelineService:
    return PipelineService(
        load_config(environ={}), database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "app" / "tmp", mock_mode=True,
    )


def test_mock_graphic_screen_persists_portable_artifacts_and_reuses_visual(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(ProjectCreate(title="Graphic", topic="testing", target_duration=3, resolution=(160, 90)))
    scene = pipeline.ensure_plan(project.id).scenes[0]
    scene = pipeline.update_scene(scene.id, {
        "visual_type": VisualType.GRAPHIC_SCREEN.value,
        "camera_instruction": "slow push in",
        "graphic_instructions": "Two labels in a clean comparison.",
        "graphic_text": ["INPUT", "OUTPUT"],
    })
    first = pipeline.generate_scene(scene.id)
    directory = pipeline.store.project_path(project) / "scenes" / "001"
    manifest = json.loads((directory / "graphic-screen.json").read_text(encoding="utf-8"))

    assert first.settings["role"] == "visual"
    assert (directory / "graphic-screen.html").is_file()
    assert manifest["project_resolution"] == [160, 90]
    assert manifest["source_hash"] and manifest["png_hash"]
    assert pipeline.generate_scene(scene.id).id == first.id
    for other_scene in pipeline.database.list_scenes(project.id)[1:]:
        pipeline.generate_scene(other_scene.id)
    create_placeholder_audio(
        pipeline.store.project_path(project) / "narration" / "master.wav",
        duration_seconds=scene.duration,
        binaries=pipeline.renderer.binaries,
    )
    assert pipeline._build_timeline(project).clips[0].camera_motion is None

    pipeline.update_scene(scene.id, {"graphic_text": ["INPUT", "CHANGED"]})
    changed = pipeline.generate_scene(scene.id)
    assert changed.id != first.id

    second = pipeline.generate_scene(scene.id, force=True)
    archive = pipeline.store.project_path(project) / "variants" / "archive"
    archived = [path.name for path in archive.iterdir()]
    assert second.id != changed.id
    assert any(name.startswith("visual-") for name in archived)
    assert any(name.startswith("graphic-screen-") and name.endswith(".html") for name in archived)
    assert any(name.startswith("graphic-screen-") and name.endswith(".json") for name in archived)


def test_graphic_settings_invalidate_only_visual_dependents(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(ProjectCreate(title="Invalidate", topic="testing", target_duration=3, resolution=(160, 90)))
    pipeline.run_project(project.id)
    scene = pipeline.database.list_scenes(project.id)[0]
    pipeline.update_scene(scene.id, {"graphic_instructions": "new layout", "graphic_text": ["A"]})
    stages = pipeline.project_snapshot(project.id)["stage_state"]["stages"]

    assert "narration" in stages
    assert "visuals" not in stages
    assert "timeline" not in stages


def test_switching_away_from_graphic_screen_does_not_reuse_graphic_asset(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(ProjectCreate(
        title="Switch", topic="testing", target_duration=3, resolution=(160, 90),
    ))
    scene = pipeline.ensure_plan(project.id).scenes[0]
    scene = pipeline.update_scene(scene.id, {
        "visual_type": VisualType.GRAPHIC_SCREEN.value, "graphic_text": ["A"],
    })
    graphic = pipeline.generate_scene(scene.id)

    pipeline.update_scene(scene.id, {"visual_type": VisualType.KREA2_STILL.value})
    still = pipeline.generate_scene(scene.id)

    assert still.id != graphic.id
    assert still.settings["visual_type"] == VisualType.KREA2_STILL.value


def test_service_error_details_are_redacted_and_bounded() -> None:
    error = BackendError(
        BackendErrorCode.INVALID_RESPONSE,
        "The local LLM returned HTTP 500.",
        retryable=True,
        details='{"error":"model load failed","auth":"Bearer sk-live-abc123"} ' + "x" * 600,
        secrets=("sk-live-abc123",),
    )
    params = PipelineService._service_error_parameters(error)

    assert "model load failed" in params["service_details"]
    assert "sk-live-abc123" not in params["service_details"]
    assert len(params["service_details"]) <= 400
    assert PipelineService._service_error_parameters(RuntimeError("plain failure")) == {}


def test_graphic_publication_restores_complete_previous_set_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(ProjectCreate(
        title="Rollback", topic="testing", target_duration=3, resolution=(160, 90),
    ))
    directory = pipeline.store.project_path(project) / "scenes" / "001"
    directory.mkdir(parents=True)
    targets = {
        directory / "graphic-screen.html": "old source",
        directory / "graphic-screen.json": "old manifest",
        directory / "visual.png": "old png",
    }
    replacements: dict[Path, Path] = {}
    for target, old_content in targets.items():
        target.write_text(old_content, encoding="utf-8")
        pending = directory / f".{target.name}.pending"
        pending.write_text(f"new {target.name}", encoding="utf-8")
        replacements[target] = pending

    real_replace = os.replace

    def fail_during_publish(source: str | Path, destination: str | Path) -> None:
        if Path(source).name == ".graphic-screen.json.pending":
            raise OSError("injected publication failure")
        real_replace(source, destination)

    monkeypatch.setattr("backend.pipeline.service.os.replace", fail_during_publish)
    with pytest.raises(OSError, match="injected publication failure"):
        pipeline._publish_graphic_artifacts(project, replacements)

    for target, old_content in targets.items():
        assert target.read_text(encoding="utf-8") == old_content
    assert not any(path.exists() for path in replacements.values())
    archive = pipeline.store.project_path(project) / "variants" / "archive"
    assert not list(archive.iterdir())
