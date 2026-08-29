"""Validated, serialization-friendly application domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)


# Portable project/plan payload version. Version 2 introduces shots; files
# written before the field existed load with the current default and are never
# rewritten eagerly.
PROJECT_SCHEMA_VERSION = 2


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    GENERATING = "generating"
    RENDERING = "rendering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class SceneStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    GENERATING = "generating"
    GENERATED = "generated"
    APPROVED = "approved"
    LOCKED = "locked"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    LOADING_MODEL = "loading_model"
    GENERATING = "generating"
    POSTPROCESSING = "postprocessing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class AssetType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    NARRATION = "narration"
    MUSIC = "music"
    SUBTITLE = "subtitle"
    THUMBNAIL = "thumbnail"
    METADATA = "metadata"


class VisualType(StrEnum):
    GRAPHIC_SCREEN = "graphic_screen"
    TEXT_OVERLAY_STILL = "text_overlay_still"
    H3_AUDIOVISUAL = "h3_audiovisual"
    H3_REFERENCE = "h3_reference"
    WAN_VIDEO = "wan_video"
    KREA2_STILL = "krea2_still"
    IDEOGRAM4_STILL = "ideogram4_still"
    QWEN_IMAGE_STILL = "qwen_image_still"
    FLUX_STILL = "flux_still"
    IMAGE_MOTION = "image_motion"
    TITLE_CARD = "title_card"
    DIAGRAM = "diagram"
    REUSED_MEDIA = "reused_media"
    TRANSITION_ONLY = "transition_only"
    CUSTOM = "custom"


class DurationMode(StrEnum):
    FIXED = "fixed"
    LLM = "llm"


class AspectRatio(StrEnum):
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"
    SQUARE = "1:1"


class VideoMode(StrEnum):
    """Top-level generation/rendering workflow selected for a project.

    The default is intentionally Classic so portable projects written before
    this field existed retain their original behavior without an eager
    migration or rewrite.
    """

    CLASSIC = "classic"
    EDITORIAL = "editorial"


class ProjectCreate(DomainModel):
    title: str = Field(min_length=1, max_length=1000)
    topic: str = Field(min_length=1)
    target_duration: float = Field(gt=0)
    # fixed: scenes are scaled to sum exactly to target_duration.
    # llm: the director sizes each scene from its narration; the summed runtime
    # becomes the effective target (clamped into guardrails around the request).
    duration_mode: DurationMode = DurationMode.FIXED
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE
    fps: int = Field(default=24, ge=1, le=240)
    resolution: tuple[int, int] = (1920, 1080)
    video_mode: VideoMode = VideoMode.CLASSIC
    style: str = "documentary"
    audience: str = "general"
    narrator_preference: str | None = None
    visual_quality: str = "balanced"
    instructions: str = ""

    @field_validator("resolution")
    @classmethod
    def positive_resolution(cls, value: tuple[int, int]) -> tuple[int, int]:
        if any(component <= 0 for component in value):
            raise ValueError("resolution components must be positive")
        return value


class Project(ProjectCreate):
    id: str = Field(default_factory=new_id)
    slug: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    status: ProjectStatus = ProjectStatus.DRAFT
    selected_llm_model: str = "auto"
    settings: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = PROJECT_SCHEMA_VERSION


class ImageModelPreference(StrEnum):
    """Image generators selectable per scene by the routing layer.

    ``automatic`` (the default) keeps historical behavior: the visual-type
    dispatch decides. Explicit values activate the Ideogram/Krea/Qwen routing
    metadata without changing how any other scene behaves.
    """

    AUTOMATIC = "automatic"
    KREA = "krea"
    QWEN_IMAGE = "qwen_image"
    IDEOGRAM4_LOCAL = "ideogram4_local"


class Scene(DomainModel):
    id: str = Field(default_factory=new_id)
    project_id: str
    index: int = Field(ge=0)
    title: str = ""
    duration: float = Field(gt=0)
    narration: str = ""
    visual_prompt: str = ""
    negative_prompt: str = ""
    visual_type: VisualType = VisualType.FLUX_STILL
    selected_backend: str = "automatic"
    camera_instruction: str = ""
    transition: str = "cut"
    music_mood: str = ""
    references: list[str] = Field(default_factory=list)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
    # --- Image-model routing metadata (Ideogram 4 addition) ---
    # True when the picture must contain readable words (thumbnail, title card,
    # poster, labeled map, infographic, sign, document screenshot, UI mockup).
    needs_embedded_text: bool = False
    # Exact literal strings to render inside the image, newline-separated.
    text_in_image: str = ""
    # One of ImageModelPreference; "automatic" defers to visual_type dispatch.
    preferred_image_model: ImageModelPreference = ImageModelPreference.AUTOMATIC
    # Side-by-side testing flags (Qwen vs Ideogram comparison mode).
    test_generate_with_qwen: bool = False
    test_generate_with_ideogram: bool = False
    status: SceneStatus = SceneStatus.DRAFT
    locked: bool = False
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def synchronize_lock(self) -> Scene:
        if self.status is SceneStatus.LOCKED:
            object.__setattr__(self, "locked", True)
        if self.locked and self.status not in {SceneStatus.APPROVED, SceneStatus.LOCKED}:
            raise ValueError("a locked scene must be approved or have locked status")
        return self


class ProjectPlan(DomainModel):
    project_id: str
    title: str
    outline: list[str] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    target_duration: float = Field(gt=0)
    strategy_notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    schema_version: int = PROJECT_SCHEMA_VERSION

    @model_validator(mode="after")
    def validate_scene_ownership(self) -> ProjectPlan:
        if any(scene.project_id != self.project_id for scene in self.scenes):
            raise ValueError("all plan scenes must belong to the plan project")
        indexes = [scene.index for scene in self.scenes]
        if len(indexes) != len(set(indexes)):
            raise ValueError("scene indexes must be unique")
        return self


class Asset(DomainModel):
    id: str = Field(default_factory=new_id)
    project_id: str
    scene_id: str | None = None
    shot_id: str | None = None
    type: AssetType
    filepath: Path
    backend: str
    model: str
    model_version: str = "unknown"
    quantization: str | None = None
    workflow_version: str | None = None
    seed: int = Field(ge=0, le=2**63 - 1)
    prompt: str = ""
    negative_prompt: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    hash: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("filepath")
    @classmethod
    def portable_filepath(cls, value: Path) -> Path:
        if value.is_absolute():
            raise ValueError("asset filepath must be project-relative for portability")
        if ".." in value.parts:
            raise ValueError("asset filepath cannot escape the project directory")
        return value


class GenerationAttempt(DomainModel):
    id: str = Field(default_factory=new_id)
    asset_id: str | None = None
    job_id: str | None = None
    scene_id: str | None = None
    shot_id: str | None = None
    backend: str
    model: str = "unknown"
    model_version: str = "unknown"
    quantization: str | None = None
    workflow_version: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    seed: int = Field(ge=0, le=2**63 - 1)
    success: bool
    error: str | None = None
    duration_seconds: float = Field(default=0, ge=0)
    peak_vram_gb: float | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_error_for_failure(self) -> GenerationAttempt:
        if not self.success and not self.error:
            raise ValueError("failed attempts require an error message")
        return self


class GenerationJob(DomainModel):
    id: str = Field(default_factory=new_id)
    project_id: str
    scene_id: str | None = None
    shot_id: str | None = None
    stage: str = Field(min_length=1)
    backend: str | None = None
    status: JobStatus = JobStatus.QUEUED
    progress: float = Field(default=0, ge=0, le=1)
    priority: int = 0
    parameters: dict[str, Any] = Field(default_factory=dict)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_terminal_state(self) -> GenerationJob:
        if self.status is JobStatus.COMPLETED and self.progress != 1:
            raise ValueError("completed jobs must have progress=1")
        if self.status is JobStatus.FAILED and not self.error:
            raise ValueError("failed jobs require an error message")
        return self
