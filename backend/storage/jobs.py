"""Persistent, restartable job queue with explicit state transitions."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from time import monotonic
from typing import Any, ContextManager

from backend.core.progress import progress_scope
from backend.schemas import GenerationJob, JobProgress, JobStatus, utc_now

from .database import StudioDatabase


class InvalidJobTransition(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    # QUEUED may also fail: a runner can raise before the first transition,
    # and recover_interrupted already fails queued rows at restart.
    JobStatus.QUEUED: {JobStatus.PREPARING, JobStatus.FAILED, JobStatus.CANCELED},
    JobStatus.PREPARING: {
        JobStatus.LOADING_MODEL, JobStatus.GENERATING, JobStatus.FAILED, JobStatus.CANCELED,
    },
    JobStatus.LOADING_MODEL: {JobStatus.GENERATING, JobStatus.FAILED, JobStatus.CANCELED},
    JobStatus.GENERATING: {
        JobStatus.POSTPROCESSING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
    },
    JobStatus.POSTPROCESSING: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.QUEUED},
    JobStatus.CANCELED: {JobStatus.QUEUED},
}

# Any non-terminal state is lost when the owning process dies: execution is
# driven by per-request background tasks, so nothing survives a restart.
_ACTIVE_STATES = {
    JobStatus.QUEUED, JobStatus.PREPARING, JobStatus.LOADING_MODEL,
    JobStatus.GENERATING, JobStatus.POSTPROCESSING,
}

# First entry into an in-flight state is the job's actual start time. The
# claim path used to be the only writer, but nothing claims jobs anymore
# (execution is task-driven), so transition() records it instead.
_STARTED_STATES = {
    JobStatus.PREPARING, JobStatus.LOADING_MODEL,
    JobStatus.GENERATING, JobStatus.POSTPROCESSING,
}


class PersistentJobQueue:
    def __init__(self, database: StudioDatabase, *, control_lock: ContextManager[Any] | None = None) -> None:
        self.database = database
        self._control_lock = control_lock or threading.RLock()
        self._accepting = True

    def close_submissions(self) -> None:
        with self._control_lock:
            self._accepting = False

    def enqueue(self, job: GenerationJob) -> GenerationJob:
        with self._control_lock:
            if not self._accepting:
                raise InvalidJobTransition("application is shutting down; new jobs are not accepted")
            if job.status is not JobStatus.QUEUED:
                raise ValueError("new jobs must be queued")
            return self.database.save_job(job)

    def claim_next(self, now: datetime | None = None) -> GenerationJob | None:
        return self.database.claim_queued_job(now or utc_now())

    def get(self, job_id: str) -> GenerationJob | None:
        return self.database.get_job(job_id)

    def list(self, project_id: str | None = None,
             status: JobStatus | None = None) -> list[GenerationJob]:
        return self.database.list_jobs(project_id, status)

    def transition(self, job_id: str, status: JobStatus, *, progress: float | None = None,
                   error: str | None = None, now: datetime | None = None) -> GenerationJob:
        timestamp = now or utc_now()

        def apply(job: GenerationJob) -> GenerationJob:
            if status not in _ALLOWED_TRANSITIONS[job.status]:
                raise InvalidJobTransition(
                    f"cannot transition job from {job.status} to {status}")
            update: dict[str, Any] = {"status": status, "updated_at": timestamp}
            if status in _STARTED_STATES and job.started_at is None:
                update["started_at"] = timestamp
            if status in _STARTED_STATES:
                update["progress_detail"] = JobProgress(phase=status.value)
            if progress is not None:
                update["progress"] = progress
            if status is JobStatus.COMPLETED:
                update.update(progress=1.0, completed_at=timestamp, error=None)
            elif status is JobStatus.FAILED:
                if not error:
                    raise ValueError("failed jobs require an error message")
                update.update(error=error, completed_at=timestamp)
            elif status is JobStatus.CANCELED:
                update.update(error=error or "canceled by user", completed_at=timestamp)
            elif error is not None:
                update["error"] = error
            return GenerationJob.model_validate({**job.model_dump(), **update})

        # Atomic read-validate-write in one transaction: a worker completing a
        # job concurrent with a user cancel can no longer lose either update.
        return self.database.update_job_in_transaction(job_id, apply)

    def complete(self, job_id: str, now: datetime | None = None) -> GenerationJob:
        return self.transition(job_id, JobStatus.COMPLETED, now=now)

    @contextmanager
    def tracking(self, job_id: str) -> Iterator[None]:
        """Bind reports to one run, even if a callback outlives a cancellation."""
        started_at = self._required(job_id).started_at
        last_detail: JobProgress | None = None
        last_write = float("-inf")
        active = True

        def report(detail: JobProgress) -> None:
            nonlocal last_detail, last_write
            if not active or detail == last_detail:
                return
            now = monotonic()
            if last_detail and (detail.phase, detail.total, detail.unit) == (
                last_detail.phase, last_detail.total, last_detail.unit,
            ):
                if detail.completed is not None and last_detail.completed is not None:
                    if detail.completed < last_detail.completed:
                        return
                    if now - last_write < 1 and detail.completed != detail.total:
                        return

            def apply(job: GenerationJob) -> GenerationJob:
                if job.status not in _STARTED_STATES or job.started_at != started_at:
                    return job
                return job.model_copy(update={"progress_detail": detail, "updated_at": utc_now()})

            self.database.update_job_in_transaction(job_id, apply)
            last_detail, last_write = detail, now

        try:
            with progress_scope(job_id, report):
                yield
        finally:
            active = False

    def fail(self, job_id: str, error: str, now: datetime | None = None) -> GenerationJob:
        return self.transition(job_id, JobStatus.FAILED, error=error, now=now)

    def cancel(self, job_id: str, now: datetime | None = None) -> GenerationJob:
        timestamp = now or utc_now()

        def apply(job: GenerationJob) -> GenerationJob:
            # Terminal jobs stay terminal; the check runs inside the
            # transaction so a racing completion cannot slip past it.
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
                raise InvalidJobTransition(f"cannot cancel terminal job in {job.status}")
            return GenerationJob.model_validate({
                **job.model_dump(),
                "status": JobStatus.CANCELED,
                "error": "canceled by user",
                "completed_at": timestamp,
                "updated_at": timestamp,
            })

        return self.database.update_job_in_transaction(job_id, apply)

    def retry(self, job_id: str, now: datetime | None = None) -> GenerationJob:
        with self._control_lock:
            if not self._accepting:
                raise InvalidJobTransition("application is shutting down; retries are not accepted")
            return self._retry(job_id, now)

    def _retry(self, job_id: str, now: datetime | None = None) -> GenerationJob:
        timestamp = now or utc_now()

        def apply(job: GenerationJob) -> GenerationJob:
            if job.status not in {JobStatus.FAILED, JobStatus.CANCELED}:
                raise InvalidJobTransition("only failed or canceled jobs can be retried")
            if job.attempt_count >= job.max_attempts:
                raise InvalidJobTransition("job has exhausted its configured attempts")
            return job.model_copy(update={
                "status": JobStatus.QUEUED, "progress": 0, "progress_detail": None,
                "error": None, "started_at": None, "completed_at": None, "updated_at": timestamp,
            })

        return self.database.update_job_in_transaction(job_id, apply)

    def recover_interrupted(self, now: datetime | None = None) -> list[GenerationJob]:
        """Fail all active jobs at startup; execution never survives a process restart.

        Requeueing here would strand jobs forever: nothing drains the persisted
        queue after startup (execution is driven by per-request background
        tasks). Failing them keeps the Job Monitor honest — the Retry endpoint
        re-executes top-level stages from already-completed ones.
        """
        timestamp = now or utc_now()
        recovered: list[GenerationJob] = []
        for job in self.database.list_jobs():
            if job.status not in _ACTIVE_STATES:
                continue
            update = {
                **job.model_dump(), "status": JobStatus.FAILED,
                "error": (
                    "backend restarted before this job finished; "
                    "retry to resume from completed stages"
                ),
                "completed_at": timestamp, "updated_at": timestamp,
            }
            recovered.append(self.database.save_job(GenerationJob.model_validate(update)))
        return recovered

    def _required(self, job_id: str) -> GenerationJob:
        job = self.database.get_job(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        return job
