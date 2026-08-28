"""Conservative JSON extraction and repair for local model responses."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from pydantic import ValidationError

from .errors import BackendError, BackendErrorCode


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _balanced_json(text: str) -> str | None:
    starts = sorted(
        (start, opening)
        for start, opening in ((text.find("{"), "{"), (text.find("["), "["))
        if start >= 0
    )
    for start, opening in starts:
        closing = "}" if opening == "{" else "]"
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
    return None


def parse_structured_json(
    value: str,
    *,
    validator: Callable[[Any], Any] | None = None,
) -> Any:
    """Parse common local-LLM wrappers without guessing missing semantic content."""

    candidate = _FENCE_RE.sub("", value.strip()).strip()
    attempts = [candidate]
    balanced = _balanced_json(candidate)
    if balanced and balanced != candidate:
        attempts.append(balanced)
    # A trailing comma is a frequent syntactic error and safe to repair.
    attempts.extend(re.sub(r",\s*([}\]])", r"\1", item) for item in list(attempts))
    last_error: Exception | None = None
    for item in attempts:
        try:
            parsed = json.loads(item)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            # Syntax-level failure: the payload is not usable JSON at all.
            last_error = exc
            continue
        if validator is None:
            return parsed
        try:
            return validator(parsed)
        except ValidationError as exc:
            # The JSON parsed cleanly but does not match the expected schema;
            # surface the first field-level problem so the UI can show it.
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The local model returned JSON that does not match the expected "
                f"structure: {_first_validation_error(exc)}.",
                details={"validation_error_count": exc.error_count()},
            ) from exc
        except (TypeError, ValueError) as exc:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "The local model returned JSON that does not match the expected structure.",
                details=exc,
            ) from exc
    raise BackendError(
        BackendErrorCode.INVALID_RESPONSE,
        "The local model returned malformed structured JSON.",
        details=last_error,
    )


def _first_validation_error(error: ValidationError) -> str:
    """Summarize the first pydantic issue as `path: message`."""

    for issue in error.errors():
        location = ".".join(str(part) for part in issue.get("loc", ()))
        message = str(issue.get("msg", "invalid value"))
        return f"{location}: {message}" if location else message
    return str(error)
