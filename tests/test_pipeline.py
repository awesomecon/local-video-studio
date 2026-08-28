from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core import load_config
from backend.director import DirectorEngine
from backend.director.engine import DirectorPlanDraft
from backend.models import GenerationResult
from backend.pipeline import PipelineService
from backend.rendering.mock_media import create_placeholder_audio, create_placeholder_image
from backend.rendering.probe import probe_media
from backend.schemas import (
    AssetType, DurationMode, ProjectCreate, ProjectPlan, ProjectStatus, Scene,
    VisualType,
)
from backend.schemas.h3_continuity import validate_continuity_graph


def service(tmp_path: Path) -> PipelineService:
    config = load_config(environ={})
    return PipelineService(
        config,
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )


def test_complete_mock_pipeline_is_restartable_and_playable(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(
            title="Roman Aqueducts",
            topic="how Roman aqueducts worked",
            target_duration=3,
            resolution=(320, 180),
            fps=12,
        )
    )
    final = pipeline.run_project(project.id)
    first_job_count = len(pipeline.jobs.list(project.id))
    rerun = pipeline.run_project(project.id)

    assert rerun == final
    assert len(pipeline.jobs.list(project.id)) == first_job_count
    info = probe_media(final, pipeline.renderer.binaries)
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (320, 180)
    assert info.duration_seconds and info.duration_seconds >= 2.5
    snapshot = pipeline.project_snapshot(project.id)
    assert snapshot["project"]["status"] == ProjectStatus.COMPLETED.value
    directory = Path(snapshot["directory"])
    for relative in (
        "project.json",
        "plan.json",
        "script/outline.md",
        "script/script.md",
        "narration/master.wav",
        "music/background.wav",
        "subtitles/captions.srt",
        "subtitles/captions.ass",
        "timeline.json",
        "renders/preview.mp4",
        "renders/final.mp4",
        "publishing-metadata.json",
    ):
        assert (directory / relative).stat().st_size > 0
    timeline = json.loads((directory / "timeline.json").read_text(encoding="utf-8"))
    assert all(not Path(clip["path"]).is_absolute() for clip in timeline["clips"])
    qc = json.loads((directory / "renders" / "qc.json").read_text(encoding="utf-8"))
    assert qc["passed"]
    assert qc["inspected_files"] >= 2
    assert "subtitle_overflow" not in {issue["code"] for issue in qc["issues"]}


def test_stage_state_json_survives_concurrent_mark_and_invalidate(tmp_path: Path) -> None:
    """stage-state.json read-modify-write is safe from request/background thread races."""
    import threading

    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Stage Race", topic="races", target_duration=3, resolution=(160, 90))
    )
    root = pipeline.store.project_path(project)
    output = root / "project.json"
    errors: list[BaseException] = []

    def mark(index: int) -> None:
        try:
            pipeline._mark_stage(project, f"stage-{index}", [output], f"job-{index}")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def invalidate() -> None:
        try:
            pipeline._invalidate_stages(project, {f"stage-{i}" for i in range(0, 20, 2)})
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=mark, args=(i,)) if i % 3 else threading.Thread(target=invalidate)
        for i in range(24)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    state = pipeline._read_stage_state(project)
    assert isinstance(state.get("stages", {}), dict)


def test_single_scene_regeneration_archives_previous_variant(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Test", topic="test", target_duration=3, resolution=(160, 90))
    )
    plan = pipeline.ensure_plan(project.id)
    pipeline._ensure_references(project, force=False)
    first = pipeline.generate_scene(plan.scenes[0].id)
    second = pipeline.generate_scene(plan.scenes[0].id, force=True)
    archive = pipeline.store.project_path(project) / "variants" / "archive"

    assert first.id != second.id
    assert any(archive.iterdir())


def test_forced_replan_preserves_scene_ids_and_invalidates_downstream(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Test", topic="test", target_duration=3, resolution=(160, 90))
    )
    initial = pipeline.ensure_plan(project.id)
    pipeline._ensure_references(project, force=False)
    replanned = pipeline.ensure_plan(project.id, force=True)
    state = pipeline.project_snapshot(project.id)["stage_state"]["stages"]

    assert [scene.id for scene in replanned.scenes] == [scene.id for scene in initial.scenes]
    assert "references" not in state
    assert "plan" in state


