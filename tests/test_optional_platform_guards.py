"""Simulated policy checks; these do not qualify native model runtimes."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.models.errors import BackendError
from backend.workers.ideogram_process import IdeogramWorkerSupervisor


def supervisor(tmp_path: Path, ready: bool) -> IdeogramWorkerSupervisor:
    return IdeogramWorkerSupervisor(
        endpoint="http://127.0.0.1:8190", start_script=tmp_path / "missing.sh",
        startup_timeout_seconds=1, log_path=tmp_path / "log",
        readiness_probe=lambda _: ready, listening_probe=lambda *_: False,
        popen_factory=lambda *a, **k: pytest.fail("Must not start a process"),
    )


def test_existing_service_does_not_require_local_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workers.ideogram_process.platform", SimpleNamespace(system=lambda: "Windows"))
    assert supervisor(tmp_path, True).ensure_running() is False


def test_native_windows_managed_script_has_clear_restriction(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workers.ideogram_process.platform", SimpleNamespace(system=lambda: "Windows"))
    with pytest.raises(BackendError, match="native Windows"):
        supervisor(tmp_path, False).ensure_running()
