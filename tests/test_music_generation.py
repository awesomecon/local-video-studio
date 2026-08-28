"""Movement planner, procedural composer, stitching, and pipeline wiring."""

from __future__ import annotations

import json
import tempfile
import wave
from array import array
from pathlib import Path

import pytest

from backend.core import load_config
from backend.music import (
    SAMPLE_RATE,
    apply_edge_fades,
    compose_movement,
    energy_for_mood,
    plan_hash,
    plan_movements,
    read_wav_frames,
    stitch_dips,
)
from backend.music.stitch import build_stitch_command
from backend.pipeline import PipelineService
from backend.schemas import ProjectCreate, Scene, SceneStatus


def _service(tmp_path: Path, mock_mode: bool = True, ace_enabled: bool = False) -> PipelineService:
    config = load_config(environ={})
    config.backends.ace_step.enabled = ace_enabled
    return PipelineService(
        config,
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=mock_mode,
    )


def test_energy_for_mood_maps_wording() -> None:
    assert energy_for_mood("curious and restrained") < 0.4
    assert energy_for_mood("tense") == 0.55
    assert energy_for_mood("uplifting climax") > 0.7
    assert energy_for_mood("") == 0.5
    assert energy_for_mood("something unheard of") == 0.5


def test_plan_movements_groups_into_long_movements() -> None:
    durations = [10.0] * 12  # 120 seconds total
    moods = (
        ["curious and restrained"] * 6
        + ["tense"] * 3
        + ["calm"] * 3
    )
    plans = plan_movements(durations, moods, 120.0)
    assert len(plans) <= 3
    assert sum(plan.duration_seconds for plan in plans) == pytest.approx(120.0, abs=1e-3)
    for plan in plans[1:-1]:
        assert plan.duration_seconds >= 10.0
    # Mood shifts move boundaries: the tense stretch must not share a movement
    # with the calm tail.
    energies = [plan.energy for plan in plans]
    assert energies != sorted(energies) or len(set(energies)) > 1


def test_plan_movements_single_movement_when_short() -> None:
    plans = plan_movements([2.0, 3.0], ["curious", "curious"], 5.0)
    assert len(plans) == 1
    assert plans[0].duration_seconds == pytest.approx(5.0)


def test_plan_movements_merges_short_final_remainder() -> None:
    plans = plan_movements(
        [35.0, 15.0], ["calm", "epic climax"], 50.0,
        movement_seconds=35.0,
    )

    assert len(plans) == 1
    assert plans[0].duration_seconds == pytest.approx(50.0)


def test_plan_movements_clamps_legacy_short_target() -> None:
    plans = plan_movements(
        [25.0, 25.0], ["calm", "epic climax"], 50.0,
        movement_seconds=20.0,
    )

    assert len(plans) == 1
    assert plans[0].duration_seconds == pytest.approx(50.0)


def test_plan_movements_respects_hard_cap() -> None:
    plans = plan_movements(
        [30.0, 30.0, 30.0], ["curious", "curious", "curious"], 90.0,
        movement_seconds=60.0, max_movement_seconds=45.0,
    )
    assert all(plan.duration_seconds <= 45.5 for plan in plans)
    assert sum(plan.duration_seconds for plan in plans) == pytest.approx(90.0, abs=1e-3)


def test_plan_hash_changes_with_boundaries() -> None:
    # 30 s per scene clears the minimum-movement threshold, so the epic mood
    # shift forms its own movement and the plan digest changes.
    a = plan_movements([30.0, 30.0], ["calm", "calm"], 60.0)
    b = plan_movements([30.0, 30.0], ["calm", "epic climax"], 60.0)
    assert len(a) == 1
    assert len(b) == 2
    assert plan_hash(a) != plan_hash(b)


def test_compose_movement_deterministic_and_exact() -> None:
    kwargs = dict(
        duration_seconds=3.0, seed=30001, bpm=90,
        key_scale="C major", time_signature_beats=4, energy=0.5,
    )
    tmp_dir = Path(tempfile.gettempdir())
    tmp_a = tmp_dir / "lvs-test-mov-a.wav"
    tmp_b = tmp_dir / "lvs-test-mov-b.wav"
    compose_movement(tmp_a, **kwargs)
    compose_movement(tmp_b, **kwargs)
    assert tmp_a.read_bytes() == tmp_b.read_bytes()
    with wave.open(str(tmp_a)) as handle:
        assert handle.getnframes() == round(3.0 * SAMPLE_RATE)
        assert handle.getframerate() == SAMPLE_RATE


def test_compose_movement_varies_over_time() -> None:
    path = Path(tempfile.gettempdir()) / "lvs-test-mov-var.wav"
    compose_movement(path, duration_seconds=8.0, seed=7, bpm=100, key_scale="A minor", energy=0.8)
    samples = read_wav_frames(path)
    window = SAMPLE_RATE // 2
    rms_values = []
    for offset in range(0, len(samples) - window, window):
        chunk = samples[offset:offset + window]
        rms = (sum(value * value for value in chunk) / len(chunk)) ** 0.5
        rms_values.append(rms)
    # A constant sine tone would give near-identical windows; an arrangement
    # (melody walk, percussion, chord changes) does not.
    assert max(rms_values) - min(rms_values) > 400
    assert min(rms_values) > 100


