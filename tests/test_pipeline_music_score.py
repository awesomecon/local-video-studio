"""score_mix stage wiring: selective invalidation and render consumption.

Phase 2 of Score Studio. ACE-Step (or the mock composer) owns
``music/background.wav``; cues are applied afterward to
``music/scored-background.wav`` and only downstream of the mix may re-run
when the cue plan changes.
"""

from __future__ import annotations

import json
import wave
from array import array
from pathlib import Path

import pytest

from backend.core import load_config
from backend.music import (
    ScoreCue,
    ScorePlan,
    hash_audio_file,
    load_score_mix_manifest,
    load_score_plan,
    save_score_plan,
    score_plan_hash,
    score_plan_path,
    scored_background_path,
)
from backend.pipeline import PipelineService
from backend.rendering.probe import probe_media
from backend.schemas import ProjectCreate


def service(tmp_path: Path) -> PipelineService:
    config = load_config(environ={})
    return PipelineService(
        config,
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )


def _create_project(pipeline: PipelineService) -> "ProjectCreate":
    project = pipeline.create_project(
        ProjectCreate(
            title="Scored Short",
            topic="testing the score studio",
            target_duration=6,
            resolution=(320, 180),
            fps=12,
        )
    )
    return project


def _rendered(pipeline: PipelineService) -> str:
    project = pipeline.create_project(
        ProjectCreate(
            title="Scored Short",
            topic="testing the score studio",
            target_duration=6,
            resolution=(320, 180),
            fps=12,
        )
    )
    pipeline.run_project(project.id)
    return project.id


def _project(pipeline: PipelineService, project_id: str):
    return pipeline._project(project_id)


def _root(pipeline: PipelineService, project_id: str) -> Path:
    return pipeline.store.project_path(pipeline._project(project_id))


def _plan_for(project: str, pipeline: PipelineService, cue_count: int = 2) -> ScorePlan:
    root = _root(pipeline, project)
    background = root / "music" / "background.wav"
    duration = probe_media(background, pipeline.renderer.binaries).duration_seconds
    cues = [
        ScoreCue(time_seconds=duration * 0.5, action="silence", transition_seconds=0.15),
        ScoreCue(
            time_seconds=duration * 0.62,
            action="impact",
            effect_path="music/effects/sting.wav",
            effect_gain_db=-4,
        ),
    ]
    return ScorePlan(
        duration_seconds=duration,
        source_music_hash=hash_audio_file(background),
        source_backend="mock",
        cues=cues[:cue_count],
    )


