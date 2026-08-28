"""Pipeline integration tests for the cross-project generation artifact cache."""

from __future__ import annotations

from pathlib import Path

import httpx

from backend.core import load_config
from backend.models import Krea2Backend
from backend.pipeline import PipelineService
from backend.schemas import AssetType, ProjectCreate, Scene, VisualType
from backend.workers.gpu import GPUSnapshot


def _snapshot(free_gb: float) -> tuple[GPUSnapshot, ...]:
    total = 23.5
    return (
        GPUSnapshot(
            index=0,
            name="NVIDIA GeForce RTX 4090",
            total_gb=total,
            used_gb=round(total - free_gb, 2),
            free_gb=free_gb,
            captured_at=0.0,
        ),
    )


def _service(tmp_path: Path, *, free_gb: float = 22.0) -> PipelineService:
    return PipelineService(
        load_config(environ={
            "LOCAL_VIDEO_STUDIO__PATHS__GENERATION_CACHE_ROOT": str(tmp_path / "generation-cache"),
        }),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=False,
        snapshot_provider=lambda: _snapshot(free_gb),
    )


def _scene(project_id: str, *, seed: int = 4242) -> Scene:
    return Scene(
        project_id=project_id,
        index=0,
        title="Still",
        duration=5,
        narration="A still image.",
        visual_prompt="Cinematic lighthouse at blue hour",
        negative_prompt="watermark, blurry details",
        visual_type=VisualType.KREA2_STILL,
        selected_backend="krea2_comfyui",
        seed=seed,
    )


def _add_project(pipeline: PipelineService, title: str, *, seed: int = 4242) -> Scene:
    project = pipeline.create_project(
        ProjectCreate(title=title, topic="local image generation", target_duration=10)
    )
    scene = _scene(project.id, seed=seed)
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)
    return scene


def _install_fake(pipeline: PipelineService) -> dict:
    stats = {"prompt_posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "comfyui_version": "0.33.0"})
        if request.url.path == "/prompt":
            stats["prompt_posts"] += 1
            return httpx.Response(200, json={"prompt_id": "krea-prompt"})
        if request.url.path == "/history/krea-prompt":
            return httpx.Response(
                200,
                json={
                    "krea-prompt": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "krea2_00001.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"fake-krea-png")
        if request.url.path == "/free":
            return httpx.Response(200)
        raise AssertionError(f"unexpected ComfyUI path: {request.url.path}")

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    pipeline.registry.register(
        Krea2Backend(poll_interval=0, client_factory=factory),
        name="krea2_comfyui",
        replace=True,
    )
    return stats


def test_identical_prompts_across_projects_share_one_backend_call(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    first_scene = _add_project(pipeline, "Cache A")
    second_scene = _add_project(pipeline, "Cache B")
    stats = _install_fake(pipeline)

    first = pipeline.generate_scene(first_scene.id)
    second = pipeline.generate_scene(second_scene.id)

    assert stats["prompt_posts"] == 1
    assert first.type is AssetType.IMAGE
    assert second.settings["cache_hit"] is True
    assert isinstance(second.settings["cache_key"], str)
    root = pipeline.store.project_path(pipeline._project(first_scene.project_id))
    other_root = pipeline.store.project_path(pipeline._project(second_scene.project_id))
    assert (root / first.filepath).read_bytes() == b"fake-krea-png"
    assert (other_root / second.filepath).read_bytes() == b"fake-krea-png"


def test_changed_seed_generates_a_fresh_artifact(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    first_scene = _add_project(pipeline, "Seed A")
    second_scene = _add_project(pipeline, "Seed B", seed=99)
    stats = _install_fake(pipeline)

    pipeline.generate_scene(first_scene.id)
    second = pipeline.generate_scene(second_scene.id)

    assert stats["prompt_posts"] == 2
    assert second.settings.get("cache_hit") is not True


def test_force_regeneration_bypasses_the_cache_read(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    scene = _add_project(pipeline, "Force")
    stats = _install_fake(pipeline)

    pipeline.generate_scene(scene.id)
    forced = pipeline.generate_scene(scene.id, force=True)

    assert stats["prompt_posts"] == 2
    assert forced.settings.get("cache_hit") is not True


def test_corrupt_cache_entry_degrades_to_regeneration(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    scene = _add_project(pipeline, "Corrupt")
    stats = _install_fake(pipeline)

    pipeline.generate_scene(scene.id)
    entry_root = tmp_path / "generation-cache" / "v1" / "krea2_comfyui"
    artifacts = list(entry_root.glob("*/artifact.bin"))
    assert len(artifacts) == 1
    artifacts[0].write_bytes(b"corrupted-payload")

    regenerated = pipeline.generate_scene(scene.id, force=True)

    assert stats["prompt_posts"] == 2
    assert regenerated.settings.get("cache_hit") is not True
