from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from backend.models.errors import BackendError, BackendErrorCode, redact_secrets
from backend.models.json_tools import parse_structured_json
from backend.models.local_llm import LocalLLMBackend


def client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    return factory


def test_model_discovery_and_structured_completion(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "super-secret-value")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer super-secret-value"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "local-model"}]})
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "local-model"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "```json\n{\"ok\": true,}\n```"}}]},
        )

    backend = LocalLLMBackend(client_factory=client_factory(handler))
    assert backend.discover_models()[0]["id"] == "local-model"
    result = backend.complete(
        messages=[{"role": "user", "content": "test"}], structured=True
    )
    assert result == {"ok": True}
    assert "super-secret-value" not in repr(backend)


def test_explicit_model_validation_does_not_mutate_shared_backend_state():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={
            "data": [{"id": "project-a"}, {"id": "project-b"}],
        })

    backend = LocalLLMBackend(
        model="project-a", client_factory=client_factory(handler),
    )
    assert backend.selected_model(model="project-b") == "project-b"
    assert backend.model == "project-a"


def test_schema_is_sent_in_the_openai_json_schema_wrapper(monkeypatch):
    """LM Studio enforces constraints only for the OpenAI-standard wrapper.

    The bare `schema` key used by older llama.cpp builds is silently ignored
    there, which let models return arbitrary field names and broke Graphic
    Screen generation most of the time.
    """
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "test-only-secret")
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "writer"}]})
        payload = json.loads(request.content)
        assert payload["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "structured_response", "schema": schema},
        }
        assert "schema" not in payload["response_format"]
        assert payload["thinking_budget_tokens"] == 10_000
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    backend = LocalLLMBackend(client_factory=client_factory(handler))
    result = backend.complete(
        messages=[{"role": "user", "content": "test"}],
        json_schema=schema,
        validator=lambda value: value,
        thinking_budget_tokens=10_000,
    )
    assert result == {"ok": True}


def test_wire_schema_drops_maxlength_that_breaks_llama_grammar_compilation(monkeypatch):
    """Huge string bounds (html_body: 80_000) become char{1,80000} and llama.cpp
    rejects the whole request; the wire schema enforces structure only, and
    pydantic validation client-side remains the authoritative length check."""
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "test-only-secret")
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 300},
            "items": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string", "maxLength": 80_000},
            },
        },
        "required": ["title"],
        "additionalProperties": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "writer"}]})
        payload = json.loads(request.content)
        wire = payload["response_format"]["json_schema"]["schema"]
        assert json.dumps(wire).find("maxLength") == -1
        assert wire["properties"]["title"] == {"type": "string", "minLength": 1}
        assert wire["properties"]["items"]["items"] == {"type": "string"}
        assert wire["properties"]["items"]["maxItems"] == 5
        assert wire["required"] == ["title"]
        assert wire["additionalProperties"] is False
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"title":"ok","items":[]}'}}]},
        )

    original = json.loads(json.dumps(schema))
    backend = LocalLLMBackend(client_factory=client_factory(handler))
    result = backend.complete(
        messages=[{"role": "user", "content": "test"}],
        json_schema=schema,
        validator=lambda value: value,
    )
    assert result == {"title": "ok", "items": []}
    # The caller's schema object is never mutated by the wire projection.
    assert schema == original


def test_structured_completion_falls_back_for_text_only_response_format_server():
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    completion_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "ninfer-model"}]})
        payload = json.loads(request.content)
        completion_payloads.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": {
                "code": "response_format_not_supported",
                "message": "only response_format {type:text} is supported",
                "param": "response_format",
                "type": "invalid_request_error",
            }})
        assert "conform exactly to this JSON Schema" in payload["messages"][0]["content"]
        assert json.dumps(schema, ensure_ascii=False, separators=(",", ":")) in payload["messages"][0]["content"]
        assert payload["thinking_budget_tokens"] == 16_384
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    backend = LocalLLMBackend(client_factory=client_factory(handler))
    kwargs = {
        "messages": [{"role": "system", "content": "Return a result."}],
        "json_schema": schema,
        "validator": lambda value: value,
        "thinking_budget_tokens": 16_384,
    }

    assert backend.complete(**kwargs) == {"ok": True}
    assert backend.complete(**kwargs) == {"ok": True}
    assert len(completion_payloads) == 3
    assert "response_format" in completion_payloads[0]
    assert "response_format" not in completion_payloads[1]
    assert "response_format" not in completion_payloads[2]


