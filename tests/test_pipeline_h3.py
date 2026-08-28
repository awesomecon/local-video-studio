"""Pipeline-level integration tests for the real MiniMax H3 AV dispatch.

A fake ComfyUI HTTP service (``httpx.MockTransport``) stands in for the local
ComfyUI instance, so these tests never download weights or touch the GPU. The
fake implements the subset of the ComfyUI API that ``ComfyUIBackend`` uses:
``/system_stats`` (health), ``/prompt`` (submit), ``/history/{id}`` (poll), and
``/view`` (retrieve).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.core import load_config
from backend.models import ComfyUIBackend
from backend.models.errors import BackendError, BackendErrorCode
from backend.pipeline import PipelineService
from backend.pipeline.service import PipelineError
from backend.rendering.binaries import discover_binaries
from backend.rendering.mock_media import create_placeholder_video
from backend.schemas import AssetType, ProjectCreate, Scene, VisualType
from backend.schemas.h3_continuity import h3_continuity_status
from backend.workers.gpu import GPUSnapshot


def _real_mp4_bytes(
    output_path: Path, *, width: int = 320, height: int = 180, seed: int = 3,
) -> bytes:
    """Encode a real, probe-able MP4 so the pipeline's QC-before-publish gate can pass.

    The fake ComfyUI /view handler returns these bytes. Encoding is required because
    the H3 publish path probes the returned file before publishing; skip when no
    FFmpeg is available (mirrors other media tests).
    """
    discovered = discover_binaries()
    if not discovered.available or discovered.ffmpeg is None:
        pytest.skip("FFmpeg is unavailable for H3 publish QC")
    create_placeholder_video(
        output_path,
        duration_seconds=0.5,
        width=width,
        height=height,
        fps=12,
        seed=seed,
        binaries=discovered,
    )
    return output_path.read_bytes()


def _service(
    tmp_path: Path,
    *,
    mock_mode: bool,
    snapshot_provider=None,
) -> PipelineService:
    config = load_config(environ={
        "LOCAL_VIDEO_STUDIO__PATHS__GENERATION_CACHE_ROOT": str(tmp_path / "app" / "generation-cache"),
    })
    return PipelineService(
        config,
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=mock_mode,
        snapshot_provider=snapshot_provider,
    )


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


def _h3_scene(project_id: str, *, prompt: str = "a lighthouse at night", seed: int = 757358688076805) -> Scene:
    return Scene(
        project_id=project_id,
        index=0,
        title="Shot",
        duration=5.0,
        narration="hello",
        visual_prompt=prompt,
        visual_type=VisualType.H3_AUDIOVISUAL,
        seed=seed,
    )


def _install_fake_comfyui(pipeline: PipelineService, *, free_bytes: bytes = b"fake-mp4-bytes") -> dict:
    """Register a fake ComfyUI backend and return a dict that captures the /prompt body."""
    submitted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "comfyui_version": "0.33.0"})
        if request.url.path == "/prompt":
            submitted.update(json.loads(request.content))
            return httpx.Response(200, json={"prompt_id": "prompt-1"})
        if request.url.path == "/upload/image":
            submitted["uploaded_reference"] = True
            return httpx.Response(200, json={"name": "first-frame.png", "subfolder": "lvs"})
        if request.url.path == "/history/prompt-1":
            return httpx.Response(
                200,
                json={
                    "prompt-1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "15": {
                                "videos": [
                                    {"filename": "minimax_h3_00001.mp4", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=submitted.get("response_bytes", free_bytes))
        raise AssertionError(f"unexpected ComfyUI path: {request.url.path}")

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    pipeline.registry.register(
        ComfyUIBackend(poll_interval=0, client_factory=factory),
        name="comfyui",
        replace=True,
    )
    return submitted


def test_h3_visual_dispatches_to_local_comfyui(tmp_path: Path) -> None:
    pipeline = _service(
        tmp_path,
        mock_mode=False,
        snapshot_provider=lambda: _snapshot(free_gb=22.0),
    )
    project = pipeline.create_project(
        ProjectCreate(
            title="H3 Shot",
            topic="minimax h3",
            target_duration=12,
            resolution=(1344, 768),
        )
    )
    scene = _h3_scene(project.id)
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)
    mp4_bytes = _real_mp4_bytes(tmp_path / "fake-output.mp4")
    submitted = _install_fake_comfyui(pipeline, free_bytes=mp4_bytes)

    asset = pipeline.generate_scene(scene.id)

    assert asset.type is AssetType.VIDEO
    assert asset.backend == "comfyui"
    assert asset.scene_id == scene.id
    output = pipeline.store.project_path(project) / asset.filepath
    assert output.read_bytes() == mp4_bytes

    # The real (non-mock) path substituted the actual template placeholders.
    prompt = submitted["prompt"]
    node_6 = prompt["6"]["inputs"]
    assert node_6["prompt"] == "a lighthouse at night"
    assert node_6["width"] == 1344
    assert node_6["height"] == 768
    # 5 s at 24 fps snapped up to the model's 17k+5 grid = 124 frames.
    assert node_6["length"] == 124
    assert prompt["9"]["inputs"]["noise_seed"] == 757358688076805
    assert asset.settings.get("role") == "visual"


def test_h3_dispatch_refuses_without_free_vram(tmp_path: Path) -> None:
    pipeline = _service(
        tmp_path,
        mock_mode=False,
        snapshot_provider=lambda: _snapshot(free_gb=4.0),
    )
    project = pipeline.create_project(
        ProjectCreate(title="H3 Blocked", topic="minimax h3", target_duration=12)
    )
    scene = _h3_scene(project.id)
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)

    with pytest.raises(BackendError, match="Free system VRAM") as exc_info:
        pipeline.generate_scene(scene.id)
    assert exc_info.value.code is BackendErrorCode.INSUFFICIENT_VRAM


def test_h3_canvas_and_frame_count() -> None:
    service = PipelineService._h3_canvas
    snap = PipelineService._h3_frame_count
    # 16:9 stays on the 768-short-edge canvas regardless of the project resolution.
    assert service(1344, 768) == (1344, 768)
    assert service(1920, 1080) == (1344, 768)
    # Portrait is mirrored to the 768-wide canvas.
    assert service(1080, 1920) == (768, 1344)
    # Durations snap up to the 17k+5 frame grid at 24 fps (minimum 5 frames).
    assert snap(0.1) == 5
    assert snap(5.0) == 124
    assert snap(10.0) == 243
    assert snap(12.0) == 294


def test_h3_canvas_override_used_in_dispatch(tmp_path: Path) -> None:
    pipeline = _service(
        tmp_path,
        mock_mode=False,
        snapshot_provider=lambda: _snapshot(free_gb=22.0),
    )
    project = pipeline.create_project(
        ProjectCreate(
            title="H3 Override",
            topic="minimax h3",
            target_duration=12,
            resolution=(1920, 1080),
        )
    )
    scene = _h3_scene(project.id)
    scene.settings = {"h3_canvas": "1024x576"}
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)
    submitted = _install_fake_comfyui(pipeline, free_bytes=_real_mp4_bytes(tmp_path / "fake-output.mp4"))

    asset = pipeline.generate_scene(scene.id)

    assert asset.type is AssetType.VIDEO
    # The scene override beats the project-resolution canvas rule.
    node_6 = submitted["prompt"]["6"]["inputs"]
    assert node_6["width"] == 1024
    assert node_6["height"] == 576


def test_h3_canvas_parse() -> None:
    parse = PipelineService._parse_h3_canvas
    assert parse("1152x640") == (1152, 640)
    # The separator is case-insensitive.
    assert parse("768X1344") == (768, 1344)
    with pytest.raises(PipelineError, match="WIDTHxHEIGHT"):
        parse("1024")
    with pytest.raises(PipelineError, match="aligned to 32"):
        parse("1025x576")
    with pytest.raises(PipelineError, match="at least 256"):
        parse("128x128")


def test_update_scene_merges_h3_canvas_into_settings(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, mock_mode=True)
    project = pipeline.create_project(
        ProjectCreate(title="H3 Merge", topic="minimax h3", target_duration=12)
    )
    scene = _h3_scene(project.id)
    scene.settings = {"other": 1}
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)

    updated = pipeline.update_scene(scene.id, {"h3_canvas": "1152x640"})

    # The override merges into settings instead of replacing them.
    assert updated.settings == {
        "other": 1, "h3_canvas": "1152x640", "visual_revision": 1,
    }
    assert pipeline.database.get_scene(scene.id).settings == {
        "other": 1,
        "h3_canvas": "1152x640",
        "visual_revision": 1,
    }


def test_update_scene_rejects_unknown_h3_quality_at_edit_time(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, mock_mode=True)
    project = pipeline.create_project(
        ProjectCreate(title="H3 quality guard", topic="minimax h3", target_duration=5)
    )
    scene = Scene(
        project_id=project.id,
        index=0,
        title="Plain scene",
        duration=5,
        narration="hello",
        visual_prompt="a shot",
        visual_type=VisualType.FLUX_STILL,
    )
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)

    # Even a non-H3 scene rejects an unusable preset instead of persisting it
    # and crashing generation later with an H3PolicyError.
    with pytest.raises(ValueError, match="Invalid h3_quality 'ultra'") as excinfo:
        pipeline.update_scene(scene.id, {"h3_quality": "ultra"})
    message = str(excinfo.value)
    for value in ("fast_safe", "standard", "high", "custom"):
        assert value in message
    assert pipeline.database.get_scene(scene.id).settings == {}

    accepted = pipeline.update_scene(scene.id, {"h3_quality": "standard"})
    assert accepted.settings["h3_quality"] == "standard"


def test_h3_continuity_status_reports_error_for_invalid_enabled_graph() -> None:
    scene = _h3_scene("project-1")
    scene.settings = {
        "h3_continuity": {"enabled": True, "predecessor_scene_id": scene.id},
    }

    status = h3_continuity_status(scene, [scene], {})

    assert status["enabled"] is True
    assert status["status"] == "error"
    assert "invalid" in status["detail"].lower()
    assert "itself" in status["detail"]


def test_update_scene_enforces_h3_policy_and_defaults_new_h3_scene(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, mock_mode=True)
    project = pipeline.create_project(
        ProjectCreate(title="H3 policy", topic="minimax h3", target_duration=12)
    )
    scene = Scene(
        project_id=project.id,
        index=0,
        title="Convert me",
        duration=5,
        narration="hello",
        visual_prompt="a shot",
        visual_type=VisualType.FLUX_STILL,
    )
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)

    updated = pipeline.update_scene(scene.id, {"visual_type": "h3_audiovisual"})
    assert updated.settings["h3_quality"] == "standard"

    with pytest.raises(ValueError, match="preset cap"):
        pipeline.update_scene(scene.id, {"h3_quality": "high", "duration": 8})
    with pytest.raises(ValueError, match="explicit.*canvas"):
        pipeline.update_scene(scene.id, {"h3_quality": "custom", "h3_canvas": "auto"})
    with pytest.raises(ValueError, match="enabled must be true or false"):
        pipeline.update_scene(scene.id, {"h3_continuity": {"enabled": "false"}})
    with pytest.raises(ValueError, match="preset cap"):
        pipeline.update_scene(scene.id, {
            "h3_quality": "fast_safe", "h3_canvas": "auto",
            "h3_long_shot": False, "duration": 20,
        })
    long_shot = pipeline.update_scene(scene.id, {
        "h3_quality": "fast_safe", "h3_canvas": "auto",
        "h3_long_shot": True, "duration": 20,
    })
    assert long_shot.duration == 20


def test_h3_continuation_dispatch_staleness_cache_and_atomic_regeneration(tmp_path: Path) -> None:
    pipeline = _service(
        tmp_path,
        mock_mode=False,
        snapshot_provider=lambda: _snapshot(free_gb=22.0),
    )
    project = pipeline.create_project(ProjectCreate(
        title="H3 continuity", topic="minimax h3", target_duration=10,
        resolution=(1920, 1080),
    ))
    first = _h3_scene(project.id, prompt="first", seed=101)
    first.settings = {"h3_quality": "standard"}
    second = _h3_scene(project.id, prompt="second", seed=102)
    second.index = 1
    second.settings = {
        "h3_quality": "standard",
        "h3_continuity": {
            "enabled": True,
            "group": "hero",
            "predecessor_scene_id": first.id,
        },
    }
    for scene in (first, second):
        pipeline.database.save_scene(scene)
        pipeline.store.save_scene(project.slug, scene)

    first_bytes = _real_mp4_bytes(
        tmp_path / "first.mp4", width=1024, height=576, seed=11,
    )
    second_bytes = _real_mp4_bytes(
        tmp_path / "second.mp4", width=1024, height=576, seed=12,
    )
    submitted = _install_fake_comfyui(pipeline, free_bytes=first_bytes)
    first_asset = pipeline.generate_scene(first.id)
    submitted["response_bytes"] = second_bytes
    second_asset = pipeline.generate_scene(second.id)

    assert submitted["uploaded_reference"] is True
    assert submitted["prompt"]["6"]["inputs"]["first_frame"] == ["16", 0]
    assert submitted["prompt"]["16"]["inputs"]["image"] == "lvs/first-frame.png"
    provenance = second_asset.settings["h3_continuity"]
    assert provenance["predecessor_asset_id"] == first_asset.id
    frame = pipeline.store.project_path(project) / provenance["keyframe_path"]
    original_frame_hash = provenance["keyframe_sha256"]
    assert frame.is_file()

    # A corrupt cached PNG is not trusted even when its JSON manifest still matches.
    frame.write_bytes(b"corrupt")
    submitted["response_bytes"] = second_bytes
    pipeline.generate_scene(second.id, force=True)
    assert frame.is_file() and frame.read_bytes() != b"corrupt"
    assert len(original_frame_hash) == 64

    # Regenerating A changes its hash and makes B stale while B's media remains reviewable.
    replacement_bytes = _real_mp4_bytes(
        tmp_path / "first-replacement.mp4", width=1024, height=576, seed=44,
    )
    submitted["response_bytes"] = replacement_bytes
    pipeline.generate_scene(first.id, force=True)
    snapshot = pipeline.project_snapshot(project.id)
    second_payload = next(item for item in snapshot["scenes"] if item["id"] == second.id)
    assert second_payload["h3"]["status"] == "source_stale"
    second_output = pipeline.store.project_path(project) / second_asset.filepath
    assert second_output.is_file()

    # A failed replacement leaves the current approved A file untouched.
    first_output = pipeline.store.project_path(project) / first_asset.filepath
    approved_bytes = first_output.read_bytes()
    submitted["response_bytes"] = b"not-an-mp4"
    with pytest.raises(PipelineError, match="failed QC"):
        pipeline.generate_scene(first.id, force=True)
    assert first_output.read_bytes() == approved_bytes


def test_h3_readiness_does_not_require_cold_load_headroom_when_resident(tmp_path: Path) -> None:
    pipeline = _service(
        tmp_path, mock_mode=False, snapshot_provider=lambda: _snapshot(free_gb=2.0),
    )
    pipeline._resident_comfy_backend = "comfyui"

    readiness = pipeline.h3_vram_readiness()

    assert readiness["cold_load_required"] is False
    assert readiness["must_free_vram"] is False
