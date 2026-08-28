import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.rendering.process import (
    _register_process,
    _unregister_process,
    cancel_media_processes_for_job,
)
from backend.schemas import GenerationJob, JobStatus, Project
from backend.storage import InvalidJobTransition, PersistentJobQueue, StudioDatabase


def queue(tmp_path: Path) -> tuple[PersistentJobQueue, Project]:
    database = StudioDatabase(tmp_path / "studio.sqlite3")
    database.initialize()
    project = database.create_project(Project(
        title="Aqueducts", topic="Water", target_duration=30, slug="aqueducts"))
    return PersistentJobQueue(database), project


def test_claim_is_priority_ordered_and_persistent(tmp_path: Path) -> None:
    jobs, project = queue(tmp_path)
    low = jobs.enqueue(GenerationJob(project_id=project.id, stage="outline", priority=0))
    high = jobs.enqueue(GenerationJob(project_id=project.id, stage="script", priority=10))
    claimed = jobs.claim_next()
    assert claimed is not None and claimed.id == high.id
    assert claimed.status is JobStatus.PREPARING
    assert claimed.attempt_count == 1
    assert jobs.get(low.id).status is JobStatus.QUEUED  # type: ignore[union-attr]


def test_full_job_lifecycle(tmp_path: Path) -> None:
    jobs, project = queue(tmp_path)
    job = jobs.enqueue(GenerationJob(project_id=project.id, stage="render"))
    claimed = jobs.claim_next()
    assert claimed and claimed.id == job.id
    jobs.transition(job.id, JobStatus.GENERATING, progress=0.1)
    jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.9)
    completed = jobs.complete(job.id)
    assert completed.status is JobStatus.COMPLETED
    assert completed.progress == 1


def test_transition_records_start_time_on_first_active_state(tmp_path: Path) -> None:
    """started_at is set on the first in-flight transition, and only once."""
    jobs, project = queue(tmp_path)
    # A job canceled while still queued never started.
    never_started = jobs.enqueue(GenerationJob(project_id=project.id, stage="render"))
    canceled = jobs.cancel(never_started.id)
    assert canceled.started_at is None

    job = jobs.enqueue(GenerationJob(project_id=project.id, stage="render"))
    preparing = jobs.transition(job.id, JobStatus.PREPARING)
    assert preparing.started_at is not None
    generating = jobs.transition(job.id, JobStatus.GENERATING)
    # Later transitions keep the first start time.
    assert generating.started_at == preparing.started_at
    completed = jobs.complete(job.id)
    assert completed.started_at == preparing.started_at


def test_failure_and_retry_resume_without_rebuilding_prior_jobs(tmp_path: Path) -> None:
    jobs, project = queue(tmp_path)
    job = jobs.enqueue(GenerationJob(project_id=project.id, stage="scene-14", max_attempts=2))
    jobs.claim_next()
    jobs.transition(job.id, JobStatus.GENERATING)
    failed = jobs.fail(job.id, "synthetic failure")
    assert failed.error == "synthetic failure"
    retried = jobs.retry(job.id)
    assert retried.status is JobStatus.QUEUED
    assert jobs.claim_next().attempt_count == 2  # type: ignore[union-attr]


def test_invalid_transition_is_rejected(tmp_path: Path) -> None:
    jobs, project = queue(tmp_path)
    job = jobs.enqueue(GenerationJob(project_id=project.id, stage="render"))
    with pytest.raises(InvalidJobTransition):
        jobs.complete(job.id)


def test_recover_fails_running_jobs_after_restart(tmp_path: Path) -> None:
    jobs, project = queue(tmp_path)
    job = jobs.enqueue(GenerationJob(project_id=project.id, stage="render"))
    jobs.claim_next()
    jobs.transition(job.id, JobStatus.GENERATING, progress=0.5)
    recovered = jobs.recover_interrupted()
    assert [item.id for item in recovered] == [job.id]
    assert recovered[0].status is JobStatus.FAILED
    assert "backend restarted" in (recovered[0].error or "")


def test_cancel_racing_completion_is_not_lost(tmp_path: Path) -> None:
    """A user cancel concurrent with worker completion must land exactly one outcome."""
    jobs, project = queue(tmp_path)
    for round_index in range(10):
        job = jobs.enqueue(GenerationJob(
            project_id=project.id, stage=f"render-{round_index}"))
        jobs.claim_next()
        jobs.transition(job.id, JobStatus.GENERATING, progress=0.5)
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def do_complete() -> None:
            barrier.wait()
            try:
                jobs.complete(job.id)
                outcomes.append("completed")
            except InvalidJobTransition:
                outcomes.append("complete-rejected")

        def do_cancel() -> None:
            barrier.wait()
            try:
                jobs.cancel(job.id)
                outcomes.append("canceled")
            except InvalidJobTransition:
                outcomes.append("cancel-rejected")

        threads = [threading.Thread(target=do_complete), threading.Thread(target=do_cancel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Exactly one side wins; the loser is rejected instead of silently
        # overwriting the winner's row.
        assert sorted(outcomes) in (
            ["cancel-rejected", "completed"],
            ["canceled", "complete-rejected"],
        )
        final = jobs.get(job.id)
        assert final is not None
        assert final.status in {JobStatus.COMPLETED, JobStatus.CANCELED}
        if "completed" in outcomes:
            assert final.status is JobStatus.COMPLETED
        else:
            assert final.status is JobStatus.CANCELED


def test_cancel_media_processes_is_scoped_to_the_job() -> None:
    """Canceling job A must not kill fake process PIDs registered under job B."""
    proc_a = Mock()
    proc_a.pid = 999_001
    proc_b = Mock()
    proc_b.pid = 999_002
    pid_a = _register_process(proc_a, "job-a")
    pid_b = _register_process(proc_b, "job-b")
    orphan = Mock()
    orphan.pid = 999_003
    pid_orphan = _register_process(orphan, None)  # unattributed process survives too
    try:
        killed = cancel_media_processes_for_job("job-a")
        assert killed == [pid_a]
        proc_a.kill.assert_called_once()
        proc_b.kill.assert_not_called()
        orphan.kill.assert_not_called()
    finally:
        _unregister_process(pid_a)
        _unregister_process(pid_b)
        _unregister_process(pid_orphan)


def test_recover_also_fails_jobs_queued_before_restart(tmp_path: Path) -> None:
    # Nothing drains the persisted queue after startup, so a queued row from a
    # dead process would otherwise sit at 0% forever.
    jobs, project = queue(tmp_path)
    running = jobs.enqueue(GenerationJob(project_id=project.id, stage="render"))
    pending = jobs.enqueue(GenerationJob(project_id=project.id, stage="narration"))
    jobs.claim_next()
    jobs.transition(running.id, JobStatus.GENERATING)
    recovered = jobs.recover_interrupted()
    assert {item.status for item in recovered} == {JobStatus.FAILED}
    assert jobs.get(pending.id).status is JobStatus.FAILED  # type: ignore[union-attr]
    assert jobs.get(running.id).attempt_count == 1  # type: ignore[union-attr]