def test_forced_replan_rewrites_h3_continuity_predecessor_links(tmp_path: Path) -> None:
    """A replan keeps persisted scene ids; continuity links must follow the swap.

    The director authors ``h3_continuity.predecessor_scene_id`` against the fresh
    in-memory ids of its own materialization. If the id remap left those links
    untouched, every continuation scene kept a predecessor id that exists in no
    scene list and generation failed with
    "Predecessor scene id ... is not a scene in this project."
    """
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="H3 replan", topic="test", target_duration=12, resolution=(160, 90))
    )

    def chain_plan() -> ProjectPlan:
        draft = DirectorPlanDraft(
            title=project.title,
            outline=["head", "continuation"],
            scenes=[
                {
                    "index": 0, "title": "Head", "duration": 5.0,
                    "visual_type": VisualType.H3_AUDIOVISUAL.value,
                },
                {
                    "index": 1, "title": "Continuation", "duration": 5.0,
                    "visual_type": VisualType.H3_AUDIOVISUAL.value,
                    "continue_previous_h3": True,
                },
            ],
        )
        return DirectorEngine._materialize_plan(pipeline._project(project.id), draft)

    class StubDirector:
        llm = None

        def plan_with_draft(self, _project: object, *, mock_mode: bool = False,
                            comparison_mode: bool | None = None):
            return chain_plan(), None

    original = pipeline.director
    pipeline.director = StubDirector()
    try:
        pipeline.ensure_plan(project.id)
        replanned = pipeline.ensure_plan(project.id, force=True)
    finally:
        pipeline.director = original

    scenes = pipeline.database.list_scenes(project.id)
    persisted_ids = {scene.index: scene.id for scene in scenes}
    assert [scene.id for scene in replanned.scenes] == [
        persisted_ids[scene.index] for scene in replanned.scenes
    ]
    for scene in scenes:
        block = scene.settings.get("h3_continuity")
        if isinstance(block, dict) and block.get("predecessor_scene_id"):
            assert block["predecessor_scene_id"] == persisted_ids[scene.index - 1]
        validate_continuity_graph(scene, scenes)


def test_project_settings_persist_portably_and_invalidate_only_dependents(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Settings", topic="test", target_duration=2, resolution=(160, 90))
    )
    pipeline.run_project(project.id)
    updated, _ = pipeline.update_project(
        project.id,
        {
            "narrator_preference": "calm",
            "settings": {"voice": {"language": "en", "speed": 1.25}},
        },
    )
    stages = pipeline.project_snapshot(project.id)["stage_state"]["stages"]

    assert updated.settings["voice"] == {"language": "en", "speed": 1.25}
    assert "visuals" in stages
    assert "narration" not in stages
    assert "thumbnails" not in stages
    portable = json.loads(
        (pipeline.store.project_path(updated) / "project.json").read_text(encoding="utf-8")
    )
    assert portable["narrator_preference"] == "calm"
    assert portable["settings"]["voice"]["language"] == "en"


def test_camera_motion_ignores_locked_and_video_instructions() -> None:
    motion = PipelineService._camera_motion

    assert motion("slow push in", "image") == "slow push in"
    assert motion("slow push in", "image", VisualType.IMAGE_MOTION) == "slow push in"
    assert motion("slow push in", "image", VisualType.KREA2_STILL) is None
    assert motion("slow push in", "image", VisualType.KREA2_STILL.value) is None
    assert motion("slow push in", "image", VisualType.GRAPHIC_SCREEN) is None
    assert motion("slow push in", "image", VisualType.GRAPHIC_SCREEN.value) is None
    assert motion("locked", "image") is None
    assert motion("locked-off", "image") is None
    assert motion("no_motion", "image") is None
    assert motion("slow push in", "video") is None


def test_update_scene_narration_invalidates_narration_and_downstream(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Narration Edit", topic="test", target_duration=2, resolution=(160, 90), fps=12)
    )
    pipeline.run_project(project.id)
    scene = pipeline.database.list_scenes(project.id)[0]

    pipeline.update_scene(scene.id, {"narration": "Completely new narration for this scene."})
    stages = pipeline.project_snapshot(project.id)["stage_state"]["stages"]

    for stage in (
        "narration", "subtitles", "timeline", "render_preview",
        "quality_control", "render_final", "thumbnails",
    ):
        assert stage not in stages, f"{stage} must be invalidated by a narration edit"
    for stage in ("plan", "references", "visuals", "music", "metadata"):
        assert stage in stages, f"{stage} must survive a narration edit"

    # The rerun rebuilds the chain instead of reusing the stale audio/timings.
    pipeline.run_project(project.id)
    root = pipeline.store.project_path(project)
    srt = (root / "subtitles" / "captions.srt").read_text(encoding="utf-8")
    assert "Completely new narration" in srt
    assert pipeline.project_snapshot(project.id)["project"]["status"] == ProjectStatus.COMPLETED.value


