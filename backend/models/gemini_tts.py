"""Remote Google Gemini TTS adapter.

Local Video Studio is local-first, and this is its one cloud TTS provider:
when a user picks it and starts generating, *narration text only* is sent to
Google's Gemini API. No reference audio, media, project files, or voice
samples ever leave the machine, and nothing is sent unless the user supplies
their own Google AI Studio API key (environment variable, or a key saved
from the Voice page, which is stored in a mode-0600 file in the application
data directory).

The adapter is stateless (nothing to load or unload), deterministic-seed
agnostic (the API does not accept seeds; requested seeds are still recorded
in provenance metadata with ``deterministic: false``), and speaks only the
public REST surface through ``httpx`` — no new dependencies.
"""

from __future__ import annotations

import base64
import os
import re
import wave
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from backend.core.secrets import LocalSecretStore, validate_api_key
from .base import (
    BackendDescriptor,
    Capability,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
)
from .errors import BackendError, BackendErrorCode, redact_secrets

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_API_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_VOICE = "Kore"
SECRET_STORE_NAME = "gemini_tts_api_key"

# Preset voices from Google's Gemini voice gallery (checked 2026-09). Google
# may add or retire voices, so names outside this list are still accepted
# when they are well-formed; the API reports unknown voices with a clear
# error.
GEMINI_VOICES: tuple[tuple[str, str], ...] = (
    ("Puck", "upbeat, engaging"),
    ("Charon", "informative, composed"),
    ("Kore", "firm, steady"),
    ("Fenrir", "excitable, high energy"),
    ("Leda", "youthful, bright"),
    ("Orus", "firm, assertive"),
    ("Aoede", "breezy, lighthearted"),
    ("Callirrhoe", "airy, gentle"),
    ("Autonoe", "clear, neutral"),
    ("Enceladus", "breathy, intimate"),
    ("Iapetus", "friendly, conversational"),
    ("Umbriel", "neutral, balanced"),
    ("Algieba", "smooth, assured"),
    ("Despina", "soft, calm"),
    ("Erinome", "gentle, soothing"),
    ("Algenib", "easy-going, relaxed"),
    ("Rasalgethi", "clear, articulate"),
    ("Laomedeia", "bright, melodic"),
    ("Achernar", "soft, mellow"),
    ("Alnilam", "easy-going, natural"),
    ("Schedar", "even, measured"),
    ("Gacrux", "refined, precise"),
    ("Pulcherrima", "warm, inviting"),
    ("Achird", "friendly, approachable"),
    ("Zubenelgenubi", "discreet, low"),
    ("Vindemiatrix", "gentle, delicate"),
    ("Sadachbia", "lively, animated"),
    ("Sadaltager", "polished, smooth"),
    ("Sulafat", "warm, expressive"),
)
KNOWN_GEMINI_VOICE_NAMES: frozenset[str] = frozenset(name for name, _ in GEMINI_VOICES)
_VOICE_NAME_SHAPE = re.compile(r"^[A-Z][A-Za-z]{2,31}$")
_RATE_RE = re.compile(r"rate\s*=\s*(\d{2,6})")


