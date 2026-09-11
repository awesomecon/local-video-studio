"""Offline contract tests for the remote Gemini TTS provider.

The API is simulated with ``httpx.MockTransport`` so the suite never contacts
Google and never needs a key. The provider must also work through the real
narration manager: one test runs the full chunk→take flow with a fake API.
"""

from __future__ import annotations

import base64
import json
import os
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.core import load_config
from backend.core.secrets import LocalSecretStore, SecretValidationError
from backend.models.base import GenerationRequest, GenerationResult
from backend.models.errors import BackendError, BackendErrorCode
from backend.models.gemini_tts import GEMINI_TTS_MODELS, GEMINI_VOICES, GeminiTTSBackend
from backend.api.main import create_app
from backend.schemas import ProjectCreate, Scene
from backend.tts.models import NarrationRequest


SAMPLE_RATE = 24000
PCM = b"\x01\x02" * 1200  # 50 ms of L16 mono
TEST_API_KEY = "AIzaSyntheticTestKey123"


def audio_payload(frames: int = 1200, rate: int = SAMPLE_RATE) -> bytes:
    pcm = b"\x01\x02" * frames
    return (
        b'audio/L16;codec=pcm;rate=' + str(rate).encode() + b" " + base64.b64encode(pcm)
    )


def generate_response(frames: int = 1200, *, finish_reason: str = "STOP") -> dict:
    return {
        "candidates": [{
            "content": {"parts": [{
                "inlineData": {
                    "mimeType": f"audio/L16;codec=pcm;rate={SAMPLE_RATE}",
                    "data": base64.b64encode(b"\x01\x02" * frames).decode(),
                },
            }], "role": "model"},
            "finishReason": finish_reason,
        }],
        "usageMetadata": {"promptTokenCount": 12, "audioTokens": 480},
    }


def client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    return factory


def make_backend(tmp_path: Path, handler, *, with_store: bool = True) -> GeminiTTSBackend:
    store = LocalSecretStore(tmp_path / "secrets") if with_store else None
    return GeminiTTSBackend(
        secret_store=store,
        client_factory=client_factory(handler),
    )


def request_to(tmp_path: Path, *, text: str = "Hello, world.",
               voice: str | None = None, style: str | None = None,
               temperature: float | None = None, model: str | None = None,
               references: tuple[Path, ...] = ()) -> GenerationRequest:
    settings = {
        "filename": "speech.wav",
        "voice_name": voice,
        "voice_instruction": style or "",
        "gemini_model": model,
    }
    if temperature is not None:
        settings["temperature"] = temperature
    return GenerationRequest(
        job_id="job", output_dir=tmp_path, prompt=text, seed=7,
        references=references, settings=settings,
    )


def test_generate_sends_key_header_and_expected_wire_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=generate_response())

    backend = make_backend(tmp_path, handler)
    result = backend.generate(request_to(tmp_path, style="warm narrator", temperature=0.7))

    assert seen["header"] == TEST_API_KEY
    assert TEST_API_KEY not in seen["url"]
    assert seen["url"].endswith(
        "/v1beta/models/gemini-3.1-flash-tts-preview:generateContent"
    )
    body = seen["body"]
    sent_prompt = body["contents"][0]["parts"][0]["text"]
    assert sent_prompt == (
        "Perform text-to-speech. Read the narration exactly as written.\n\n"
        "Delivery direction: warm narrator\n\nNarration:\nHello, world."
    )
    assert body["generationConfig"]["responseModalities"] == ["AUDIO"]
    prebuilt = body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
    assert prebuilt == {"voiceName": "Kore"}
    assert body["generationConfig"]["temperature"] == 0.7

    output = result.outputs[0]
    with wave.open(str(output), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 1200
    assert result.metadata["workflow_version"] == "gemini-tts-v1"
    assert result.metadata["backend"] == "gemini_tts"
    assert result.metadata["prompt"] == sent_prompt
    settings = result.metadata["settings"]
    assert settings["voice_name"] == "Kore"
    assert settings["deterministic"] is False
    assert settings["metrics"]["audio_duration_seconds"] == pytest.approx(0.05)
    assert result.peak_vram_gb is None


def test_default_voice_and_voice_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"])
        return httpx.Response(200, json=generate_response())

    backend = make_backend(tmp_path, handler)
    backend.generate(request_to(tmp_path, voice="Charon"))
    backend.generate(request_to(tmp_path))
    assert calls == [{"voiceName": "Charon"}, {"voiceName": "Kore"}]
    # Unknown-but-well-formed voices are allowed: Google's gallery evolves.
    backend.generate(request_to(tmp_path, voice="Tomorrowstar"))
    assert calls[-1] == {"voiceName": "Tomorrowstar"}


def test_reference_audio_is_rejected_before_any_network_call(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=generate_response())

    backend = make_backend(tmp_path, handler)
    reference = tmp_path / "ref.wav"
    reference.write_bytes(b"RIFF")
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path, references=(reference,)))
    assert raised.value.code == BackendErrorCode.INVALID_RESPONSE
    assert "preset voices" in str(raised.value)
    assert calls == []


