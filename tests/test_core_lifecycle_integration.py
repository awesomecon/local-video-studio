"""Cancellation at the core mock-output and service shutdown boundaries."""

from pathlib import Path
import sys
import threading
import pytest

from backend.core import load_config
from backend.models.base import GenerationRequest
from backend.models.errors import BackendError, BackendErrorCode
from backend.models.mock import MockGeneratorBackend
from backend.pipeline.service import PipelineService
from backend.rendering import process
from backend.schemas import GenerationJob, JobStatus, ProjectCreate
from backend.storage.jobs import InvalidJobTransition


def test_mock_cancel_preserves_previous_output(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "video.mp4"
    output.write_bytes(b"previous completed output")
    runner = process.run_media_process
    entered = threading.Event()

    def slow_media(argv, **kwargs):
        entered.set()
        return runner([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(process, "run_media_process", slow_media)
    backend = MockGeneratorBackend(ffmpeg_path=sys.executable)
    request = GenerationRequest(job_id="mock-cancel-integration", output_dir=tmp_path,
                                prompt="test", seed=1, settings={"kind": "video"})
    errors = []

    def generate():
        try:
            backend.generate(request)
        except BackendError as exc:
            errors.append(exc)

    thread = threading.Thread(target=generate)
    thread.start()
    assert entered.wait(5)
    backend.cancel(request.job_id)
    thread.join(10)
    assert not thread.is_alive()
    assert errors and errors[0].code == BackendErrorCode.CANCELED
    assert output.read_bytes() == b"previous completed output"
    assert not list(tmp_path.glob(".mock-video-*"))


def test_service_shutdown_cancels_its_queued_job_and_retry_resets_intent(tmp_path: Path) -> None:
    service = PipelineService(load_config(environ={}), database_path=tmp_path / "index.db",
                              project_root=tmp_path / "projects", temp_root=tmp_path / "tmp",
                              mock_mode=True)
    project = service.create_project(ProjectCreate(title="Shutdown", topic="test", target_duration=1))
    job = service.jobs.enqueue(GenerationJob(project_id=project.id, stage="pipeline"))
    service.cancel_job(job.id)
    assert service.jobs.get(job.id).status is JobStatus.CANCELED
    service.retry_job(job.id)
    with process.media_process_scope(job.id):
        process.raise_if_media_job_canceled()
    service.shutdown()
    assert service.jobs.get(job.id).status is JobStatus.CANCELED
    with pytest.raises(InvalidJobTransition, match="shutting down"):
        service.jobs.enqueue(GenerationJob(project_id=project.id, stage="pipeline"))


def test_mock_prestart_cancel_requires_explicit_reset(tmp_path: Path) -> None:
    backend = MockGeneratorBackend()
    request = GenerationRequest(job_id="prestart-cancel", output_dir=tmp_path,
                                prompt="test", seed=1, settings={"kind": "image"})
    backend.cancel(request.job_id)
    with pytest.raises(BackendError) as error:
        backend.generate(request)
    assert error.value.code == BackendErrorCode.CANCELED
    assert not (tmp_path / "image.png").exists()
    backend.reset_cancel(request.job_id)
    assert backend.generate(request).outputs[0].is_file()
