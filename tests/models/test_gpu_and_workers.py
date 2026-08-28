from __future__ import annotations

import threading
import time

import httpx
import pytest

from backend.models import ComfyUIBackend
from backend.models.base import BackendDescriptor, Capability, GenerationRequest
from backend.models.errors import BackendError, BackendErrorCode
from backend.models.mock import MockGeneratorBackend
from backend.workers.gpu import GPUResourceManager, GPUSnapshot
from backend.workers.serial import JobStatus, SerialWorkerQueue


class HeavyFake(MockGeneratorBackend):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.loads = 0
        self.unloads = 0

    def descriptor(self):
        return BackendDescriptor(
            backend_name=self.name,
            model_name="fake",
            device="cuda",
            vram_required_gb=10,
            capabilities=frozenset({Capability.TEXT_TO_IMAGE}),
            heavyweight=True,
        )

    def load(self):
        self.loads += 1

    def unload(self):
        self.unloads += 1

    def estimate_resources(self, request):
        return {"estimated_vram_gb": 10}


def snapshot(free=22.0):
    return (GPUSnapshot(0, "RTX 4090", 23.5, 23.5 - free, free, time.time()),)


def test_gpu_manager_unloads_actual_previous_backend():
    cleanup = []
    manager = GPUResourceManager(
        minimum_free_vram_gb=10,
        snapshot_provider=lambda: snapshot(),
        cache_cleanup=lambda: cleanup.append(True),
    )
    first, second = HeavyFake("first"), HeavyFake("second")
    with manager.acquire(first):
        assert manager.active_backend_name == "first"
    with manager.acquire(second):
        assert first.unloads == 1
        assert manager.active_backend_name == "second"
    manager.unload_active()
    assert second.unloads == 1
    assert len(cleanup) == 2


def test_gpu_manager_reuses_resident_backend_when_free_vram_is_low():
    snapshots = iter((snapshot(22), snapshot(3)))
    manager = GPUResourceManager(
        minimum_free_vram_gb=20,
        snapshot_provider=lambda: next(snapshots),
        cache_cleanup=lambda: None,
    )
    backend = HeavyFake("resident")

    with manager.acquire(backend):
        assert backend.loads == 1
    with manager.acquire(backend) as second_snapshot:
        assert second_snapshot[0].free_gb == 3
        assert backend.loads == 1
        assert backend.unloads == 0

    manager.unload_active()
    assert backend.unloads == 1


def test_gpu_manager_reports_external_vram_pressure():
    manager = GPUResourceManager(
        minimum_free_vram_gb=20,
        snapshot_provider=lambda: snapshot(3),
        cache_cleanup=lambda: None,
    )
    with pytest.raises(BackendError) as raised:
        with manager.acquire(HeavyFake("heavy")):
            pass
    assert raised.value.code == BackendErrorCode.INSUFFICIENT_VRAM
    assert "did not stop" in str(raised.value)


def test_serial_worker_completes_mock_job(tmp_path):
    backend = MockGeneratorBackend()
    manager = GPUResourceManager(snapshot_provider=lambda: snapshot())
    worker = SerialWorkerQueue({"mock": backend}, manager)
    worker.start()
    worker.submit(
        "mock",
        GenerationRequest(
            job_id="queued",
            output_dir=tmp_path,
            prompt="hello",
            settings={"kind": "image"},
        ),
    )
    deadline = time.monotonic() + 5
    while worker.get("queued").status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    worker.stop()
    assert worker.get("queued").status == JobStatus.COMPLETED


def test_serial_worker_can_retry_a_canceled_pending_job(tmp_path):
    backend = MockGeneratorBackend()
    manager = GPUResourceManager(snapshot_provider=lambda: snapshot())
    worker = SerialWorkerQueue({"mock": backend}, manager)
    worker.submit(
        "mock",
        GenerationRequest(
            job_id="retryable",
            output_dir=tmp_path,
            prompt="hello",
            settings={"kind": "image"},
        ),
    )
    assert worker.cancel("retryable")
    assert backend._canceled == set()
    worker.retry("retryable")
    worker.start()
    deadline = time.monotonic() + 5
    while worker.get("retryable").status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    worker.stop()
    assert worker.get("retryable").status == JobStatus.COMPLETED


def _wait_for_terminal(worker: SerialWorkerQueue, job_id: str) -> None:
    deadline = time.monotonic() + 5
    while worker.get(job_id).status not in {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    }:
        assert time.monotonic() < deadline
        time.sleep(0.01)


