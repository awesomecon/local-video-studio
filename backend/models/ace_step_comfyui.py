"""ComfyUI-native ACE-Step 1.5 XL backend for background-music generation."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

import httpx

from .comfyui import ComfyUIBackend
from .errors import BackendError, BackendErrorCode
from .base import Capability, GenerationResult


_ACE_WORKFLOW_NAMES = {
    "xl_turbo": "ace-step-1.5-xl-turbo.workflow.json",
    "xl_sft": "ace-step-1.5-xl-sft.workflow.json",
}

_ACE_COMBO_FIELDS = {
    "language": "language",
    "key_scale": "keyscale",
    "time_signature": "timesignature",
}


def _combo_values(field_spec: Any) -> list[str]:
    """Extract choices from current and older ComfyUI object-info shapes."""
    if not isinstance(field_spec, list) or not field_spec:
        return []
    if isinstance(field_spec[0], list):
        return [str(item) for item in field_spec[0]]
    for candidate in field_spec:
        if not isinstance(candidate, dict):
            continue
        values = candidate.get("options", candidate.get("combo", []))
        if isinstance(values, list):
            return [str(item) for item in values]
    return []


class ACEStepComfyUIBackend(ComfyUIBackend):
    """Native ComfyUI adapter for ACE-Step 1.5 XL text-to-music."""

    BACKEND_NAME = "ace_step_comfyui"
    DEFAULT_MODEL = "xl_turbo"
    VRAM_REQUIRED_GB = 20.0
    POLL_INTERVAL = 0.5
    GENERATION_TIMEOUT = 1800.0
    MAX_OUTPUT_BYTES = 200 * 1024 * 1024

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8188",
        *,
        model_name: str = "xl_turbo",
        workflow_path: str | Path | None = None,
        workflows_dir: str | Path | None = None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.5,
        generation_timeout: float = 1800.0,
        client_factory: Any = httpx.Client,
    ) -> None:
        super().__init__(
            endpoint,
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            generation_timeout=generation_timeout,
            client_factory=client_factory,
        )
        self._workflow_path = Path(workflow_path) if workflow_path else None
        self._workflows_dir = Path(workflows_dir) if workflows_dir else None
        self._workflow_metadata: dict[str, Any] | None = None
        self._expected_output_node_id: str | None = None
        self._expected_output_category: str = "audio"

    def descriptor(self) -> Any:
        descriptor = super().descriptor()
        quantization = "bf16"
        if self._workflow_metadata:
            quantization = self._workflow_metadata.get("quantization", "bf16")
        return descriptor.__class__(
            backend_name=self.BACKEND_NAME,
            model_name=f"ACE-Step 1.5 XL {self.model_name}",
            model_version="ace-step-1.5-xl-comfy-v1",
            quantization=quantization,
            device=descriptor.device,
            vram_required_gb=self.VRAM_REQUIRED_GB,
            capabilities=frozenset({Capability.TEXT_TO_MUSIC}),
            supported_inputs=(
                "prompt",
                "lyrics",
                "seed",
                "duration",
                "bpm",
                "time_signature",
                "language",
                "key_scale",
                "generate_audio_codes",
                "workflow",
                "model_filename",
            ),
            supported_outputs=("audio",),
            heavyweight=True,
        )

    def _resolve_workflow_files(self, preset: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Load a preset's workflow and sidecar metadata without touching instance state."""
        if self._workflow_path and self._workflow_path.is_file():
            workflow = json.loads(self._workflow_path.read_text(encoding="utf-8"))
            metadata_path = self._workflow_path.with_suffix(".metadata.json")
        else:
            workflows_dir = self._workflows_dir or Path(__file__).resolve().parents[2] / "workflows" / "comfyui"
            workflow_name = _ACE_WORKFLOW_NAMES.get(preset, _ACE_WORKFLOW_NAMES[self.DEFAULT_MODEL])
            workflow_path = workflows_dir / workflow_name
            if not workflow_path.is_file():
                raise BackendError(
                    BackendErrorCode.BACKEND_UNAVAILABLE,
                    f"ACE-Step workflow not found: {workflow_path}",
                )
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
            metadata_path = workflows_dir / workflow_name.replace(".workflow.json", ".metadata.json")

        metadata: dict[str, Any] = {}
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return workflow, metadata

    def _resolve_workflow(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve the configured model's workflow and commit its output-node state."""
        workflow, metadata = self._resolve_workflow_files(self.model_name)
        self._workflow_metadata = metadata
        self._expected_output_node_id = str(metadata.get("output_node_id", "")) or None
        self._expected_output_category = str(metadata.get("output_category", "audio"))
        return workflow, metadata

    def _load_workflow(self, request: Any) -> dict[str, Any]:
        # Mirror the base-class contract: an explicit settings["workflow"] wins.
        # The pipeline builds the exact preset workflow at submission time and
        # passes it with its output-node metadata; submitting that document
        # keeps the graph, its substitutions, and the output node validated by
        # readiness in sync even when the preset differs from the configured
        # model. Direct/legacy callers without a workflow fall back to disk
        # resolution for the configured model.
        settings: Mapping[str, Any] = getattr(request, "settings", None) or {}
        provided = settings.get("workflow")
        if provided is None:
            workflow, _ = self._resolve_workflow()
            return workflow
        if isinstance(provided, Mapping):
            workflow = copy.deepcopy(dict(provided))
        else:
            workflow = super()._load_workflow(request)
        self._workflow_metadata = dict(settings.get("workflow_metadata") or {})
        self._expected_output_node_id = str(settings.get("output_node_id", "")) or None
        self._expected_output_category = str(settings.get("output_category", "audio"))
        return workflow

    def _load_workflow_for_preset(self, preset: str) -> dict[str, Any]:
        """Resolve a preset's workflow without mutating instance state.

        Like the base backend, this instance is not thread-safe: generation
        runs are serialized by the pipeline's GPU lock, and instance state
        (``model_name``, ``_workflow_metadata``, ``_expected_output_node_id``)
        belongs to the configured ``self.model_name``. Resolving the preset
        fully locally means a failed resolution — or a readiness check
        interleaved with an in-flight generation — cannot leave stale preset
        state behind for the next submission.
        """
        workflow, _ = self._resolve_workflow_files(preset)
        return workflow

    def generate(self, request: Any) -> GenerationResult:
        result = super().generate(request)
        extended_metadata = dict(result.metadata)
        extended_metadata.update({
            "ace_model": request.settings.get("model_filename"),
            "generate_audio_codes": request.settings.get("generate_audio_codes"),
            "sampler": request.settings.get("sampler"),
            "fingerprint": request.settings.get("fingerprint"),
            "workflow_hash": request.settings.get("workflow_hash"),
            "output_node_id": request.settings.get("output_node_id"),
            "requested_duration": request.duration_seconds,
        })
        return GenerationResult(
            outputs=result.outputs,
            metadata=extended_metadata,
            peak_vram_gb=result.peak_vram_gb,
        )

    def _retrieve_outputs(self, record: Mapping[str, Any], output_dir: Path) -> list[Any]:
        outputs: list[Any] = []
        nodes = record.get("outputs", {})
        if not isinstance(nodes, Mapping):
            return outputs
        output_dir.mkdir(parents=True, exist_ok=True)
        target_node = self._expected_output_node_id
        target_category = self._expected_output_category
        for node_id, node in nodes.items():
            if not isinstance(node, Mapping):
                continue
            if target_node is not None and str(node_id) != str(target_node):
                continue
            for category in (target_category,):
                files = node.get(category, [])
                if not isinstance(files, list):
                    continue
                for remote in files:
                    if not isinstance(remote, Mapping) or not remote.get("filename"):
                        continue
                    filename = Path(str(remote["filename"])).name
                    response = self._request(
                        "GET",
                        "/view",
                        params={
                            "filename": remote["filename"],
                            "subfolder": remote.get("subfolder", ""),
                            "type": remote.get("type", "output"),
                        },
                    )
                    if len(response.content) > self.MAX_OUTPUT_BYTES:
                        raise BackendError(
                            BackendErrorCode.INVALID_RESPONSE,
                            f"ACE audio output exceeds {self.MAX_OUTPUT_BYTES} bytes limit.",
                        )
                    target = output_dir / filename
                    target.write_bytes(response.content)
                    outputs.append(target)
        return outputs

    def readiness(self) -> dict[str, Any]:
        health = self.health()
        if health.get("status") != "healthy":
            return {
                "comfyui_healthy": False,
                "turbo": {"ready": False, "missing_nodes": [], "missing_files": []},
                "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
                "combo_choices": {},
                "duration_range": {"min": None, "max": None},
                "error": health.get("error", {}).get("message", "ComfyUI is not healthy"),
            }

        try:
            response = self._request("GET", "/object_info")
            info: dict[str, Any] = response.json()
        except BackendError as exc:
            return {
                "comfyui_healthy": False,
                "turbo": {"ready": False, "missing_nodes": [], "missing_files": []},
                "sft": {"ready": False, "missing_nodes": [], "missing_files": []},
                "combo_choices": {},
                "duration_range": {"min": None, "max": None},
                "error": str(exc),
            }

        required_nodes = sorted({
            node["class_type"]
            for node in self._load_workflow(None).values()
            if isinstance(node, dict) and "class_type" in node
        })
        missing_nodes = [node for node in required_nodes if node not in info]

        def check_preset(preset: str) -> dict[str, Any]:
            workflow_name = _ACE_WORKFLOW_NAMES.get(preset)
            if not workflow_name:
                return {"ready": False, "missing_nodes": missing_nodes, "missing_files": []}
            workflow, metadata = self._resolve_workflow_from_info(info, preset)
            missing_files = self._missing_files_from_metadata(metadata, info)
            return {
                "ready": len(missing_nodes) == 0 and len(missing_files) == 0,
                "missing_nodes": missing_nodes,
                "missing_files": missing_files,
            }

        turbo_ready = check_preset("xl_turbo")
        sft_ready = check_preset("xl_sft")

        combo_choices: dict[str, Any] = {}
        ace_node = info.get("TextEncodeAceStepAudio1.5", {})
        ace_input = ace_node.get("input", {}).get("required", {})
        for public_name, node_name in _ACE_COMBO_FIELDS.items():
            choices = _combo_values(ace_input.get(node_name, []))
            if choices:
                combo_choices[public_name] = choices

        duration_range = {"min": None, "max": None}
        duration_constraints: list[dict[str, Any]] = []
        for node_name, field_name in (
            ("TextEncodeAceStepAudio1.5", "duration"),
            ("EmptyAceStep1.5LatentAudio", "seconds"),
        ):
            spec = info.get(node_name, {}).get("input", {}).get("required", {}).get(field_name, [])
            if isinstance(spec, list) and len(spec) >= 2 and isinstance(spec[1], dict):
                duration_constraints.append(spec[1])
        minimums = [item["min"] for item in duration_constraints if "min" in item]
        maximums = [item["max"] for item in duration_constraints if "max" in item]
        if minimums:
            duration_range["min"] = max(minimums)
        if maximums:
            duration_range["max"] = min(maximums)

        return {
            "comfyui_healthy": True,
            "turbo": turbo_ready,
            "sft": sft_ready,
            "combo_choices": combo_choices,
            "duration_range": duration_range,
            "error": None,
        }

    def _resolve_workflow_from_info(self, info: dict[str, Any], preset: str) -> tuple[dict[str, Any], dict[str, Any]]:
        workflow_name = _ACE_WORKFLOW_NAMES.get(preset)
        if not workflow_name:
            raise BackendError(BackendErrorCode.BACKEND_UNAVAILABLE, f"Unknown ACE preset: {preset}")
        workflows_dir = self._workflows_dir or Path(__file__).resolve().parents[2] / "workflows" / "comfyui"
        workflow_path = workflows_dir / workflow_name
        if not workflow_path.is_file():
            return {}, {}
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        metadata_path = workflows_dir / workflow_name.replace(".workflow.json", ".metadata.json")
        metadata: dict[str, Any] = {}
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return workflow, metadata

    def _missing_files_from_metadata(self, metadata: dict[str, Any], info: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for required in metadata.get("required_files", []):
            filename = required.get("filename", "")
            found = False
            for node_name in ("UNETLoader", "DualCLIPLoader", "VAELoader"):
                node = info.get(node_name, {})
                input_spec = node.get("input", {}).get("required", {})
                for field_name, field_spec in input_spec.items():
                    if filename in _combo_values(field_spec):
                        found = True
                        break
                if found:
                    break
            if not found:
                missing.append(filename)
        return missing
