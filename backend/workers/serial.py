"""Lightweight single-consumer queue for serialized generation jobs."""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from backend.models.base import GenerationRequest, GenerationResult, GeneratorBackend
from backend.models.errors import BackendError, BackendErrorCode, redact_secrets

from .gpu import GPUResourceManager

logger = logging.getLogger(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    LOADING_MODEL = "loading_model"
    GENERATING = "generating"
    POSTPROCESSING = "postprocessing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    backend_name: str
    request: GenerationRequest
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    result: GenerationResult | None = None
    error: Mapping[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


StateCallback = Callable[[JobRecord], None]


class SerialWorkerQueue:
    """Serial executor; persistence is supplied by the storage-owned state callback."""

    def __init__(
        self,
        registry: Mapping[str, GeneratorBackend] | Any,
        gpu_manager: GPUResourceManager,
        *,
        state_callback: StateCallback | None = None,
    ) -> None:
        self.registry = registry
        self.gpu_manager = gpu_manager
        self.state_callback = state_callback
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._records: dict[str, JobRecord] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="lvs-serial-worker", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread:
            thread.join(timeout=timeout)

    def submit(
        self,
        backend_name: str,
        request: GenerationRequest,
        *,
        job_id: str | None = None,
    ) -> JobRecord:
        identifier = job_id or request.job_id or str(uuid.uuid4())
        record = JobRecord(id=identifier, backend_name=backend_name, request=request)
        with self._lock:
            if identifier in self._records:
                raise ValueError(f"Job {identifier!r} already exists")
            self._records[identifier] = record
        self._emit(record)
        self._queue.put(identifier)
        return record

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._records[job_id]

    def list(self) -> tuple[JobRecord, ...]:
        with self._lock:
            return tuple(sorted(self._records.values(), key=lambda record: record.created_at))

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.status in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELED,
            }:
                return False
            generating = record.status is JobStatus.GENERATING
        acknowledged = self._backend(record.backend_name).cancel(job_id)
        if generating and not acknowledged:
            # The backend could not interrupt the running work; reporting a
            # canceled job here would hide generation that is still executing.
            return False
        self._update(job_id, status=JobStatus.CANCELED, progress=record.progress)
        return True

    def retry(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self._records[job_id]
            if record.status not in {JobStatus.FAILED, JobStatus.CANCELED}:
                raise ValueError("Only failed or canceled jobs may be retried")
            retried = replace(
                record,
                status=JobStatus.QUEUED,
                progress=0.0,
                result=None,
                error=None,
                updated_at=time.time(),
            )
            self._records[job_id] = retried
        reset_cancel = getattr(self._backend(record.backend_name), "reset_cancel", None)
        if callable(reset_cancel):
            reset_cancel(job_id)
        self._emit(retried)
        self._queue.put(job_id)
        return retried

    def _backend(self, name: str) -> GeneratorBackend:
        getter = getattr(self.registry, "get", None)
        try:
            backend = getter(name) if callable(getter) else self.registry[name]
        except KeyError:
            backend = None
        if backend is None:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"Backend {name!r} is not registered",
            )
        return backend

    def _update(self, job_id: str, **changes: Any) -> JobRecord:
        with self._lock:
            current = self._records[job_id]
            updated = replace(current, updated_at=time.time(), **changes)
            self._records[job_id] = updated
        self._emit(updated)
        return updated

    def _emit(self, record: JobRecord) -> None:
        if self.state_callback:
            self.state_callback(record)

    def _run(self) -> None:
        while not self._stop.is_set():
            identifier = self._queue.get()
            if identifier is None:
                self._queue.task_done()
                break
            try:
                self._execute(identifier)
            except Exception as exc:
                logger.error(
                    "serial worker job %s crashed outside execution guard: %s",
                    identifier,
                    redact_secrets(exc),
                )
            finally:
                self._queue.task_done()

    def _execute(self, job_id: str) -> None:
        with self._lock:
            record = self._records.get(job_id)
        if record is None or record.status == JobStatus.CANCELED:
            return
        try:
            backend = self._backend(record.backend_name)
            self._update(job_id, status=JobStatus.PREPARING, progress=0.05)
            self._update(job_id, status=JobStatus.LOADING_MODEL, progress=0.1)
            with self.gpu_manager.acquire(backend, record.request):
                if self.get(job_id).status == JobStatus.CANCELED:
                    return
                self._update(job_id, status=JobStatus.GENERATING, progress=0.2)
                result = backend.generate(record.request)
                if self.get(job_id).status == JobStatus.CANCELED:
                    return
                self._update(job_id, status=JobStatus.POSTPROCESSING, progress=0.9)
            self._update(
                job_id,
                status=JobStatus.COMPLETED,
                progress=1.0,
                result=result,
                error=None,
            )
        except BackendError as exc:
            status = (
                JobStatus.CANCELED if exc.code == BackendErrorCode.CANCELED else JobStatus.FAILED
            )
            self._update(job_id, status=status, error=exc.as_dict())
        except Exception as exc:  # Sanitize at the worker/API persistence boundary.
            self._update(
                job_id,
                status=JobStatus.FAILED,
                error={
                    "code": BackendErrorCode.INVALID_RESPONSE.value,
                    "message": redact_secrets(exc),
                    "retryable": False,
                },
            )
