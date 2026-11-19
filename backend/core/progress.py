"""Progress reporting for the job currently running on this execution path."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from backend.schemas import JobProgress

ProgressReporter = Callable[[JobProgress], None]
_active: ContextVar[tuple[str, ProgressReporter] | None] = ContextVar("job_progress", default=None)


@contextmanager
def progress_scope(job_id: str, reporter: ProgressReporter) -> Iterator[None]:
    token = _active.set((job_id, reporter))
    try:
        yield
    finally:
        _active.reset(token)


def current_progress_job() -> str | None:
    active = _active.get()
    return active[0] if active else None


def current_progress_reporter() -> ProgressReporter | None:
    active = _active.get()
    return active[1] if active else None


def report_progress(
    phase: str, *, completed: float | None = None, total: float | None = None,
    unit: str | None = None,
) -> None:
    reporter = current_progress_reporter()
    if reporter is not None:
        reporter(JobProgress(phase=phase, completed=completed, total=total, unit=unit))
