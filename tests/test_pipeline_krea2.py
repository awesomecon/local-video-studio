"""Pipeline integration tests for local Krea 2 Turbo through a fake ComfyUI."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.core import load_config
from backend.models import Krea2Backend
from backend.pipeline import PipelineService
from backend.pipeline.service import PipelineError
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
            "LOCAL_VIDEO_STUDIO__PATHS__GENERATION_CACHE_ROOT": str(tmp_path / "app" / "generation-cache"),
        }),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=False,
        snapshot_provider=lambda: _snapshot(free_gb),
    )


def _scene(project_id: str) -> Scene:
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
        seed=4242,
    )


def _install_fake(pipeline: PipelineService) -> dict:
    submitted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "comfyui_version": "0.33.0"})
        if request.url.path == "/prompt":
            submitted.update(json.loads(request.content))
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
    return submitted


def test_krea2_scene_dispatches_native_fp8_workflow(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(
            title="Krea Still",
            topic="local image generation",
            target_duration=10,
            resolution=(1920, 1080),
        )
    )
    scene = _scene(project.id)
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)
    submitted = _install_fake(pipeline)

    asset = pipeline.generate_scene(scene.id)

    assert asset.type is AssetType.IMAGE
    assert asset.backend == "krea2_comfyui"
    assert asset.model == "Krea 2 Turbo"
    assert asset.model_version == "open-v1.0"
    assert asset.quantization == "fp8_scaled"
    assert asset.workflow_version == "krea2-turbo-fp8-v1"
    assert asset.settings == {
        "kind": "image",
        "scene_id": scene.id,
        "width": 1344,
        "height": 768,
        "steps": 8,
        "cfg": 1.0,
        "sampler": "er_sde",
        "scheduler": "simple",
        "visual_type": "krea2_still",
        "role": "visual",
    }
    assert (pipeline.store.project_path(project) / asset.filepath).read_bytes() == b"fake-krea-png"

    prompt = submitted["prompt"]
    assert prompt["1"]["inputs"]["unet_name"] == "krea2_turbo_fp8_scaled.safetensors"
    assert prompt["2"]["inputs"] == {
        "clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
        "type": "krea2",
        "device": "default",
    }
    assert prompt["3"]["inputs"]["vae_name"] == "qwen_image_vae.safetensors"
    assert prompt["4"]["inputs"]["width"] == 1344
    assert prompt["4"]["inputs"]["height"] == 768
    assert prompt["7"]["inputs"]["seed"] == 4242
    assert prompt["7"]["inputs"]["steps"] == 8
    assert prompt["7"]["inputs"]["cfg"] == 1.0
    assert prompt["7"]["inputs"]["sampler_name"] == "er_sde"
    assert prompt["7"]["inputs"]["scheduler"] == "simple"
    assert prompt["5"]["inputs"]["text"] == "Cinematic lighthouse at blue hour"
    assert "Avoid:" not in json.dumps(prompt)


def test_consecutive_krea_scenes_reuse_resident_model_without_vram_recheck(
    tmp_path: Path,
) -> None:
    snapshot_calls = 0

    def snapshots() -> tuple[GPUSnapshot, ...]:
        nonlocal snapshot_calls
        snapshot_calls += 1
        # A loaded Krea stack leaves less than the cold-load threshold free.
        return _snapshot(22.0 if snapshot_calls == 1 else 4.0)

    pipeline = PipelineService(
        load_config(environ={
            "LOCAL_VIDEO_STUDIO__PATHS__GENERATION_CACHE_ROOT": str(tmp_path / "app" / "generation-cache"),
        }),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=False,
        snapshot_provider=snapshots,
    )
    project = pipeline.create_project(
        ProjectCreate(title="Krea Batch", topic="two scenes", target_duration=10)
    )
    first = _scene(project.id)
    second = _scene(project.id).model_copy(update={"index": 1, "id": "scene-two", "seed": 4243})
    for scene in (first, second):
        pipeline.database.save_scene(scene)
        pipeline.store.save_scene(project.slug, scene)
    _install_fake(pipeline)

    pipeline.generate_scene(first.id)
    pipeline.generate_scene(second.id)

    assert snapshot_calls == 1
    assert pipeline.resident_comfy_backend == "krea2_comfyui"


def test_krea2_canvas_override_and_validation(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Portrait", topic="portrait", target_duration=10, resolution=(1080, 1920))
    )
    scene = _scene(project.id)
    assert pipeline._krea2_scene_canvas(project, scene) == (768, 1344)
    scene.settings = {"krea_canvas": "896x1152"}
    assert pipeline._krea2_scene_canvas(project, scene) == (896, 1152)

    parse = PipelineService._parse_krea2_canvas
    assert parse("1024X1024") == (1024, 1024)
    with pytest.raises(PipelineError, match="WIDTHxHEIGHT"):
        parse("1024")
    with pytest.raises(PipelineError, match="aligned to 16"):
        parse("1025x768")
    with pytest.raises(PipelineError, match="at least 256"):
        parse("128x128")
    with pytest.raises(PipelineError, match="one-megapixel"):
        parse("1344x1024")


def test_krea2_refuses_to_load_without_free_vram(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, free_gb=4.0)
    project = pipeline.create_project(
        ProjectCreate(title="Blocked", topic="krea", target_duration=10)
    )
    scene = _scene(project.id)
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)

    with pytest.raises(PipelineError, match="Free system VRAM"):
        pipeline.generate_scene(scene.id)


def test_update_scene_merges_both_canvas_settings(tmp_path: Path) -> None:
    pipeline = PipelineService(
        load_config(environ={}),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )
    project = pipeline.create_project(ProjectCreate(title="Canvases", topic="settings", target_duration=10))
    scene = _scene(project.id)
    scene.settings = {"other": 1, "h3_canvas": "auto"}
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)

    updated = pipeline.update_scene(
        scene.id,
        {"h3_canvas": "1024x576", "krea_canvas": "1024x1024"},
    )

    assert updated.settings == {
        "other": 1,
        "h3_canvas": "1024x576",
        "krea_canvas": "1024x1024",
        "visual_revision": 1,
    }
