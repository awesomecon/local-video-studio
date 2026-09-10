"""Non-mutating system inspection and compatibility classification."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .capabilities import (
    CapabilityReport,
    CapabilityStatus,
    build_capability_report,
)
from .config import AppConfig, load_config


class EnvironmentClassification(StrEnum):
    COMPATIBLE = "compatible_existing_environment"
    WARNINGS = "compatible_with_warnings"
    ISOLATION_REQUIRED = "incompatible_environment_requiring_isolation"


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolInfo(ReportModel):
    available: bool
    path: str | None = None
    version: str | None = None
    source: str = "system"
    error: str | None = None


class GPUInfo(ReportModel):
    name: str
    driver_version: str | None = None
    total_vram_gb: float
    free_vram_gb: float | None = None


class TorchInfo(ReportModel):
    installed: bool
    version: str | None = None
    import_probed: bool = False
    cuda_runtime: str | None = None
    cuda_probed: bool = False
    cuda_available: bool = False
    cuda_error: str | None = None
    cuda_device_name: str | None = None
    total_vram_gb: float | None = None
    import_error: str | None = None


class DiskInfo(ReportModel):
    target: str
    inspected_path: str
    total_gb: float
    free_gb: float
    meets_free_space_policy: bool


class BackendCompatibility(ReportModel):
    disposition: str
    available: bool
    detail: str


class EnvironmentReport(ReportModel):
    classification: EnvironmentClassification
    python_version: str
    python_executable: str
    operating_system: str
    system_ram_gb: float
    torch: TorchInfo
    nvidia_gpus: list[GPUInfo] = Field(default_factory=list)
    ffmpeg: ToolInfo
    ffprobe: ToolInfo
    git: ToolInfo
    disks: list[DiskInfo] = Field(default_factory=list)
    minimum_free_disk_gb: float = 50
    optional_packages: dict[str, str | None] = Field(default_factory=dict)
    backend_compatibility: dict[str, BackendCompatibility] = Field(default_factory=dict)
    capabilities: CapabilityReport
    warnings: list[str] = Field(default_factory=list)
    version_conflicts: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


def _command_version(executable: str | None, *arguments: str) -> ToolInfo:
    if not executable:
        return ToolInfo(available=False)
    try:
        completed = subprocess.run(
            [executable, *arguments], check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolInfo(available=False, path=executable, error=type(exc).__name__)
    output = (completed.stdout or completed.stderr).splitlines()
    return ToolInfo(
        available=completed.returncode == 0,
        path=executable,
        version=output[0].strip() if output else None,
        error=None if completed.returncode == 0 else f"exit code {completed.returncode}",
    )


def _ffmpeg_info() -> ToolInfo:
    # Reuse the renderer's portable discovery order.  In particular, it avoids
    # treating a confined Snap shim that answers ``-version`` inconsistently as
    # preferable to a working bundled imageio-ffmpeg executable.
    from backend.rendering.binaries import discover_binaries

    binaries = discover_binaries()
    result = _command_version(str(binaries.ffmpeg) if binaries.ffmpeg else None, "-version")
    return result.model_copy(update={"source": binaries.source})


def _ffprobe_info() -> ToolInfo:
    from backend.rendering.binaries import discover_binaries

    binaries = discover_binaries()
    result = _command_version(str(binaries.ffprobe) if binaries.ffprobe else None, "-version")
    return result.model_copy(update={"source": binaries.source})


def _chromium_info() -> ToolInfo:
    """Discover Chromium without statically coupling core diagnostics to a UI module."""
    try:
        try:
            # Platform discovery owns this helper once available.
            from backend.rendering.binaries import discover_chromium
        except ImportError:
            # Compatibility fallback for older checkouts.
            from backend.graphics.browser import discover_chromium

        executable = discover_chromium()
    except Exception as exc:  # Discovery is diagnostic and must not break the report.
        return ToolInfo(available=False, source="discovery", error=type(exc).__name__)
    return ToolInfo(
        available=executable is not None,
        path=str(executable) if executable is not None else None,
        source="discovery",
    )


def _torch_info(probe_cuda: bool, installed_version: str | None = None) -> TorchInfo:
    if not probe_cuda:
        # Package metadata is enough to distinguish absent from deliberately
        # unprobed, and avoids importing a heavyweight model runtime merely to
        # determine whether the core Python/FFmpeg path is ready.
        return TorchInfo(installed=installed_version is not None, version=installed_version)
    try:
        import torch
    except Exception as exc:  # Import failures often reveal binary conflicts.
        return TorchInfo(
            installed=False,
            version=installed_version,
            import_probed=True,
            import_error=f"{type(exc).__name__}: {exc}",
        )
    cuda_available = False
    cuda_error = None
    if probe_cuda:
        try:
            cuda_available = bool(torch.cuda.is_available())
        except Exception as exc:
            cuda_error = type(exc).__name__
    device_name, total_vram = None, None
    if cuda_available:
        try:
            device_name = torch.cuda.get_device_name(0)
            total_vram = torch.cuda.get_device_properties(0).total_memory / 1024**3
        except Exception as exc:
            cuda_available = False
            cuda_error = type(exc).__name__
    return TorchInfo(
        installed=True,
        version=str(torch.__version__),
        import_probed=True,
        cuda_runtime=str(torch.version.cuda) if torch.version.cuda else None,
        cuda_probed=probe_cuda,
        cuda_available=cuda_available,
        cuda_error=cuda_error,
        cuda_device_name=device_name,
        total_vram_gb=total_vram,
    )


def _nvidia_probe() -> tuple[list[GPUInfo], CapabilityStatus, dict[str, str]]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return [], CapabilityStatus.ABSENT, {}
    try:
        completed = subprocess.run(
            [executable, "--query-gpu=name,driver_version,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (
            [], CapabilityStatus.PROBE_FAILED,
            {"path": executable, "error": type(exc).__name__},
        )
    if completed.returncode != 0:
        return (
            [], CapabilityStatus.PROBE_FAILED,
            {"path": executable, "error": f"exit code {completed.returncode}"},
        )
    results = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            results.append(GPUInfo(
                name=parts[0], driver_version=parts[1],
                total_vram_gb=float(parts[2]) / 1024,
                free_vram_gb=float(parts[3]) / 1024,
            ))
        except ValueError:
            continue
    status = CapabilityStatus.AVAILABLE if results else CapabilityStatus.ABSENT
    return results, status, {"path": executable, "gpu_count": str(len(results))}


def _nvidia_gpus() -> list[GPUInfo]:
    """Return the legacy inventory view while the typed report retains probe state."""
    return _nvidia_probe()[0]


def _system_ram_gb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().total / 1024**3
    except ImportError:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        return page_size * pages / 1024**3


def _nearest_existing(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _disk_info(label: str, path: Path, minimum_free_gb: float) -> DiskInfo:
    existing = _nearest_existing(path)
    usage = shutil.disk_usage(existing)
    return DiskInfo(
        target=label, inspected_path=str(existing), total_gb=usage.total / 1024**3,
        free_gb=usage.free / 1024**3,
        meets_free_space_policy=(usage.free / 1024**3) >= minimum_free_gb,
    )


def _package_versions() -> dict[str, str | None]:
    packages = (
        "torch", "torchvision", "torchaudio", "fastapi", "httpx", "diffusers",
        "transformers", "accelerate", "xformers", "chatterbox-tts", "ace-step",
        "openai-whisper", "faster-whisper", "imageio-ffmpeg",
    )
    versions: dict[str, str | None] = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _major_minor(version: str | None) -> tuple[int, int] | None:
    if not version:
        return None
    components = version.split("+", 1)[0].split(".")
    try:
        return int(components[0]), int(components[1])
    except (ValueError, IndexError):
        return None


def _version_conflicts(packages: dict[str, str | None]) -> list[str]:
    conflicts: list[str] = []
    torch_version = _major_minor(packages["torch"])
    audio_version = _major_minor(packages["torchaudio"])
    vision_version = _major_minor(packages["torchvision"])
    if torch_version and audio_version and torch_version != audio_version:
        conflicts.append(
            f"torchaudio {packages['torchaudio']} does not match "
            f"torch {packages['torch']} major/minor"
        )
    if torch_version and vision_version and torch_version[0] == 2:
        expected_vision_minor = torch_version[1] + 15
        if vision_version != (0, expected_vision_minor):
            conflicts.append(
                f"torchvision {packages['torchvision']} may not match torch {packages['torch']}"
            )
    return conflicts


def _backend_compatibility(torch: TorchInfo, packages: dict[str, str | None],
                           ffmpeg: ToolInfo) -> dict[str, BackendCompatibility]:
    return {
        "mock": BackendCompatibility(
            disposition="share_current_environment", available=ffmpeg.available,
            detail="Uses lightweight Python dependencies and FFmpeg; no model weights required.",
        ),
        "whisper": BackendCompatibility(
            disposition="share_current_environment_if_installed", available=bool(
                packages["openai-whisper"] or packages["faster-whisper"]),
            detail=(
                "Can share CUDA PyTorch when package constraints match; "
                "whisper.cpp may be external."
            ),
        ),
        "comfyui": BackendCompatibility(
            disposition="external_local_service", available=False,
            detail="Keep ComfyUI and its model dependencies in its own environment/service.",
        ),
        "flux": BackendCompatibility(
            disposition="external_via_comfyui",
            available=False,
            detail="Use FLUX through the isolated local ComfyUI service initially.",
        ),
        "wan": BackendCompatibility(
            disposition="external_via_comfyui",
            available=False,
            detail="Use Wan through ComfyUI or a separately isolated local worker.",
        ),
        "local_llm": BackendCompatibility(
            disposition="external_local_service",
            available=False,
            detail=(
                "Use the existing OpenAI-compatible service; "
                "never claim its reserved port 1234."
            ),
        ),
        "h3": BackendCompatibility(
            disposition="isolated_local_service_recommended", available=False,
            detail="Use an H3-specific environment because its pinned dependencies may differ.",
        ),
        "chatterbox": BackendCompatibility(
            disposition="isolated_local_service_recommended", available=bool(
                packages["chatterbox-tts"]),
            detail="Isolate if its audio/PyTorch constraints differ from the working installation.",
        ),
        "ace_step": BackendCompatibility(
            disposition="isolated_local_service_recommended", available=bool(packages["ace-step"]),
            detail="Isolate if model-specific PyTorch constraints differ.",
        ),
        "torch_backends": BackendCompatibility(
            disposition="share_current_environment",
            available=torch.installed and torch.cuda_available,
            detail=(
                "Existing CUDA PyTorch is reusable when an adapter's declared constraints match."
            ),
        ),
    }


def _tool_capability(tool: ToolInfo) -> tuple[CapabilityStatus, dict[str, str]]:
    evidence: dict[str, str] = {}
    if tool.path:
        evidence["path"] = tool.path
    if tool.version:
        evidence["version"] = tool.version
    if tool.source:
        evidence["source"] = tool.source
    if tool.error:
        evidence["error"] = tool.error
    if tool.available:
        return CapabilityStatus.AVAILABLE, evidence
    if tool.path or tool.error:
        return CapabilityStatus.PROBE_FAILED, evidence
    return CapabilityStatus.ABSENT, evidence


def _torch_capabilities(
    torch: TorchInfo,
    installed_version: str | None,
    *,
    probe_cuda: bool,
) -> tuple[
    CapabilityStatus, dict[str, str], CapabilityStatus, dict[str, str],
]:
    pytorch_evidence: dict[str, str] = {}
    if torch.version or installed_version:
        pytorch_evidence["version"] = torch.version or installed_version or ""
    if torch.import_error:
        pytorch_evidence["error"] = torch.import_error

    if not probe_cuda:
        pytorch_status = (
            CapabilityStatus.NOT_PROBED
            if installed_version is not None
            else CapabilityStatus.ABSENT
        )
    elif torch.installed:
        pytorch_status = CapabilityStatus.AVAILABLE
    elif installed_version is None and torch.import_error and torch.import_error.startswith(
        "ModuleNotFoundError:"
    ):
        pytorch_status = CapabilityStatus.ABSENT
    else:
        pytorch_status = CapabilityStatus.PROBE_FAILED

    cuda_evidence: dict[str, str] = {}
    if torch.cuda_runtime:
        cuda_evidence["runtime"] = torch.cuda_runtime
    if torch.cuda_device_name:
        cuda_evidence["device"] = torch.cuda_device_name
    if torch.cuda_error:
        cuda_evidence["error"] = torch.cuda_error
    if not probe_cuda:
        cuda_status = CapabilityStatus.NOT_PROBED
    elif torch.import_error:
        cuda_status = (
            CapabilityStatus.ABSENT
            if pytorch_status == CapabilityStatus.ABSENT
            else CapabilityStatus.PROBE_FAILED
        )
    elif torch.cuda_runtime is None:
        cuda_status = CapabilityStatus.UNSUPPORTED
    elif torch.cuda_error:
        cuda_status = CapabilityStatus.PROBE_FAILED
    elif torch.cuda_available:
        cuda_status = CapabilityStatus.AVAILABLE
    else:
        cuda_status = CapabilityStatus.ABSENT
    return pytorch_status, pytorch_evidence, cuda_status, cuda_evidence


def inspect_environment(config: AppConfig | None = None, *,
                        probe_cuda: bool = True) -> EnvironmentReport:
    """Inspect only; never installs, downloads, starts, or terminates anything."""
    settings = config or load_config()
    packages = _package_versions()
    torch = _torch_info(probe_cuda, packages["torch"])
    ffmpeg = _ffmpeg_info()
    ffprobe = _ffprobe_info()
    chromium = _chromium_info()
    git = _command_version(shutil.which("git"), "--version")
    if probe_cuda:
        gpus, nvidia_status, nvidia_evidence = _nvidia_probe()
    else:
        gpus = []
        nvidia_status = CapabilityStatus.NOT_PROBED
        nvidia_evidence = {}
    disk_targets = {
        "model_root": settings.paths.model_root,
        "project_root": settings.paths.project_root,
        "cache_root": settings.paths.cache_root,
    }
    disks = [_disk_info(name, path, settings.paths.minimum_free_disk_gb)
             for name, path in disk_targets.items()]
    warnings: list[str] = []
    conflicts = _version_conflicts(packages)
    recommendations: list[str] = []
    python_version = sys.version.split()[0]
    python_pair = sys.version_info[:2]
    incompatible = python_pair < (3, 11)
    ffmpeg_status, ffmpeg_evidence = _tool_capability(ffmpeg)
    ffprobe_status, ffprobe_evidence = _tool_capability(ffprobe)
    chromium_status, chromium_evidence = _tool_capability(chromium)
    pytorch_status, pytorch_evidence, cuda_status, cuda_evidence = _torch_capabilities(
        torch, packages["torch"], probe_cuda=probe_cuda,
    )
    host_system = platform.system()
    host_release = platform.release()
    host_architecture = platform.machine()
    repository_root = Path(__file__).resolve().parents[2]
    ideogram_script = repository_root / "scripts" / "start_ideogram4.sh"
    ideogram_script_path = (
        str(ideogram_script)
        if ideogram_script.is_file() and os.access(ideogram_script, os.X_OK)
        else None
    )
    tts_worker = repository_root / "services" / "tts_worker" / "app.py"
    tts_worker_path = str(tts_worker) if tts_worker.is_file() else None
    capabilities = build_capability_report(
        python_version=python_version,
        python_supported=not incompatible,
        ffmpeg_status=ffmpeg_status,
        ffmpeg_evidence=ffmpeg_evidence,
        ffprobe_status=ffprobe_status,
        ffprobe_evidence=ffprobe_evidence,
        chromium_status=chromium_status,
        chromium_evidence=chromium_evidence,
        pytorch_status=pytorch_status,
        pytorch_evidence=pytorch_evidence,
        cuda_status=cuda_status,
        cuda_evidence=cuda_evidence,
        nvidia_status=nvidia_status,
        nvidia_evidence=nvidia_evidence,
        host_system=host_system,
        host_release=host_release,
        host_architecture=host_architecture,
        bash_path=shutil.which("bash"),
        ideogram_script_path=ideogram_script_path,
        tts_worker_path=tts_worker_path,
    )
    if incompatible:
        conflicts.append("Python 3.11 or newer is required for the main application")
    low_vram = [gpu for gpu in gpus if gpu.free_vram_gb is not None and
                gpu.free_vram_gb < settings.gpu.minimum_free_vram_gb_for_heavy_job]
    if low_vram:
        warnings.append(
            "System-wide free VRAM is below the configured heavy-job minimum; "
            "a local LLM or another process may be using GPU memory"
        )
        recommendations.append(
            "Wait for VRAM or manually unload the external LLM model before a heavy job; "
            "Local Video Studio will not terminate it."
        )
    if not ffmpeg.available:
        conflicts.append("FFmpeg is required for the end-to-end mock and render pipelines")
    low_disks = [disk.target for disk in disks if not disk.meets_free_space_policy]
    if low_disks:
        warnings.append(
            "Free-space policy is not met for: " + ", ".join(low_disks)
        )
        recommendations.append(
            "Configure model/cache roots on a larger mounted drive before downloading weights."
        )
    if incompatible or not ffmpeg.available:
        classification = EnvironmentClassification.ISOLATION_REQUIRED
    elif warnings or conflicts:
        classification = EnvironmentClassification.WARNINGS
    else:
        classification = EnvironmentClassification.COMPATIBLE
    return EnvironmentReport(
        classification=classification,
        python_version=python_version,
        python_executable=sys.executable,
        operating_system=platform.platform(),
        system_ram_gb=_system_ram_gb(),
        torch=torch,
        nvidia_gpus=gpus,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        git=git,
        disks=disks,
        minimum_free_disk_gb=settings.paths.minimum_free_disk_gb,
        optional_packages=packages,
        backend_compatibility=_backend_compatibility(torch, packages, ffmpeg),
        capabilities=capabilities,
        warnings=warnings,
        version_conflicts=conflicts,
        recommendations=recommendations,
    )


def format_markdown(report: EnvironmentReport) -> str:
    """Render a secret-free diagnostic report suitable for docs/system-diagnostics.md."""
    lines = [
        "# System diagnostics", "", f"Classification: `{report.classification.value}`", "",
        "## Core readiness", "",
        f"- Status: `{report.capabilities.core.status.value}`",
        f"- {report.capabilities.core.detail}",
        "- Core requirements: " + ", ".join(
            f"{name}={item.status.value}"
            for name, item in report.capabilities.core.requirements.items()
        ), "",
        "## Runtime", "", f"- Python: `{report.python_version}`",
        f"- Executable: `{report.python_executable}`",
        f"- OS: `{report.operating_system}`", f"- System RAM: `{report.system_ram_gb:.2f} GiB`", "",
        "## PyTorch and CUDA", "", f"- PyTorch: `{report.torch.version or 'not installed'}`",
        f"- PyTorch CUDA runtime: `{report.torch.cuda_runtime or 'unavailable'}`",
        f"- CUDA device probe: `{'performed' if report.torch.cuda_probed else 'not performed'}`",
        f"- CUDA available to PyTorch: `{report.torch.cuda_available}`",
        f"- CUDA device: `{report.torch.cuda_device_name or 'unavailable'}`",
        f"- Device VRAM: `{report.torch.total_vram_gb or 0:.2f} GiB`", "",
        "## Tools", "", f"- FFmpeg: `{report.ffmpeg.path or 'not found'}` ({report.ffmpeg.source})",
        f"- ffprobe: `{report.ffprobe.path or 'not found'}`",
        f"- Git: `{report.git.path or 'not found'}`", "",
        "## Feature and optional capabilities", "",
        *(
            f"- Feature `{name}`: `{item.status.value}` — {item.detail}"
            for name, item in report.capabilities.features.items()
        ),
        *(
            f"- Optional `{name}`: `{item.status.value}` — {item.detail}"
            for name, item in report.capabilities.optional.items()
        ), "",
        "## Host evidence (descriptive only)", "",
        f"- Operating system label: `{report.capabilities.host.operating_system}`",
        f"- Release label: `{report.capabilities.host.release}`",
        f"- Architecture label: `{report.capabilities.host.architecture}`", "",
        "## System-wide GPU memory", "",
    ]
    if report.nvidia_gpus:
        for gpu in report.nvidia_gpus:
            lines.append(
                f"- {gpu.name}: {gpu.free_vram_gb or 0:.2f} / {gpu.total_vram_gb:.2f} GiB free"
            )
    else:
        lines.append("- No system-wide NVIDIA result was available in this execution context.")
    lines.extend(["", "## Disk targets", ""])
    for disk in report.disks:
        lines.append(
            f"- {disk.target} (`{disk.inspected_path}`): {disk.free_gb:.2f} GiB free "
            f"of {disk.total_gb:.2f} GiB; {report.minimum_free_disk_gb:g} GiB policy: "
            f"{'pass' if disk.meets_free_space_policy else 'warning'}"
        )
    lines.extend(["", "## Compatibility notes", ""])
    for warning in [*report.warnings, *report.version_conflicts, *report.recommendations]:
        lines.append(f"- {warning}")
    lines.extend(["", "This report never reads or prints API-key values.", ""])
    return "\n".join(lines)
