"""Local ComfyUI service adapter with reusable workflow substitution."""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from .base import (
    BackendDescriptor,
    Capability,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
)
from .errors import BackendError, BackendErrorCode


def substitute_workflow(value: Any, substitutions: Mapping[str, Any]) -> Any:
    """Recursively replace exact {{name}} values and placeholders embedded in strings."""

    if isinstance(value, dict):
        return {key: substitute_workflow(item, substitutions) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_workflow(item, substitutions) for item in value]
    if not isinstance(value, str):
        return value
    for key, replacement in substitutions.items():
        token = "{{" + key + "}}"
        if value == token:
            return replacement
        value = value.replace(token, str(replacement))
    return value


class ComfyUIBackend(GeneratorBackend):
    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8188",
        *,
        workflow: Mapping[str, Any] | Path | None = None,
        model_name: str = "custom-workflow",
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.5,
        generation_timeout: float = 3600.0,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.workflow = workflow
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self.generation_timeout = generation_timeout
        self._client_factory = client_factory
        self.client_id = str(uuid.uuid4())
        self._jobs: dict[str, str] = {}
        self._active: set[str] = set()
        self._canceled: set[str] = set()
        self._cancel_lock = threading.Lock()

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name="comfyui",
            model_name=self.model_name,
            model_version="workflow-managed",
            device="external-local-service",
            vram_required_gb=12.0,
            capabilities=frozenset(
                {
                    Capability.TEXT_TO_IMAGE,
                    Capability.IMAGE_TO_IMAGE,
                    Capability.TEXT_TO_VIDEO,
                    Capability.IMAGE_TO_VIDEO,
                    Capability.REFERENCE_TO_VIDEO,
                }
            ),
            supported_inputs=("prompt", "image", "workflow_json"),
            supported_outputs=("image", "video", "audio"),
            heavyweight=True,
        )

    def _client(self) -> httpx.Client:
        return self._client_factory(base_url=self.endpoint, timeout=self.timeout_seconds)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with self._client() as client:
                response = client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise BackendError(
                BackendErrorCode.SERVER_NOT_RUNNING,
                "ComfyUI is not reachable at the configured local endpoint.",
                retryable=True,
                details=exc,
            ) from None
        except httpx.TimeoutException as exc:
            raise BackendError(
                BackendErrorCode.REQUEST_TIMEOUT,
                "The ComfyUI request timed out.",
                retryable=True,
                details=exc,
            ) from None
        if response.status_code >= 400:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                self._http_error_message(response),
                retryable=response.status_code >= 500,
                details=response.text[:1000],
            )
        return response

    @staticmethod
    def _http_error_message(response: httpx.Response) -> str:
        """Prefer ComfyUI's own error text over a bare HTTP code.

        Prompt-validation failures arrive as HTTP 400 with an `error` object
        plus per-node `node_errors`; without this the job log only says
        "ComfyUI returned HTTP 400." while the real cause (e.g. a missing
        model file) stays buried in `details`.
        """
        try:
            body = response.json()
        except ValueError:
            return f"ComfyUI returned HTTP {response.status_code}."
        if not isinstance(body, dict):
            return f"ComfyUI returned HTTP {response.status_code}."
        parts: list[str] = []
        error = body.get("error")
        if isinstance(error, Mapping):
            message = error.get("message") or error.get("type")
            if message:
                parts.append(str(message))
        node_errors = body.get("node_errors")
        if isinstance(node_errors, Mapping):
            for node_id, info in list(node_errors.items())[:3]:
                if not isinstance(info, Mapping):
                    continue
                for node_error in info.get("errors", [])[:2]:
                    if not isinstance(node_error, Mapping):
                        continue
                    message = node_error.get("message")
                    if not message:
                        continue
                    detail = node_error.get("details")
                    parts.append(
                        f"node {node_id}: {message}" + (f" ({detail})" if detail else "")
                    )
        if not parts:
            return f"ComfyUI returned HTTP {response.status_code}."
        return "ComfyUI rejected the request: " + "; ".join(parts)

    def health(self) -> Mapping[str, Any]:
        try:
            response = self._request("GET", "/system_stats")
            body = response.json()
            if not isinstance(body, dict) or not (
                "system" in body or "devices" in body or "comfyui_version" in body
            ):
                raise BackendError(
                    BackendErrorCode.UNEXPECTED_SERVICE,
                    "The configured endpoint did not return a recognizable ComfyUI response.",
                )
            return {"status": "healthy", "endpoint": self.endpoint, "details": body}
        except ValueError:
            return {
                "status": "unhealthy",
                "endpoint": self.endpoint,
                "error": {
                    "code": BackendErrorCode.UNEXPECTED_SERVICE.value,
                    "message": "The ComfyUI health endpoint did not return JSON.",
                },
            }
        except BackendError as exc:
            return {"status": "unhealthy", "endpoint": self.endpoint, "error": exc.as_dict()}

    def load(self) -> None:
        status = self.health()
        if status["status"] != "healthy":
            error = status.get("error", {})
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                str(error.get("message", "ComfyUI is unhealthy.")),
            )

    def unload(self) -> None:
        """Ask ComfyUI to release its cached models and allocator memory."""
        self._request(
            "POST",
            "/free",
            json={"unload_models": True, "free_memory": True},
        )

    def _load_workflow(self, request: GenerationRequest) -> dict[str, Any]:
        source = request.settings.get("workflow", self.workflow)
        if isinstance(source, Path):
            loaded = json.loads(source.read_text(encoding="utf-8"))
        elif isinstance(source, str):
            loaded = json.loads(Path(source).read_text(encoding="utf-8"))
        elif isinstance(source, Mapping):
            loaded = copy.deepcopy(dict(source))
        else:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "A ComfyUI API-format workflow is required for generation.",
            )
        if not isinstance(loaded, dict):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The ComfyUI workflow root must be a JSON object.",
            )
        return loaded

    def upload_image(self, image: Path, *, subfolder: str = "lvs") -> str:
        with image.open("rb") as handle:
            response = self._request(
                "POST",
                "/upload/image",
                files={"image": (image.name, handle, "application/octet-stream")},
                data={"overwrite": "true", "subfolder": subfolder, "type": "input"},
            )
        try:
            body = response.json()
            name = body["name"]
        except (ValueError, KeyError, TypeError):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "ComfyUI did not acknowledge the uploaded image.",
            ) from None
        return f"{body.get('subfolder', subfolder)}/{name}".strip("/")

    def submit_workflow(self, workflow: Mapping[str, Any], *, job_id: str) -> str:
        response = self._request(
            "POST",
            "/prompt",
            json={"prompt": dict(workflow), "client_id": self.client_id},
        )
        try:
            prompt_id = str(response.json()["prompt_id"])
        except (ValueError, KeyError, TypeError):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "ComfyUI did not return a prompt identifier.",
            ) from None
        self._jobs[job_id] = prompt_id
        return prompt_id

    def poll(self, prompt_id: str) -> Mapping[str, Any] | None:
        response = self._request("GET", f"/history/{prompt_id}")
        try:
            body = response.json()
        except ValueError:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "ComfyUI history did not return JSON.",
            ) from None
        if not isinstance(body, dict):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "ComfyUI history response was invalid.",
            )
        record = body.get(prompt_id)
        return record if isinstance(record, dict) else None

    def _retrieve_outputs(self, record: Mapping[str, Any], output_dir: Path) -> list[Path]:
        outputs: list[Path] = []
        nodes = record.get("outputs", {})
        if not isinstance(nodes, Mapping):
            return outputs
        output_dir.mkdir(parents=True, exist_ok=True)
        for node in nodes.values():
            if not isinstance(node, Mapping):
                continue
            for category in ("images", "videos", "audio"):
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
                    target = output_dir / filename
                    target.write_bytes(response.content)
                    outputs.append(target)
        return outputs

    def generate(self, request: GenerationRequest) -> GenerationResult:
        with self._cancel_lock:
            self._active.add(request.job_id)
            self._canceled.discard(request.job_id)
        try:
            return self._generate(request)
        finally:
            with self._cancel_lock:
                self._active.discard(request.job_id)
                self._canceled.discard(request.job_id)

    def _generate(self, request: GenerationRequest) -> GenerationResult:
        workflow = self._load_workflow(request)
        uploaded = [self.upload_image(path) for path in request.references]
        substitutions = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "seed": request.seed,
            "width": request.width or 1024,
            "height": request.height or 576,
            "duration": request.duration_seconds or 5.0,
            "reference_image": uploaded[0] if uploaded else "",
        }
        substitutions.update(request.settings.get("substitutions", {}))
        submitted = substitute_workflow(workflow, substitutions)
        prompt_id = self.submit_workflow(submitted, job_id=request.job_id)
        deadline = time.monotonic() + float(
            request.settings.get("generation_timeout", self.generation_timeout)
        )
        record: Mapping[str, Any] | None = None
        while time.monotonic() < deadline:
            self._check_canceled(request.job_id)
            record = self.poll(prompt_id)
            if record is not None:
                break
            time.sleep(self.poll_interval)
        # A canceled prompt is deleted from the ComfyUI queue, so /history never
        # returns a record for it; re-check before misreporting a timeout.
        self._check_canceled(request.job_id)
        if record is None:
            raise BackendError(
                BackendErrorCode.REQUEST_TIMEOUT,
                "Timed out waiting for the ComfyUI workflow.",
                retryable=True,
            )
        status = record.get("status", {})
        if isinstance(status, Mapping) and status.get("status_str") == "error":
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The ComfyUI workflow failed.",
                details=status,
            )
        outputs = self._retrieve_outputs(record, request.output_dir)
        if not outputs:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The ComfyUI workflow completed without retrievable outputs.",
            )
        descriptor = self.descriptor()
        return GenerationResult(
            outputs=tuple(outputs),
            metadata={
                "backend": descriptor.backend_name,
                "model": descriptor.model_name,
                "model_version": descriptor.model_version,
                "quantization": descriptor.quantization,
                "prompt_id": prompt_id,
                "seed": request.seed,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "workflow_version": request.settings.get("workflow_version", "unknown"),
            },
        )

    def cancel(self, job_id: str) -> bool:
        # Only a run that is in flight can be interrupted; flagging it first
        # lets the generate() poll loop stop even when the remote queue lookup
        # below finds nothing to delete or interrupt.
        with self._cancel_lock:
            if job_id in self._active:
                self._canceled.add(job_id)
        prompt_id = self._jobs.get(job_id)
        if not prompt_id:
            return False
        try:
            response = self._request("GET", "/queue")
            queue = response.json()
            if not isinstance(queue, Mapping):
                return False
            pending = self._queue_prompt_ids(queue.get("queue_pending", []))
            running = self._queue_prompt_ids(queue.get("queue_running", []))
            if prompt_id in pending:
                self._request("POST", "/queue", json={"delete": [prompt_id]})
            elif running == {prompt_id}:
                # /interrupt is global, so call it only when the sole running
                # prompt is proven to belong to this backend job.
                self._request("POST", "/interrupt")
            else:
                return False
            self._jobs.pop(job_id, None)
            return True
        except (BackendError, ValueError):
            return False

    def reset_cancel(self, job_id: str) -> None:
        """Allow the worker to explicitly retry previously canceled pending work."""

        with self._cancel_lock:
            self._canceled.discard(job_id)

    def _check_canceled(self, job_id: str) -> None:
        with self._cancel_lock:
            if job_id in self._canceled:
                raise BackendError(BackendErrorCode.CANCELED, "The ComfyUI job was canceled.")

    @staticmethod
    def _queue_prompt_ids(entries: Any) -> set[str]:
        prompt_ids: set[str] = set()
        if not isinstance(entries, list):
            return prompt_ids
        for entry in entries:
            if isinstance(entry, (list, tuple)) and len(entry) > 1:
                prompt_ids.add(str(entry[1]))
            elif isinstance(entry, Mapping) and entry.get("prompt_id") is not None:
                prompt_ids.add(str(entry["prompt_id"]))
        return prompt_ids

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        megapixels = (request.width or 1024) * (request.height or 576) / 1_000_000
        return {
            "device": "external-local-service",
            "estimated_vram_gb": round(10.0 + megapixels * 3.0, 1),
            "heavyweight": True,
        }
