"""Pipeline tests for ACE-Step 1.5 XL music generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core import load_config
from backend.models import ACEStepComfyUIBackend
from backend.models.errors import BackendError, BackendErrorCode
from backend.pipeline import PipelineService
from backend.pipeline.service import PipelineError
from backend.schemas import AssetType, ProjectCreate


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


def test_mock_music_deterministic(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Mock Music", topic="test", target_duration=2, resolution=(160, 90))
    )
    pipeline.run_project(project.id)
    output = pipeline.store.project_path(project) / "music" / "background.wav"
    assert output.is_file()
    assert output.stat().st_size > 0


def test_disabled_music_is_no_output_stage(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=False)
    project = pipeline.create_project(
        ProjectCreate(title="No Music", topic="test", target_duration=2, resolution=(160, 90))
    )
    result = pipeline._ensure_music(project, force=True)
    assert result is None


def test_real_music_skipped_when_backend_not_ace_step_comfyui(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)
    project = pipeline.create_project(
        ProjectCreate(title="Wrong Backend", topic="test", target_duration=2, resolution=(160, 90))
    )
    project.settings["music"] = {"backend": "mock"}
    result = pipeline._ensure_music(project, force=True)
    assert result is None


def test_real_music_selects_correct_workflow(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)
    project = pipeline.create_project(
        ProjectCreate(title="Workflow Select", topic="test", target_duration=2, resolution=(160, 90))
    )
    project.settings["music"] = {"backend": "ace_step_comfyui", "model": "xl_sft"}
    with pytest.raises(Exception):
        pipeline._ensure_music(project, force=True)
    job = pipeline.jobs.list(project_id=project.id, status=None)
    music_jobs = [j for j in job if j.stage == "music"]
    assert len(music_jobs) == 1
    assert music_jobs[0].status.value == "failed"


def test_audio_code_generation_enabled_by_default(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)
    project = pipeline.create_project(
        ProjectCreate(title="Audio Codes", topic="test", target_duration=2, resolution=(160, 90))
    )
    project.settings["music"] = {"backend": "ace_step_comfyui"}
    subs = pipeline._build_ace_substitutions(project, project.settings.get("music", {}))
    assert subs["generate_audio_codes"] is True


def test_unchanged_inputs_reuse_completed_stage(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Reuse", topic="test", target_duration=2, resolution=(160, 90))
    )
    pipeline.run_project(project.id)
    output = pipeline.store.project_path(project) / "music" / "background.wav"
    assert output.is_file()
    first_mtime = output.stat().st_mtime
    pipeline.run_project(project.id, force=False)
    assert output.is_file()
    assert output.stat().st_mtime == first_mtime


def test_changed_settings_invalidate_music(tmp_path: Path) -> None:
    pipeline = _service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(title="Invalidate", topic="test", target_duration=2, resolution=(160, 90))
    )
    pipeline.run_project(project.id)
    output = pipeline.store.project_path(project) / "music" / "background.wav"
    assert output.is_file()
    project.settings["music"] = {"style": "different"}
    pipeline.run_project(project.id, force=False)
    assert output.is_file()


def test_insufficient_vram_fails_before_submission(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)
    project = pipeline.create_project(
        ProjectCreate(title="VRAM", topic="test", target_duration=2, resolution=(160, 90))
    )
    project.settings["music"] = {"backend": "ace_step_comfyui"}

    def fake_snapshots():
        from backend.workers.gpu import GPUSnapshot
        return (GPUSnapshot(index=0, name="RTX 4090", total_gb=24.0, used_gb=20.0, free_gb=4.0),)

    pipeline._snapshot_provider = fake_snapshots
    with pytest.raises(Exception):
        pipeline._ensure_music(project, force=True)


def test_render_without_music_still_works(tmp_path: Path) -> None:
    pipeline = _service(tmp_path, ace_enabled=False)
    project = pipeline.create_project(
        ProjectCreate(title="No Music Render", topic="test", target_duration=2, resolution=(160, 90))
    )
    project.settings["music"] = {"mood": "none"}
    pipeline.run_project(project.id)
    output = pipeline.store.project_path(project) / "renders" / "final.mp4"
    assert output.is_file()


def test_ace_duration_above_installed_maximum_fails_before_submission() -> None:
    readiness = {"duration_range": {"min": 1.0, "max": 1000.0}}
    with pytest.raises(PipelineError, match="exceeds ACE maximum"):
        PipelineService._ace_generation_duration(1000.1, readiness)


def test_ace_duration_below_installed_minimum_generates_then_trims() -> None:
    readiness = {"duration_range": {"min": 10.0, "max": 600.0}}
    assert PipelineService._ace_generation_duration(2.0, readiness) == 10.0
    assert PipelineService._ace_generation_duration(30.0, readiness) == 30.0


def test_render_with_uninstalled_sft_fails_before_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turbo being installed must not green-light an SFT submission.

    Regression: _ensure_music validated only turbo readiness, so a render with
    xl_sft selected submitted a workflow referencing
    acestep_v1.5_xl_sft_bf16.safetensors, which ComfyUI rejects at validation.
    """
    import backend.models.ace_step_comfyui as ace_mod

    def fake_readiness(self):
        return {
            "comfyui_healthy": True,
            "turbo": {"ready": True, "missing_nodes": [], "missing_files": []},
            "sft": {
                "ready": False,
                "missing_nodes": [],
                "missing_files": ["acestep_v1.5_xl_sft_bf16.safetensors"],
            },
            "combo_choices": {},
            "duration_range": {"min": 10, "max": 600},
            "error": None,
        }

    def fail_generate(self, request):
        raise AssertionError("generate must not run when the selected preset is missing")

    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "readiness", fake_readiness)
    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "generate", fail_generate)

    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)
    project = pipeline.create_project(
        ProjectCreate(title="Missing SFT", topic="test", target_duration=2, resolution=(160, 90))
    )
    project.settings["music"] = {"backend": "ace_step_comfyui", "model": "xl_sft"}
    with pytest.raises(PipelineError, match="xl_sft"):
        pipeline._ensure_music(project, force=True)


