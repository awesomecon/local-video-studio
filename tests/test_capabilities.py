from __future__ import annotations

from pathlib import Path

from backend.core.capabilities import (
    CapabilityStatus,
    ReadinessStatus,
    build_capability_report,
)
from backend.core.config import AppConfig
from backend.core import environment
from backend.core.environment import DiskInfo, ToolInfo, TorchInfo


def _capability_report(
    *, host_system: str = "TestOS", host_architecture: str = "test-arch",
    bash_path: str | None = None, ideogram_script_path: str | None = None,
    tts_worker_path: str | None = None,
):
    return build_capability_report(
        python_version="3.12.1",
        python_supported=True,
        ffmpeg_status=CapabilityStatus.AVAILABLE,
        ffmpeg_evidence={"path": "/tools/ffmpeg"},
        ffprobe_status=CapabilityStatus.ABSENT,
        ffprobe_evidence={},
        chromium_status=CapabilityStatus.NOT_PROBED,
        chromium_evidence={},
        pytorch_status=CapabilityStatus.ABSENT,
        pytorch_evidence={},
        cuda_status=CapabilityStatus.NOT_PROBED,
        cuda_evidence={},
        nvidia_status=CapabilityStatus.PROBE_FAILED,
        nvidia_evidence={"error": "test failure"},
        host_system=host_system,
        host_release="test-release",
        host_architecture=host_architecture,
        bash_path=bash_path,
        ideogram_script_path=ideogram_script_path,
        tts_worker_path=tts_worker_path,
    )


def test_core_readiness_is_independent_of_features_and_optional_runtimes() -> None:
    report = _capability_report()

    assert report.core.ready is True
    assert report.core.status == ReadinessStatus.READY
    assert report.features["media_inspection"].status == CapabilityStatus.ABSENT
    assert report.features["browser_rendering"].status == CapabilityStatus.NOT_PROBED
    assert report.optional["pytorch"].status == CapabilityStatus.ABSENT
    assert report.optional["cuda"].status == CapabilityStatus.NOT_PROBED
    assert report.optional["nvidia_gpu_inventory"].status == CapabilityStatus.PROBE_FAILED
    assert report.optional["external_http_services"].status == CapabilityStatus.UNTESTED


def test_platform_labels_are_evidence_and_only_linux_bash_qualifies_managed_launch() -> None:
    unsupported = _capability_report(
        host_system="Windows",
        host_architecture="ExampleArch",
        bash_path="C:/tools/bash.exe",
        ideogram_script_path="C:/checkout/scripts/start_ideogram4.sh",
    )
    supported = _capability_report(
        host_system="Linux",
        host_architecture="ExampleArch",
        bash_path="/bin/bash",
        ideogram_script_path="/checkout/scripts/start_ideogram4.sh",
        tts_worker_path="/checkout/services/tts_worker/app.py",
    )

    assert unsupported.host.model_dump() == {
        "operating_system": "Windows",
        "release": "test-release",
        "architecture": "ExampleArch",
        "qualification": "descriptive_evidence_only",
    }
    assert (
        unsupported.optional["managed_ideogram_launch"].status
        == CapabilityStatus.UNSUPPORTED
    )
    assert (
        unsupported.optional["managed_higgs_launch"].status
        == CapabilityStatus.UNSUPPORTED
    )
    assert supported.optional["managed_ideogram_launch"].status == CapabilityStatus.AVAILABLE
    assert supported.optional["managed_tts_launch"].status == CapabilityStatus.AVAILABLE
    assert supported.optional["managed_higgs_launch"].status == CapabilityStatus.AVAILABLE
    assert supported.core.ready is True


def test_linux_without_bash_is_distinct_from_an_unsupported_host() -> None:
    report = _capability_report(
        host_system="Linux",
        bash_path=None,
        ideogram_script_path="/checkout/scripts/start_ideogram4.sh",
    )

    assert report.optional["managed_ideogram_launch"].status == CapabilityStatus.ABSENT


def test_skipped_cuda_probe_does_not_import_torch(monkeypatch) -> None:
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name == "torch":
            raise AssertionError("torch import is not part of an unprobed core readiness check")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    result = environment._torch_info(False, "2.10.0")

    assert result.installed is True
    assert result.import_probed is False
    assert result.cuda_probed is False


