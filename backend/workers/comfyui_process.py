"""On-demand startup for isolated ComfyUI services."""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import ParseResult, urlparse

import httpx

from backend.core import AppConfig
from backend.core.ports import RESERVED_EXTERNAL_PORTS, is_listening
from backend.models.errors import BackendError, BackendErrorCode


class ComfyUIWorkerSupervisor:
    """Wait for the required node and stop only the process started by Studio."""

    service_name: str
    required_node: str
    requires_executable = True
    port_variable: str | None = None

    def __init__(
        self,
        *,
        endpoint: str,
        start_script: Path,
        startup_timeout_seconds: float,
        log_path: Path,
        blocked_ports: frozenset[int] = frozenset(),
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
        self._blocked_ports = RESERVED_EXTERNAL_PORTS | blocked_ports
        self._popen_factory = popen_factory
        self._readiness_probe = readiness_probe or self._probe
        self._listening_probe = listening_probe
        self._sleep = sleep
        self._monotonic = monotonic
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()

    def ensure_running(self, *, before_start: Callable[[], None] | None = None) -> bool:
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
                    f"configured {self.service_name} ComfyUI; it was not stopped.",
                )

            if platform.system() == "Windows":
                raise BackendError(
                    BackendErrorCode.BACKEND_UNAVAILABLE,
                    f"Managed {self.service_name} startup uses Bash and is unavailable on native Windows. "
                    "Configure an already-running compatible service instead; core mock mode remains available.",
                )
            access_mode = os.X_OK if self.requires_executable else os.R_OK
            if not self.start_script.is_file() or not os.access(self.start_script, access_mode):
                raise BackendError(
                    BackendErrorCode.BACKEND_UNAVAILABLE,
                    f"Managed {self.service_name} startup requires the launcher at {self.start_script}. "
                    "Configure an already-running compatible service instead.",
                )

            if before_start is not None:
                before_start()
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            if self.port_variable is not None:
                environment[self.port_variable] = str(parsed.port)
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("ab") as log_handle:
                    process = self._popen_factory(
                        self._launch_command(parsed.hostname, parsed.port),
                        cwd=self.start_script.parent,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                    )
            except OSError as exc:
                raise BackendError(
                    BackendErrorCode.BACKEND_UNAVAILABLE,
                    f"Could not launch {self.service_name} at {self.endpoint}. See {self.log_path}.",
                    details=exc,
                ) from None
            self._process = process
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

    def _launch_command(self, host: str, port: int) -> list[str]:
        return [str(self.start_script)]

    def _validate_configuration(self) -> ParseResult:
        try:
            parsed = urlparse(self.endpoint)
            port = parsed.port
        except ValueError:
            parsed = urlparse("")
            port = None
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not port
            or parsed.path or parsed.params or parsed.query or parsed.fragment
            or parsed.username is not None
        ):
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"Managed {self.service_name} startup requires a loopback HTTP endpoint with a port.",
            )
        if port in self._blocked_ports:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"Managed {self.service_name} startup cannot claim externally owned port {port}.",
            )
        return parsed

    def _wait_until_ready(self, process: subprocess.Popen[bytes]) -> None:
        deadline = self._monotonic() + self.startup_timeout_seconds
        while self._monotonic() < deadline:
            code = process.poll()
            if code is not None:
                raise BackendError(
                    BackendErrorCode.BACKEND_UNAVAILABLE,
                    f"{self.service_name} service at {self.endpoint} exited during startup with code {code}. "
                    f"See {self.log_path}.",
                )
            if self._readiness_probe(self.endpoint):
                return
            self._sleep(0.1)
        raise BackendError(
            BackendErrorCode.REQUEST_TIMEOUT,
            f"Timed out waiting for {self.service_name} ComfyUI at {self.endpoint}. See {self.log_path}.",
            retryable=True,
        )

    def _stop_owned(self) -> bool:
        process = self._process
        self._process = None
        if process is None:
            return False
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return True

    def _probe(self, endpoint: str) -> bool:
        """Verify that the service supports this workflow's required node."""
        try:
            response = httpx.get(
                f"{endpoint}/object_info/{self.required_node}",
                timeout=0.75,
                trust_env=False,
            )
            if response.status_code >= 400:
                return False
            body = response.json()
            return isinstance(body, dict) and self.required_node in body
        except (httpx.HTTPError, ValueError):
            return False


class QwenImage21WorkerSupervisor(ComfyUIWorkerSupervisor):
    service_name = "Qwen-Image-2.1"
    required_node = "TextEncodeQwenImage21"
    requires_executable = False

    @classmethod
    def from_config(cls, config: AppConfig) -> QwenImage21WorkerSupervisor:
        item = config.backends.qwen_image_21
        assert item.source_path is not None and item.endpoint is not None
        shared_endpoint = config.backends.comfyui.endpoint
        shared_port = urlparse(shared_endpoint).port if shared_endpoint else None
        blocked_ports = {8188, config.ports.comfyui}
        if shared_port is not None:
            blocked_ports.add(shared_port)
        return cls(
            endpoint=item.endpoint,
            start_script=(item.source_path / "start.sh").absolute(),
            startup_timeout_seconds=item.startup_timeout_seconds,
            log_path=config.paths.app_data / "logs" / "qwen-image-2.1.log",
            blocked_ports=frozenset(blocked_ports),
        )

    def _launch_command(self, host: str, port: int) -> list[str]:
        address = "127.0.0.1" if host == "localhost" else host
        return ["bash", str(self.start_script), "--listen", address, "--port", str(port)]
