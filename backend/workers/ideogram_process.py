"""Ownership-aware startup for the isolated Ideogram 4 ComfyUI service."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from backend.core import AppConfig
from backend.core.ports import RESERVED_EXTERNAL_PORTS, is_listening
from backend.models.errors import BackendError, BackendErrorCode


class IdeogramWorkerSupervisor:
    """Start Ideogram on demand and stop only the process started by this app."""

    def __init__(
        self,
        *,
        endpoint: str,
        start_script: Path,
        startup_timeout_seconds: float,
        log_path: Path,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        readiness_probe: Callable[[str], bool] | None = None,
        listening_probe: Callable[[str, int], bool] = is_listening,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.start_script = start_script
        self.startup_timeout_seconds = startup_timeout_seconds
        self.log_path = log_path
        self._popen_factory = popen_factory
        self._readiness_probe = readiness_probe or self._probe
        self._listening_probe = listening_probe
        self._sleep = sleep
        self._monotonic = monotonic
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any | None = None
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: AppConfig) -> IdeogramWorkerSupervisor:
        item = config.backends.ideogram4_local
        repo_root = Path(__file__).resolve().parents[2]
        return cls(
            endpoint=item.endpoint or "http://127.0.0.1:8190",
            start_script=repo_root / "scripts" / "start_ideogram4.sh",
            startup_timeout_seconds=item.startup_timeout_seconds,
            log_path=config.paths.app_data / "logs" / "ideogram4.log",
        )

    def ensure_running(self) -> bool:
        """Ensure the correct service is ready; return True only when we started it."""
        with self._lock:
            parsed = self._validate_configuration()
            process = self._process
            if process is not None and process.poll() is None:
                self._wait_until_ready(process)
                return False
            if process is not None:
                self._stop_owned()
            if self._readiness_probe(self.endpoint):
                return False
            assert parsed.hostname is not None and parsed.port is not None
            if self._listening_probe(parsed.hostname, parsed.port):
                raise BackendError(
                    BackendErrorCode.UNEXPECTED_SERVICE,
                    f"Port {parsed.port} is occupied by a service that is not the "
                    "configured Ideogram 4 ComfyUI; it was not stopped.",
                )

            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = self.log_path.open("ab")
            environment = os.environ.copy()
            environment.update({
                "LVS_IDEOGRAM_PORT": str(parsed.port),
                "PYTHONUNBUFFERED": "1",
            })
            try:
                process = self._popen_factory(
                    [str(self.start_script)],
                    cwd=self.start_script.parents[1],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
            except Exception:
                log_handle.close()
                raise
            self._process = process
            self._log_handle = log_handle
            try:
                self._wait_until_ready(process)
            except Exception:
                self._stop_owned()
                raise
            return True

    def stop(self) -> bool:
        with self._lock:
            return self._stop_owned()

    def is_owned(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def _validate_configuration(self):
        parsed = urlparse(self.endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
        ):
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "Managed Ideogram startup requires a loopback HTTP endpoint with a port.",
            )
        if parsed.port in RESERVED_EXTERNAL_PORTS:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"Managed Ideogram startup cannot claim externally owned port {parsed.port}.",
            )
        if not self.start_script.is_file() or not os.access(self.start_script, os.X_OK):
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"Ideogram startup script is unavailable: {self.start_script}",
            )
        return parsed

    def _wait_until_ready(self, process: subprocess.Popen[bytes]) -> None:
        deadline = self._monotonic() + self.startup_timeout_seconds
        while self._monotonic() < deadline:
            code = process.poll()
            if code is not None:
                raise BackendError(
                    BackendErrorCode.BACKEND_UNAVAILABLE,
                    f"Ideogram service exited during startup with code {code}. "
                    f"See {self.log_path}.",
                )
            if self._readiness_probe(self.endpoint):
                return
            self._sleep(0.1)
        raise BackendError(
            BackendErrorCode.REQUEST_TIMEOUT,
            f"Timed out starting Ideogram 4. See {self.log_path}.",
            retryable=True,
        )

    def _stop_owned(self) -> bool:
        process = self._process
        log_handle = self._log_handle
        self._process = None
        self._log_handle = None
        if process is None:
            return False
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            if log_handle is not None:
                log_handle.close()
        return True

    @staticmethod
    def _probe(endpoint: str) -> bool:
        """Require the official Ideogram loader node, not merely any ComfyUI."""
        try:
            response = httpx.get(
                f"{endpoint.rstrip('/')}/object_info/Ideogram4PipelineLoader",
                timeout=0.75,
            )
            if response.status_code >= 400:
                return False
            body = response.json()
            return isinstance(body, dict) and "Ideogram4PipelineLoader" in body
        except (httpx.HTTPError, ValueError):
            return False
