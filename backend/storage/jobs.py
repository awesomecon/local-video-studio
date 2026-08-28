"""Persistent, restartable job queue with explicit state transitions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.schemas import GenerationJob, JobStatus, utc_now

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
    def __init__(self, database: StudioDatabase):
        self.database = database

    def enqueue(self, job: GenerationJob) -> GenerationJob:
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
        job = self._required(job_id)
        if job.status not in {JobStatus.FAILED, JobStatus.CANCELED}:
            raise InvalidJobTransition("only failed or canceled jobs can be retried")
        if job.attempt_count >= job.max_attempts:
            raise InvalidJobTransition("job has exhausted its configured attempts")
        timestamp = now or utc_now()
        retried = GenerationJob.model_validate({
            **job.model_dump(), "status": JobStatus.QUEUED, "progress": 0,
            "error": None, "started_at": None, "completed_at": None, "updated_at": timestamp,
        })
        return self.database.save_job(retried)

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
