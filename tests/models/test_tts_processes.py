from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from backend.models.errors import BackendError, BackendErrorCode
from backend.workers.tts_processes import TTSWorkerSpec, TTSWorkerSupervisor


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


def worker_spec(tmp_path: Path) -> TTSWorkerSpec:
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    model = tmp_path / "model"
    model.mkdir()
    return TTSWorkerSpec(
        provider="qwen_tts", endpoint="http://127.0.0.1:8191",
        python_path=python, model_path=model, tokenizer_path=None,
        startup_timeout_seconds=2,
    )


def test_supervisor_starts_waits_and_stops_only_owned_process(tmp_path: Path) -> None:
    process = FakeProcess()
    commands = []
    probes = iter((None, None, {"provider": "qwen_tts", "status": "healthy"}))

    def popen(command, **kwargs):
        commands.append((command, kwargs))
        return process

    supervisor = TTSWorkerSupervisor(
        {"qwen_tts": worker_spec(tmp_path)}, output_root=tmp_path / "projects",
        cache_root=tmp_path / "cache", log_root=tmp_path / "logs",
        popen_factory=popen, health_probe=lambda endpoint: next(probes), sleep=lambda _: None,
    )
    assert supervisor.ensure_running("qwen_tts") is True
    assert supervisor.is_owned("qwen_tts") is True
    command, kwargs = commands[0]
    assert command[:4] == [str(tmp_path / "python"), "-m", "services.tts_worker.app", "--provider"]
    assert command[-2:] == ["--port", "8191"]
    assert kwargs["stdin"] is not None
    assert supervisor.stop("qwen_tts") is True
    assert process.terminated is True
    assert supervisor.stop("qwen_tts") is False


def test_supervisor_never_stops_unowned_or_unexpected_service(tmp_path: Path) -> None:
    called = False

    def popen(*args, **kwargs):
        nonlocal called
        called = True
        return FakeProcess()

    supervisor = TTSWorkerSupervisor(
        {"qwen_tts": worker_spec(tmp_path)}, output_root=tmp_path,
        cache_root=tmp_path / "cache", log_root=tmp_path / "logs",
        popen_factory=popen,
        health_probe=lambda endpoint: {"provider": "different_service"},
    )
    with pytest.raises(BackendError) as raised:
        supervisor.ensure_running("qwen_tts")
    assert raised.value.code is BackendErrorCode.UNEXPECTED_SERVICE
    assert called is False
    assert supervisor.stop("qwen_tts") is False


def test_supervisor_leaves_already_running_matching_worker_alone(tmp_path: Path) -> None:
    supervisor = TTSWorkerSupervisor(
        {"qwen_tts": worker_spec(tmp_path)}, output_root=tmp_path,
        cache_root=tmp_path / "cache", log_root=tmp_path / "logs",
        popen_factory=lambda *args, **kwargs: pytest.fail("must not spawn"),
        health_probe=lambda endpoint: {"provider": "qwen_tts", "status": "healthy"},
    )
    with supervisor.running("qwen_tts"):
        assert supervisor.is_owned("qwen_tts") is False
    assert supervisor.stop("qwen_tts") is False


def test_optional_start_leaves_unmanaged_provider_to_its_external_backend(tmp_path: Path) -> None:
    supervisor = TTSWorkerSupervisor(
        {}, output_root=tmp_path, cache_root=tmp_path / "cache", log_root=tmp_path / "logs",
        popen_factory=lambda *args, **kwargs: pytest.fail("must not spawn"),
        health_probe=lambda endpoint: pytest.fail("must not probe"),
    )

    assert supervisor.ensure_running_if_managed("fish_s2_pro") is False


def test_optional_start_still_starts_configured_managed_provider(tmp_path: Path) -> None:
    process = FakeProcess()
    probes = iter((None, {"provider": "qwen_tts", "status": "healthy"}))
    supervisor = TTSWorkerSupervisor(
        {"qwen_tts": worker_spec(tmp_path)}, output_root=tmp_path,
        cache_root=tmp_path / "cache", log_root=tmp_path / "logs",
        popen_factory=lambda *args, **kwargs: process,
        health_probe=lambda endpoint: next(probes), sleep=lambda _: None,
    )

    assert supervisor.ensure_running_if_managed("qwen_tts") is True
    assert supervisor.is_owned("qwen_tts") is True
    assert supervisor.stop("qwen_tts") is True


def test_managed_worker_on_reserved_external_port_is_rejected(tmp_path: Path) -> None:
    spec = replace(worker_spec(tmp_path), endpoint="http://127.0.0.1:1234")
    supervisor = TTSWorkerSupervisor(
        {"qwen_tts": spec}, output_root=tmp_path, cache_root=tmp_path, log_root=tmp_path,
        popen_factory=lambda *args, **kwargs: pytest.fail("must not spawn"),
        health_probe=lambda endpoint: None,
    )

    with pytest.raises(ValueError, match="RESERVED_EXTERNAL_PORTS"):
        supervisor.ensure_running("qwen_tts")
    assert supervisor.stop("qwen_tts") is False


def test_two_managed_providers_sharing_one_port_are_rejected(tmp_path: Path) -> None:
    qwen = worker_spec(tmp_path)
    step = replace(
        qwen,
        provider="step_audio_editx",
        tokenizer_path=qwen.model_path,
    )
    with pytest.raises(ValueError, match="both claim port 8191"):
        TTSWorkerSupervisor(
            {"qwen_tts": qwen, "step_audio_editx": step},
            output_root=tmp_path, cache_root=tmp_path, log_root=tmp_path,
            popen_factory=lambda *args, **kwargs: pytest.fail("must not spawn"),
            health_probe=lambda endpoint: None,
        )