class GeminiTTSBackend(GeneratorBackend):
    """Stateless client for the remote Google Gemini TTS API."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 180.0,
        enabled: bool = True,
        secret_store: LocalSecretStore | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self.model = model
        self.voice = voice
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled
        self.secret_store = secret_store
        self._client_factory = client_factory

    def __repr__(self) -> str:
        return (
            f"GeminiTTSBackend(model={self.model!r}, voice={self.voice!r}, "
            f"api_key_env={self.api_key_env!r}, base_url={self.base_url!r})"
        )

    # ------------------------------------------------------------------ keys

    def key_status(self) -> Mapping[str, Any]:
        """Honest, value-free key configuration report."""

        if os.environ.get(self.api_key_env, "").strip():
            source = "environment"
        elif self.secret_store is not None and self.secret_store.exists(SECRET_STORE_NAME):
            source = "file"
        else:
            source = "none"
        return {
            "configured": source != "none",
            "source": source,
            "api_key_env": self.api_key_env,
            "secret_file": (
                str(self.secret_store.path_for(SECRET_STORE_NAME))
                if self.secret_store is not None
                else None
            ),
        }

    def _api_key(self) -> str:
        value = os.environ.get(self.api_key_env, "").strip()
        if value:
            return value
        if self.secret_store is not None:
            value = self.secret_store.read(SECRET_STORE_NAME) or ""
        if value:
            return value
        raise BackendError(
            BackendErrorCode.AUTHENTICATION_FAILED,
            "Gemini TTS needs a Google AI Studio API key. Add one in the Voice "
            f"page, or set the {self.api_key_env} environment variable.",
        )

    def set_api_key(self, value: str) -> None:
        if self.secret_store is None:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "This install has no local secret store; set "
                f"{self.api_key_env} in the environment instead.",
            )
        # Validate before touching disk; nothing is written on failure.
        validate_api_key(value)
        self.secret_store.write(SECRET_STORE_NAME, value)

    def clear_api_key(self) -> bool:
        if self.secret_store is None:
            return False
        return self.secret_store.delete(SECRET_STORE_NAME)

    # ------------------------------------------------------------- descriptor

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name="gemini_tts",
            model_name=f"Google {self.model}",
            model_version="remote-api",
            device="remote-google-api" if self.enabled else "disabled",
            vram_required_gb=0.0,
            capabilities=frozenset({Capability.TEXT_TO_SPEECH}),
            supported_inputs=("text", "voice_name", "style_prompt", "temperature"),
            supported_outputs=("wav", "generation_metrics"),
            heavyweight=False,
        )

    def health(self) -> Mapping[str, Any]:
        """Local, value-free health: no network contact is made here."""

        status = self.key_status()
        if not self.enabled:
            return {
                "status": "not_configured",
                "backend": "gemini_tts",
                "detail": "Gemini TTS is disabled in the configuration.",
                **status,
            }
        if not status["configured"]:
            return {
                "status": "key_required",
                "backend": "gemini_tts",
                "remote": True,
                "detail": (
                    "Gemini TTS is a remote Google API. Set a Google AI Studio "
                    f"key (Voice page, or {self.api_key_env}) to use it."
                ),
                **status,
            }
        return {
            "status": "healthy",
            "backend": "gemini_tts",
            "remote": True,
            "model": self.model,
            "detail": "Ready. Narration text is sent to Google's Gemini API on generate.",
            **status,
        }

    def readiness(self) -> dict[str, str]:
        status = self.health()
        if status["status"] == "healthy":
            return {"state": "ready", "detail": "Ready."}
        if status["status"] == "key_required":
            return {
                "state": "needs_key",
                "detail": "Needs a Google AI Studio API key (Voice page or "
                          f"{self.api_key_env}).",
            }
        return {"state": "off", "detail": "Disabled in the configuration."}

    # ------------------------------------------------------------- lifecycle

    def load(self) -> None:
        if not self.enabled:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "Gemini TTS is disabled in the configuration.",
            )
        # Fail fast, with an actionable message, before any chunk is attempted.
        self._api_key()

    def unload(self) -> None:
        return None

    def cancel(self, job_id: str) -> bool:
        # The API has no job surface; an in-flight request finishes or times
        # out on its own. The manager treats non-cancellable backends fine.
        return False

    def reset_cancel(self, job_id: str) -> None:
        return None

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        del request
        return {
            "estimated_vram_gb": 0.0,
            "remote_service": True,
            "heavyweight": False,
        }

    # -------------------------------------------------------------- request

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> httpx.Response:
        key = self._api_key()
        url = f"{self.base_url}{path}"
        try:
            with self._client_factory(timeout=self.timeout_seconds) as client:
                response = client.request(
                    method, url, json=json,
                    headers={"x-goog-api-key": key},
                )
        except httpx.TimeoutException as exc:
            raise BackendError(
                BackendErrorCode.REQUEST_TIMEOUT,
                f"Gemini TTS did not finish within {self.timeout_seconds:g}s; "
                "long takes are split into chunks, so a shorter chunk limit helps.",
                retryable=True,
                details=exc,
            ) from None
        except httpx.NetworkError as exc:
            raise BackendError(
                BackendErrorCode.SERVER_NOT_RUNNING,
                "Could not reach the Gemini TTS API (network or DNS).",
                retryable=True,
                details=exc,
            ) from None
        if response.status_code >= 400:
            raise self._api_error(response)
        return response

    def _api_error(self, response: httpx.Response) -> BackendError:
        detail = ""
        error_status = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            error = payload["error"]
            detail = str(error.get("message", ""))[:500]
            error_status = str(error.get("status", "")).upper()
        if not detail:
            detail = redact_secrets(response.text[:500])
        message = f"Gemini TTS API returned HTTP {response.status_code}."
        if detail:
            message = f"{message} {detail}"
        if response.status_code in {401, 403} or error_status in {"PERMISSION_DENIED", "API_KEY_INVALID"}:
            return BackendError(
                BackendErrorCode.AUTHENTICATION_FAILED,
                "Google rejected the Gemini API key; check it in the Voice page or "
                f"in {self.api_key_env}.",
            )
        if response.status_code == 429 or error_status == "RESOURCE_EXHAUSTED":
            return BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "Gemini TTS quota or rate limit was hit; wait a bit and retry.",
                retryable=True,
            )
        if response.status_code == 404:
            return BackendError(
                BackendErrorCode.MODEL_UNAVAILABLE,
                f"Gemini TTS model {self.model!r} was not found; check the model "
                "identifier in the configuration.",
            )
        if response.status_code >= 500:
            return BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                message,
                retryable=True,
                details=detail or None,
            )
        return BackendError(
            BackendErrorCode.INVALID_RESPONSE,
            message,
            details=detail or None,
        )

    # -------------------------------------------------------------- generate

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.references:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Gemini TTS speaks its preset voices only; it cannot clone a "
                "reference audio. Clear the voice profile and pick a Gemini "
                "voice instead.",
            )
        settings = dict(request.settings)
        voice = str(settings.get("voice_name") or settings.get("gemini_voice") or self.voice)
        if not _VOICE_NAME_SHAPE.fullmatch(voice):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"Gemini TTS voice {voice!r} is not a valid preset voice name.",
            )
        style_prompt = str(settings.get("voice_instruction") or "").strip()[:500]
        temperature = settings.get("temperature")
        if temperature is not None and not 0.0 <= float(temperature) <= 2.0:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Gemini TTS temperature must be between 0.0 and 2.0.",
            )

        text = request.prompt.strip()
        if not text:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE, "Gemini TTS received empty narration text.",
            )
        prebuilt: dict[str, Any] = {"voiceName": voice}
        if style_prompt:
            prebuilt["stylePrompt"] = style_prompt
        generation_config: dict[str, Any] = {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": prebuilt}},
        }
        if temperature is not None:
            generation_config["temperature"] = float(temperature)
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": generation_config,
        }

        output_dir = request.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / str(settings.get("filename", "speech.wav"))

        response = self._request(
            "POST",
            f"/models/{self.model}:generateContent",
            json=body,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Gemini TTS returned non-JSON output.",
                details=exc,
            ) from None
        if not isinstance(data, dict):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE, "Gemini TTS returned an invalid payload.",
            )

        candidates = data.get("candidates")
        prompt_feedback = data.get("promptFeedback")
        if (isinstance(prompt_feedback, dict)
                and prompt_feedback.get("blockReason")):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"Gemini TTS declined the text ({prompt_feedback['blockReason']}); "
                "try rephrasing the narration line.",
            )
        if not isinstance(candidates, list) or not candidates:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Gemini TTS returned no audio for this text; try rephrasing the "
                "narration line or a different voice.",
            )
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE, "Gemini TTS returned an invalid candidate.",
            )
        finish_reason = str(candidate.get("finishReason", "UNKNOWN"))
        content = candidate.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            parts = []

        audio = None
        mime_type = ""
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                audio = str(inline["data"])
                mime_type = str(inline.get("mimeType") or inline.get("mime_type") or "")
                break
        if audio is None:
            if finish_reason in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"}:
                raise BackendError(
                    BackendErrorCode.INVALID_RESPONSE,
                    f"Gemini TTS blocked this narration line ({finish_reason}); "
                    "rephrase the line and retry.",
                )
            if finish_reason == "MAX_TOKENS":
                raise BackendError(
                    BackendErrorCode.INVALID_RESPONSE,
                    "Gemini TTS ran out of output capacity for this chunk; lower "
                    "the chunk limit (e.g. 15-20 seconds) and retry.",
                    retryable=True,
                )
            if isinstance(parts, list) and parts and isinstance(parts[0], dict) \
                    and parts[0].get("text") is not None:
                raise BackendError(
                    BackendErrorCode.INVALID_RESPONSE,
                    "Gemini TTS answered with text instead of audio for this "
                    "chunk; the audio-native model can do this intermittently. "
                    "Retry the chunk, or lower the chunk limit.",
                    retryable=True,
                )
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                f"Gemini TTS returned no audio (finish reason: {finish_reason}).",
            )

        sample_rate = 24000
        match = _RATE_RE.search(mime_type.replace(";", " "))
        if match:
            sample_rate = int(match.group(1))
        try:
            pcm = base64.b64decode(audio, validate=False)
        except (ValueError, TypeError) as exc:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Gemini TTS returned undecodable audio data.",
                details=exc,
            ) from None
        if not pcm:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Gemini TTS returned empty audio for this chunk.",
            )
        if len(pcm) % 2:
            pcm = pcm[:-1]  # L16 is two bytes per sample; drop a stray byte
        _write_pcm_wav(output, pcm, sample_rate)

        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        descriptor = self.descriptor()
        metrics = {
            "deterministic": False,
            "note": (
                "Gemini TTS does not accept seeds; takes are reproducible in "
                "intent, not bit-for-bit."
            ),
            "sample_rate": sample_rate,
            "frames": len(pcm) // 2,
            "audio_duration_seconds": round((len(pcm) // 2) / sample_rate, 3),
            "finish_reason": finish_reason,
            "prompt_tokens": usage.get("promptTokenCount"),
            "audio_tokens": usage.get("audioTokens") or usage.get("candidatesTokenCount"),
        }
        return GenerationResult(
            outputs=(output,),
            metadata={
                "backend": "gemini_tts",
                "model": descriptor.model_name,
                "model_version": descriptor.model_version,
                "workflow_version": "gemini-tts-v1",
                "seed": request.seed,
                "prompt": request.prompt,
                "settings": {
                    **settings,
                    "voice_name": voice,
                    "style_prompt": style_prompt or None,
                    "deterministic": False,
                    "metrics": metrics,
                },
            },
            peak_vram_gb=None,
        )


def _write_pcm_wav(path: Path | str, pcm: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