def test_environment_optional_absence_does_not_degrade_core_classification(
    tmp_path: Path, monkeypatch,
) -> None:
    config = AppConfig(paths={
        "model_root": tmp_path,
        "project_root": tmp_path,
        "cache_root": tmp_path,
        "minimum_free_disk_gb": 0,
    })
    package_versions = {
        name: None
        for name in (
            "torch", "torchvision", "torchaudio", "fastapi", "httpx", "diffusers",
            "transformers", "accelerate", "xformers", "chatterbox-tts", "ace-step",
            "openai-whisper", "faster-whisper", "imageio-ffmpeg",
        )
    }
    available_ffmpeg = ToolInfo(
        available=True, path="/tools/ffmpeg", version="ffmpeg test", source="test",
    )
    missing_tool = ToolInfo(available=False, source="test")
    monkeypatch.setattr(environment, "_package_versions", lambda: package_versions)
    monkeypatch.setattr(
        environment, "_torch_info",
        lambda *_args: TorchInfo(
            installed=False,
            import_probed=True,
            import_error="ModuleNotFoundError: No module named 'torch'",
        ),
    )
    monkeypatch.setattr(environment, "_ffmpeg_info", lambda: available_ffmpeg)
    monkeypatch.setattr(environment, "_ffprobe_info", lambda: missing_tool)
    monkeypatch.setattr(environment, "_chromium_info", lambda: missing_tool)
    monkeypatch.setattr(environment, "_command_version", lambda *_args: missing_tool)
    monkeypatch.setattr(
        environment, "_nvidia_probe",
        lambda: ([], CapabilityStatus.ABSENT, {}),
    )
    monkeypatch.setattr(
        environment, "_disk_info",
        lambda label, _path, _minimum: DiskInfo(
            target=label,
            inspected_path=str(tmp_path),
            total_gb=100,
            free_gb=100,
            meets_free_space_policy=True,
        ),
    )
    monkeypatch.setattr(environment, "_system_ram_gb", lambda: 16)
    monkeypatch.setattr(environment.platform, "system", lambda: "ExampleOS")
    monkeypatch.setattr(environment.platform, "release", lambda: "test-release")
    monkeypatch.setattr(environment.platform, "machine", lambda: "test-arch")
    monkeypatch.setattr(environment.platform, "platform", lambda: "descriptive-test-label")
    monkeypatch.setattr(environment.shutil, "which", lambda _name: None)

    report = environment.inspect_environment(config, probe_cuda=True)

    assert report.capabilities.core.ready is True
    assert report.classification == environment.EnvironmentClassification.COMPATIBLE
    assert report.capabilities.features["media_inspection"].status == CapabilityStatus.ABSENT
    assert report.capabilities.optional["pytorch"].status == CapabilityStatus.ABSENT
    assert report.capabilities.optional["nvidia_gpu_inventory"].status == CapabilityStatus.ABSENT
    assert not any("sandbox" in warning.lower() for warning in report.warnings)


def test_system_status_serves_separated_capability_report(tmp_path: Path, monkeypatch) -> None:
    """The served status payload must preserve the core/optional separation.

    Unit tests pin the report builder; this pins the serving layer so an
    unavailable optional backend served over HTTP can never decide core
    readiness, and host labels stay descriptive evidence only.
    """
    from fastapi.testclient import TestClient

    import backend.api.main as api_main
    from backend.core import load_config

    report = _capability_report()
    assert report.core.ready is True
    monkeypatch.setattr(api_main, "inspect_environment", lambda *_a, **_k: environment.EnvironmentReport(
        classification=environment.EnvironmentClassification.COMPATIBLE,
        python_version="3.12.1",
        python_executable="/tools/python",
        operating_system="TestOS",
        system_ram_gb=16,
        torch=environment.TorchInfo(installed=False, import_probed=True),
        ffmpeg=environment.ToolInfo(available=True, path="/tools/ffmpeg", source="test"),
        ffprobe=environment.ToolInfo(available=False, source="test"),
        git=environment.ToolInfo(available=False, source="test"),
        capabilities=report,
    ))

    class _Snapshot:
        def as_dict(self) -> dict:
            return {"devices": [], "active_backend": None}

    class _GPU:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def snapshot(self) -> _Snapshot:
            return _Snapshot()

    monkeypatch.setattr(api_main, "GPUResourceManager", _GPU)
    app = api_main.create_app(
        load_config(environ={}),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "tmp",
        mock_mode=True,
    )
    response = TestClient(app).get("/api/system/status")
    assert response.status_code == 200
    served = response.json()["environment"]["capabilities"]
    requirements = served["core"]["requirements"]
    assert set(requirements) == {"python", "ffmpeg"}
    assert served["core"]["ready"] is True
    assert served["core"]["ready"] == all(
        entry["status"] == CapabilityStatus.AVAILABLE for entry in requirements.values()
    )
    assert {"pytorch", "cuda", "managed_higgs_launch"} <= set(served["optional"])
    assert not set(served["optional"]).intersection(requirements)
    assert served["host"]["qualification"] == "descriptive_evidence_only"
