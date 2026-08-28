"""ComfyUI-native voice-cloning TTS adapters for the four-model comparison.

Each provider runs behind ``GeneratorBackend`` through a versioned API-format
workflow template under ``workflows/comfyui/tts/``. Templates are pinned to a
reviewed custom-node commit and validated against ``GET /object_info`` before
any submission, so an uninstalled or updated node pack fails fast instead of
half-generating audio. No weights are downloaded and no model library is ever
imported into this process.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .base import BackendDescriptor, Capability, GenerationRequest, GenerationResult
from .comfyui import ComfyUIBackend
from .errors import BackendError, BackendErrorCode
from backend.rendering.binaries import require_ffmpeg
from backend.rendering.process import run_media_process


_TTS_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "comfyui" / "tts"
_MAX_OUTPUT_BYTES = 200 * 1024 * 1024
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_-]+")


def safe_component(value: str, *, limit: int = 120) -> str:
    component = _SAFE_COMPONENT.sub("-", value).strip("-")
    if not component:
        raise BackendError(
            BackendErrorCode.INVALID_RESPONSE,
            "A job identifier could not be converted into a safe filename component.",
        )
    return component[:limit]


class ComfyUITTSBackend(ComfyUIBackend):
    """Shared adapter for ComfyUI voice-cloning workflows that emit one WAV."""

    BACKEND_NAME: str = "comfyui_tts"
    PROVIDER: str = "unset"
    WORKFLOW_STEM: str = ""
    MODEL_LABEL: str = "unset"
    MODEL_VERSION: str = "unverified-template-v1"
    CHECKPOINT_REVISION: str = "unknown"
    QUANTIZATION: str = "bf16"
    VRAM_REQUIRED_GB: float = 8.0
    #: Largest seed accepted by the pinned node's INT input.
    SEED_MAX: int = 2**63 - 1
    #: How request.language maps onto the node's combo values.
    LANGUAGE_CASE: str = "lower"

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8188",
        *,
        workflows_dir: str | Path | None = None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.5,
        generation_timeout: float = 1800.0,
        client_factory: Any = None,
    ) -> None:
        if client_factory is None:
            import httpx

            client_factory = httpx.Client
        super().__init__(
            endpoint,
            model_name=self.MODEL_LABEL,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            generation_timeout=generation_timeout,
            client_factory=client_factory,
        )
        self._workflows_dir = Path(workflows_dir) if workflows_dir else _TTS_WORKFLOWS_DIR
        self._metadata: dict[str, Any] = {}

    @property
    def workflow_path(self) -> Path:
        return self._workflows_dir / f"{self.WORKFLOW_STEM}.workflow.json"

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name=self.PROVIDER,
            model_name=self.MODEL_LABEL,
            model_version=self.MODEL_VERSION,
            quantization=self.QUANTIZATION,
            device="external-local-service",
            vram_required_gb=self.VRAM_REQUIRED_GB,
            capabilities=frozenset({Capability.TEXT_TO_SPEECH}),
            supported_inputs=(
                "text",
                "authorized_reference_voice",
                "reference_text",
                "language",
                "seed",
                "workflow_json",
            ),
            supported_outputs=("wav",),
            heavyweight=True,
        )

    def _load_template(self) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self.workflow_path
        if not path.is_file():
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"{self.PROVIDER} workflow template is missing: {path}",
            )
        try:
            workflow = json.loads(path.read_text(encoding="utf-8"))
            metadata = json.loads(
                path.with_suffix("").with_suffix(".metadata.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"{self.PROVIDER} workflow template or metadata is unreadable: {path}",
                details=exc,
            ) from None
        if not isinstance(workflow, dict) or not isinstance(metadata, dict):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"{self.PROVIDER} workflow template root must be a JSON object.",
            )
        return workflow, metadata

    def _load_workflow(self, request: GenerationRequest) -> dict[str, Any]:
        settings: Mapping[str, Any] = request.settings or {}
        provided = settings.get("workflow")
        if isinstance(provided, Mapping):
            workflow = json.loads(json.dumps(provided))
            metadata = dict(settings.get("workflow_metadata") or {})
        else:
            workflow, metadata = self._load_template()
        self._metadata = metadata
        return workflow

    def _language(self, language: str) -> str | None:
        if self.LANGUAGE_CASE == "none":
            return None
        code = (language or "").strip()
        if self.LANGUAGE_CASE == "upper":
            return code.upper()
        return code.lower()

    def _seed_for_node(self, seed: int) -> int:
        return int(seed) % (self.SEED_MAX + 1)

    def _default_from_metadata(self, key: str, fallback: Any = None) -> Any:
        for item in self._metadata.get("substitutions", []):
            if isinstance(item, Mapping) and item.get("key") == key:
                return item.get("default", fallback)
        return fallback

    def _control(self, settings: Mapping[str, Any], key: str, default: Any) -> Any:
        value = settings.get(key)
        return default if value is None else value

    def substitutions(self, request: GenerationRequest, reference: str) -> dict[str, Any]:
        settings = request.settings or {}
        substitutions: dict[str, Any] = {
            "prompt": request.prompt,
            "seed": self._seed_for_node(request.seed),
            "reference_audio": reference,
            "reference_text": str(settings.get("reference_text", "") or ""),
            "filename_prefix": "lvs/tts/{}/{}".format(
                safe_component(self.PROVIDER), safe_component(request.job_id)
            ),
        }
        language = self._language(str(settings.get("language", "") or ""))
        if language is not None:
            substitutions["language"] = language or self._default_from_metadata("language", "auto")
        return substitutions

    def _generate(self, request: GenerationRequest) -> GenerationResult:
        workflow = self._load_workflow(request)
        references = list(request.references)
        if not references:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"{self.PROVIDER} voice cloning requires an authorized reference WAV.",
            )
        uploaded = [self.upload_image(path) for path in references]
        substitutions = self.substitutions(request, uploaded[0])
        from .comfyui import substitute_workflow

        submitted = substitute_workflow(workflow, substitutions)
        prompt_id = self.submit_workflow(submitted, job_id=request.job_id)
        record = self._poll_until_done(prompt_id, request)
        outputs = self._retrieve_outputs(record, request.output_dir)
        if not outputs:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"The {self.PROVIDER} workflow completed without retrievable audio.",
            )
        return self._result(outputs, request, prompt_id)

    def _poll_until_done(self, prompt_id: str, request: GenerationRequest) -> Mapping[str, Any]:
        import time

        deadline = time.monotonic() + float(
            (request.settings or {}).get("generation_timeout", self.generation_timeout)
        )
        record: Mapping[str, Any] | None = None
        while time.monotonic() < deadline:
            self._check_canceled(request.job_id)
            record = self.poll(prompt_id)
            if record is not None:
                break
            time.sleep(self.poll_interval)
        self._check_canceled(request.job_id)
        if record is None:
            raise BackendError(
                BackendErrorCode.REQUEST_TIMEOUT,
                f"Timed out waiting for the {self.PROVIDER} workflow.",
                retryable=True,
            )
        status = record.get("status", {})
        if isinstance(status, Mapping) and status.get("status_str") == "error":
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"The {self.PROVIDER} workflow failed.",
                details=status,
            )
        return record

    def _retrieve_outputs(
        self, record: Mapping[str, Any], output_dir: Path
    ) -> list[Path]:
        outputs: list[Path] = []
        nodes = record.get("outputs", {})
        if not isinstance(nodes, Mapping):
            return outputs
        output_dir.mkdir(parents=True, exist_ok=True)
        target_node = str(self._metadata.get("output_node_id", "")) or None
        category = str(self._metadata.get("output_category", "audio"))
        for node_id, node in nodes.items():
            if not isinstance(node, Mapping):
                continue
            if target_node is not None and str(node_id) != target_node:
                continue
            files = node.get(category, [])
            if not isinstance(files, list):
                continue
            for remote in files:
                if not isinstance(remote, Mapping) or not remote.get("filename"):
                    continue
                response = self._request(
                    "GET",
                    "/view",
                    params={
                        "filename": remote["filename"],
                        "subfolder": remote.get("subfolder", ""),
                        "type": remote.get("type", "output"),
                    },
                )
                if len(response.content) > _MAX_OUTPUT_BYTES:
                    raise BackendError(
                        BackendErrorCode.INVALID_RESPONSE,
                        f"{self.PROVIDER} audio output exceeds {_MAX_OUTPUT_BYTES} bytes limit.",
                    )
                target = self._write_pcm_wav(
                    output_dir,
                    Path(str(remote["filename"])).name,
                    response.content,
                )
                outputs.append(target)
        return sorted(outputs, key=lambda item: item.name)

    def _write_pcm_wav(self, output_dir: Path, remote_name: str, audio: bytes) -> Path:
        """Persist ComfyUI audio as the PCM WAV required by the TTS pipeline."""
        target = output_dir / Path(remote_name).with_suffix(".wav").name
        if Path(remote_name).suffix.lower() == ".wav":
            target.write_bytes(audio)
            return target

        source_fd, source_name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=Path(remote_name).suffix or ".audio",
            dir=output_dir,
        )
        output_fd, output_name = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=".wav", dir=output_dir,
        )
        os.close(output_fd)
        source = Path(source_name)
        temporary = Path(output_name)
        try:
            with os.fdopen(source_fd, "wb") as handle:
                handle.write(audio)
            run_media_process([
                str(require_ffmpeg()), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-y", "-i", str(source), "-map_metadata", "-1", "-vn",
                "-c:a", "pcm_s16le", str(temporary),
            ])
            os.replace(temporary, target)
        except Exception as exc:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"{self.PROVIDER} returned audio that could not be converted to PCM WAV.",
                details=exc,
            ) from None
        finally:
            source.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
        return target

    def _result(
        self,
        outputs: list[Path],
        request: GenerationRequest,
        prompt_id: str,
    ) -> GenerationResult:
        descriptor = self.descriptor()
        metadata = {
            "backend": self.PROVIDER,
            "provider": self.PROVIDER,
            "model": descriptor.model_name,
            "model_version": self.MODEL_VERSION,
            "checkpoint_revision": self.CHECKPOINT_REVISION,
            "node_project": self._metadata.get("node_project"),
            "node_commit_sha": self._metadata.get("node_commit_sha"),
            "workflow_version": self.MODEL_VERSION,
            "quantization": descriptor.quantization,
            "prompt_id": prompt_id,
            "seed": request.seed,
            "seed_submitted": self._seed_for_node(request.seed),
            "prompt": request.prompt,
            "settings": {
                **dict(request.settings or {}),
                "workflow_hash": self._metadata.get("workflow_version"),
                "verified_template": bool(self._metadata.get("verified", False)),
            },
        }
        return GenerationResult(outputs=tuple(outputs), metadata=metadata)

    def readiness(self) -> dict[str, Any]:
        """Validate the pinned template against the live ComfyUI node registry."""
        health = self.health()
        if health.get("status") != "healthy":
            return {
                "provider": self.PROVIDER,
                "comfyui_healthy": False,
                "ready": False,
                "missing_nodes": [],
                "template_present": self.workflow_path.is_file(),
                "error": (health.get("error") or {}).get("message", "ComfyUI is not healthy"),
            }
        try:
            info: Mapping[str, Any] = self._request("GET", "/object_info").json()
        except (BackendError, ValueError) as exc:
            return {
                "provider": self.PROVIDER,
                "comfyui_healthy": True,
                "ready": False,
                "missing_nodes": [],
                "template_present": self.workflow_path.is_file(),
                "error": str(exc),
            }
        try:
            workflow, metadata = self._load_template()
        except BackendError as exc:
            return {
                "provider": self.PROVIDER,
                "comfyui_healthy": True,
                "ready": False,
                "missing_nodes": [],
                "template_present": False,
                "error": exc.as_dict()["message"],
            }
        required = sorted({
            str(node["class_type"])
            for node in workflow.values()
            if isinstance(node, Mapping) and node.get("class_type")
        })
        missing = [name for name in required if name not in info]
        return {
            "provider": self.PROVIDER,
            "comfyui_healthy": True,
            "ready": not missing,
            "missing_nodes": missing,
            "template_present": True,
            "verified_template": bool(metadata.get("verified", False)),
            "workflow_version": metadata.get("workflow_version"),
            "node_commit_sha": metadata.get("node_commit_sha"),
            "error": None,
        }

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        del request
        return {
            "device": "external-local-service",
            "estimated_vram_gb": self.VRAM_REQUIRED_GB,
            "heavyweight": True,
        }


class FishS2ProBackend(ComfyUITTSBackend):
    BACKEND_NAME = "fish_s2_pro_comfyui"
    PROVIDER = "fish_s2_pro"
    WORKFLOW_STEM = "fish-s2-pro-clone"
    MODEL_LABEL = "Fish Audio S2 Pro"
    MODEL_VERSION = "fish-s2-pro-saganaki-comfy-v3"
    CHECKPOINT_REVISION = "s2-pro local checkout"
    QUANTIZATION = "bfloat16"
    VRAM_REQUIRED_GB = 14.0
    SEED_MAX = 2**31 - 1
    LANGUAGE_CASE = "lower"
    DEFAULT_MODEL_PRESET = "s2-pro"

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8188",
        *,
        model_preset: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(endpoint, **kwargs)
        self._model_preset = model_preset or self.DEFAULT_MODEL_PRESET

    def substitutions(self, request: GenerationRequest, reference: str) -> dict[str, Any]:
        settings = request.settings or {}
        substitutions = super().substitutions(request, reference)
        substitutions.update({
            "model_preset": str(settings.get("model_preset", "") or "") or self._model_preset,
            "temperature": self._control(
                settings, "temperature", self._default_from_metadata("temperature", 0.8)
            ),
        })
        return substitutions


class VoxCPM2Backend(ComfyUITTSBackend):
    BACKEND_NAME = "voxcpm2_comfyui"
    PROVIDER = "voxcpm2"
    WORKFLOW_STEM = "voxcpm2-clone"
    MODEL_LABEL = "VoxCPM2 2B"
    MODEL_VERSION = "voxcpm2-saganaki-comfy-v2"
    CHECKPOINT_REVISION = "openbmb/VoxCPM2 local checkout"
    QUANTIZATION = "bf16"
    VRAM_REQUIRED_GB = 8.0
    LANGUAGE_CASE = "none"

    def substitutions(self, request: GenerationRequest, reference: str) -> dict[str, Any]:
        settings = request.settings or {}
        substitutions = super().substitutions(request, reference)
        substitutions.update({
            "voice_description": str(settings.get("voice_instruction", "") or ""),
            "cfg_value": self._control(
                settings, "guidance_scale", self._default_from_metadata("cfg_value", 2.0)
            ),
            "inference_timesteps": self._control(
                settings, "inference_timesteps", self._default_from_metadata("inference_timesteps", 10)
            ),
        })
        return substitutions


class IndexTTS25Backend(ComfyUITTSBackend):
    BACKEND_NAME = "index_tts_2_5_comfyui"
    PROVIDER = "index_tts_2_5"
    WORKFLOW_STEM = "index-tts-2.5-clone"
    MODEL_LABEL = "IndexTTS 2.5"
    MODEL_VERSION = "index-tts-2.5-t8-comfy-v2"
    CHECKPOINT_REVISION = "IndexTTS-2.5 local checkout"
    QUANTIZATION = "auto"
    VRAM_REQUIRED_GB = 6.0
    SEED_MAX = 2**31 - 1
    LANGUAGE_CASE = "upper"

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8188",
        *,
        model_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(endpoint, **kwargs)
        self._model_path = str(model_path) if model_path else ""

    def substitutions(self, request: GenerationRequest, reference: str) -> dict[str, Any]:
        substitutions = super().substitutions(request, reference)
        substitutions["model_path"] = (
            str(request.settings.get("model_path", "") or "") or self._model_path
        )
        return substitutions