def test_stitch_dips_keeps_total_and_interior_fades() -> None:
    first = array("h", [8000] * 2400)
    second = array("h", [-6000] * 2400)
    stitched = stitch_dips([first, second], int(0.05 * SAMPLE_RATE))
    assert len(stitched) == len(first) + len(second)
    boundary_start = len(first) - int(0.05 * SAMPLE_RATE)
    fade_zone = 2 * int(0.05 * SAMPLE_RATE)
    interior_peak = max(
        abs(value) for value in stitched[boundary_start:boundary_start + fade_zone]
    )
    assert interior_peak < 8000


def test_apply_edge_fades_zeroes_edges() -> None:
    samples = array("h", [10000] * 1000)
    apply_edge_fades(samples, 10, 10)
    assert samples[0] == 0
    assert abs(samples[len(samples) - 1]) < 1500
    assert samples[500] == 10000


def test_build_stitch_command_concatenates_with_fades(tmp_path: Path) -> None:
    clips: list[Path] = []
    for index in range(3):
        clip = tmp_path / f"movement-{index}.wav"
        with wave.open(str(clip), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(b"\x00\x01" * SAMPLE_RATE)
        clips.append(clip)
    argv = build_stitch_command(clips, tmp_path / "out.wav", dip_seconds=1.5)
    joined = " ".join(argv)
    assert "concat=n=3:v=0:a=1" in joined
    assert "afade=t=out" in joined and "afade=t=in" in joined
    assert joined.count("loudnorm=I=-16:TP=-1.5:LRA=11") == len(clips)
    assert str(tmp_path / "out.wav") in argv[-1]


# ---------------------------------------------------------------------------
# Pipeline integration


def test_mock_pipeline_records_manifest_and_movements(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Movements Mock", topic="test", target_duration=6, resolution=(160, 90))
    )
    pipeline.run_project(project.id)
    root = pipeline.store.project_path(project)
    background = root / "music" / "background.wav"
    manifest_path = root / "music" / "manifest.json"
    assert background.is_file()
    assert manifest_path.is_file()
    with wave.open(str(background)) as handle:
        duration = handle.getnframes() / handle.getframerate()
    assert duration > 0
    payload = json.loads(manifest_path.read_text())
    assert payload["movements"]
    assert all((root / entry["file"]).is_file() for entry in payload["movements"])
    assets = pipeline.database.list_assets(project.id)
    roles = {asset.settings.get("role") for asset in assets}
    assert {"music", "music_movement"} <= roles
    music = [asset for asset in assets if asset.settings.get("role") == "music"][-1]
    assert music.settings["movement_asset_ids"] == [
        entry["asset_id"] for entry in payload["movements"]
    ]


def test_real_path_generates_one_clip_per_movement_and_stitches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.models.ace_step_comfyui as ace_mod

    calls: list[dict] = []

    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {},
            "duration_range": {"min": 1.0, "max": 600.0},
            "error": None,
        }

    def fake_generate(self, request):
        calls.append({
            "prompt": request.prompt,
            "seed": request.seed,
            "duration": request.duration_seconds,
        })
        output = request.output_dir / "candidate.wav"
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(b"\x00\x40" * 44100 * 2)
        from backend.models import GenerationResult

        return GenerationResult(
            outputs=(output,),
            metadata={"prompt_id": "test", "peak_vram_gb": 0},
            peak_vram_gb=1.0,
        )

    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "readiness", fake_readiness)
    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "generate", fake_generate)

    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)

    def fake_snapshots():
        from backend.workers.gpu import GPUSnapshot
        return (GPUSnapshot(
            index=0, name="RTX 4090", total_gb=24.0,
            used_gb=1.0, free_gb=23.0, captured_at=0.0,
        ),)

    pipeline._snapshot_provider = fake_snapshots
    project = pipeline.create_project(
        ProjectCreate(title="Sectioned ACE", topic="test", target_duration=10, resolution=(160, 90))
    )
    project.settings["music"] = {
        "backend": "ace_step_comfyui",
        "movement_seconds": 30,
    }
    scenes = [
        Scene(project_id=project.id, index=0, duration=30.0, music_mood="calm",
              seed=1, status=SceneStatus.APPROVED),
        Scene(project_id=project.id, index=1, duration=30.0, music_mood="epic climax",
              seed=2, status=SceneStatus.APPROVED),
    ]
    for scene in scenes:
        pipeline.database.save_scene(scene)

    output = pipeline._ensure_music(project, force=True)
    root = pipeline.store.project_path(project)
    assert output is not None and output.is_file()

    # Two 30-second scenes at the minimum target produce two movements.
    assert len(calls) >= 2
    prompts = [call["prompt"] for call in calls]
    seeds = [call["seed"] for call in calls]
    assert len(set(prompts)) == len(prompts)
    assert len(set(seeds)) == len(seeds)
    assert any("section 1 of" in prompt for prompt in prompts)

    manifest = json.loads((root / "music" / "manifest.json").read_text())
    assert manifest["backend"] == "ace_step_comfyui"
    assert len(manifest["movements"]) == len(calls)
    music_assets = [
        asset for asset in pipeline.database.list_assets(project.id)
        if asset.settings.get("role") == "music"
    ]
    assert music_assets[-1].settings["movement_asset_ids"] == [
        entry["asset_id"] for entry in manifest["movements"]
    ]
    with wave.open(str(root / "music" / "background.wav")) as handle:
        stitched_duration = handle.getnframes() / handle.getframerate()
    assert stitched_duration == pytest.approx(60.0, abs=0.5)


