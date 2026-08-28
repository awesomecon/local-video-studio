"""Adapter for the user's externally managed OpenAI-compatible local LLM."""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from .base import (
    BackendDescriptor,
    Capability,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
)
from .errors import BackendError, BackendErrorCode, redact_secrets
from .json_tools import parse_structured_json


JsonValidator = Callable[[Any], Any]


def _grammar_safe_schema(node: Any) -> Any:
    """Deep-copy a JSON schema, dropping string `maxLength` bounds.

    llama.cpp's schema-to-grammar compiler turns maxLength N into a bounded
    repetition (char{1,N}); large N (e.g. graphic html_body maxLength 80,000)
    trips its "exceeds sane defaults" repetition limit and the whole request is
    rejected with HTTP 400 before any token is generated. The wire schema keeps
    structure, field names, and types enforced by grammar; client-side pydantic
    validation remains the authoritative length bound.
    """

    if isinstance(node, Mapping):
        return {
            key: _grammar_safe_schema(value)
            for key, value in node.items()
            if key != "maxLength"
        }
    if isinstance(node, (list, tuple)):
        return [_grammar_safe_schema(item) for item in node]
    return node


class LocalLLMBackend(GeneratorBackend):
    """Talk to an existing service; never starts, stops, or binds its port."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234/v1",
        api_key_env: str = "LOCAL_LLM_API_KEY",
        model: str = "auto",
        timeout_seconds: float = 600.0,
        *,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory
        self._active: set[str] = set()
        self._canceled: set[str] = set()
        self._cancel_lock = threading.Lock()
        self._response_format_unsupported_models: set[str] = set()
        self._capability_lock = threading.Lock()
        self._loaded = False

    def __repr__(self) -> str:
        return (
            f"LocalLLMBackend(base_url={self.base_url!r}, api_key_env={self.api_key_env!r}, "
            f"model={self.model!r})"
        )

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name="local_llm",
            model_name=self.model,
            model_version="server-managed",
            device="external-local-service",
            capabilities=frozenset({Capability.TEXT_GENERATION}),
            supported_inputs=("text", "messages", "json_schema"),
            supported_outputs=("text", "json"),
        )

    def _api_key(self) -> str:
        # Resolve on use so it is not retained in backend state or serialized metadata.
        return os.environ.get(self.api_key_env, "")

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _client(self) -> httpx.Client:
        return self._client_factory(
            base_url=f"{self.base_url}/",
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )

    def _tcp_check(self, timeout: float = 1.0) -> None:
        parsed = urlparse(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return
        except OSError as exc:
            raise BackendError(
                BackendErrorCode.SERVER_NOT_RUNNING,
                f"The local LLM server is not reachable at {host}:{port}.",
                retryable=True,
                details=exc,
            ) from None

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        secret = self._api_key()
        try:
            with self._client() as client:
                response = client.request(method, path.lstrip("/"), **kwargs)
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise BackendError(
                BackendErrorCode.SERVER_NOT_RUNNING,
                "The configured local LLM server is not reachable.",
                retryable=True,
                details=exc,
                secrets=(secret,),
            ) from None
        except httpx.TimeoutException as exc:
            raise BackendError(
                BackendErrorCode.REQUEST_TIMEOUT,
                "The local LLM request timed out.",
                retryable=True,
                details=exc,
                secrets=(secret,),
            ) from None
        if response.status_code in {401, 403}:
            raise BackendError(
                BackendErrorCode.AUTHENTICATION_FAILED,
                f"The local LLM rejected {self.api_key_env}; verify the environment variable.",
            )
        if response.status_code == 404 and path == "/models":
            raise BackendError(
                BackendErrorCode.NOT_OPENAI_COMPATIBLE,
                "The service is listening but does not expose the OpenAI-compatible "
                "/v1/models endpoint.",
            )
        if response.status_code >= 400:
            body = redact_secrets(response.text[:500], (secret,))
            code = (
                BackendErrorCode.MODEL_UNAVAILABLE
                if response.status_code in {400, 404, 422}
                else BackendErrorCode.INVALID_RESPONSE
            )
            raise BackendError(
                code,
                f"The local LLM returned HTTP {response.status_code}.",
                retryable=response.status_code >= 500,
                details=body,
            )
        return response

    def discover_models(self) -> tuple[dict[str, Any], ...]:
        """Return structurally valid model records, preserving useful server metadata."""

        response = self._request("GET", "/models")
        try:
            body = response.json()
        except ValueError as exc:
            raise BackendError(
                BackendErrorCode.UNEXPECTED_SERVICE,
                "A service is listening on the LLM port but /v1/models did not return JSON.",
                details=exc,
            ) from None
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise BackendError(
                BackendErrorCode.NOT_OPENAI_COMPATIBLE,
                "The models endpoint response is not OpenAI-compatible.",
            )
        models: list[dict[str, Any]] = []
        for record in body["data"]:
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                models.append(dict(record))
        if body["data"] and not models:
            raise BackendError(
                BackendErrorCode.NOT_OPENAI_COMPATIBLE,
                "The models endpoint did not contain valid model identifiers.",
            )
        return tuple(models)

    def _selected_model(
        self,
        models: Sequence[Mapping[str, Any]] | None = None,
        *,
        model: str | None = None,
    ) -> str:
        available = list(models) if models is not None else list(self.discover_models())
        names = [str(item["id"]) for item in available]
        configured = self.model if model is None else model
        if configured == "auto":
            if not names:
                raise BackendError(
                    BackendErrorCode.MODEL_UNAVAILABLE,
                    "The local LLM server has no loaded models.",
                )
            return names[0]
        if configured not in names:
            raise BackendError(
                BackendErrorCode.MODEL_UNAVAILABLE,
                f"Configured local model {configured!r} is not available.",
            )
        return configured

    def selected_model(self, *, model: str | None = None) -> str:
        """Resolve the current model without changing the externally managed service."""
        return self._selected_model(model=model)

    def health(self, check_completion: bool = False) -> Mapping[str, Any]:
        try:
            self._tcp_check()
            models = self.discover_models()
            selected = self._selected_model(models)
            result: dict[str, Any] = {
                "status": "healthy",
                "endpoint": self.base_url,
                "models": [item["id"] for item in models],
                "selected_model": selected,
                "authenticated": bool(self._api_key()),
            }
            if check_completion:
                reply = self.complete(
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    max_tokens=4,
                    temperature=0.0,
                )
                result["completion_check"] = bool(reply.strip())
            return result
        except BackendError as exc:
            return {"status": "unhealthy", "endpoint": self.base_url, "error": exc.as_dict()}

    def feature_detection(self, *, test_streaming: bool = False) -> Mapping[str, Any]:
        models = self.discover_models()
        result: dict[str, Any] = {
            "model_listing": True,
            "streaming": "untested",
            "json_schema": "unknown",
            "tool_calling": "unknown",
            "models": [item["id"] for item in models],
        }
        if test_streaming:
            result["streaming"] = self._probe_streaming(self._selected_model(models))
        return result

    def _probe_streaming(self, model: str) -> bool:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Say OK."}],
            "max_tokens": 2,
            "temperature": 0,
            "stream": True,
        }
        response = self._request("POST", "/chat/completions", json=payload)
        event_stream = "text/event-stream" in response.headers.get("content-type", "")
        return event_stream or response.text.startswith("data:")

    @staticmethod
    def _response_format_not_supported(exc: BackendError) -> bool:
        """Recognize an explicit OpenAI-style structured-output rejection."""

        if not exc.details:
            return False
        try:
            body = json.loads(exc.details)
        except (TypeError, ValueError):
            return False
        error = body.get("error") if isinstance(body, Mapping) else None
        return (
            isinstance(error, Mapping)
            and error.get("code") == "response_format_not_supported"
            and error.get("param") == "response_format"
        )

    @staticmethod
    def _text_structured_messages(
        messages: Sequence[Mapping[str, Any]],
        json_schema: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Carry structured-output requirements in-band for text-only servers."""

        instruction = "Return only one valid JSON object, with no Markdown or commentary."
        if json_schema:
            schema_text = json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
            instruction += f" It must conform exactly to this JSON Schema: {schema_text}"
        wire_messages = [dict(item) for item in messages]
        for item in wire_messages:
            if item.get("role") == "system" and isinstance(item.get("content"), str):
                item["content"] = f"{item['content']}\n\n{instruction}"
                break
        else:
            wire_messages.insert(0, {"role": "system", "content": instruction})
        return wire_messages

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int = 1024,
        temperature: float = 0.2,
        structured: bool = False,
        json_schema: Mapping[str, Any] | None = None,
        validator: JsonValidator | None = None,
        model: str | None = None,
        thinking_budget_tokens: int | None = None,
    ) -> str | Any:
        if thinking_budget_tokens is not None and thinking_budget_tokens < 0:
            raise ValueError("thinking_budget_tokens must be nonnegative")
        chosen_model = model or self._selected_model()
        structured = structured or json_schema is not None
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": [dict(item) for item in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        with self._capability_lock:
            use_response_format = chosen_model not in self._response_format_unsupported_models
        if structured and use_response_format:
            if json_schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    # OpenAI-standard wrapper. This is what LM Studio enforces; the
                    # bare `schema` key used by older llama.cpp builds is silently
                    # ignored there, which let models return arbitrary field names.
                    "json_schema": {
                        "name": "structured_response",
                        "schema": _grammar_safe_schema(json_schema),
                    },
                }
            else:
                payload["response_format"] = {"type": "json_object"}
        elif structured:
            payload["messages"] = self._text_structured_messages(messages, json_schema)
        if thinking_budget_tokens is not None:
            # Current llama.cpp routers accept this per-request extension and keep
            # reasoning separate from the schema-constrained final response.
            payload["thinking_budget_tokens"] = thinking_budget_tokens
        try:
            response = self._request("POST", "/chat/completions", json=payload)
        except BackendError as exc:
            if not (structured and "response_format" in payload and self._response_format_not_supported(exc)):
                raise
            # NInfer deliberately supports text response_format only. Preserve
            # reasoning and sampling settings, carry the schema in-band, and let
            # parse_structured_json plus the caller's validator remain authoritative.
            with self._capability_lock:
                self._response_format_unsupported_models.add(chosen_model)
            payload.pop("response_format")
            payload["messages"] = self._text_structured_messages(messages, json_schema)
            response = self._request("POST", "/chat/completions", json=payload)
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The local LLM completion response was not OpenAI-compatible.",
                details=exc,
            ) from None
        if choice.get("finish_reason") == "length":
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The local LLM response was truncated at its token limit. Reduce the "
                "requested script length or increase the completion limit.",
                retryable=True,
            )
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The local LLM refused the script-generation request.",
                retryable=True,
            )
        if choice.get("finish_reason") == "content_filter":
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The local LLM stopped before returning a script.",
                retryable=True,
            )
        if not isinstance(content, str):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The local LLM completion did not contain text content.",
            )
        return parse_structured_json(content, validator=validator) if structured else content

    def load(self) -> None:
        # A logical connection check only. Model lifecycle belongs to the external owner.
        models = self.discover_models()
        self._selected_model(models)
        self._loaded = True

    def unload(self) -> None:
        # Intentionally does not ask the external server to unload its model.
        self._loaded = False

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
        with self._cancel_lock:
            if request.job_id in self._canceled:
                raise BackendError(BackendErrorCode.CANCELED, "The LLM job was canceled.")
        messages = request.settings.get("messages") or [{"role": "user", "content": request.prompt}]
        structured = bool(request.settings.get("structured", False))
        content = self.complete(
            messages=messages,
            max_tokens=int(request.settings.get("max_tokens", 2048)),
            temperature=float(request.settings.get("temperature", 0.2)),
            structured=structured,
            json_schema=request.settings.get("json_schema"),
            model=request.settings.get("model"),
        )
        request.output_dir.mkdir(parents=True, exist_ok=True)
        output = request.output_dir / ("response.json" if structured else "response.txt")
        output.write_text(
            json.dumps(content, indent=2, ensure_ascii=False) if structured else str(content),
            encoding="utf-8",
        )
        return GenerationResult(
            outputs=(output,),
            metadata={
                "backend": "local_llm",
                "model": request.settings.get("model", self.model),
                "seed": request.seed,
                "structured": structured,
            },
        )

    def cancel(self, job_id: str) -> bool:
        # Prevents pending work; HTTP cancellation is not standardized.
        with self._cancel_lock:
            if job_id in self._active:
                self._canceled.add(job_id)
        return True

    def reset_cancel(self, job_id: str) -> None:
        """Allow the worker to explicitly retry previously canceled pending work."""

        with self._cancel_lock:
            self._canceled.discard(job_id)

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        del request
        return {
            "local_vram_gb": 0.0,
            "device": "external-local-service",
            "note": "The external LLM may independently consume system GPU memory.",
        }
