"""Pipeline integration tests for Qwen-Image-2512 through a fake ComfyUI."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from backend.core import load_config
from backend.models import QwenImage2512Backend
from backend.pipeline import PipelineService
from backend.schemas import AssetType, ProjectCreate, Scene, VisualType
from backend.workers.gpu import GPUSnapshot


def _service(tmp_path: Path) -> PipelineService:
    snapshot = GPUSnapshot(
        index=0, name="RTX 4090", total_gb=23.5, used_gb=1.0,
        free_gb=22.5, captured_at=0.0,
    )
    return PipelineService(
        load_config(environ={
            "LOCAL_VIDEO_STUDIO__PATHS__GENERATION_CACHE_ROOT": str(
                tmp_path / "app" / "generation-cache"
            ),
        }),
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=False,
        snapshot_provider=lambda: (snapshot,),
    )


def test_qwen_image_scene_dispatches_text_capable_fp8_workflow(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    project = pipeline.create_project(ProjectCreate(
        title="Qwen Text", topic="generated signage", target_duration=10,
        resolution=(1920, 1080),
    ))
    scene = Scene(
        project_id=project.id, index=0, title="Sign", duration=5,
        narration="A sign appears.",
        visual_prompt="A cinematic neon storefront sign at night",
        negative_prompt="watermark", visual_type=VisualType.QWEN_IMAGE_STILL,
        selected_backend="qwen_image_2512_comfyui", seed=2512,
        settings={"on_screen_text": ["OPEN LATE", "NIGHT MARKET"]},
    )
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)
    submitted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "comfyui_version": "0.18.1"})
        if request.url.path == "/prompt":
            submitted.update(json.loads(request.content))
            return httpx.Response(200, json={"prompt_id": "qwen-prompt"})
        if request.url.path == "/history/qwen-prompt":
            return httpx.Response(200, json={
                "qwen-prompt": {
                    "status": {"status_str": "success"},
                    "outputs": {"10": {"images": [{
                        "filename": "qwen_00001.png", "subfolder": "", "type": "output",
                    }]}},
                },
            })
        if request.url.path == "/view":
            return httpx.Response(200, content=b"fake-qwen-png")
        if request.url.path == "/free":
            return httpx.Response(200)
        raise AssertionError(f"unexpected ComfyUI path: {request.url.path}")

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    pipeline.registry.register(
        QwenImage2512Backend(poll_interval=0, client_factory=factory),
        name="qwen_image_2512_comfyui", replace=True,
    )

    asset = pipeline.generate_scene(scene.id)

    assert asset.type is AssetType.IMAGE
    assert asset.backend == "qwen_image_2512_comfyui"
    assert asset.model == "Qwen-Image-2512"
    assert asset.model_version == "2512"
    assert asset.quantization == "fp8_e4m3fn"
    assert asset.workflow_version == "qwen-image-2512-fp8-v1"
    assert asset.settings["on_screen_text"] == ["OPEN LATE", "NIGHT MARKET"]
    assert asset.settings["width"] == 1664
    assert asset.settings["height"] == 928
    prompt = submitted["prompt"]
    assert prompt["1"]["inputs"]["unet_name"] == "qwen_image_2512_fp8_e4m3fn.safetensors"
    assert prompt["2"]["inputs"]["clip_name"] == "qwen_2.5_vl_7b_fp8_scaled.safetensors"
    assert prompt["4"]["inputs"]["shift"] == 3.1
    assert prompt["8"]["inputs"]["steps"] == 50
    assert prompt["8"]["inputs"]["cfg"] == 4.0
    assert prompt["8"]["inputs"]["seed"] == 2512
    assert '"OPEN LATE"' in prompt["5"]["inputs"]["text"]
    assert '"NIGHT MARKET"' in prompt["5"]["inputs"]["text"]


def test_qwen_image_canvas_presets_and_validation(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    project = pipeline.create_project(ProjectCreate(
        title="Portrait", topic="text", target_duration=10, resolution=(1080, 1920),
    ))
    scene = Scene(project_id=project.id, index=0, title="Text", duration=5)
    assert pipeline._qwen_image_scene_canvas(project, scene) == (928, 1664)
    scene.settings = {"qwen_image_canvas": "1328x1328"}
    assert pipeline._qwen_image_scene_canvas(project, scene) == (1328, 1328)
