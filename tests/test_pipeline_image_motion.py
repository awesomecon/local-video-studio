from pathlib import Path

from backend.core import load_config
from backend.models import GenerationResult
from backend.pipeline import PipelineService
from backend.rendering.mock_media import create_placeholder_audio
from backend.schemas import AssetType, ProjectCreate, Scene, VisualType


def test_image_motion_uses_krea_still_and_reaches_timeline(
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
            title="Moving Still",
            topic="deterministic image motion",
            target_duration=5,
            resolution=(1920, 1080),
        )
    )
    scene = Scene(
        project_id=project.id,
        index=0,
        title="Push in",
        duration=5,
        narration="The camera moves into the generated still.",
        visual_prompt="A moonlit observatory on a mountain",
        visual_type=VisualType.IMAGE_MOTION,
        camera_instruction="slow push in",
        seed=77,
    )
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)
    dispatched: list[str] = []

    def fake_krea(project, scene, directory, *, use_cache=True):
        dispatched.append(scene.id)
        output = directory / "krea-source.png"
        output.write_bytes(b"generated-still")
        return GenerationResult(
            outputs=(output,),
            metadata={
                "backend": "krea2_comfyui",
                "model": "Krea 2 Turbo",
                "model_version": "open-v1.0",
                "quantization": "fp8_scaled",
                "workflow_version": "krea2-turbo-fp8-v1",
                "seed": scene.seed,
                "settings": {"kind": "image"},
            },
        )

    monkeypatch.setattr(pipeline, "_dispatch_krea2", fake_krea)

    asset = pipeline.generate_scene(scene.id)
    create_placeholder_audio(
        pipeline.store.project_path(project) / "narration" / "master.wav",
        duration_seconds=scene.duration,
        binaries=pipeline.renderer.binaries,
    )
    timeline = pipeline._build_timeline(project)

    assert dispatched == [scene.id]
    assert asset.type is AssetType.IMAGE
    assert asset.backend == "krea2_comfyui"
    assert asset.filepath == Path("scenes/001/visual.png")
    assert asset.settings["image_motion_source"] == "krea2"
    assert timeline.clips[0].media_kind == "image"
    assert timeline.clips[0].camera_motion == "slow push in"


def test_image_motion_can_use_qwen_and_source_switch_stales_old_asset(
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
            title="Lettered Motion",
            topic="readable text with camera movement",
            target_duration=5,
            resolution=(1920, 1080),
        )
    )
    scene = Scene(
        project_id=project.id,
        index=0,
        title="Storefront",
        duration=5,
        narration="The camera approaches the sign.",
        visual_prompt="A cinematic storefront with a large illuminated sign",
        visual_type=VisualType.IMAGE_MOTION,
        camera_instruction="slow push in",
        seed=91,
        settings={
            "image_motion_source": "qwen_image_2512",
            "on_screen_text": ["OPEN LATE"],
        },
    )
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)
    dispatched: list[str] = []

    def fake_qwen(project, scene, directory, *, use_cache=True):
        dispatched.append(scene.id)
        output = directory / "qwen-source.png"
        output.write_bytes(b"generated-qwen-still")
        return GenerationResult(
            outputs=(output,),
            metadata={
                "backend": "qwen_image_2512_comfyui",
                "model": "Qwen-Image-2512",
                "model_version": "2512",
                "quantization": "fp8_e4m3fn",
                "workflow_version": "qwen-image-2512-fp8-v1",
                "seed": scene.seed,
                "settings": {"kind": "image", "on_screen_text": ["OPEN LATE"]},
            },
        )

    def unexpected_krea(*args, **kwargs):
        raise AssertionError("Krea must not run when Qwen is selected")

    monkeypatch.setattr(pipeline, "_dispatch_qwen_image_2512", fake_qwen)
    monkeypatch.setattr(pipeline, "_dispatch_krea2", unexpected_krea)

    asset = pipeline.generate_scene(scene.id)

    assert dispatched == [scene.id]
    assert asset.type is AssetType.IMAGE
    assert asset.backend == "qwen_image_2512_comfyui"
    assert asset.settings["image_motion_source"] == "qwen_image_2512"
    assert asset.settings["on_screen_text"] == ["OPEN LATE"]
    assert pipeline._is_current_visual_asset(scene, asset)

    updated = pipeline.update_scene(scene.id, {"image_motion_source": "krea2"})

    assert updated.settings["image_motion_source"] == "krea2"
    assert not pipeline._is_current_visual_asset(updated, asset)
