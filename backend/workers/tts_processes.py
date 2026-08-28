"""Ownership-aware on-demand process supervisor for isolated TTS workers."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlparse

import httpx

from backend.core import AppConfig
from backend.core.ports import RESERVED_EXTERNAL_PORTS
from backend.models.errors import BackendError, BackendErrorCode


def _endpoint_port(endpoint: str) -> int | None:
    try:
        return urlparse(endpoint).port
    except ValueError:
        return None


def _reject_shared_spec_ports(specs: Mapping[str, TTSWorkerSpec]) -> None:
    """Two managed providers must never target the same loopback port."""
    owners: dict[int, str] = {}
    for provider, spec in specs.items():
        port = _endpoint_port(spec.endpoint)
        if port is None:
            continue
        other = owners.get(port)
        if other is not None:
            raise ValueError(
                f"Managed TTS workers {other} and {provider} both claim port {port}; "
                "each provider requires its own loopback port."
            )
        owners[port] = provider


@dataclass(frozen=True, slots=True)
class TTSWorkerSpec:
    provider: str
    endpoint: str
    python_path: Path
    model_path: Path
    tokenizer_path: Path | None
    startup_timeout_seconds: float


@dataclass(slots=True)
class _OwnedProcess:
    process: subprocess.Popen[bytes]
    log_handle: Any


class TTSWorkerSupervisor:
    """Starts only configured workers and stops only processes it owns."""

    def __init__(
        self,
        specs: Mapping[str, TTSWorkerSpec],
        *,
        output_root: Path,
        cache_root: Path,
        log_root: Path,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        health_probe: Callable[[str], Mapping[str, Any] | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.specs = dict(specs)
        _reject_shared_spec_ports(self.specs)
        self.output_root = output_root.expanduser()
        self.cache_root = cache_root.expanduser()
        self.log_root = log_root.expanduser()
        self._popen_factory = popen_factory
        self._health_probe = health_probe or self._probe
        self._sleep = sleep
        self._monotonic = monotonic
        self._owned: dict[str, _OwnedProcess] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: AppConfig, *, output_root: Path) -> TTSWorkerSupervisor:
        specs: dict[str, TTSWorkerSpec] = {}
        for provider in ("qwen_tts", "step_audio_editx", "chatterbox", "omnivoice", "breeze_tts_2"):
            item = getattr(config.backends, provider)
            if not item.enabled or not item.managed:
                continue
            assert item.endpoint and item.python_path and item.model_path
            specs[provider] = TTSWorkerSpec(
                provider=provider,
                endpoint=item.endpoint,
                python_path=item.python_path,
                model_path=item.model_path,
                tokenizer_path=item.tokenizer_path,
                startup_timeout_seconds=item.startup_timeout_seconds,
            )
        return cls(
            specs,
            output_root=output_root,
            cache_root=config.paths.cache_root,
            log_root=config.paths.app_data / "logs",
        )

    @contextmanager
    def running(self, provider: str) -> Iterator[None]:
        started = self.ensure_running(provider)
        try:
            yield
        finally:
            if started:
                self.stop(provider)

    def ensure_running(self, provider: str) -> bool:
        """Return True only when this call started a process owned by the app."""
        with self._lock:
            spec = self._required_spec(provider)
            existing = self._health_probe(spec.endpoint)
            if existing is not None:
                if existing.get("provider") != provider:
                    raise BackendError(
                        BackendErrorCode.UNEXPECTED_SERVICE,
                        f"Port for {provider} is occupied by a different service; it was not stopped.",
                    )
                return False
            owned = self._owned.get(provider)
            if owned is not None and owned.process.poll() is None:
                self._wait_until_healthy(spec, owned.process)
                return False
            self._validate_spec(spec)
            self.output_root.mkdir(parents=True, exist_ok=True)
            self.log_root.mkdir(parents=True, exist_ok=True)
            log_handle = (self.log_root / f"tts-{provider}.log").open("ab")
            command = self._command(spec)
            environment = os.environ.copy()
            environment.update({
                "LVS_AI_CACHE_ROOT": str(self.cache_root),
                "PYTHONUNBUFFERED": "1",
            })
            try:
                process = self._popen_factory(
                    command,
                    cwd=Path(__file__).resolve().parents[2],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
            except Exception:
                log_handle.close()
                raise
            self._owned[provider] = _OwnedProcess(process=process, log_handle=log_handle)
            try:
                self._wait_until_healthy(spec, process)
            except Exception:
                self._stop_owned(provider)
                raise
            return True

    def ensure_running_if_managed(self, provider: str) -> bool:
        """Start a configured managed worker, or leave external backends alone."""
        if provider not in self.specs:
            return False
        return self.ensure_running(provider)

    def stop(self, provider: str) -> bool:
        with self._lock:
            return self._stop_owned(provider)

    def stop_all(self) -> None:
        with self._lock:
            for provider in tuple(self._owned):
                self._stop_owned(provider)

    def is_owned(self, provider: str) -> bool:
        with self._lock:
            item = self._owned.get(provider)
            return item is not None and item.process.poll() is None

    def _required_spec(self, provider: str) -> TTSWorkerSpec:
        try:
            return self.specs[provider]
        except KeyError:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"{provider} is not configured for automatic worker startup.",
            ) from None

    @staticmethod
    def _validate_spec(spec: TTSWorkerSpec) -> None:
        parsed = urlparse(spec.endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "Managed TTS workers require a loopback HTTP endpoint.",
            )
        port = _endpoint_port(spec.endpoint)
        if port is not None and port in RESERVED_EXTERNAL_PORTS:
            # RESERVED_EXTERNAL_PORTS is externally owned (1234 belongs to the
            # user's local LLM); the supervisor must never spawn onto it.
            raise ValueError(
                f"Managed TTS worker for {spec.provider} targets port {port}, which is "
                "in RESERVED_EXTERNAL_PORTS; reserved external ports are never claimed."
            )
        if not spec.python_path.is_file() or not os.access(spec.python_path, os.X_OK):
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"Isolated Python is unavailable for {spec.provider}: {spec.python_path}",
            )
        if not spec.model_path.is_dir():
            raise BackendError(
                BackendErrorCode.MODEL_UNAVAILABLE,
                f"Model directory is unavailable for {spec.provider}: {spec.model_path}",
            )
        if spec.provider == "step_audio_editx" and (
            spec.tokenizer_path is None or not spec.tokenizer_path.is_dir()
        ):
            raise BackendError(
                BackendErrorCode.MODEL_UNAVAILABLE,
                "Step-Audio-Tokenizer directory is unavailable.",
            )

    def _command(self, spec: TTSWorkerSpec) -> list[str]:
        port = urlparse(spec.endpoint).port
        if port is None:
            raise BackendError(BackendErrorCode.BACKEND_UNAVAILABLE, "TTS endpoint needs a port.")
        command = [
            str(spec.python_path), "-m", "services.tts_worker.app",
            "--provider", spec.provider,
            "--model-path", str(spec.model_path),
            "--output-root", str(self.output_root),
            "--host", "127.0.0.1", "--port", str(port),
        ]
        if spec.tokenizer_path is not None:
            command.extend(("--tokenizer-path", str(spec.tokenizer_path)))
        return command

    def _wait_until_healthy(
        self, spec: TTSWorkerSpec, process: subprocess.Popen[bytes],
    ) -> None:
        deadline = self._monotonic() + spec.startup_timeout_seconds
        while self._monotonic() < deadline:
            code = process.poll()
            if code is not None:
                raise BackendError(
                    BackendErrorCode.BACKEND_UNAVAILABLE,
                    f"{spec.provider} worker exited during startup with code {code}. "
                    f"See {self.log_root / f'tts-{spec.provider}.log'}.",
                )
            health = self._health_probe(spec.endpoint)
            if health is not None:
                if health.get("provider") != spec.provider:
                    raise BackendError(
                        BackendErrorCode.UNEXPECTED_SERVICE,
                        f"Unexpected service responded on the {spec.provider} endpoint.",
                    )
                return
            self._sleep(0.1)
        raise BackendError(
            BackendErrorCode.REQUEST_TIMEOUT,
            f"Timed out starting {spec.provider}. See {self.log_root / f'tts-{spec.provider}.log'}.",
            retryable=True,
        )

    def _stop_owned(self, provider: str) -> bool:
        item = self._owned.pop(provider, None)
        if item is None:
            return False
        try:
            if item.process.poll() is None:
                item.process.terminate()
                try:
                    item.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    item.process.kill()
                    item.process.wait(timeout=5)
        finally:
            item.log_handle.close()
        return True

    @staticmethod
    def _probe(endpoint: str) -> Mapping[str, Any] | None:
        try:
            response = httpx.get(f"{endpoint.rstrip('/')}/health", timeout=0.5)
            if response.status_code >= 400:
                return None
            body = response.json()
            return body if isinstance(body, dict) else None
        except (httpx.HTTPError, ValueError):
            return None
