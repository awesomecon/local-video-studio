"""Typed, non-mutating runtime capability reporting.

The core application needs only a supported Python runtime and FFmpeg.  Model
stacks, accelerators, media inspection, and browser rendering are reported
separately so an unavailable optional component cannot hide core readiness.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CapabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilityStatus(StrEnum):
    """Result of one bounded capability check.

    The non-available states are intentionally distinct.  In particular,
    ``absent`` is observed absence, while ``not_probed`` and ``untested`` do
    not make a claim about what the host can support.
    """

    AVAILABLE = "available"
    ABSENT = "absent"
    NOT_PROBED = "not_probed"
    PROBE_FAILED = "probe_failed"
    UNSUPPORTED = "unsupported"
    UNTESTED = "untested"


class ReadinessStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


class CapabilityInfo(CapabilityModel):
    status: CapabilityStatus
    detail: str
    evidence: dict[str, str] = Field(default_factory=dict)


class CoreReadiness(CapabilityModel):
    status: ReadinessStatus
    ready: bool
    detail: str
    requirements: dict[str, CapabilityInfo]


class HostEvidence(CapabilityModel):
    """Descriptive labels from the host, never a model-support assertion."""

    operating_system: str
    release: str
    architecture: str
    qualification: str = "descriptive_evidence_only"


class CapabilityReport(CapabilityModel):
    host: HostEvidence
    core: CoreReadiness
    features: dict[str, CapabilityInfo] = Field(default_factory=dict)
    optional: dict[str, CapabilityInfo] = Field(default_factory=dict)


def build_capability_report(
    *,
    python_version: str,
    python_supported: bool,
    ffmpeg_status: CapabilityStatus,
    ffmpeg_evidence: dict[str, str],
    ffprobe_status: CapabilityStatus,
    ffprobe_evidence: dict[str, str],
    chromium_status: CapabilityStatus,
    chromium_evidence: dict[str, str],
    pytorch_status: CapabilityStatus,
    pytorch_evidence: dict[str, str],
    cuda_status: CapabilityStatus,
    cuda_evidence: dict[str, str],
    nvidia_status: CapabilityStatus,
    nvidia_evidence: dict[str, str],
    host_system: str,
    host_release: str,
    host_architecture: str,
    bash_path: str | None,
    ideogram_script_path: str | None,
    tts_worker_path: str | None,
) -> CapabilityReport:
    """Build the stable capability contract from already collected evidence.

    This function performs no imports, subprocess calls, network requests, or
    hardware probes.  It is therefore safe to use for core readiness even when
    heavyweight optional model packages are broken or unavailable.
    """

    python = CapabilityInfo(
        status=(CapabilityStatus.AVAILABLE if python_supported else CapabilityStatus.UNSUPPORTED),
        detail=(
            "Python meets the core requirement (3.11 or newer)."
            if python_supported
            else "Python 3.11 or newer is required for the core application."
        ),
        evidence={"version": python_version},
    )
    ffmpeg = CapabilityInfo(
        status=ffmpeg_status,
        detail=(
            "An FFmpeg executable is available for the core command path. Requested codecs "
            "and filters are validated separately for each render."
            if ffmpeg_status == CapabilityStatus.AVAILABLE
            else "FFmpeg is required for deterministic mock, edit, and render operations."
        ),
        evidence=ffmpeg_evidence,
    )
    ready = (
        python.status == CapabilityStatus.AVAILABLE
        and ffmpeg.status == CapabilityStatus.AVAILABLE
    )

    ideogram_evidence = {
        "operating_system": host_system,
        "architecture": host_architecture,
    }
    if bash_path:
        ideogram_evidence["bash_path"] = bash_path
    if ideogram_script_path:
        ideogram_evidence["script_path"] = ideogram_script_path
    if host_system == "Windows":
        ideogram_status = CapabilityStatus.UNSUPPORTED
        ideogram_detail = (
            "Managed Ideogram startup uses a Bash script and is unavailable on native Windows. "
            "An already-running compatible HTTP service remains usable."
        )
    elif not bash_path or not ideogram_script_path:
        ideogram_status = CapabilityStatus.ABSENT
        ideogram_detail = (
            "Managed Ideogram startup requires Bash and its executable source-checkout script. "
            "An already-running compatible HTTP service remains usable."
        )
    elif host_system == "Linux":
        ideogram_status = CapabilityStatus.AVAILABLE
        ideogram_detail = (
            "The Linux, Bash, and source-script prerequisites for managed Ideogram launch are "
            "present; its model runtime is validated separately."
        )
    else:
        ideogram_status = CapabilityStatus.UNTESTED
        ideogram_detail = (
            "The Bash launcher is present, but managed Ideogram startup has not been qualified "
            "for this operating system. An external compatible service may still be used."
        )

    tts_status = (
        CapabilityStatus.AVAILABLE if tts_worker_path else CapabilityStatus.ABSENT
    )
    tts_evidence = {"worker_path": tts_worker_path} if tts_worker_path else {}
    tts_detail = (
        "The optional TTS worker source is available; each provider runtime is validated separately."
        if tts_worker_path
        else "Managed TTS startup requires the optional worker source checkout."
    )

    higgs_evidence = {
        "operating_system": host_system,
        "architecture": host_architecture,
    }
    if tts_worker_path:
        higgs_evidence["worker_path"] = tts_worker_path
    if host_system != "Linux":
        higgs_status = CapabilityStatus.UNSUPPORTED
        higgs_detail = (
            "Managed Higgs TTS requires its Linux CUDA toolchain. A compatible external service "
            "may still be configured; no GPU support is inferred from the host labels."
        )
    elif not tts_worker_path:
        higgs_status = CapabilityStatus.ABSENT
        higgs_detail = "Managed Higgs TTS requires the optional TTS worker source checkout."
    else:
        higgs_status = CapabilityStatus.AVAILABLE
        higgs_detail = (
            "The Linux and worker-source launch prerequisites for managed Higgs TTS are present. "
            "CUDA, model files, and the provider runtime remain separately validated."
        )

    return CapabilityReport(
        host=HostEvidence(
            operating_system=host_system or "unknown",
            release=host_release or "unknown",
            architecture=host_architecture or "unknown",
        ),
        core=CoreReadiness(
            status=ReadinessStatus.READY if ready else ReadinessStatus.BLOCKED,
            ready=ready,
            detail=(
                "Core mock, edit, and render command paths are ready; each render validates its "
                "requested FFmpeg features."
                if ready
                else "Core mock, edit, and render operations are blocked by a required runtime."
            ),
            requirements={"python": python, "ffmpeg": ffmpeg},
        ),
        features={
            "media_inspection": CapabilityInfo(
                status=ffprobe_status,
                detail=(
                    "ffprobe is available for detailed media inspection and quality checks."
                    if ffprobe_status == CapabilityStatus.AVAILABLE
                    else "Detailed media inspection is limited without ffprobe."
                ),
                evidence=ffprobe_evidence,
            ),
            "browser_rendering": CapabilityInfo(
                status=chromium_status,
                detail=(
                    "A Chromium executable was discovered for browser-rendered graphics."
                    if chromium_status == CapabilityStatus.AVAILABLE
                    else "Browser-rendered graphics require a discoverable Chromium executable."
                ),
                evidence=chromium_evidence,
            ),
        },
        optional={
            "pytorch": CapabilityInfo(
                status=pytorch_status,
                detail="PyTorch is optional and does not affect core mock, edit, or render readiness.",
                evidence=pytorch_evidence,
            ),
            "cuda": CapabilityInfo(
                status=cuda_status,
                detail=(
                    "CUDA through PyTorch is optional; this status makes no Metal or AMD claim."
                ),
                evidence=cuda_evidence,
            ),
            "nvidia_gpu_inventory": CapabilityInfo(
                status=nvidia_status,
                detail=(
                    "NVIDIA inventory is optional. Absence or a failed probe does not imply that "
                    "the process is sandboxed."
                ),
                evidence=nvidia_evidence,
            ),
            "managed_ideogram_launch": CapabilityInfo(
                status=ideogram_status,
                detail=ideogram_detail,
                evidence=ideogram_evidence,
            ),
            "managed_tts_launch": CapabilityInfo(
                status=tts_status,
                detail=tts_detail,
                evidence=tts_evidence,
            ),
            "managed_higgs_launch": CapabilityInfo(
                status=higgs_status,
                detail=higgs_detail,
                evidence=higgs_evidence,
            ),
            "external_http_services": CapabilityInfo(
                status=CapabilityStatus.UNTESTED,
                detail=(
                    "Environment inspection does not contact configured services. HTTP service "
                    "reachability would not prove local model or hardware support on this host."
                ),
            ),
        },
    )
