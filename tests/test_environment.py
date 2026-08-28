from pathlib import Path

from backend.core.config import AppConfig
from backend.core.environment import (
    EnvironmentClassification, format_markdown, inspect_environment,
)


def test_environment_report_is_structured_and_secret_free(tmp_path: Path) -> None:
    config = AppConfig(paths={
        "model_root": tmp_path / "models",
        "project_root": tmp_path / "projects",
        "cache_root": tmp_path / "cache",
        "torch_cache": tmp_path / "cache/torch",
        "huggingface_cache": tmp_path / "cache/hf",
        "huggingface_hub_cache": tmp_path / "cache/hf/hub",
        "temp_root": tmp_path / "tmp",
        "app_data": tmp_path / "data",
        "minimum_free_disk_gb": 0,
    })
    report = inspect_environment(config, probe_cuda=False)
    assert report.classification in set(EnvironmentClassification)
    assert report.python_executable
    assert not report.torch.cuda_probed
    assert {item.target for item in report.disks} >= {
        "model_root", "project_root", "cache_root"
    }
    rendered = format_markdown(report)
    assert "System diagnostics" in rendered
    assert "LOCAL_LLM_API_KEY" not in rendered
    assert "replace_me" not in rendered
    # The report renders the configured threshold, not a hardcoded 50 GiB.
    assert report.minimum_free_disk_gb == 0
    assert "0 GiB policy" in rendered


def test_disk_policy_renders_configured_threshold(tmp_path: Path) -> None:
    config = AppConfig(paths={
        "model_root": tmp_path, "project_root": tmp_path, "cache_root": tmp_path,
        "minimum_free_disk_gb": 42,
    })
    report = inspect_environment(config, probe_cuda=False)
    rendered = format_markdown(report)
    assert report.minimum_free_disk_gb == 42
    assert "42 GiB policy" in rendered
    assert "50 GiB policy" not in rendered


def test_environment_reports_backend_isolation_policy(tmp_path: Path) -> None:
    config = AppConfig(paths={
        "model_root": tmp_path, "project_root": tmp_path, "cache_root": tmp_path,
        "minimum_free_disk_gb": 0,
    })
    report = inspect_environment(config, probe_cuda=False)
    assert report.backend_compatibility["comfyui"].disposition == "external_local_service"
    assert "isolated" in report.backend_compatibility["h3"].disposition
    assert report.backend_compatibility["mock"].disposition == "share_current_environment"