def test_failed_music_generation_records_failed_attempt_with_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ACE generation must persist a failed attempt (AGENTS.md).

    Regression: _ensure_music's operation had try/finally with no except, so
    failed generations left no attempt record, and the surviving records
    dropped the prompt (popped from substitutions) and the real job id.
    """
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

    def fail_generate(self, request):
        raise BackendError(BackendErrorCode.INVALID_RESPONSE, "ComfyUI rejected the request")

    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "readiness", fake_readiness)
    monkeypatch.setattr(ace_mod.ACEStepComfyUIBackend, "generate", fail_generate)

    pipeline = _service(tmp_path, mock_mode=False, ace_enabled=True)
    project = pipeline.create_project(
        ProjectCreate(title="Fail Attempt", topic="test", target_duration=2, resolution=(160, 90))
    )
    project.settings["music"] = {"backend": "ace_step_comfyui", "model": "xl_turbo"}

    def fake_snapshots():
        from backend.workers.gpu import GPUSnapshot
        return (GPUSnapshot(
            index=0, name="RTX 4090", total_gb=24.0, used_gb=1.0, free_gb=23.0,
            captured_at=0.0,
        ),)

    pipeline._snapshot_provider = fake_snapshots
    with pytest.raises(BackendError, match="rejected"):
        pipeline._ensure_music(project, force=True)

    attempts = [
        attempt
        for attempt in pipeline.database.list_attempts()
        if attempt.backend == "ace_step_comfyui" and not attempt.success
    ]
    assert len(attempts) == 1
    attempt = attempts[0]
    assert "rejected" in (attempt.error or "")
    # The attempt is attributed to the real stage job (whose row exists,
    # satisfying the generation_attempts.job_id foreign key), and the prompt
    # survives even though it was popped out of the ComfyUI substitutions.
    music_jobs = [
        job for job in pipeline.jobs.list(project_id=project.id, status=None)
        if job.stage == "music"
    ]
    assert len(music_jobs) == 1
    assert attempt.job_id == music_jobs[0].id
    assert attempt.parameters.get("request_job_id")
    assert "instrumental background music" in attempt.parameters.get("prompt", "")
    assert attempt.parameters.get("fingerprint")
