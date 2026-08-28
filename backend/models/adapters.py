"""Descriptors and safe local-service scaffolds for heavyweight model families."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

import httpx

from .base import (
    BackendDescriptor,
    Capability,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
)
from .comfyui import ComfyUIBackend
from .errors import BackendError, BackendErrorCode


class LocalServiceScaffold(GeneratorBackend):
    """A non-invasive service adapter that never installs or starts a runtime."""

    backend_name = "local_service"
    model_name = "unconfigured"
    model_version = "service-managed"
    backend_capabilities: frozenset[Capability] = frozenset()
    vram_required_gb = 0.0
    install_guidance = "Configure an isolated local service endpoint."
    supported_inputs: tuple[str, ...] = ("text",)
    supported_outputs: tuple[str, ...] = ()

    def __init__(self, endpoint: str | None = None, *, timeout_seconds: float = 5.0) -> None:
        self.endpoint = endpoint.rstrip("/") if endpoint else None
        self.timeout_seconds = timeout_seconds
        self._loaded = False

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name=self.backend_name,
            model_name=self.model_name,
            model_version=self.model_version,
            device="external-local-service" if self.endpoint else "unconfigured",
            vram_required_gb=self.vram_required_gb,
            capabilities=self.backend_capabilities,
            supported_inputs=self.supported_inputs,
            supported_outputs=self.supported_outputs,
            heavyweight=self.vram_required_gb >= 8,
        )

    def health(self) -> Mapping[str, Any]:
        if not self.endpoint:
            return {
                "status": "not_configured",
                "backend": self.backend_name,
                "install_guidance": self.install_guidance,
            }
        try:
            response = httpx.get(f"{self.endpoint}/health", timeout=self.timeout_seconds)
        except (httpx.NetworkError, httpx.TimeoutException) as exc:
            return {
                "status": "unhealthy",
                "backend": self.backend_name,
                "endpoint": self.endpoint,
                "error": str(exc),
                "install_guidance": self.install_guidance,
            }
        if response.status_code >= 400:
            return {
                "status": "unhealthy",
                "backend": self.backend_name,
                "endpoint": self.endpoint,
                "http_status": response.status_code,
            }
        content_type = response.headers.get("content-type", "")
        details: Any = response.json() if "json" in content_type else response.text[:200]
        return {
            "status": "healthy",
            "backend": self.backend_name,
            "endpoint": self.endpoint,
            "details": details,
        }

    def load(self) -> None:
        health = self.health()
        if health["status"] != "healthy":
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"{self.backend_name} is not ready. {self.install_guidance}",
            )
        self._loaded = True

    def unload(self) -> None:
        # This adapter never controls the external service model lifecycle.
        self._loaded = False

    def generate(self, request: GenerationRequest) -> GenerationResult:
        del request
        raise BackendError(
            BackendErrorCode.BACKEND_UNAVAILABLE,
            f"{self.backend_name} generation requires a configured backend-specific worker. "
            f"{self.install_guidance}",
        )

    def cancel(self, job_id: str) -> bool:
        del job_id
        return False

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        del request
        return {
            "estimated_vram_gb": self.vram_required_gb,
            "external_service": True,
            "heavyweight": self.vram_required_gb >= 8,
        }


class H3Backend(LocalServiceScaffold):
    backend_name = "minimax_h3"
    model_name = "H3-Base"
    backend_capabilities = frozenset(
        {
            Capability.TEXT_TO_VIDEO,
            Capability.IMAGE_TO_VIDEO,
            Capability.REFERENCE_TO_VIDEO,
            Capability.AUDIO_VIDEO_GENERATION,
        }
    )
    vram_required_gb = 20.0
    supported_inputs = ("text", "image", "reference", "duration", "resolution", "seed")
    supported_outputs = ("video", "audio")
    install_guidance = (
        "Run the H3 Base worker in a backend-specific isolated environment, then configure its "
        "localhost endpoint. FL2VA, Ref2VA, CPU offload, and quantization are worker settings."
    )

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        mode = str(request.settings.get("mode", "fl2va")).lower()
        return {
            "estimated_vram_gb": float(request.settings.get("estimated_vram_gb", 20.0)),
            "heavyweight": True,
            "external_service": True,
            "mode": mode,
            "cpu_offload": bool(request.settings.get("cpu_offload", True)),
            "quantization": request.settings.get("quantization"),
            "native_audio": mode in {"fl2va", "ref2va", "audiovisual"},
        }


class FluxBackend(ComfyUIBackend):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("model_name", "FLUX")
        super().__init__(*args, **kwargs)

    def descriptor(self) -> BackendDescriptor:
        descriptor = super().descriptor()
        return BackendDescriptor(
            backend_name="flux_comfyui",
            model_name=descriptor.model_name,
            model_version=descriptor.model_version,
            device=descriptor.device,
            vram_required_gb=12.0,
            capabilities=frozenset({Capability.TEXT_TO_IMAGE, Capability.IMAGE_TO_IMAGE}),
            supported_inputs=("prompt", "image", "workflow_json"),
            supported_outputs=("image",),
            heavyweight=True,
        )


class Krea2Backend(ComfyUIBackend):
    """Native ComfyUI adapter descriptor for the open Krea 2 Turbo checkpoint."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("model_name", "Krea 2 Turbo")
        super().__init__(*args, **kwargs)

    def descriptor(self) -> BackendDescriptor:
        descriptor = super().descriptor()
        return BackendDescriptor(
            backend_name="krea2_comfyui",
            model_name=descriptor.model_name,
            model_version="open-v1.0",
            quantization="fp8_scaled",
            device=descriptor.device,
            vram_required_gb=20.0,
            capabilities=frozenset({Capability.TEXT_TO_IMAGE}),
            # The krea2-turbo workflow runs the distilled recipe (CFG 1.0, zeroed
            # negative conditioning), so a negative prompt cannot influence the
            # image; keep the field as scene provenance only.
            supported_inputs=("prompt", "seed", "workflow_json"),
            supported_outputs=("image",),
            heavyweight=True,
        )