def test_invalid_voice_name_is_rejected_client_side(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    backend = make_backend(tmp_path, handler=lambda r: httpx.Response(200, json=generate_response()))
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path, voice="lowercase voice"))
    assert raised.value.code == BackendErrorCode.INVALID_RESPONSE
    assert "not a valid preset voice" in str(raised.value)


def test_missing_key_is_actionable_and_never_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=generate_response())

    backend = make_backend(tmp_path, handler, with_store=True)
    assert backend.key_status() == {
        "configured": False, "invalid": False, "source": "none",
        "api_key_env": "GEMINI_API_KEY",
        "secret_file": str(backend.secret_store.path_for("gemini_tts_api_key")),
    }
    health = backend.health()
    assert health["status"] == "key_required"
    assert "GEMINI_API_KEY" in health["detail"]
    assert backend.readiness() == {
        "state": "needs_key",
        "detail": "Needs a Google AI Studio API key (Voice page or GEMINI_API_KEY).",
    }
    with pytest.raises(BackendError) as raised:
        backend.load()
    assert raised.value.code == BackendErrorCode.AUTHENTICATION_FAILED
    assert "Voice page" in str(raised.value)
    assert calls == []


def test_stored_key_roundtrip_precedence_and_clear(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    backend = make_backend(tmp_path, handler=lambda r: httpx.Response(200, json=generate_response()))
    backend.set_api_key("  AIzaStored123456789  ")
    status = backend.key_status()
    assert status["configured"] is True and status["source"] == "file"
    path = backend.secret_store.path_for("gemini_tts_api_key")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert backend.key_status()["secret_file"] == str(path)

    # The environment variable always wins over the stored file.
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    assert backend.key_status()["source"] == "environment"
    monkeypatch.delenv("GEMINI_API_KEY")

    assert backend.clear_api_key() is True
    assert backend.key_status()["source"] == "none"
    assert backend.clear_api_key() is False
    assert backend.health()["status"] == "key_required"


def test_set_api_key_rejects_bad_values_without_writing(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    backend = make_backend(tmp_path, handler=lambda r: httpx.Response(200))
    for bad in ("", "   ", "too short", "has spaces", "line\nbreak"):
        with pytest.raises(SecretValidationError):
            backend.set_api_key(bad)
    assert backend.key_status()["configured"] is False


def test_api_error_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)

    def make(status: int, payload: dict) -> GeminiTTSBackend:
        return make_backend(
            tmp_path, lambda r: httpx.Response(status, json=payload),
        )

    backend = make(400, {"error": {"message": "nope", "status": "INVALID_ARGUMENT"}})
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.INVALID_RESPONSE
    assert "nope" in str(raised.value)

    backend = make(401, {"error": {"message": "bad key", "status": "PERMISSION_DENIED"}})
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.AUTHENTICATION_FAILED

    backend = make(404, {"error": {"message": "not found", "status": "NOT_FOUND"}})
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.MODEL_UNAVAILABLE

    backend = make(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.BACKEND_UNAVAILABLE
    assert raised.value.retryable is True

    backend = make(503, {"error": {"message": "boom", "status": "UNAVAILABLE"}})
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.BACKEND_UNAVAILABLE
    assert raised.value.retryable is True


def test_auth_error_never_leaks_the_key(tmp_path, monkeypatch):
    secret = "do-not-log-this-key"
    monkeypatch.setenv("GEMINI_API_KEY", secret)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == secret
        return httpx.Response(403, json={"error": {
            "message": f"API key not valid. Key: {secret}",
            "status": "PERMISSION_DENIED",
        }})

    backend = make_backend(tmp_path, handler)
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.AUTHENTICATION_FAILED
    assert secret not in str(raised.value)
    assert secret not in str(raised.value.as_dict())
    assert secret not in repr(backend)


def test_malformed_environment_key_is_rejected_without_a_network_call(tmp_path, monkeypatch):
    secret = "synthetic-secret-marker"
    monkeypatch.setenv("GEMINI_API_KEY", f"prefix\n{secret}")
    calls: list[str] = []
    backend = make_backend(
        tmp_path,
        lambda request: calls.append(str(request.url)) or httpx.Response(200),
    )

    assert backend.health()["status"] == "key_invalid"
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.AUTHENTICATION_FAILED
    assert secret not in str(raised.value.as_dict())
    assert calls == []


def test_transport_errors_redact_the_key(tmp_path, monkeypatch):
    secret = "synthetic-transport-key"
    monkeypatch.setenv("GEMINI_API_KEY", secret)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError(f"bad header {secret}")

    backend = make_backend(tmp_path, handler)
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.SERVER_NOT_RUNNING
    assert secret not in str(raised.value.as_dict())


def test_safety_block_and_empty_and_text_only_responses(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)

    def with_response(payload: dict) -> GeminiTTSBackend:
        return make_backend(tmp_path, lambda r: httpx.Response(200, json=payload))

    blocked = with_response({
        "candidates": [{
            "content": {"parts": []}, "finishReason": "SAFETY",
        }],
        "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
    })
    with pytest.raises(BackendError) as raised:
        blocked.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.INVALID_RESPONSE
    assert "PROHIBITED_CONTENT" in str(raised.value)

    truncated = with_response({
        "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
    })
    with pytest.raises(BackendError) as raised:
        truncated.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.INVALID_RESPONSE
    assert raised.value.retryable is True
    assert "chunk" in str(raised.value).lower()

    text_only = with_response({
        "candidates": [{"content": {"parts": [{"text": "I cannot do that."}]},
                        "finishReason": "STOP"}],
    })
    with pytest.raises(BackendError) as raised:
        text_only.generate(request_to(tmp_path))
    assert raised.value.code == BackendErrorCode.INVALID_RESPONSE
    assert raised.value.retryable is True

    empty = with_response({"candidates": []})
    with pytest.raises(BackendError) as raised:
        empty.generate(request_to(tmp_path))
    assert "no audio" in str(raised.value)

    assert with_response(generate_response()).generate(
        request_to(tmp_path)
    ).outputs[0].stat().st_size > 44


def test_disabled_backend_refuses_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    backend = make_backend(tmp_path, handler=lambda r: httpx.Response(200, json=generate_response()))
    backend.enabled = False
    assert backend.descriptor().device == "disabled"
    with pytest.raises(BackendError) as raised:
        backend.load()
    assert raised.value.code == BackendErrorCode.BACKEND_UNAVAILABLE
    assert backend.health()["status"] == "not_configured"
    assert backend.readiness()["state"] == "off"
    assert backend.cancel("job") is False
    assert backend.estimate_resources(request_to(tmp_path))["remote_service"] is True


def test_voice_gallery_covers_documented_presets() -> None:
    names = [name for name, _ in GEMINI_VOICES]
    for expected in ("Kore", "Puck", "Charon", "Fenrir", "Sulafat"):
        assert expected in names
    assert len(names) == len(set(names))


def test_model_gallery_covers_curated_models() -> None:
    ids = [model_id for model_id, _ in GEMINI_TTS_MODELS]
    assert ids == [
        "gemini-3.1-flash-tts-preview",
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-preview-tts",
    ]
    assert len(ids) == len(set(ids))


def test_model_override_uses_request_model_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=generate_response())

    backend = make_backend(tmp_path, handler)
    default = backend.generate(request_to(tmp_path))
    assert seen["url"].endswith("/v1beta/models/gemini-3.1-flash-tts-preview:generateContent")
    assert default.metadata["settings"]["gemini_model"] == "gemini-3.1-flash-tts-preview"

    override = backend.generate(request_to(tmp_path, model="gemini-2.5-pro-preview-tts"))
    assert seen["url"].endswith("/v1beta/models/gemini-2.5-pro-preview-tts:generateContent")
    assert override.metadata["settings"]["gemini_model"] == "gemini-2.5-pro-preview-tts"
    assert override.metadata["model"] == "Google gemini-2.5-pro-preview-tts"
    assert override.metadata["settings"]["deterministic"] is False

    # Unknown-but-well-formed IDs pass through; Google answers 404 clearly.
    backend.generate(request_to(tmp_path, model="gemini-9.9-future-tts"))
    assert seen["url"].endswith("/v1beta/models/gemini-9.9-future-tts:generateContent")


def test_invalid_model_identifier_is_rejected_client_side(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=generate_response())

    backend = make_backend(tmp_path, handler)
    with pytest.raises(BackendError) as raised:
        backend.generate(request_to(tmp_path, model="not a model!!"))
    assert raised.value.code == BackendErrorCode.INVALID_RESPONSE
    assert "not a valid model identifier" in str(raised.value)
    assert calls == []


# --------------------------------------------------------------------- API

def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mock: bool):
    app = create_app(
        load_config(environ={
            # Keep the local secret store inside the test sandbox: tests must
            # never read, overwrite, or delete the user's real stored key.
            "LOCAL_VIDEO_STUDIO__paths__app_data": str(tmp_path / "app_data"),
        }),
        database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=mock,
    )
    service = app.state.service
    monkeypatch.setattr(service.tts_workers, "ensure_running", lambda provider: False)
    monkeypatch.setattr(service.tts_workers, "stop", lambda provider: False)
    return app


