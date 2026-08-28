"""System-wide GPU memory accounting and serialized heavyweight leases."""

from __future__ import annotations

import gc
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator

from backend.models.base import GenerationRequest, GeneratorBackend
from backend.models.errors import BackendError, BackendErrorCode


@dataclass(frozen=True, slots=True)
class GPUSnapshot:
    index: int
    name: str
    total_gb: float
    used_gb: float
    free_gb: float
    captured_at: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MappingSnapshot:
    devices: tuple[GPUSnapshot, ...]
    active_backend: str | None
    minimum_free_vram_gb: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "devices": [device.as_dict() for device in self.devices],
            "active_backend": self.active_backend,
            "minimum_free_vram_gb": self.minimum_free_vram_gb,
        }


def query_nvidia_smi() -> tuple[GPUSnapshot, ...]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,memory.used,memory.free",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise BackendError(
            BackendErrorCode.BACKEND_UNAVAILABLE,
            "System-wide GPU memory cannot be inspected because nvidia-smi is unavailable.",
            details=exc,
        ) from None
    if completed.returncode:
        raise BackendError(
            BackendErrorCode.BACKEND_UNAVAILABLE,
            "nvidia-smi could not inspect system-wide GPU memory.",
            details=completed.stderr[-1000:],
        )
    captured = time.time()
    snapshots: list[GPUSnapshot] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            index, name = int(parts[0]), parts[1]
            total, used, free = (float(value) / 1024 for value in parts[2:])
        except ValueError:
            continue
        snapshots.append(GPUSnapshot(index, name, total, used, free, captured))
    if not snapshots:
        raise BackendError(
            BackendErrorCode.BACKEND_UNAVAILABLE,
            "nvidia-smi did not return parseable GPU memory information.",
        )
    return tuple(snapshots)


class GPUResourceManager:
    """Serializes heavy jobs and owns only lifecycle calls on registered backends."""

    def __init__(
        self,
        minimum_free_vram_gb: float = 20.0,
        *,
        wait_for_vram: bool = False,
        poll_interval: float = 5.0,
        wait_timeout: float | None = None,
        snapshot_provider: Callable[[], tuple[GPUSnapshot, ...]] = query_nvidia_smi,
        cache_cleanup: Callable[[], None] | None = None,
    ) -> None:
        self.minimum_free_vram_gb = minimum_free_vram_gb
        self.wait_for_vram = wait_for_vram
        self.poll_interval = poll_interval
        self.wait_timeout = wait_timeout
        self._snapshot_provider = snapshot_provider
        self._cache_cleanup = cache_cleanup or self._default_cache_cleanup
        self._heavy_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._active_backend: GeneratorBackend | None = None

    @property
    def active_backend_name(self) -> str | None:
        with self._state_lock:
            return (
                self._active_backend.descriptor().backend_name if self._active_backend else None
            )

    def snapshot(self) -> MappingSnapshot:
        return MappingSnapshot(
            devices=self._snapshot_provider(),
            active_backend=self.active_backend_name,
            minimum_free_vram_gb=self.minimum_free_vram_gb,
        )

    def _wait_until_available(self, required_gb: float) -> tuple[GPUSnapshot, ...]:
        started = time.monotonic()
        while True:
            devices = self._snapshot_provider()
            if max(device.free_gb for device in devices) >= required_gb:
                return devices
            if not self.wait_for_vram:
                used = max(device.used_gb for device in devices)
                raise BackendError(
                    BackendErrorCode.INSUFFICIENT_VRAM,
                    "Heavy generation requires additional free VRAM. An external process such as "
                    "the local LLM server may be using GPU memory; Local Video Studio did not "
                    "stop it.",
                    retryable=True,
                    details=f"required={required_gb:.2f} GiB, highest used={used:.2f} GiB",
                )
            if self.wait_timeout is not None and time.monotonic() - started >= self.wait_timeout:
                raise BackendError(
                    BackendErrorCode.INSUFFICIENT_VRAM,
                    "Timed out waiting for enough system-wide free VRAM.",
                    retryable=True,
                )
            time.sleep(self.poll_interval)

    @contextmanager
    def acquire(
        self,
        backend: GeneratorBackend,
        request: GenerationRequest | None = None,
    ) -> Iterator[tuple[GPUSnapshot, ...]]:
        descriptor = backend.descriptor()
        if not descriptor.heavyweight:
            backend.load()
            try:
                yield ()
            finally:
                backend.unload()
            return
        with self._heavy_lock:
            current = self._active_backend
            if current is not None and current is not backend:
                self._unload_backend(current)
            if current is backend:
                # The resident model already accounts for much of the reported used VRAM.
                # Preserve system-wide visibility without applying a second pre-load gate.
                snapshots = self._snapshot_provider()
            else:
                estimate = backend.estimate_resources(request) if request is not None else {}
                requested = float(
                    estimate.get("estimated_vram_gb", descriptor.vram_required_gb)
                )
                snapshots = self._wait_until_available(
                    max(self.minimum_free_vram_gb, requested)
                )
                backend.load()
                with self._state_lock:
                    self._active_backend = backend
            try:
                yield snapshots
            except Exception:
                self._unload_backend(backend)
                raise

    def release(self, *, unload: bool = False) -> None:
        if not unload:
            return
        with self._heavy_lock:
            current = self._active_backend
            if current is not None:
                self._unload_backend(current)

    def unload_active(self) -> None:
        self.release(unload=True)

    def _unload_backend(self, backend: GeneratorBackend) -> None:
        try:
            backend.unload()
        finally:
            with self._state_lock:
                if self._active_backend is backend:
                    self._active_backend = None
            # empty_cache only follows actual model reference release by unload().
            self._cache_cleanup()

    @staticmethod
    def _default_cache_cleanup() -> None:
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except (ImportError, RuntimeError):
            return
