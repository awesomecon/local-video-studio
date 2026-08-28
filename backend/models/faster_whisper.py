"""Optional, local-only Faster-Whisper narration alignment backend."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping

from backend.captions import AlignmentResult, CaptionWord

from .base import BackendDescriptor, Capability, GenerationRequest, GenerationResult, GeneratorBackend
from .errors import BackendError, BackendErrorCode


_MIN_WORD_DURATION_SECONDS = 0.01


def _caption_word(start: object, end: object, text: object) -> CaptionWord:
    """Convert a Whisper word while tolerating its zero-duration boundary tokens."""

    start_seconds = float(start)
    end_seconds = float(end)
    if end_seconds == start_seconds:
        # Faster-Whisper can place a short word exactly on a segment boundary
        # and report identical start/end values. Keep the recognized word and
        # give it one ASS timestamp tick instead of rejecting the full
        # transcript. Reversed and otherwise invalid timestamps still fail.
        end_seconds += _MIN_WORD_DURATION_SECONDS
    return CaptionWord(start_seconds, end_seconds, str(text))


class FasterWhisperBackend(GeneratorBackend):
    """Align local narration audio without implicit package or model downloads."""

    backend_name = "whisper"
    model_name = "Whisper large-v3-turbo"
    model_version = "large-v3-turbo"

    def __init__(self, model_path: Path | None, *, device: str = "cuda") -> None:
        self.model_path = model_path
        self.device = device if device in {"cuda", "cpu"} else "cpu"
        self._model: Any | None = None

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name=self.backend_name,
            model_name=self.model_name,
            model_version=self.model_version,
            quantization="float16" if self.device == "cuda" else "int8",
            device=self.device,
            vram_required_gb=5.0 if self.device == "cuda" else 0.0,
            capabilities=frozenset({Capability.SPEECH_TO_TEXT}),
            supported_inputs=("audio", "language"),
            supported_outputs=("segments", "words", "srt", "ass"),
            heavyweight=False,
        )

    def health(self) -> Mapping[str, Any]:
        if self.model_path is None or not self.model_path.is_dir():
            return {
                "status": "not_configured",
                "backend": self.backend_name,
                "install_guidance": "Set backends.whisper.model_path to a local Faster-Whisper "
                "large-v3-turbo model directory. The application will not download model weights.",
            }
        if importlib.util.find_spec("faster_whisper") is None:
            return {
                "status": "incompatible",
                "backend": self.backend_name,
                "install_guidance": "Install the optional local caption dependency: "
                "pip install 'local-video-studio[captions]'.",
            }
        return {
            "status": "healthy",
            "backend": self.backend_name,
            "model_path": str(self.model_path),
            "device": self.device,
        }

    def load(self) -> None:
        health = self.health()
        if health["status"] != "healthy":
            raise BackendError(BackendErrorCode.BACKEND_UNAVAILABLE, str(health["install_guidance"]))
        try:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                str(self.model_path),
                device=self.device,
                compute_type="float16" if self.device == "cuda" else "int8",
                local_files_only=True,
            )
        except Exception as exc:
            self._model = None
            raise BackendError(
                BackendErrorCode.MODEL_UNAVAILABLE,
                "Could not load the configured local Whisper alignment model.",
                details=exc,
            ) from exc

    def unload(self) -> None:
        self._model = None

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if self._model is None:
            raise BackendError(BackendErrorCode.BACKEND_UNAVAILABLE, "Whisper alignment model is not loaded.")
        if len(request.references) != 1 or not request.references[0].is_file():
            raise BackendError(BackendErrorCode.INVALID_RESPONSE, "Whisper alignment requires one local audio file.")
        language = request.settings.get("language")
        try:
            segments, info = self._model.transcribe(
                str(request.references[0]),
                language=str(language) if language else None,
                word_timestamps=True,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            words = tuple(
                _caption_word(word.start, word.end, word.word)
                for segment in segments
                for word in (segment.words or [])
                if word.start is not None and word.end is not None and str(word.word).strip()
            )
        except Exception as exc:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Whisper could not derive word timestamps from narration.",
                details=exc,
            ) from exc
        if not words:
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "Whisper returned no spoken-word timestamps for the narration.",
            )
        alignment = AlignmentResult(
            words=words,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
        )
        output = request.output_dir / "word-timings.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(alignment.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        descriptor = self.descriptor()
        return GenerationResult(
            outputs=(output,),
            metadata={
                "backend": self.backend_name,
                "model": descriptor.model_name,
                "model_version": descriptor.model_version,
                "quantization": descriptor.quantization,
                "workflow_version": "faster-whisper-caption-alignment-v1",
                "seed": request.seed,
                "settings": {
                    "audio_derived": True,
                    "word_timestamps": True,
                    "vad_filter": True,
                    "language": alignment.language,
                    "language_probability": alignment.language_probability,
                },
            },
            peak_vram_gb=None,
        )

    def cancel(self, job_id: str) -> bool:
        del job_id
        return False

    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]:
        del request
        return {"estimated_vram_gb": self.descriptor().vram_required_gb, "heavyweight": False}