def test_api_reports_key_status_without_the_key(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, mock=True)
    client = TestClient(app)
    with client:
        status = client.get("/api/tts/gemini/key").json()
        assert status["configured"] is False
        assert status["source"] == "none"
        assert status["enabled"] is True

        saved = client.put("/api/tts/gemini/key", json={"api_key": "AIzaStored1234567890"})
        assert saved.status_code == 204
        status = client.get("/api/tts/gemini/key").json()
        assert status["configured"] is True
        assert status["source"] == "file"

        bad = client.put("/api/tts/gemini/key", json={"api_key": "short"})
        assert bad.status_code == 422
        assert "too short" in bad.json()["detail"]

        cleared = client.delete("/api/tts/gemini/key")
        assert cleared.status_code == 204
        assert client.get("/api/tts/gemini/key").json()["source"] == "none"


def test_api_models_endpoint_lists_gemini_with_honest_health(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app = _app(tmp_path, monkeypatch, mock=True)
    client = TestClient(app)
    with client:
        payload = client.get("/api/tts/models").json()
        entry = payload["models"]["gemini_tts"]
        assert entry["health"]["status"] == "key_required"
        assert entry["readiness"]["state"] == "needs_key"
        assert entry["managed"] is False


def test_api_models_entry_advertises_gemini_model_gallery(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app = _app(tmp_path, monkeypatch, mock=True)
    client = TestClient(app)
    with client:
        payload = client.get("/api/tts/models").json()
        entry = payload["models"]["gemini_tts"]
        assert [item["id"] for item in entry["gemini_models"]] == [
            "gemini-3.1-flash-tts-preview",
            "gemini-2.5-flash-preview-tts",
            "gemini-2.5-pro-preview-tts",
        ]
        assert entry["default_model"] == "gemini-3.1-flash-tts-preview"


def test_api_rejects_gemini_narration_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app = _app(tmp_path, monkeypatch, mock=False)
    project = app.state.service.create_project(ProjectCreate(
        title="Gemini", topic="t", target_duration=1,
    ))
    client = TestClient(app)
    with client:
        response = client.post(
            f"/api/projects/{project.id}/tts/generate",
            json={"provider": "gemini_tts", "text": "Hello."},
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "API key" in detail and "GEMINI_API_KEY" in detail


def test_api_rejects_gemini_with_a_voice_profile(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, mock=True)
    project = app.state.service.create_project(ProjectCreate(
        title="Gemini profile", topic="t", target_duration=1,
    ))
    client = TestClient(app)
    with client:
        response = client.post(
            f"/api/projects/{project.id}/tts/generate",
            json={"provider": "gemini_tts", "voice_profile_id": "abc", "text": "Hello."},
        )
    assert response.status_code == 422
    assert "preset voices" in response.json()["detail"]


def test_full_narration_flow_with_real_backend_and_fake_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    app = _app(tmp_path, monkeypatch, mock=True)
    service = app.state.service
    backend = make_backend(
        tmp_path,
        lambda request: httpx.Response(200, json=generate_response(frames=2400)),
    )
    service.registry.register(backend, name="gemini_tts", replace=True)

    project = service.create_project(ProjectCreate(
        title="Gemini flow", topic="t", target_duration=2,
    ))
    scenes = [
        Scene(project_id=project.id, index=index, duration=1, narration=text)
        for index, text in enumerate(("First scene line.", "Second scene line."))
    ]
    for scene in scenes:
        service.database.save_scene(scene)
        service.store.save_scene(project.slug, scene)

    service.tts.generate(
        project.id,
        NarrationRequest(provider="gemini_tts", voice_profile_id=None, chunk_seconds=5),
        job_id="gemini-flow",
    )

    takes, active_id = service.tts.list_narration_takes(project.id)
    take = next(item for item in takes if item.id == active_id)
    assert take.backend == "gemini_tts"
    assert take.filepath.name == "gemini-flow.wav"
    chunks = service.tts.list_take_chunks(project.id, take.id)
    assert [chunk["text"] for chunk in chunks] == [scene.narration for scene in scenes]
    for chunk in chunks:
        assert chunk["provider"] == "gemini_tts"
        assert chunk["filepath"].startswith("audio/gemini/")
        assert chunk["duration"] > 0
    # Per-chunk sidecars keep provenance honest for a non-deterministic API.
    project_root = tmp_path / "projects" / project.slug
    for index, _ in enumerate(scenes, start=1):
        sidecar = json.loads((project_root / "audio" / "gemini" / "gemini-flow"
                              / f"{index:04d}.json").read_text(encoding="utf-8"))
        assert sidecar["status"] == "completed"
        assert sidecar["voice_profile_id"] is None
        assert sidecar["settings"]["voice_name"] == "Kore"
        assert sidecar["settings"]["deterministic"] is False
        assert sidecar["settings"]["metrics"]["sample_rate"] == SAMPLE_RATE
    # Deterministic audio keeps the take joinable; provenance stays honest.
    assert (project_root / take.filepath).is_file()
    assert take.filepath.as_posix() == "narration/takes/gemini_tts/gemini-flow.wav"


def test_full_narration_flow_honors_per_request_model_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", TEST_API_KEY)
    seen: list[str] = []
    app = _app(tmp_path, monkeypatch, mock=True)
    service = app.state.service

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=generate_response(frames=2400))

    service.registry.register(
        make_backend(tmp_path, handler), name="gemini_tts", replace=True,
    )

    project = service.create_project(ProjectCreate(
        title="Gemini model", topic="t", target_duration=2,
    ))
    scene = Scene(project_id=project.id, index=0, duration=1, narration="One line.")
    service.database.save_scene(scene)
    service.store.save_scene(project.slug, scene)

    service.tts.generate(
        project.id,
        NarrationRequest(provider="gemini_tts", voice_profile_id=None, chunk_seconds=5,
                         gemini_model="gemini-2.5-pro-preview-tts"),
        job_id="gemini-model",
    )

    assert seen and all("gemini-2.5-pro-preview-tts" in url for url in seen)
    project_root = tmp_path / "projects" / project.slug
    sidecar = json.loads((project_root / "audio" / "gemini" / "gemini-model"
                          / "0001.json").read_text(encoding="utf-8"))
    assert sidecar["status"] == "completed"
    assert sidecar["settings"]["gemini_model"] == "gemini-2.5-pro-preview-tts"
    assert sidecar["model"] == "Google gemini-2.5-pro-preview-tts"
    takes, active_id = service.tts.list_narration_takes(project.id)
    take = next(item for item in takes if item.id == active_id)
    assert take.model == "Google gemini-2.5-pro-preview-tts"