def test_failed_movement_run_resumes_without_regenerating_finished_clips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.models.ace_step_comfyui as ace_mod
    from backend.models.errors import BackendError, BackendErrorCode

    state = {"calls": 0}

    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {},
            "duration_range": {"min": 1.0, "max": 600.0},
            "error": None,
        }

    def fake_generate(self, request):
        state["calls"] += 1
        if state["calls"] >= 2:
            raise BackendError(BackendErrorCode.INVALID_RESPONSE, "boom on second movement")
        output = request.output_dir / "candidate.wav"
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(b"\x00\x40" * 44100 * 2)
        from backend.models import GenerationResult

        return GenerationResult(outputs=(output,), metadata={}, peak_vram_gb=1.0)

    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "readiness", fake_readiness)
    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "generate", fake_generate)

    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)

    def fake_snapshots():
        from backend.workers.gpu import GPUSnapshot
        return (GPUSnapshot(
            index=0, name="RTX 4090", total_gb=24.0,
            used_gb=1.0, free_gb=23.0, captured_at=0.0,
        ),)

    pipeline._snapshot_provider = fake_snapshots
    project = pipeline.create_project(
        ProjectCreate(
            title="Resume Movements", topic="test", target_duration=10,
            resolution=(160, 90),
        )
    )
    project.settings["music"] = {"backend": "ace_step_comfyui", "movement_seconds": 30}
    for index, mood in enumerate(("calm", "epic climax")):
        pipeline.database.save_scene(
            Scene(project_id=project.id, index=index, duration=30.0, music_mood=mood,
                  seed=index + 1, status=SceneStatus.APPROVED)
        )

    from backend.pipeline.service import PipelineError

    with pytest.raises((BackendError, PipelineError)):
        pipeline._ensure_music(project, force=True)
    calls_after_first_run = state["calls"]

    # Retry without force: finished movement 1 is reused, only the remainder
    # regenerates before failing again at the same spot.
    with pytest.raises((BackendError, PipelineError)):
        pipeline._ensure_music(project, force=False)
    assert state["calls"] == calls_after_first_run + 1


def test_single_movement_regeneration_requires_matching_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.models.ace_step_comfyui as ace_mod

    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
            "combo_choices": {},
            "duration_range": {"min": 1.0, "max": 600.0},
            "error": None,
        }

    calls = {"count": 0}

    def fake_generate(self, request):
        calls["count"] += 1
        output = request.output_dir / "candidate.wav"
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(b"\x00\x40" * 44100)
        from backend.models import GenerationResult

        return GenerationResult(outputs=(output,), metadata={}, peak_vram_gb=1.0)

    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "readiness", fake_readiness)
    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "generate", fake_generate)

    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)

    def fake_snapshots():
        from backend.workers.gpu import GPUSnapshot
        return (GPUSnapshot(
            index=0, name="RTX 4090", total_gb=24.0,
            used_gb=1.0, free_gb=23.0, captured_at=0.0,
        ),)

    pipeline._snapshot_provider = fake_snapshots
    project = pipeline.create_project(
        ProjectCreate(
            title="Single Movement Regen", topic="test", target_duration=4,
            resolution=(160, 90),
        )
    )
    project.settings["music"] = {"backend": "ace_step_comfyui"}
    pipeline.database.save_scene(
        Scene(project_id=project.id, index=0, duration=4.0, music_mood="calm",
              seed=1, status=SceneStatus.APPROVED)
    )
    assert pipeline._ensure_music(project, force=True) is not None
    whole_runs = calls["count"]

    from backend.pipeline.service import PipelineError

    # No manifest yet matches after settings change → single-movement regen refuses.
    project.settings["music"]["bpm"] = 120
    with pytest.raises(PipelineError, match="Regenerate the whole soundtrack"):
        pipeline._ensure_music(project, force=True, regenerate_movement=0)
    assert calls["count"] == whole_runs