def _write_effect(project: str, pipeline: PipelineService) -> None:
    root = _root(pipeline, project)
    effects = root / "music" / "effects"
    effects.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(1200):  # 0.25 s at 48 kHz, 2 kHz tone
        value = int(20000 * (index / 1200) * (index / 1200))
        frames.extend([value] * 2)
    with wave.open(str(effects / "sting.wav"), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(array("h", frames).tobytes())


def _timeline_audio_tracks(pipeline: PipelineService, project_id: str) -> list[dict]:
    payload = json.loads((_root(pipeline, project_id) / "timeline.json").read_text(encoding="utf-8"))
    return payload["audio_tracks"]


def _stage_state(pipeline: PipelineService, project_id: str) -> dict:
    return pipeline._read_stage_state(pipeline._project(project_id))


def test_unscored_project_renders_unchanged(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project_id = _rendered(pipeline)
    root = _root(pipeline, project_id)
    # No score plan: score_mix passes through, writes no scored artifact and
    # no stage record; the timeline reads the generated master as before.
    assert not (root / "music" / "scored-background.wav").exists()
    assert not (root / "music" / "score-mix-manifest.json").exists()
    assert "score_mix" not in _stage_state(pipeline, project_id)["stages"]
    tracks = {track["kind"]: track["path"] for track in _timeline_audio_tracks(pipeline, project_id)}
    assert tracks["music"] == "music/background.wav"
    assert (root / "renders" / "final.mp4").is_file()


def test_scored_project_mixes_and_renders_without_regen_music(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project_id = _rendered(pipeline)
    root = _root(pipeline, project_id)
    background_hash = hash_audio_file(root / "music" / "background.wav")
    music_stage_before = _stage_state(pipeline, project_id)["stages"]["music"]

    _write_effect(project_id, pipeline)
    plan = _plan_for(project_id, pipeline)
    save_score_plan(root, plan, expected_revision=0)
    plan_hash = score_plan_hash(plan)

    pipeline.run_project(project_id)

    scored = root / "music" / "scored-background.wav"
    assert scored.is_file()
    # The generated master is untouched; only the scored copy differs.
    assert hash_audio_file(root / "music" / "background.wav") == background_hash
    assert scored.read_bytes() != (root / "music" / "background.wav").read_bytes()
    assert probe_media(scored, pipeline.renderer.binaries).has_audio

    # score_mix is recorded; music re-used its original record (no re-run).
    stages = _stage_state(pipeline, project_id)["stages"]
    assert stages["score_mix"]["status"] == "completed"
    assert "scored-background.wav" in stages["score_mix"]["outputs"][0]
    assert stages["music"] == music_stage_before

    # The manifest records inputs, plan hash, and the output hash.
    manifest = load_score_mix_manifest(root)
    assert manifest is not None
    assert manifest["plan"]["plan_hash"] == plan_hash
    assert manifest["source"]["sha256"] == background_hash
    assert manifest["output"]["sha256"] == hash_audio_file(scored)
    assert manifest["loudness_normalization"] is False

    # The timeline consumes the scored mix, not the raw master.
    tracks = {track["kind"]: track["path"] for track in _timeline_audio_tracks(pipeline, project_id)}
    assert tracks["music"] == "music/scored-background.wav"
    assert (root / "renders" / "final.mp4").is_file()


def test_cue_change_rebuilds_mix_but_not_music(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project_id = _rendered(pipeline)
    root = _root(pipeline, project_id)
    _write_effect(project_id, pipeline)
    save_score_plan(root, _plan_for(project_id, pipeline), expected_revision=0)
    pipeline.run_project(project_id)
    background_hash = hash_audio_file(root / "music" / "background.wav")
    music_record = _stage_state(pipeline, project_id)["stages"]["music"]
    mix_record = _stage_state(pipeline, project_id)["stages"]["score_mix"]
    scored_hash = hash_audio_file(scored_background_path(root))

    # A new cue revision: only the mix (and its render descendants) rebuild.
    new_plan = _plan_for(project_id, pipeline, cue_count=1)
    save_score_plan(root, new_plan, expected_revision=1)
    pipeline._invalidate_stages(
        pipeline._project(project_id),
        PipelineService.SCORE_PLAN_DOWNSTREAM_STAGES,
    )
    pipeline.run_project(project_id)

    assert hash_audio_file(root / "music" / "background.wav") == background_hash
    assert _stage_state(pipeline, project_id)["stages"]["music"] == music_record
    mix_after = _stage_state(pipeline, project_id)["stages"]["score_mix"]
    assert mix_after["job_id"] != mix_record["job_id"], "score_mix must have re-run"
    assert hash_audio_file(scored_background_path(root)) != scored_hash
    assert load_score_mix_manifest(root)["plan"]["plan_hash"] == score_plan_hash(new_plan)


def test_unchanged_plan_reuses_cached_mix(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project_id = _rendered(pipeline)
    root = _root(pipeline, project_id)
    _write_effect(project_id, pipeline)
    save_score_plan(root, _plan_for(project_id, pipeline), expected_revision=0)
    pipeline.run_project(project_id)
    scored_hash = hash_audio_file(scored_background_path(root))
    first_mix = _stage_state(pipeline, project_id)["stages"]["score_mix"]

    pipeline.run_project(project_id)

    assert hash_audio_file(scored_background_path(root)) == scored_hash
    assert _stage_state(pipeline, project_id)["stages"]["score_mix"] == first_mix


def test_score_mix_stage_rerun(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project_id = _rendered(pipeline)
    root = _root(pipeline, project_id)
    _write_effect(project_id, pipeline)
    save_score_plan(root, _plan_for(project_id, pipeline), expected_revision=0)
    pipeline.run_project(project_id)
    before = hash_audio_file(scored_background_path(root))

    outputs = pipeline.run_render_stage(project_id, "score_mix", force=True)

    assert any(path.name == "scored-background.wav" for path in outputs)
    assert any(path.name == "score-mix-manifest.json" for path in outputs)
    assert hash_audio_file(scored_background_path(root)) == before  # deterministic


def test_music_settings_invalidate_mix_but_not_visuals(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = _create_project(pipeline)
    updated, invalidated = pipeline.update_project(
        project.id,
        {"settings": {"music": {"bpm": 120, "direction": "tense and urgent"}}},
    )
    assert "music" in invalidated
    assert "score_mix" in invalidated
    assert "render_preview" in invalidated
    assert "visuals" not in invalidated
    assert "narration" not in invalidated
    assert "plan" not in invalidated

    updated, invalidated = pipeline.update_project(
        project.id, {"instructions": "focus on the reveal"}
    )
    assert "score_mix" in invalidated
    assert "visuals" in invalidated


def test_score_plan_downstream_set_never_touches_generation(tmp_path: Path) -> None:
    stages = PipelineService.SCORE_PLAN_DOWNSTREAM_STAGES
    assert {"score_mix", "timeline", "render_preview", "quality_control", "render_final"} <= stages
    assert not stages & {"music", "visuals", "narration", "plan", "subtitles", "references"}
    # The re-run endpoint accepts the new stage name.
    assert "score_mix" in PipelineService.RENDER_STAGE_NAMES
