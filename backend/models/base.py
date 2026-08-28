"""Stable contract implemented by every local or local-service generator backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class Capability(StrEnum):
    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"
    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    TEXT_TO_SPEECH = "text_to_speech"
    TEXT_TO_MUSIC = "text_to_music"
    SPEECH_TO_TEXT = "speech_to_text"
    AUDIO_VIDEO_GENERATION = "audio_video_generation"
    TEXT_GENERATION = "text_generation"


@dataclass(frozen=True, slots=True)
class BackendDescriptor:
    backend_name: str
    model_name: str
    model_version: str = "unknown"
    quantization: str | None = None
    device: str = "external"
    vram_required_gb: float = 0.0
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    supported_inputs: tuple[str, ...] = ()
    supported_outputs: tuple[str, ...] = ()
    heavyweight: bool = False


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    job_id: str
    output_dir: Path
    prompt: str
    negative_prompt: str = ""
    seed: int = 0
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    references: tuple[Path, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    outputs: tuple[Path, ...]
    metadata: Mapping[str, Any]
    peak_vram_gb: float | None = None


class GeneratorBackend(ABC):
    """Lifecycle and generation API. Implementations must release real model references."""

    @abstractmethod
    def descriptor(self) -> BackendDescriptor: ...

    @abstractmethod
    def health(self) -> Mapping[str, Any]: ...

    def capabilities(self) -> frozenset[Capability]:
        return self.descriptor().capabilities

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def unload(self) -> None: ...

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResult: ...

    @abstractmethod
    def cancel(self, job_id: str) -> bool: ...

    @abstractmethod
    def estimate_resources(self, request: GenerationRequest) -> Mapping[str, Any]: ...
