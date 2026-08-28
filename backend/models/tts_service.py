"""HTTP adapter for isolated, localhost-only TTS workers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import httpx

from .base import BackendDescriptor, Capability, GenerationRequest, GenerationResult, GeneratorBackend
from .errors import BackendError, BackendErrorCode

logger = logging.getLogger(__name__)


_MODELS = {
    "qwen_tts": ("Qwen3-TTS-12Hz-1.7B Base/CustomVoice", "1.7B", 8.0),
    "step_audio_editx": ("Step-Audio-EditX", "official", 16.0),
    "chatterbox": ("Chatterbox Multilingual V3", "500M", 6.0),
    "omnivoice": ("OmniVoice (k2-fsa)", "0.2.1", 8.0),
    "breeze_tts_2": ("Breeze TTS 2 (≈3.5B)", "breeze-tts ca632ce6 (2026-08-26)", 8.0),
}


class TTSServiceBackend(GeneratorBackend):
    """Keeps dependency-heavy speech models outside the dashboard process."""

    def __init__(
        self,
        backend_name: str,
        endpoint: str | None,
        *,
        timeout_seconds: float = 900.0,
        client_factory: Any = httpx.Client,
    ) -> None:
        if backend_name not in _MODELS:
            raise ValueError(f"unsupported TTS backend: {backend_name}")
        self.backend_name = backend_name
        self.endpoint = endpoint.rstrip("/") if endpoint else None
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def descriptor(self) -> BackendDescriptor:
        model, version, vram = _MODELS[self.backend_name]
        inputs = ["text", "authorized_reference_voice", "language", "seed"]
        if self.backend_name == "step_audio_editx":
            inputs.extend(("source_audio", "edit_type", "edit_instruction"))
        if self.backend_name == "qwen_tts":
            inputs.extend(("speaker", "voice_instruction"))
        if self.backend_name == "chatterbox":
            inputs.extend(("exaggeration", "cfg_weight", "temperature"))
        if self.backend_name == "omnivoice":
            inputs.extend(("voice_instruction", "num_step", "guidance_scale", "speed"))
        if self.backend_name == "breeze_tts_2":
            inputs.extend(("voice_instruction", "guidance_scale", "breeze_mode"))
        return BackendDescriptor(
            backend_name=self.backend_name,
            model_name=model,
            model_version=version,
            device="external-local-service" if self.endpoint else "unconfigured",
            vram_required_gb=vram,
            capabilities=frozenset({Capability.TEXT_TO_SPEECH}),
            supported_inputs=tuple(inputs),
            supported_outputs=("wav", "generation_metrics"),
            heavyweight=True,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.endpoint:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"{self.backend_name} has no configured local worker endpoint.",
            )
        try:
            with self._client_factory(base_url=self.endpoint, timeout=self.timeout_seconds) as client:
                response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise BackendError(
                BackendErrorCode.REQUEST_TIMEOUT,
                f"{self.backend_name} worker did not finish within {self.timeout_seconds:g}s.",
                retryable=True,
                details=exc,
            ) from None
        except httpx.NetworkError as exc:
            raise BackendError(
                BackendErrorCode.SERVER_NOT_RUNNING,
                f"{self.backend_name} worker is not reachable at {self.endpoint}.",
                retryable=True,
                details=exc,
            ) from None
        if response.status_code >= 400:
            detail = response.text[:1000].strip()
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("detail") is not None:
                detail = str(payload["detail"])[:1000].strip()
            message = f"{self.backend_name} worker returned HTTP {response.status_code}."
            if detail:
                message = f"{message} {detail}"
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                message,
                retryable=response.status_code >= 500,
                details=detail or None,
            )
        return response

    def health(self) -> Mapping[str, Any]:
        if not self.endpoint:
            return {"status": "not_configured", "backend": self.backend_name}
        try:
            payload = self._request("GET", "/health", timeout=3.0).json()
            if not isinstance(payload, dict) or payload.get("provider") != self.backend_name:
                raise ValueError("unexpected worker identity")
            return {"status": "healthy", "endpoint": self.endpoint, **payload}
        except (BackendError, ValueError) as exc:
            error = exc.as_dict() if isinstance(exc, BackendError) else {"message": str(exc)}
            return {"status": "unhealthy", "backend": self.backend_name, "error": error}

    def load(self) -> None:
        self._request("POST", "/load")

    def unload(self) -> None:
        if self.endpoint:
            self._request("POST", "/unload")

    def generate(self, request: GenerationRequest) -> GenerationResult:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        output = request.output_dir / str(request.settings.get("filename", "speech.wav"))
        # Derived keys win over caller settings so a job cannot redirect the
        # worker's output path or change the reproducible seed.
        payload = {
            **dict(request.settings),
            "job_id": request.job_id,
            "text": request.prompt,
            "output_path": str(output.resolve()),
            "reference_audio": str(request.references[0].resolve()) if request.references else None,
            "seed": request.seed,
        }
        try:
            body = self._request("POST", "/generate", json=payload).json()
        except ValueError as exc:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"{self.backend_name} worker returned non-JSON output.",
                details=exc,
            ) from None
        if not isinstance(body, dict):
            raise BackendError(BackendErrorCode.INVALID_RESPONSE, "TTS worker returned invalid JSON.")
        returned = Path(str(body.get("output_path", output))).resolve()
        root = request.output_dir.resolve()
        if returned != output.resolve() or (returned != root and root not in returned.parents):
            raise BackendError(BackendErrorCode.INVALID_RESPONSE, "TTS worker returned an unsafe path.")
        if not returned.is_file():
            raise BackendError(BackendErrorCode.INVALID_RESPONSE, "TTS worker did not create WAV output.")
        descriptor = self.descriptor()
        metrics = body.get("metrics", {})
        return GenerationResult(
            outputs=(returned,),
            metadata={
                "backend": self.backend_name,
                "model": descriptor.model_name,
                "model_version": descriptor.model_version,
                "workflow_version": "tts-service-v1",
                "seed": request.seed,
                "prompt": request.prompt,
                "settings": {**dict(request.settings), "metrics": metrics},
            },
            peak_vram_gb=metrics.get("peak_vram_gb") if isinstance(metrics, dict) else None,
        )

    def cancel(self, job_id: str) -> bool:
        if not self.endpoint:
            return False
        try:
            body = self._request("POST", f"/jobs/{job_id}/cancel").json()
        except (BackendError, ValueError):
            return False
        return isinstance(body, dict) and bool(body.get("canceled"))

    def reset_cancel(self, job_id: str) -> None:
        if not self.endpoint:
            return
        try:
            self._request("POST", f"/jobs/{job_id}/reset_cancel")
        except BackendError as exc:
            logger.debug(
                "%s worker cancel reset ignored for job %s; a retry may 409: %s",
                self.backend_name,
                job_id,
                exc.as_dict(),
            )

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        del request
        return {
            "estimated_vram_gb": self.descriptor().vram_required_gb,
            "external_service": True,
            "heavyweight": True,
        }
