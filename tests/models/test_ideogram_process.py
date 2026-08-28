from __future__ import annotations

from pathlib import Path

import pytest

from backend.models.errors import BackendError, BackendErrorCode
from backend.workers.ideogram_process import IdeogramWorkerSupervisor


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


def _script(tmp_path: Path) -> Path:
    script = tmp_path / "start_ideogram4.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_starts_waits_and_stops_only_owned_service(tmp_path: Path) -> None:
    process = FakeProcess()
    calls = []
    probes = iter((False, False, True))

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    supervisor = IdeogramWorkerSupervisor(
        endpoint="http://127.0.0.1:8190",
        start_script=_script(tmp_path),
        startup_timeout_seconds=2,
        log_path=tmp_path / "logs" / "ideogram.log",
        popen_factory=popen,
        readiness_probe=lambda endpoint: next(probes),
        listening_probe=lambda host, port: False,
        sleep=lambda _: None,
    )

    assert supervisor.ensure_running() is True
    assert supervisor.is_owned() is True
    command, kwargs = calls[0]
    assert command == [str(tmp_path / "start_ideogram4.sh")]
    assert kwargs["env"]["LVS_IDEOGRAM_PORT"] == "8190"
    assert supervisor.stop() is True
    assert process.terminated is True
    assert supervisor.stop() is False


def test_reuses_compatible_unowned_service_without_stopping_it(tmp_path: Path) -> None:
    supervisor = IdeogramWorkerSupervisor(
        endpoint="http://127.0.0.1:8190",
        start_script=_script(tmp_path),
        startup_timeout_seconds=2,
        log_path=tmp_path / "ideogram.log",
        popen_factory=lambda *args, **kwargs: pytest.fail("must not spawn"),
        readiness_probe=lambda endpoint: True,
        listening_probe=lambda host, port: True,
    )
    assert supervisor.ensure_running() is False
    assert supervisor.stop() is False


def test_refuses_to_replace_unexpected_service_on_configured_port(tmp_path: Path) -> None:
    supervisor = IdeogramWorkerSupervisor(
        endpoint="http://127.0.0.1:8190",
        start_script=_script(tmp_path),
        startup_timeout_seconds=2,
        log_path=tmp_path / "ideogram.log",
        popen_factory=lambda *args, **kwargs: pytest.fail("must not spawn"),
        readiness_probe=lambda endpoint: False,
        listening_probe=lambda host, port: True,
    )
    with pytest.raises(BackendError) as raised:
        supervisor.ensure_running()
    assert raised.value.code is BackendErrorCode.UNEXPECTED_SERVICE
    assert supervisor.stop() is False


def test_never_claims_external_llm_port(tmp_path: Path) -> None:
    supervisor = IdeogramWorkerSupervisor(
        endpoint="http://127.0.0.1:1234",
        start_script=_script(tmp_path),
        startup_timeout_seconds=2,
        log_path=tmp_path / "ideogram.log",
        popen_factory=lambda *args, **kwargs: pytest.fail("must not spawn"),
    )
    with pytest.raises(BackendError, match="externally owned port 1234"):
        supervisor.ensure_running()