def test_update_scene_duration_and_camera_invalidate_timeline_and_renders(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Timing Edit", topic="test", target_duration=2, resolution=(160, 90), fps=12)
    )
    pipeline.run_project(project.id)
    scene = pipeline.database.list_scenes(project.id)[-1]  # image_motion still: camera motion applies

    pipeline.update_scene(
        scene.id,
        {"duration": scene.duration + 2, "camera_instruction": "slow push in"},
    )
    stages = pipeline.project_snapshot(project.id)["stage_state"]["stages"]

    for stage in ("timeline", "render_preview", "quality_control", "render_final", "thumbnails"):
        assert stage not in stages, f"{stage} must be invalidated by a duration/camera edit"
    for stage in ("plan", "narration", "references", "visuals", "music", "subtitles", "metadata"):
        assert stage in stages, f"{stage} must survive a duration/camera edit"

    pipeline.run_project(project.id)
    assert pipeline.project_snapshot(project.id)["project"]["status"] == ProjectStatus.COMPLETED.value


def test_update_scene_irrelevant_edits_invalidate_nothing(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="No-op Edit", topic="test", target_duration=2, resolution=(160, 90), fps=12)
    )
    pipeline.run_project(project.id)
    scene = pipeline.database.list_scenes(project.id)[0]
    before = set(pipeline.project_snapshot(project.id)["stage_state"]["stages"])

    # A same-value edit is a no-op, and prompt/seed/reference edits only take
    # effect through per-scene regeneration (generate_scene invalidates the
    # timeline chain itself), so neither may disturb completed stages.
    pipeline.update_scene(scene.id, {"narration": scene.narration})
    pipeline.update_scene(scene.id, {
        "visual_prompt": "a different establishing shot",
        "negative_prompt": "no watermark",
        "references": ["mood.png"],
        "seed": scene.seed + 1,
    })

    after = set(pipeline.project_snapshot(project.id)["stage_state"]["stages"])
    assert after == before
    updated = pipeline.database.get_scene(scene.id)
    assert updated is not None
    assert updated.settings["visual_revision"] == 1
    stale_visuals = [
        asset for asset in pipeline.project_snapshot(project.id)["assets"]
        if asset["scene_id"] == scene.id and asset["settings"].get("role") == "visual"
    ]
    assert stale_visuals
    assert all(asset["current"] is False for asset in stale_visuals)


def test_update_scene_syncs_prompt_into_portable_plan(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Portable prompt edit", topic="test", target_duration=2)
    )
    scene = Scene(
        project_id=project.id,
        index=0,
        title="Opening",
        narration="Original narration",
        visual_prompt="Original visual prompt",
        duration=2,
        visual_type=VisualType.IMAGE_MOTION,
    )
    pipeline.database.save_scene(scene)
    pipeline.store.save_scene(project.slug, scene)
    pipeline.store.save_plan(
        project.slug,
        ProjectPlan(
            project_id=project.id,
            title=project.title,
            target_duration=project.target_duration,
            scenes=[scene],
        ),
    )

    pipeline.update_scene(scene.id, {"visual_prompt": "A precise new visual prompt"})

    stored = pipeline.store.load_plan(project.slug).scenes[0]
    assert stored.visual_prompt == "A precise new visual prompt"
    assert stored.settings["visual_revision"] == 1