class QwenImage2512Backend(ComfyUIBackend):
    """Native ComfyUI adapter for the Qwen-Image-2512 FP8 workflow."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("model_name", "Qwen-Image-2512")
        super().__init__(*args, **kwargs)

    def descriptor(self) -> BackendDescriptor:
        descriptor = super().descriptor()
        return BackendDescriptor(
            backend_name="qwen_image_2512_comfyui",
            model_name=descriptor.model_name,
            model_version="2512",
            quantization="fp8_e4m3fn",
            device=descriptor.device,
            vram_required_gb=20.0,
            capabilities=frozenset({Capability.TEXT_TO_IMAGE}),
            supported_inputs=("prompt", "negative_prompt", "seed", "workflow_json"),
            supported_outputs=("image",),
            heavyweight=True,
        )


class Ideogram4LocalBackend(ComfyUIBackend):
    """Local ComfyUI adapter for Ideogram 4 (structured JSON prompts).

    Added alongside Qwen Image — not replacing it — because Qwen's rendering of
    embedded lettering was not strong enough for headlines/posters/maps/UI
    mockups. Ideogram is preferred for those text-heavy scenes while both are
    compared; Krea remains the generator for non-text cinematic imagery.

    The workflow receives the pipeline-built structured prompt via the
    ``prompt_json`` substitution (see workflows/comfyui/ideogram4-local.workflow.json).
    Quick mode runs the vendored official open-source Magic Prompt instructions
    through the configured local LLM; Precise mode validates native JSON. No
    hosted Magic Prompt or other external prompt expansion is used.
    """

    # The isolated Ideogram worker has no other Torch model family. A fresh
    # process reports zero Torch-reserved VRAM, while the NF4 pipeline reserves
    # substantially more than this once its custom-node cache is populated.
    _MIN_RESIDENT_TORCH_VRAM_BYTES = 8 * 1024**3

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("model_name", "Ideogram 4")
        super().__init__(*args, **kwargs)

    def has_resident_pipeline(self) -> bool:
        """Reconcile Studio state with the isolated worker's warm CUDA cache.

        Ideogram's custom loader retains its pipeline outside ComfyUI's normal
        model manager. Consequently the pipeline can survive after Studio's
        process-local residency marker is cleared. ``system_stats`` exposes
        only allocator totals, but that is sufficient here because this worker
        is dedicated exclusively to Ideogram.
        """
        status = self.health()
        if status.get("status") != "healthy":
            return False
        details = status.get("details")
        if not isinstance(details, Mapping):
            return False
        devices = details.get("devices")
        if not isinstance(devices, list):
            return False
        for device in devices:
            if not isinstance(device, Mapping):
                continue
            try:
                reserved = float(device.get("torch_vram_total", 0))
            except (TypeError, ValueError):
                continue
            if reserved >= self._MIN_RESIDENT_TORCH_VRAM_BYTES:
                return True
        return False

    @staticmethod
    def _is_content_filter_placeholder(path: Path) -> bool:
        """Recognize Ideogram's rendered safety card without inspecting prompts.

        The local Ideogram node reports a safety refusal as a normal PNG whose
        only content is ``Image blocked by safety filter``. ComfyUI therefore
        marks the workflow successful. OCR is deliberately scoped to Ideogram
        outputs and the exact known refusal phrase, so ordinary generated text
        and local Graphic Screens cannot be mistaken for a filtered result.
        """
        executable = shutil.which("tesseract")
        if executable is None:
            return False
        try:
            completed = subprocess.run(
                [executable, str(path), "stdout"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        normalized = re.sub(r"[^a-z]+", " ", completed.stdout.lower()).strip()
        return "image blocked by safety filter" in normalized

    def _retrieve_outputs(self, record: Mapping[str, Any], output_dir: Path) -> list[Path]:
        outputs = super()._retrieve_outputs(record, output_dir)
        if any(self._is_content_filter_placeholder(path) for path in outputs):
            for path in outputs:
                path.unlink(missing_ok=True)
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Ideogram blocked this image with its content safety filter. "
                "Revise the visual prompt or generate the text-free scene with Krea.",
            )
        return outputs

    def descriptor(self) -> BackendDescriptor:
        descriptor = super().descriptor()
        return BackendDescriptor(
            backend_name="ideogram4_local_comfyui",
            model_name=descriptor.model_name,
            model_version="4.0",
            quantization="nf4",
            device=descriptor.device,
            vram_required_gb=22.0,
            capabilities=frozenset({Capability.TEXT_TO_IMAGE}),
            supported_inputs=("prompt_json", "seed", "width", "height", "workflow_json"),
            supported_outputs=("image",),
            heavyweight=True,
        )


class WanBackend(ComfyUIBackend):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("model_name", "Wan")
        super().__init__(*args, **kwargs)

    def descriptor(self) -> BackendDescriptor:
        descriptor = super().descriptor()
        return BackendDescriptor(
            backend_name="wan_comfyui",
            model_name=descriptor.model_name,
            model_version=descriptor.model_version,
            device=descriptor.device,
            vram_required_gb=16.0,
            capabilities=frozenset(
                {Capability.TEXT_TO_VIDEO, Capability.IMAGE_TO_VIDEO, Capability.REFERENCE_TO_VIDEO}
            ),
            supported_inputs=("prompt", "image", "workflow_json"),
            supported_outputs=("video",),
            heavyweight=True,
        )


class ChatterboxBackend(LocalServiceScaffold):
    backend_name = "chatterbox"
    model_name = "Chatterbox"
    backend_capabilities = frozenset({Capability.TEXT_TO_SPEECH})
    vram_required_gb = 6.0
    supported_inputs = ("text", "authorized_reference_voice", "speed", "language")
    supported_outputs = ("audio", "voice_metadata")
    install_guidance = (
        "Install Chatterbox only in a compatible isolated environment or configure its local "
        "service. Voice cloning requires an explicitly authorized reference file."
    )


class ACEStepBackend(LocalServiceScaffold):
    backend_name = "ace_step"
    model_name = "ACE-Step"
    backend_capabilities = frozenset({Capability.TEXT_TO_MUSIC})
    vram_required_gb = 8.0
    supported_inputs = ("mood", "duration", "style", "instrumental")
    supported_outputs = ("audio",)
    install_guidance = (
        "Install ACE-Step in an isolated environment or configure its local service endpoint."
    )


class WhisperBackend(LocalServiceScaffold):
    backend_name = "whisper"
    model_name = "Whisper/whisper.cpp"
    backend_capabilities = frozenset({Capability.SPEECH_TO_TEXT})
    vram_required_gb = 4.0
    supported_inputs = ("audio", "language")
    supported_outputs = ("segments", "words", "srt", "ass")
    install_guidance = (
        "Configure a local whisper.cpp service or executable; keep its model in the shared "
        "model root."
    )

    def health(self) -> Mapping[str, Any]:
        if self.endpoint:
            return super().health()
        executable = shutil.which("whisper-cli") or shutil.which("whisper.cpp")
        if executable:
            return {"status": "healthy", "backend": self.backend_name, "executable": executable}
        return super().health()