class UninterruptibleBackend(MockGeneratorBackend):
    """A backend whose in-flight work cannot be interrupted (cancel reports failure)."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def cancel(self, job_id: str) -> bool:
        return False

    def _generate(self, request: GenerationRequest) -> GenerationResult:
        self.started.set()
        assert self.release.wait(timeout=5)
        return super()._generate(request)


def test_cancel_during_generation_requires_backend_acknowledgment(tmp_path):
    backend = UninterruptibleBackend()
    manager = GPUResourceManager(snapshot_provider=lambda: snapshot())
    worker = SerialWorkerQueue({"mock": backend}, manager)
    worker.start()
    worker.submit(
        "mock",
        GenerationRequest(
            job_id="running",
            output_dir=tmp_path,
            prompt="hello",
            settings={"kind": "image"},
        ),
    )
    deadline = time.monotonic() + 5
    while not backend.started.is_set():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert worker.cancel("running") is False
    assert worker.get("running").status is JobStatus.GENERATING
    assert backend._canceled == set()
    backend.release.set()
    _wait_for_terminal(worker, "running")
    worker.stop()
    assert worker.get("running").status is JobStatus.COMPLETED


def test_serial_worker_survives_unregistered_backend(tmp_path):
    backend = MockGeneratorBackend()
    manager = GPUResourceManager(snapshot_provider=lambda: snapshot())
    worker = SerialWorkerQueue({"mock": backend}, manager)
    worker.start()
    worker.submit(
        "missing",
        GenerationRequest(
            job_id="orphan",
            output_dir=tmp_path,
            prompt="hello",
            settings={"kind": "image"},
        ),
    )
    worker.submit(
        "mock",
        GenerationRequest(
            job_id="after",
            output_dir=tmp_path,
            prompt="hello",
            settings={"kind": "image"},
        ),
    )
    _wait_for_terminal(worker, "orphan")
    _wait_for_terminal(worker, "after")
    worker.stop()
    failed = worker.get("orphan")
    assert failed.status == JobStatus.FAILED
    assert failed.error is not None
    assert failed.error["code"] == BackendErrorCode.BACKEND_UNAVAILABLE.value
    assert worker.get("after").status == JobStatus.COMPLETED


def _comfy_worker_backend():
    state = {"submitted": 0, "interrupted": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "test"}})
        if request.url.path == "/free":
            return httpx.Response(200)
        if request.url.path == "/prompt":
            state["submitted"] += 1
            return httpx.Response(200, json={"prompt_id": f"p{state['submitted']}"})
        if request.url.path == "/history/p1":
            # A canceled prompt is removed from the queue and never reaches history.
            return httpx.Response(200, json={})
        if request.url.path == "/history/p2":
            return httpx.Response(
                200,
                json={
                    "p2": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "out.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/queue" and request.method == "GET":
            running = [] if state["interrupted"] else [[1, "p1", {}]]
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        if request.url.path == "/interrupt":
            state["interrupted"] = True
            return httpx.Response(200)
        if request.url.path == "/view":
            return httpx.Response(200, content=b"fake-png")
        raise AssertionError(request.url)

    return ComfyUIBackend(
        workflow={"1": {"inputs": {"text": "{{prompt}}", "seed": "{{seed}}"}}},
        poll_interval=0.01,
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )


def test_canceling_comfyui_job_unblocks_serial_queue_for_next_job(tmp_path):
    comfy = _comfy_worker_backend()
    manager = GPUResourceManager(snapshot_provider=lambda: snapshot())
    worker = SerialWorkerQueue({"comfyui": comfy}, manager)
    worker.start()
    worker.submit(
        "comfyui",
        GenerationRequest(job_id="stalled", output_dir=tmp_path / "a", prompt="hello"),
    )
    worker.submit(
        "comfyui",
        GenerationRequest(job_id="next", output_dir=tmp_path / "b", prompt="hello"),
    )
    deadline = time.monotonic() + 5
    while worker.get("stalled").status != JobStatus.GENERATING:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert worker.cancel("stalled")
    _wait_for_terminal(worker, "stalled")
    _wait_for_terminal(worker, "next")
    worker.stop()
    canceled = worker.get("stalled")
    assert canceled.status == JobStatus.CANCELED
    assert canceled.error is not None
    assert canceled.error["code"] == BackendErrorCode.CANCELED.value
    assert worker.get("next").status == JobStatus.COMPLETED
    assert comfy._active == set()
    assert comfy._canceled == set()
