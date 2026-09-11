"""Portable TTS request and voice-profile models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.schemas import utc_now


TTSProviderName = Literal[
    "qwen_tts", "step_audio_editx", "chatterbox",
    "fish_s2_pro", "voxcpm2", "omnivoice", "index_tts_2_5", "breeze_tts_2",
    "higgs_tts_3", "gemini_tts",
]
QwenSpeaker = Literal[
    "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee",
]


class VoiceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    project_id: str
    name: str = Field(min_length=1, max_length=100)
    reference_audio: Path
    reference_transcript: str = ""
    language: str = "en"
    authorized: bool
    gain_db: float = Field(default=0.0, ge=0, le=24)
    audio_sha256: str
    source_audio_sha256: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("reference_audio")
    @classmethod
    def portable_reference_audio(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("reference audio must be project-relative")
        return value


class NarrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: TTSProviderName = "qwen_tts"
    voice_profile_id: str | None = None
    text: str | None = None
    language: str = "en"
    chunk_seconds: float | None = Field(default=None, ge=5, le=180)
    # Planned narration normally creates one or more chunks per scene so the
    # measured audio duration can drive exact scene timing. Short scripts may
    # opt into one continuous chunking pass for more natural TTS delivery;
    # their scene clock is then projected proportionally across the take.
    combine_scene_chunks: bool = Field(
        default=False,
        description=(
            "Join neighboring planned scenes before applying the selected provider's "
            "chunk-duration limit. Supported by every TTS provider."
        ),
    )
    pause_ms: int = Field(default=350, ge=0, le=5000)
    seed: int = Field(default=20001, ge=0, le=2**63 - 1)
    unload_after: bool = True
    enhance_with_step: bool = False
    step_edit_type: str = "emotion"
    step_instruction: str = ""
    exaggeration: float = Field(default=0.5, ge=0, le=2)
    cfg_weight: float = Field(default=0.5, ge=0, le=1)
    temperature: float = Field(default=0.8, gt=0, le=2)
    speaker: QwenSpeaker = "Ryan"
    voice_instruction: str = Field(default="", max_length=500)
    # Provider-specific controls for the four comparison providers. `None`
    # means "use the model/workflow default"; unsupported values are rejected
    # by the provider adapter instead of being silently ignored.
    guidance_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    inference_timesteps: int | None = Field(default=None, ge=1, le=200)
    num_steps: int | None = Field(default=None, ge=1, le=128)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    # Breeze TTS 2 execution engine (ignored by the other providers):
    # "eager" ≈7.7 GiB VRAM, "fast" ≈14.4 GiB and needs ~20 GiB free.
    breeze_mode: Literal["eager", "fast"] = "eager"
    # Google Gemini TTS preset voice (ignored by the other providers). Gemini
    # speaks preset voices only and cannot clone a reference audio; it also
    # requires a user-supplied API key, so it works like a built-in voice.
    gemini_voice: str | None = Field(default=None, max_length=32, pattern=r"^[A-Z][A-Za-z]{2,31}$")
    # Apply this provider's delivery-tag script to matching scene segments
    # for Fish S2 Pro and Higgs TTS 3. Other providers receive clean text.
    use_performance_tags: bool = False
