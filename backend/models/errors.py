"""Sanitized, actionable errors raised by generator backends."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Iterable


class BackendErrorCode(StrEnum):
    SERVER_NOT_RUNNING = "server_not_running"
    UNEXPECTED_SERVICE = "unexpected_service"
    AUTHENTICATION_FAILED = "authentication_failed"
    NOT_OPENAI_COMPATIBLE = "not_openai_compatible"
    MODEL_SELECTION_REQUIRED = "model_selection_required"
    MODEL_UNAVAILABLE = "model_unavailable"
    REQUEST_TIMEOUT = "request_timeout"
    INVALID_RESPONSE = "invalid_response"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    INSUFFICIENT_VRAM = "insufficient_vram"
    CANCELED = "canceled"


_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+")
_KEY_RE = re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def redact_secrets(value: object, secrets: Iterable[str] = ()) -> str:
    """Return a log-safe representation without authorization material."""

    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _AUTH_RE.sub(r"\1[REDACTED]", text)
    text = _KEY_RE.sub(r"\1[REDACTED]", text)
    return _BEARER_RE.sub("Bearer [REDACTED]", text)


class BackendError(RuntimeError):
    """An error safe to display through the local API and UI."""

    def __init__(
        self,
        code: BackendErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: object | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.details = redact_secrets(details, secrets) if details is not None else None
        safe_message = redact_secrets(message, secrets)
        super().__init__(safe_message)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code.value,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.details:
            result["details"] = self.details
        return result