def test_timeline_extends_stills_to_narration_without_mutating_scenes(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Extended", topic="test", target_duration=1, resolution=(160, 90))
    )
    scenes = [
        Scene(
            project_id=project.id,
            index=0,
            duration=0.4,
            narration="First.",
            visual_type=VisualType.FLUX_STILL,
        ),
        Scene(
            project_id=project.id,
            index=1,
            duration=0.6,
            narration="Second.",
            visual_type=VisualType.FLUX_STILL,
            transition="crossfade",
        ),
    ]
    root = pipeline.store.project_path(project)
    for scene in scenes:
        pipeline.database.save_scene(scene)
        visual = create_placeholder_image(
            root / "scenes" / f"{scene.index + 1:03d}" / "visual.png",
            width=160,
            height=90,
            seed=scene.index,
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
                    "backend": "mock",
                    "model": "placeholder",
                    "workflow_version": "mock-v1",
                    "seed": scene.index,
                },
            ),
            role="visual",
        )
    create_placeholder_audio(
        root / "narration" / "master.wav",
        duration_seconds=1.6,
        binaries=pipeline.renderer.binaries,
    )

    timeline = pipeline._ensure_timeline(project, force=True)
    persisted = pipeline.database.list_scenes(project.id)
    payload = json.loads((root / "timeline.json").read_text(encoding="utf-8"))

    assert timeline.duration_seconds == pytest.approx(1.6)
    base_durations = [
        timeline.clips[0].duration_seconds - timeline.clips[1].transition_duration_seconds,
        timeline.clips[1].duration_seconds,
    ]
    assert base_durations == pytest.approx([0.64, 0.96])
    assert [scene.duration for scene in persisted] == [0.4, 0.6]
    assert payload["metadata"] == {
        "workflow_version": "timeline-v2",
        "duration_policy": "extend_visuals_to_narration_v1",
            "planned_scene_duration_seconds": 1.0,
            "narration_duration_seconds": pytest.approx(1.6),
            "narration_gain_db": 0.0,
            "visuals_extended": True,
            "scene_audio_synced": False,
        }
    assert all(not Path(clip["path"]).is_absolute() for clip in payload["clips"])


def test_short_narration_does_not_contract_planned_timeline(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Planned", topic="test", target_duration=1.6, resolution=(160, 90))
    )
    scene = Scene(
        project_id=project.id,
        index=0,
        duration=1.6,
        narration="Keep the plan.",
        visual_type=VisualType.FLUX_STILL,
    )
    pipeline.database.save_scene(scene)
    root = pipeline.store.project_path(project)
    visual = create_placeholder_image(
        root / "scenes" / "001" / "visual.png",
        width=160,
        height=90,
        seed=1,
        binaries=pipeline.renderer.binaries,
    )
    pipeline._record_asset(
        project,
        scene,
        visual,
        AssetType.IMAGE,
        GenerationResult(
            outputs=(visual,),
            metadata={"backend": "mock", "model": "placeholder", "seed": 1},
        ),
        role="visual",
    )
    create_placeholder_audio(
        root / "narration" / "master.wav",
        duration_seconds=1.0,
        binaries=pipeline.renderer.binaries,
    )

    timeline = pipeline._build_timeline(project)

    assert timeline.duration_seconds == pytest.approx(1.6)
    assert timeline.metadata["visuals_extended"] is False


def test_llm_duration_mode_adopts_director_runtime_as_project_target(tmp_path: Path) -> None:
    """duration_mode=llm: the script-sized runtime becomes the project target."""
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(
            title="Director Runtime",
            topic="runtime control",
            target_duration=6,
            resolution=(160, 90),
            duration_mode=DurationMode.LLM,
        )
    )
    scenes = [
        Scene(project_id=project.id, index=0, title="A", duration=4.0),
        Scene(project_id=project.id, index=1, title="B", duration=4.0),
    ]
    plan = ProjectPlan(
        project_id=project.id, title="Runtime plan", outline=["a"],
        scenes=scenes, target_duration=8.0,
    )

    class StubDirector:
        llm = None

        def plan_with_draft(self, _project: object, *, mock_mode: bool = False,
                            comparison_mode: bool | None = None):
            return plan, None

    original = pipeline.director
    pipeline.director = StubDirector()
    try:
        planned = pipeline.ensure_plan(project.id)
    finally:
        pipeline.director = original

    assert planned.target_duration == 8.0
    reloaded = pipeline._project(project.id)
    assert reloaded.target_duration == 8.0
    on_disk = json.loads(
        (pipeline.store.project_path(reloaded) / "project.json").read_text(encoding="utf-8")
    )
    assert on_disk["target_duration"] == 8.0


def test_changing_duration_mode_invalidates_the_plan(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Mode Switch", topic="test", target_duration=3, resolution=(160, 90))
    )
    pipeline.ensure_plan(project.id)
    assert "plan" in pipeline.project_snapshot(project.id)["stage_state"]["stages"]

    _updated, invalidated = pipeline.update_project(project.id, {"duration_mode": "llm"})

    assert "plan" in invalidated
    assert "plan" not in pipeline.project_snapshot(project.id)["stage_state"]["stages"]


def test_mock_pipeline_completes_with_llm_duration_mode(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(
            title="Mock LLM Mode", topic="test", target_duration=3,
            resolution=(160, 90), duration_mode=DurationMode.LLM,
        )
    )

    final = pipeline.run_project(project.id)

    info = probe_media(final, pipeline.renderer.binaries)
    assert info.has_video and info.has_audio
    assert info.duration_seconds and info.duration_seconds >= 2.5