def test_structured_completion_does_not_hide_other_http_400_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "writer"}]})
        return httpx.Response(400, json={"error": {
            "code": "context_length_exceeded",
            "message": "request is too large",
            "param": "messages",
        }})

    backend = LocalLLMBackend(client_factory=client_factory(handler))
    with pytest.raises(BackendError) as raised:
        backend.complete(
            messages=[{"role": "user", "content": "test"}],
            structured=True,
        )
    assert "context_length_exceeded" in str(raised.value.details)


def test_negative_thinking_budget_is_rejected_before_request():
    backend = LocalLLMBackend(model="writer")
    with pytest.raises(ValueError, match="must be nonnegative"):
        backend.complete(
            messages=[{"role": "user", "content": "test"}],
            thinking_budget_tokens=-1,
        )


def test_token_limited_completion_is_reported_as_truncated():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "writer"}]})
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "length",
                "message": {"content": '{"unfinished":'},
            }],
        })

    backend = LocalLLMBackend(client_factory=client_factory(handler))
    with pytest.raises(BackendError) as raised:
        backend.complete(messages=[{"role": "user", "content": "test"}], structured=True)
    assert raised.value.code == BackendErrorCode.INVALID_RESPONSE
    assert raised.value.retryable is True
    assert "truncated at its token limit" in str(raised.value)


def test_token_limit_takes_precedence_over_missing_content():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "writer"}]})
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"content": None}}],
        })

    backend = LocalLLMBackend(client_factory=client_factory(handler))
    with pytest.raises(BackendError, match="truncated at its token limit"):
        backend.complete(messages=[{"role": "user", "content": "test"}])


def test_model_refusal_has_an_actionable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "writer"}]})
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": None, "refusal": "No."},
            }],
        })

    backend = LocalLLMBackend(client_factory=client_factory(handler))
    with pytest.raises(BackendError, match="refused the script-generation request") as raised:
        backend.complete(messages=[{"role": "user", "content": "test"}])
    assert raised.value.retryable is True


def test_authentication_error_never_leaks_key(monkeypatch):
    secret = "do-not-log-this"
    monkeypatch.setenv("LOCAL_LLM_API_KEY", secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"Authorization: Bearer {secret}")

    backend = LocalLLMBackend(client_factory=client_factory(handler))
    with pytest.raises(BackendError) as raised:
        backend.discover_models()
    assert raised.value.code == BackendErrorCode.AUTHENTICATION_FAILED
    assert secret not in str(raised.value)
    assert secret not in str(raised.value.as_dict())


def test_json_repair_is_conservative():
    assert parse_structured_json('preface {"scenes": [1, 2,],} epilogue') == {
        "scenes": [1, 2]
    }
    with pytest.raises(BackendError):
        parse_structured_json("not json")


def test_schema_mismatch_is_distinguished_from_syntax_error():
    class Shape(BaseModel):
        ok: bool

    with pytest.raises(BackendError) as mismatch:
        parse_structured_json('{"nope": 1}', validator=Shape.model_validate)
    assert mismatch.value.code == BackendErrorCode.INVALID_RESPONSE
    assert "does not match the expected structure" in str(mismatch.value)
    assert "malformed structured JSON" not in str(mismatch.value)
    # The first field-level problem is surfaced for the UI.
    assert "ok: Field required" in str(mismatch.value)
    assert "input_value" not in str(mismatch.value.as_dict().get("details"))

    with pytest.raises(BackendError) as unparseable:
        parse_structured_json("not json at all", validator=Shape.model_validate)
    assert "malformed structured JSON" in str(unparseable.value)


def test_redaction_handles_common_header_formats():
    value = redact_secrets("Authorization: Bearer abc123 api_key=xyz token: qwerty")
    assert "abc123" not in value
    assert "xyz" not in value
    assert "qwerty" not in value
