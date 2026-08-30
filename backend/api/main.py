"""Loopback-only local API for project orchestration and monitoring."""

from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core import AppConfig, inspect_environment, load_config
from backend.core.h3_policy import h3_policy_payload
from backend.core.ports import select_application_port
from backend.editorial import EditPlan, compile_edit_plan_html
from backend.models import LocalLLMBackend
from backend.models.errors import BackendError, BackendErrorCode
from backend.models.ideogram_prompt import validate_ideogram_prompt_json
from backend.pipeline import PipelineService
from backend.pipeline.service import LaneResolutionRejected, PipelineError
from backend.schemas import (
    AspectRatio, DurationMode, GenerationJob, JobStatus, ProjectCreate,
    ThumbnailCandidateRequest, ThumbnailPlan, VideoMode, VisualType,
)
from backend.tts import NarrationRequest, NoNarrationTextError
from backend.storage.jobs import InvalidJobTransition
from backend.workers.gpu import GPUResourceManager


class RenderRequest(BaseModel):
    force: bool = False


class EditorialSettingsEdit(BaseModel):
    """Narrow Edit Plan switches exposed without accepting arbitrary plan JSON."""

    model_config = ConfigDict(extra="forbid")
    captions_enabled: bool | None = None
    editorial_text_enabled: bool | None = None


class ApproveRequest(BaseModel):
    lock: bool = False


class VisualBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None queues every type; a value restricts the batch to scenes of that
    # visual type (Storyboard "Generate all <type>" buttons).
    visual_type: str | None = None
    # Restrict by the effective still-image backend. This is intentionally
    # separate from visual_type: Image Motion scenes can still use Krea (or
    # Ideogram) for their source frame and should batch with that family.
    image_model: str | None = None

    @field_validator("visual_type")
    @classmethod
    def validate_visual_type(cls, value: str | None) -> str | None:
        if value is None:
            return value
        valid = {item.value for item in VisualType}
        if value not in valid:
            raise ValueError(f"visual_type must be one of: {', '.join(sorted(valid))}")
        return value

    @field_validator("image_model")
    @classmethod
    def validate_image_model(cls, value: str | None) -> str | None:
        if value is None:
            return value
        valid = {"krea", "qwen_image", "ideogram4_local"}
        if value not in valid:
            raise ValueError(f"image_model must be one of: {', '.join(sorted(valid))}")
        return value


class SceneEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narration: str | None = None
    visual_prompt: str | None = None
    negative_prompt: str | None = None
    visual_type: str | None = None
    needs_embedded_text: bool | None = None
    text_in_image: str | None = Field(default=None, max_length=20_000)
    text_overlay_layout: str | None = None
    preferred_image_model: str | None = None
    h3_canvas: str | None = None
    krea_canvas: str | None = None
    qwen_image_canvas: str | None = None
    ideogram_prompt_mode: str | None = None
    ideogram_prompt_json: dict[str, Any] | None = None
    image_motion_source: str | None = None
    selected_backend: str | None = None
    camera_instruction: str | None = None
    seed: int | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, gt=0)
    references: list[str] | None = None
    graphic_instructions: str | None = Field(default=None, max_length=8_000)
    graphic_text: list[str] | None = Field(default=None, max_length=160)
    on_screen_text: list[str] | None = Field(default=None, max_length=160)
    h3_quality: str | None = None
    h3_long_shot: bool | None = None
    h3_continuity: dict[str, Any] | None = None

    @field_validator("graphic_text")
    @classmethod
    def validate_graphic_text(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if any(not isinstance(item, str) or not item or len(item) > 500 for item in value):
            raise ValueError("graphic_text entries must be non-empty strings up to 500 characters")
        return value

    @field_validator("on_screen_text")
    @classmethod
    def validate_on_screen_text(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if any(not isinstance(item, str) or not item or len(item) > 500 for item in value):
            raise ValueError("on_screen_text entries must be non-empty strings up to 500 characters")
        return value

    @field_validator("image_motion_source")
    @classmethod
    def validate_image_motion_source(cls, value: str | None) -> str | None:
        if value is not None and value not in {"krea2", "qwen_image_2512"}:
            raise ValueError("image_motion_source must be 'krea2' or 'qwen_image_2512'")
        return value

    @field_validator("preferred_image_model")
    @classmethod
    def validate_preferred_image_model(cls, value: str | None) -> str | None:
        if value is None:
            return value
        valid = {"automatic", "krea", "qwen_image", "ideogram4_local"}
        if value not in valid:
            raise ValueError(
                f"preferred_image_model must be one of: {', '.join(sorted(valid))}"
            )
        return value

    @field_validator("ideogram_prompt_mode")
    @classmethod
    def validate_ideogram_prompt_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in {"quick", "precise"}:
            raise ValueError("ideogram_prompt_mode must be 'quick' or 'precise'")
        return value

    @field_validator("ideogram_prompt_json")
    @classmethod
    def validate_precise_ideogram_json(
        cls, value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return validate_ideogram_prompt_json(value) if value is not None else None


class TransitionInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "cut"
    duration_seconds: float = Field(default=0.0, ge=0, le=3600)


class OverlayCueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=100)
    kind: str
    asset_id: str | None = Field(default=None, max_length=100)
    exact_text: str | None = Field(default=None, max_length=2000)
    template: str = ""
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0, le=3600)
    z_index: int = Field(default=0, ge=0)
    anchor: str = "center"
    x: float | None = None
    y: float | None = None
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    safe_area: float = Field(default=0.0, ge=0, le=0.25)
    fit: str = "contain"
    opacity: float = Field(default=1.0, gt=0, le=1)
    fade_in_seconds: float = Field(default=0.0, ge=0)
    fade_out_seconds: float = Field(default=0.0, ge=0)
    style: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class AudioCueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=100)
    kind: str
    asset_id: str
    start_seconds: float = Field(default=0.0, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0, le=3600)
    gain_db: float = Field(default=0.0, ge=-60, le=12)
    fade_in_seconds: float = Field(default=0.0, ge=0)
    fade_out_seconds: float = Field(default=0.0, ge=0)
    loop: bool = False
    mix_policy: str = "under_narration"


class NarrationGainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gain_db: float = Field(ge=0, le=24)


class PerformanceTagsGenerateRequest(BaseModel):
    """Body for POST /api/projects/{id}/tts/performance-tags."""

    model_config = ConfigDict(extra="forbid")
    #: Script-override text to tag instead of the scene narration. ``None``
    #: (the default) tags the narration that would actually be generated.
    text: str | None = None
    intensity: Literal["subtle", "balanced", "expressive"] = "balanced"
    notes: str = Field(default="", max_length=2000)
    #: Re-tag over an existing script instead of returning it unchanged.
    force: bool = False


class PerformanceTagsSegmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=200)
    tagged: str = Field(min_length=1)


class PerformanceTagsSaveRequest(BaseModel):
    """Body for PUT /api/projects/{id}/tts/performance-tags."""

    model_config = ConfigDict(extra="forbid")
    segments: list[PerformanceTagsSegmentEdit] = Field(min_length=1)


class PerformanceTagsRegenerateRequest(BaseModel):
    """Body for POST /api/projects/{id}/tts/performance-tags/regenerate."""

    model_config = ConfigDict(extra="forbid")
    #: Key of the single segment to re-tag (e.g. ``scene:<id>`` or ``override``).
    key: str = Field(min_length=1, max_length=200)
    intensity: Literal["subtle", "balanced", "expressive"] = "balanced"
    notes: str = Field(default="", max_length=2000)


class ShotReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    asset_id: str


class MediaSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="", max_length=2000)
    creator: str = Field(default="", max_length=1000)
    publisher: str = Field(default="", max_length=1000)
    source_url: str = Field(default="", max_length=4000)
    access_date: str = Field(default="", max_length=10)
    license_note: str = Field(default="", max_length=4000)
    classification: str = "illustration"
    notes: str = Field(default="", max_length=8000)
    sha256: str | None = Field(default=None, max_length=64)


class ShotCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int | None = Field(default=None, ge=0)
    title: str = Field(default="", max_length=1000)
    duration_seconds: float = Field(gt=0, le=3600)
    start_mode: str = "weighted"
    lane: str = "image"
    visual_type: str = "flux_still"
    selected_backend: str = "automatic"
    visual_prompt: str = Field(default="", max_length=8000)
    negative_prompt: str = Field(default="", max_length=4000)
    camera_instruction: str = Field(default="", max_length=4000)
    source_asset_id: str | None = Field(default=None, max_length=100)
    source_in_seconds: float | None = Field(default=None, ge=0)
    source_out_seconds: float | None = Field(default=None, ge=0)
    transition_in: TransitionInRequest | None = None
    references: list[str] | None = Field(default=None, max_length=40)
    reference_assets: list[ShotReferenceRequest] | None = Field(default=None, max_length=40)
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    overlays: list[OverlayCueRequest] | None = Field(default=None, max_length=32)
    audio_cues: list[AudioCueRequest] | None = Field(default=None, max_length=16)
    source: MediaSourceRequest | None = None
    settings: dict[str, Any] | None = None


class ShotEdit(BaseModel):
    """Partial shot edit; approval/locking goes through /approve only."""
    model_config = ConfigDict(extra="forbid")
    index: int | None = None
    title: str | None = Field(default=None, max_length=1000)
    duration_seconds: float | None = Field(default=None, gt=0, le=3600)
    start_mode: str | None = None
    lane: str | None = None
    visual_type: str | None = None
    selected_backend: str | None = None
    visual_prompt: str | None = Field(default=None, max_length=8000)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    camera_instruction: str | None = Field(default=None, max_length=4000)
    source_asset_id: str | None = None
    source_in_seconds: float | None = None
    source_out_seconds: float | None = None
    transition_in: TransitionInRequest | None = None
    references: list[str] | None = None
    reference_assets: list[ShotReferenceRequest] | None = None
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    overlays: list[OverlayCueRequest] | None = None
    audio_cues: list[AudioCueRequest] | None = None
    source: MediaSourceRequest | None = None
    settings: dict[str, Any] | None = None


class OverlayPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str | None = None
    asset_id: str | None = None
    exact_text: str | None = Field(default=None, max_length=2000)
    template: str | None = Field(default=None, max_length=200)
    start_seconds: float | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0, le=3600)
    z_index: int | None = Field(default=None, ge=0)
    anchor: str | None = None
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    safe_area: float | None = Field(default=None, ge=0, le=0.25)
    fit: str | None = None
    opacity: float | None = Field(default=None, gt=0, le=1)
    fade_in_seconds: float | None = Field(default=None, ge=0)
    fade_out_seconds: float | None = Field(default=None, ge=0)
    style: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class ProjectEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")    # Editable brief fields (mirrors ProjectCreate bounds; validated on apply).
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    target_duration: float | None = Field(default=None, gt=0)
    duration_mode: DurationMode | None = None
    aspect_ratio: AspectRatio | None = None
    fps: int | None = Field(default=None, ge=1, le=240)
    resolution: tuple[int, int] | None = None
    video_mode: VideoMode | None = None
    narrator_preference: str | None = Field(default=None, max_length=300)
    style: str | None = Field(default=None, min_length=1, max_length=100)
    audience: str | None = Field(default=None, min_length=1, max_length=100)
    visual_quality: str | None = Field(default=None, min_length=1, max_length=100)
    instructions: str | None = Field(default=None, max_length=20_000)
    settings: dict[str, Any] | None = None
    # Control flag: when a dimension field changes after planning, the caller may
    # explicitly choose to mark existing (non-locked) scenes stale rather than
    # silently rewrite them. Not a project field.
    mark_scenes_stale: bool = False

    @field_validator("resolution")
    @classmethod
    def positive_resolution(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is None:
            return value
        if any(component <= 0 for component in value):
            raise ValueError("resolution components must be positive")
        return value


class LLMSelectionRequest(BaseModel):
    model: str = Field(min_length=1)
    project_id: str | None = None


_COMFYUI_BACKENDS = frozenset({"comfyui", "flux_comfyui", "krea2_comfyui", "qwen_image_2512_comfyui", "wan_comfyui", "ace_step_comfyui"})
_TTS_BACKENDS = frozenset({"qwen_tts", "step_audio_editx", "chatterbox"})


def _model_runtime_status(
    name: str,
    service: PipelineService,
    settings: AppConfig,
    *,
    selected_llm_model: str | None = None,
) -> dict[str, Any]:
    """Return Studio-known model state without probing or loading external services.

    GPU process memory cannot reliably identify a checkpoint owned by another
    process.  We therefore expose only state this process can honestly know,
    and label router/ComfyUI ownership rather than inferring a loaded model.
    """
    if service.mock_mode:
        return {
            "state": "mock",
            "ownership": "studio",
            "detail": "Deterministic mock backend; no model weights are loaded.",
            "actions": [],
    }
    if name == "local_llm":
        selected = selected_llm_model if selected_llm_model is not None else settings.llm.model
        return {
            "state": "selection_required" if selected == "auto" else "selected",
            "ownership": "external-router",
            "detail": (
                "Choose a model per project before script generation. The router owns model loading "
                "and unloading."
            ),
            "actions": ["select"],
        }
    if name in _COMFYUI_BACKENDS:
        resident = service.resident_comfy_backend == name
        return {
            "state": "resident" if resident else "not_loaded",
            "ownership": "studio-coordinated-external-service",
            "detail": (
                "Studio has retained this workflow family for reuse."
                if resident
                else "Loaded on demand by ComfyUI when a matching workflow runs."
            ),
            "actions": ["release"],
        }
    if name in _TTS_BACKENDS:
        backend_config = getattr(settings.backends, name)
        return {
            "state": "on_demand" if backend_config.enabled else "disabled",
            "ownership": "studio-managed-service" if backend_config.managed else "external-local-service",
            "detail": (
                "Starts for narration and releases after the job."
                if backend_config.managed
                else "Configured as a local service; Studio does not infer its loaded model."
            ),
            "actions": [],
        }
    if name == "whisper":
        return {
            "state": "on_demand" if settings.backends.whisper.enabled else "disabled",
            "ownership": "studio",
            "detail": "Loaded only while aligning captions, then released.",
            "actions": [],
        }
    return {
        "state": "registered",
        "ownership": "studio",
        "detail": "Loaded only for work that selects this backend.",
        "actions": [],
    }


def create_app(
    config: AppConfig | None = None,
    *,
    database_path: str | Path | None = None,
    project_root: str | Path | None = None,
    temp_root: str | Path | None = None,
    mock_mode: bool | None = None,
    initialize: bool = True,
) -> FastAPI:
    settings = config or load_config()
    service = PipelineService(
        settings,
        database_path=database_path,
        project_root=project_root,
        temp_root=temp_root,
        mock_mode=mock_mode,
        initialize=initialize,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        service.initialize()
        try:
            yield
        finally:
            service.ideogram_worker.stop()
            service.tts_workers.stop_all()

    application = FastAPI(title="Local Video Studio", version="0.1.0", lifespan=lifespan)
    application.state.settings = settings
    application.state.service = service

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Content-Type", "Authorization"],
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": "mock" if service.mock_mode else "local"}

    @application.get("/api/system/status")
    def system_status(request: Request) -> dict[str, Any]:
        environment = inspect_environment(settings, probe_cuda=True)
        try:
            gpu = GPUResourceManager(
                minimum_free_vram_gb=settings.gpu.minimum_free_vram_gb_for_heavy_job
            ).snapshot().as_dict()
        except BackendError as exc:
            gpu = {"error": exc.as_dict(), "devices": [], "active_backend": None}
        jobs = service.jobs.list()
        return {
            "environment": environment.model_dump(mode="json"),
            "gpu": gpu,
            "queued_jobs": sum(job.status is JobStatus.QUEUED for job in jobs),
            "active_model": gpu.get("active_backend"),
            "comfyui_resident_backend": service.resident_comfy_backend,
            "h3_readiness": service.h3_vram_readiness(),
            "ports": {
                "llm_external": 1234,
                "backend_configured": settings.ports.backend,
                "backend_effective": request.url.port,
                "frontend_configured": settings.ports.frontend,
                "comfyui_external": settings.ports.comfyui,
            },
            "mock_mode": service.mock_mode,
        }

    @application.get("/api/h3/policy")
    def h3_policy() -> dict[str, Any]:
        return h3_policy_payload()

    @application.get("/api/models")
    def models(project_id: str | None = None) -> dict[str, Any]:
        selected_llm_model = None
        if project_id:
            try:
                selected_llm_model = service._project(project_id).selected_llm_model
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
        descriptors = {
            name: jsonable_encoder(asdict(descriptor))
            for name, descriptor in service.registry.descriptors().items()
        }
        return {
            "models": descriptors,
            "runtime": {
                name: _model_runtime_status(
                    name, service, settings, selected_llm_model=selected_llm_model,
                )
                for name in descriptors
            },
        }

    @application.post("/api/comfyui/free")
    def free_comfyui_memory() -> dict[str, Any]:
        try:
            return service.release_comfyui_memory()
        except BackendError as exc:
            raise HTTPException(status_code=503, detail=exc.as_dict()) from None

    @application.post("/api/ideogram4/unload")
    def unload_ideogram4() -> dict[str, Any]:
        """Unload Ideogram 4 and stop only the worker owned by this Studio."""
        try:
            return service.unload_ideogram4()
        except BackendError as exc:
            raise HTTPException(status_code=503, detail=exc.as_dict()) from None

    @application.get("/api/music/models")
    def music_models() -> dict[str, Any]:
        try:
            backend = service.registry.get("ace_step_comfyui")
        except KeyError as exc:
            raise HTTPException(status_code=503, detail="ACE-Step ComfyUI backend not registered") from None
        descriptor = backend.descriptor()
        try:
            readiness = backend.readiness()
        except BackendError as exc:
            raise HTTPException(status_code=503, detail=exc.as_dict()) from None
        comfyui_health = (
            {"status": "mock", "endpoint": settings.backends.comfyui.endpoint}
            if service.mock_mode
            else service.registry.get("comfyui").health()
        )
        vram = (
            {"mock_mode": True, "devices": []}
            if service.mock_mode
            else service.h3_vram_readiness()
        )
        return {
            "backend": jsonable_encoder(asdict(descriptor)),
            "enabled": service.config.backends.ace_step.enabled,
            "provider": "comfyui",
            "comfyui_health": comfyui_health,
            "readiness": readiness,
            "comfyui_resident": service.resident_comfy_backend,
            "vram": vram,
        }

    @application.post("/api/projects/{project_id}/music/generate", status_code=status.HTTP_202_ACCEPTED)
    def generate_music(
        project_id: str,
        body: dict[str, Any] | None = None,
        background_tasks: BackgroundTasks = BackgroundTasks(),
    ) -> dict[str, Any]:
        body = body or {}
        raw_force = body.get("force", False)
        # Accept JSON booleans only: the string "false" is truthy in Python
        # and would silently invert the caller's intent.
        if not isinstance(raw_force, bool):
            raise HTTPException(status_code=422, detail="force must be a JSON boolean")
        force = raw_force
        raw_movement = body.get("movement_index")
        movement_index: int | None = None
        if raw_movement is not None:
            invalid_movement = (
                isinstance(raw_movement, bool)
                or not isinstance(raw_movement, int)
                or raw_movement < 0
            )
            if invalid_movement:
                raise HTTPException(
                    status_code=422, detail="movement_index must be a non-negative integer"
                )
            movement_index = raw_movement
        try:
            existing = service.jobs.list(project_id=project_id)
            active = next(
                (j for j in existing if j.stage == "music" and j.status not in {
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                }),
                None,
            )
            if active and not force:
                return active.model_dump(mode="json")
            if active and force:
                raise PipelineError(
                    "A music generation is already running for this project. "
                    "Wait for it to complete, then force-regenerate."
                )
            job = service.queue_music_generation(
                project_id, force=force, movement_index=movement_index
            )
            background_tasks.add_task(
                service.run_music_generation,
                project_id,
                force=force,
                parent_job_id=job.id,
                movement_index=movement_index,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except (PipelineError, BackendError) as exc:
            status_code = (
                status.HTTP_409_CONFLICT
                if isinstance(exc, PipelineError)
                or exc.code in {BackendErrorCode.INSUFFICIENT_VRAM, BackendErrorCode.MODEL_SELECTION_REQUIRED}
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict() if isinstance(exc, BackendError) else str(exc)) from None
        return job.model_dump(mode="json")

    @application.get("/api/tts/models")
    def tts_models() -> dict[str, Any]:
        names = (
            "qwen_tts", "step_audio_editx", "chatterbox",
            "fish_s2_pro", "voxcpm2", "omnivoice", "index_tts_2_5",
            "breeze_tts_2",
        )
        models: dict[str, Any] = {}
        for name in names:
            backend = service.registry.get(name)
            entry: dict[str, Any] = {
                **jsonable_encoder(asdict(backend.descriptor())),
                "health": backend.health(),
                "managed": getattr(settings.backends, name).managed,
            }
            readiness = getattr(backend, "readiness", None)
            if callable(readiness):
                entry["readiness"] = readiness()
            models[name] = entry
        return {"models": models}

    @application.post("/api/tts/{provider}/unload")
    def unload_tts_provider(provider: str) -> dict[str, Any]:
        """Unload a TTS provider's weights and stop its owned worker.

        Works for every voice-cloning provider reachable from the dashboard,
        including the ComfyUI-backed fish/voxcpm/index adapters (their
        ``unload`` posts to ComfyUI's ``/free`` to release cached models and
        allocator memory). For isolated-worker providers this Studio owns, the
        worker process is also stopped so the OS reclaims its memory.
        """
        try:
            return service.unload_tts_provider(provider)
        except BackendError as exc:
            raise HTTPException(status_code=503, detail=exc.as_dict()) from None

    @application.get("/api/captions/models")
    def captions_models() -> dict[str, Any]:
        # The Captions screen needs the alignment model's identity and honest
        # readiness (configured / dependency present / healthy) before any real
        # render.  health() is read-only and cheap; it never loads the model.
        backend = service.registry.get("whisper")
        config = settings.backends.whisper
        return {
            "backend": "whisper",
            "descriptor": jsonable_encoder(asdict(backend.descriptor())),
            "health": backend.health(),
            "enabled": config.enabled,
            "model_path": str(config.model_path) if config.model_path else None,
            "mock_mode": service.mock_mode,
        }

    @application.post(
        "/api/projects/{project_id}/captions/generate",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def generate_captions(project_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            job = service.queue_caption_alignment(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.run_caption_alignment_job, job.id)
        return job.model_dump(mode="json")

    @application.get("/api/llm/models")
    def llm_models(project_id: str | None = None) -> dict[str, Any]:
        configured_model = settings.llm.model
        if project_id:
            try:
                configured_model = service._project(project_id).selected_llm_model
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
        backend = LocalLLMBackend(
            base_url=settings.llm.base_url,
            api_key_env=settings.llm.api_key_env,
            model=configured_model,
            timeout_seconds=min(settings.llm.timeout_seconds, 3.0),
        )
        health_result = backend.health(check_completion=False)
        if health_result.get("status") != "healthy":
            raise HTTPException(status_code=503, detail=health_result.get("error", {}))
        return {
            "endpoint": settings.llm.base_url,
            "models": [{"id": model_id} for model_id in health_result.get("models", [])],
            "selected_model": None if configured_model == "auto" else configured_model,
            "resolved_model": health_result.get("selected_model"),
        }

    @application.put("/api/llm/models")
    def select_llm_model(request: LLMSelectionRequest) -> dict[str, Any]:
        backend = LocalLLMBackend(
            base_url=settings.llm.base_url,
            api_key_env=settings.llm.api_key_env,
            model=request.model,
            timeout_seconds=min(settings.llm.timeout_seconds, 3.0),
        )
        try:
            discovered = list(backend.discover_models())
        except BackendError as exc:
            raise HTTPException(status_code=503, detail=exc.as_dict()) from None
        identifiers = [record["id"] for record in discovered]
        if request.model != "auto" and request.model not in identifiers:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "model_unavailable",
                    "message": "The selected local model is not currently available.",
                },
            )
        settings.llm.model = request.model
        if request.project_id:
            try:
                service.select_project_llm_model(request.project_id, request.model)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
        return {
            "endpoint": settings.llm.base_url,
            "models": discovered,
            "selected_model": request.model,
            "project_id": request.project_id,
        }

    @application.get("/api/projects")
    def projects() -> dict[str, Any]:
        listed, recovery = service.list_projects()
        return {
            "projects": [project.model_dump(mode="json") for project in listed],
            "recovery": recovery,
        }

    @application.post("/api/projects", status_code=status.HTTP_201_CREATED)
    def create_project(request: ProjectCreate) -> dict[str, Any]:
        project = service.create_project(request)
        return service.project_snapshot(project.id)

    @application.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        try:
            return service.project_snapshot(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @application.get("/api/projects/{project_id}/editorial/edit-plan")
    def get_editorial_edit_plan(
        project_id: str, download: bool = False,
    ) -> Any:
        try:
            payload = service.load_edit_plan(project_id).model_dump(mode="json")
            if download:
                return JSONResponse(
                    content=jsonable_encoder(payload),
                    headers={"Content-Disposition": 'attachment; filename="edit-plan.json"'},
                )
            return payload
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.put("/api/projects/{project_id}/editorial/edit-plan")
    def put_editorial_edit_plan(project_id: str, request: EditPlan) -> dict[str, Any]:
        try:
            return service.save_edit_plan(project_id, request).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post("/api/projects/{project_id}/editorial/plan")
    def generate_editorial_edit_plan(project_id: str) -> dict[str, Any]:
        try:
            return service.ensure_edit_plan(project_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BackendError as exc:
            status_code = (
                status.HTTP_409_CONFLICT
                if exc.code is BackendErrorCode.MODEL_SELECTION_REQUIRED
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @application.post(
        "/api/projects/{project_id}/editorial/compositions/{composition_id}/regenerate"
    )
    def regenerate_editorial_composition(
        project_id: str, composition_id: str,
    ) -> dict[str, Any]:
        try:
            return service.regenerate_edit_plan_composition(
                project_id, composition_id,
            ).model_dump(mode="json")
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BackendError as exc:
            status_code = (
                status.HTTP_409_CONFLICT
                if exc.code is BackendErrorCode.MODEL_SELECTION_REQUIRED
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @application.patch("/api/projects/{project_id}/editorial/settings")
    def update_editorial_settings(
        project_id: str, request: EditorialSettingsEdit,
    ) -> dict[str, Any]:
        try:
            return service.update_edit_plan_settings(
                project_id,
                captions_enabled=request.captions_enabled,
                editorial_text_enabled=request.editorial_text_enabled,
            ).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @application.get(
        "/api/projects/{project_id}/editorial/preview",
        response_class=HTMLResponse,
    )
    def preview_editorial_project(project_id: str) -> HTMLResponse:
        try:
            plan = service.load_edit_plan(project_id)
            html = compile_edit_plan_html(
                plan,
                asset_url_resolver=lambda asset: (
                    f"/api/projects/{project_id}/assets/{asset.asset_id}/file"
                    if asset.asset_id else None
                ),
            )
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return HTMLResponse(
            html,
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; img-src 'self' data: blob:; "
                    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "font-src 'self' data:"
                ),
                "Cache-Control": "no-store",
            },
        )

    @application.get("/api/projects/{project_id}/tts/voices")
    def list_voice_profiles(project_id: str) -> dict[str, Any]:
        try:
            profiles = service.tts.list_voice_profiles(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        voices = []
        for profile in profiles:
            payload = profile.model_dump(mode="json")
            payload["url"] = (
                f"/api/projects/{project_id}/tts/voices/{profile.id}/file"
            )
            voices.append(payload)
        return {"voices": voices}

    @application.get("/api/projects/{project_id}/tts/voices/{profile_id}/file")
    def voice_profile_audio(project_id: str, profile_id: str) -> FileResponse:
        try:
            profile = service.tts.get_voice_profile(project_id, profile_id)
            project = service._project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        path = service.store.project_path(project) / profile.reference_audio
        if not path.is_file():
            raise HTTPException(status_code=404, detail="reference voice audio is missing")
        return FileResponse(path, media_type="audio/wav")

    @application.post(
        "/api/projects/{project_id}/tts/voices", status_code=status.HTTP_201_CREATED,
    )
    async def create_voice_profile(
        project_id: str,
        request: Request,
        name: str,
        transcript: str = "",
        language: str = "en",
        authorized: bool = False,
        gain_db: float = Query(default=0.0, ge=0, le=24),
    ) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"audio/wav", "audio/x-wav", "audio/wave"}:
            raise HTTPException(status_code=415, detail="reference voice must be PCM WAV")
        try:
            profile = service.tts.create_voice_profile(
                project_id, name=name, transcript=transcript, language=language,
                authorized=authorized, audio=await request.body(), gain_db=gain_db,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return profile.model_dump(mode="json")

    @application.get("/api/projects/{project_id}/tts/narrations")
    def list_narration_takes(project_id: str) -> dict[str, Any]:
        try:
            takes, active_asset_id = service.tts.list_narration_takes(project_id)
            gains = service.tts.narration_take_gains(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        payload = []
        for asset in takes:
            item = asset.model_dump(mode="json")
            item["url"] = f"/api/projects/{project_id}/assets/{asset.id}/file"
            item["active"] = asset.id == active_asset_id
            item["gain_db"] = gains.get(asset.id, 0.0)
            item["chunks"] = [
                {
                    **chunk,
                    "url": (
                        f"/api/projects/{project_id}/tts/narrations/{asset.id}/chunks/"
                        f"{int(chunk['index'])}/file"
                    ),
                }
                for chunk in service.tts.list_take_chunks(project_id, asset.id)
            ]
            payload.append(item)
        return {"takes": payload, "active_asset_id": active_asset_id}

    @application.put(
        "/api/projects/{project_id}/tts/narrations/{asset_id}/gain",
    )
    def set_narration_take_gain(
        project_id: str, asset_id: str, request: NarrationGainRequest,
    ) -> dict[str, Any]:
        try:
            gain_db = service.tts.set_narration_take_gain(
                project_id, asset_id, request.gain_db,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {"asset_id": asset_id, "gain_db": gain_db}

    @application.get(
        "/api/projects/{project_id}/tts/narrations/{asset_id}/chunks/{chunk_index}/file",
    )
    def narration_chunk_audio(
        project_id: str, asset_id: str, chunk_index: int,
    ) -> FileResponse:
        try:
            path = service.tts.take_chunk_path(project_id, asset_id, chunk_index)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return FileResponse(path, media_type="audio/wav")

    @application.post(
        "/api/projects/{project_id}/tts/narrations/{asset_id}/chunks/{chunk_index}/regenerate",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def regenerate_narration_chunk(
        project_id: str,
        asset_id: str,
        chunk_index: int,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            job = service.tts.queue_chunk_regeneration(project_id, asset_id, chunk_index)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.tts.run_chunk_regeneration_job, job.id)
        return job_payload(job)

    @application.post(
        "/api/projects/{project_id}/tts/narrations/{asset_id}/activate",
    )
    def activate_narration_take(project_id: str, asset_id: str) -> dict[str, Any]:
        try:
            service.tts.activate_take(project_id, asset_id)
            takes, active_asset_id = service.tts.list_narration_takes(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        asset = next(item for item in takes if item.id == active_asset_id)
        payload = asset.model_dump(mode="json")
        payload["url"] = f"/api/projects/{project_id}/assets/{asset.id}/file"
        payload["active"] = True
        return {"take": payload, "active_asset_id": active_asset_id}

    @application.post(
        "/api/projects/{project_id}/tts/generate", status_code=status.HTTP_202_ACCEPTED,
    )
    def generate_narration(
        project_id: str, request: NarrationRequest, background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            if request.enhance_with_step and request.provider != "qwen_tts":
                raise ValueError("Step enhancement is supported after Qwen generation")
            if request.voice_profile_id:
                service.tts.get_voice_profile(project_id, request.voice_profile_id)
            elif request.provider not in {"chatterbox", "qwen_tts"}:
                raise ValueError(f"{request.provider} requires an authorized reference voice")
            elif request.enhance_with_step:
                raise ValueError("Step enhancement requires an authorized reference voice")
            else:
                service._project(project_id)
            # Validate the script source before returning a queued job. Otherwise
            # an unplanned project fails later with a raw missing plan.json error.
            service.tts.resolve_narration_text(project_id, request.text)
            # Mirror the music guard: two concurrent narration jobs would both
            # run a full TTS pass over the same chunk files.
            active = next(
                (j for j in service.jobs.list(project_id=project_id)
                 if j.stage == "narration" and j.status not in {
                     JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                 }),
                None,
            )
            if active is not None:
                raise PipelineError(
                    "A narration generation is already running for this project. "
                    "Wait for it to complete or cancel it first."
                )
            job = service.queue_narration(project_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.run_narration_job, job.id)
        return job.model_dump(mode="json")

    @application.get("/api/projects/{project_id}/tts/performance-tags")
    def get_performance_tags(project_id: str) -> dict[str, Any]:
        """Current Fish S2 Pro delivery-tag script plus staleness and LLM state."""
        try:
            project = service._project(project_id)
            script = service.tts.get_performance_script(project_id)
            stale = (
                service.tts.performance_script_is_stale(project_id, script)
                if script is not None else False
            )
            llm = service.director.llm
            selected = project.selected_llm_model.strip()
            model = selected if selected not in {"", "auto"} else None
            available = False
            if llm is not None and not service.mock_mode and model is not None:
                try:
                    llm.selected_model(model=model)
                except BackendError:
                    pass
                else:
                    available = True
            return {
                "script": script.model_dump(mode="json") if script is not None else None,
                "stale": stale,
                "tag_count": script.tag_count if script is not None else 0,
                "llm": {"available": available, "model": model},
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @application.post("/api/projects/{project_id}/tts/performance-tags")
    def generate_performance_tags(
        project_id: str, request: PerformanceTagsGenerateRequest,
    ) -> dict[str, Any]:
        """Tag the narration with the local LLM (synchronous, like POST /plan)."""
        try:
            if not request.force:
                existing = service.tts.get_performance_script(project_id)
                if existing is not None:
                    return {
                        "script": existing.model_dump(mode="json"),
                        "tag_count": existing.tag_count,
                        "warnings": [],
                    }
            script, warnings = service.tts.generate_performance_script(
                project_id,
                text=request.text,
                intensity=request.intensity,
                notes=request.notes,
            )
            return {
                "script": script.model_dump(mode="json"),
                "tag_count": script.tag_count,
                "warnings": warnings,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except NoNarrationTextError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BackendError as exc:
            # A structured body lets the UI show the real cause instead of a
            # connection failure ("Cannot reach the backend").
            status_code = (
                status.HTTP_409_CONFLICT
                if exc.code is BackendErrorCode.MODEL_SELECTION_REQUIRED
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @application.post("/api/projects/{project_id}/tts/performance-tags/regenerate")
    def regenerate_performance_tags(
        project_id: str, request: PerformanceTagsRegenerateRequest,
    ) -> dict[str, Any]:
        """Re-tag a single segment with the local LLM; other segments are kept."""
        try:
            script, warnings = service.tts.regenerate_performance_segment(
                project_id,
                request.key,
                intensity=request.intensity,
                notes=request.notes,
            )
            return {
                "script": script.model_dump(mode="json"),
                "tag_count": script.tag_count,
                "warnings": warnings,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except BackendError as exc:
            status_code = (
                status.HTTP_409_CONFLICT
                if exc.code is BackendErrorCode.MODEL_SELECTION_REQUIRED
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @application.put("/api/projects/{project_id}/tts/performance-tags")
    def save_performance_tags(
        project_id: str,
        request: PerformanceTagsSaveRequest,
        accept: bool = Query(default=False),
    ) -> dict[str, Any]:
        """Save hand-edited tagged text; validates each segment against source."""
        try:
            script = service.tts.get_performance_script(project_id)
            if script is None:
                raise ValueError("no delivery-tag script exists to edit")
            by_key = {segment.key: segment for segment in script.segments}
            for edit in request.segments:
                segment = by_key.get(edit.key)
                if segment is None:
                    raise ValueError(f"unknown segment key: {edit.key}")
                segment.tagged = edit.tagged
            script = service.tts.save_performance_script(
                project_id, script, accept=accept,
            )
            return {"script": script.model_dump(mode="json")}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @application.delete("/api/projects/{project_id}/tts/performance-tags")
    def delete_performance_tags(project_id: str) -> dict[str, Any]:
        try:
            service.tts.clear_performance_script(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"deleted": True}

    @application.patch("/api/projects/{project_id}")
    def edit_project(project_id: str, request: ProjectEdit) -> dict[str, Any]:
        try:
            _, invalidated = service.update_project(
                project_id,
                request.model_dump(exclude_unset=True, exclude_none=False),
            )
            snapshot = service.project_snapshot(project_id)
            snapshot["invalidated_stages"] = sorted(invalidated)
            return snapshot
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, Any]:
        try:
            return service.delete_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.get("/api/projects/{project_id}/assets/{asset_id}/file")
    def project_asset(project_id: str, asset_id: str, download: bool = False) -> FileResponse:
        try:
            project = service._project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        asset = service.database.get_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise HTTPException(status_code=404, detail="asset not found")
        root = service.store.project_path(project).resolve()
        path = (root / asset.filepath).resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="asset file not found")
        return FileResponse(
            path,
            filename=path.name if download else None,
            content_disposition_type="attachment" if download else "inline",
        )

    @application.get("/api/projects/{project_id}/thumbnails")
    def get_thumbnails(project_id: str) -> dict[str, Any]:
        try:
            return service.thumbnails.snapshot(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.put("/api/projects/{project_id}/thumbnails/plan")
    def put_thumbnail_plan(project_id: str, request: ThumbnailPlan) -> dict[str, Any]:
        try:
            return service.thumbnails.save_plan(project_id, request).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post("/api/projects/{project_id}/thumbnails/magic-prompt/regenerate")
    def regenerate_thumbnail_magic_prompt(project_id: str) -> dict[str, Any]:
        """Build and persist the local-LLM caption without loading Ideogram."""
        try:
            return service.thumbnails.prepare_ideogram_magic_prompt(
                project_id, regenerate=True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post(
        "/api/projects/{project_id}/thumbnails/candidates",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_thumbnail_candidate(
        project_id: str,
        request: ThumbnailCandidateRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            job = service.thumbnails.queue_candidate(project_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.thumbnails.run_candidate_job, job.id)
        return job.model_dump(mode="json")

    @application.post(
        "/api/projects/{project_id}/thumbnails/candidates/{candidate_id}/regenerate",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def regenerate_thumbnail_candidate(
        project_id: str,
        candidate_id: str,
        background_tasks: BackgroundTasks,
        request: ThumbnailCandidateRequest = ThumbnailCandidateRequest(),
    ) -> dict[str, Any]:
        try:
            job = service.thumbnails.queue_candidate(
                project_id, request, candidate_id=candidate_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.thumbnails.run_candidate_job, job.id)
        return job.model_dump(mode="json")

    @application.delete("/api/projects/{project_id}/thumbnails/candidates/{candidate_id}")
    def delete_thumbnail_candidate(project_id: str, candidate_id: str) -> dict[str, Any]:
        try:
            return service.thumbnails.delete_candidate(project_id, candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post(
        "/api/projects/{project_id}/thumbnails/candidates/{candidate_id}/select"
    )
    def select_thumbnail_candidate(project_id: str, candidate_id: str) -> dict[str, Any]:
        try:
            return service.thumbnails.select(project_id, candidate_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.get(
        "/api/projects/{project_id}/thumbnails/candidates/{candidate_id}/file"
    )
    def thumbnail_candidate_file(
        project_id: str, candidate_id: str, download: bool = False,
    ) -> FileResponse:
        try:
            path = service.thumbnails.candidate_file(project_id, candidate_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return FileResponse(
            path,
            media_type="image/png",
            filename=path.name if download else None,
            content_disposition_type="attachment" if download else "inline",
        )

    @application.post("/api/projects/{project_id}/plan")
    def plan_project(project_id: str, request: RenderRequest = RenderRequest()) -> dict[str, Any]:
        try:
            plan = service.ensure_plan(project_id, force=request.force)
            return plan.model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except BackendError as exc:
            # A structured body lets the UI show the real cause instead of a
            # connection failure ("Cannot reach the backend").
            status_code = (
                status.HTTP_409_CONFLICT
                if exc.code is BackendErrorCode.MODEL_SELECTION_REQUIRED
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from None
        except ValueError as exc:
            # Director/policy rejections (e.g. the project scene cap,
            # H3 duration minimums) are client-visible request failures.
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @application.post("/api/projects/{project_id}/script")
    def script_project(project_id: str, request: RenderRequest = RenderRequest()) -> dict[str, Any]:
        return plan_project(project_id, request)

    @application.post("/api/projects/{project_id}/render", status_code=status.HTTP_202_ACCEPTED)
    def render_project(
        project_id: str,
        background_tasks: BackgroundTasks,
        request: RenderRequest = RenderRequest(),
    ) -> dict[str, Any]:
        try:
            job = service.queue_render(project_id, force=request.force)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(
            service.run_render,
            project_id,
            force=request.force,
            parent_job_id=job.id,
        )
        return job.model_dump(mode="json")

    @application.post(
        "/api/projects/{project_id}/visuals/batch",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_project_visual_batch(
        project_id: str,
        background_tasks: BackgroundTasks,
        request: VisualBatchRequest = VisualBatchRequest(),
    ) -> dict[str, Any]:
        """Queue sequential generation for scenes still missing a visual."""
        try:
            visual_type = (
                VisualType(request.visual_type) if request.visual_type else None
            )
            job = service.queue_visual_batch(
                project_id, visual_type=visual_type, image_model=request.image_model,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.run_visual_batch_job, job.id)
        return job.model_dump(mode="json")

    @application.post("/api/projects/{project_id}/jobs/cancel-all")
    def cancel_all_project_jobs(project_id: str) -> dict[str, Any]:
        """Cancel every active job for the project (storyboard "Cancel all").

        Visual batch children come back in the result tagged as canceled with
        their parent; terminal jobs are never touched.
        """
        try:
            canceled = service.cancel_all_jobs(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {
            "canceled": [job_payload(job) for job in canceled],
            "count": len(canceled),
        }

    @application.patch("/api/scenes/{scene_id}")
    def edit_scene(scene_id: str, request: SceneEdit) -> dict[str, Any]:
        try:
            scene = service.update_scene(scene_id, request.model_dump(exclude_none=True))
            return scene.model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post("/api/scenes/{scene_id}/reused-media", status_code=status.HTTP_201_CREATED)
    async def import_scene_reused_media(
        scene_id: str,
        file: UploadFile = File(...),
        source: str = Form(...),
    ) -> dict[str, Any]:
        """Copy a user-selected local asset into a REAL scene without remote fetches."""
        filename = Path(file.filename or "upload").name
        staged_path: Path | None = None
        try:
            source_payload = json.loads(source)
            if not isinstance(source_payload, dict):
                raise ValueError("source metadata must be a JSON object")
            suffix = Path(filename).suffix.lower()
            service.temp_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="lvs-reused-media-", suffix=suffix, dir=service.temp_root, delete=False,
            ) as staged:
                staged_path = Path(staged.name)
                total = 0
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 5 * 1024 * 1024 * 1024:
                        raise ValueError("uploaded media exceeds the 5 GB local import limit")
                    staged.write(chunk)
            asset = service.import_reused_media(
                scene_id, staged_path, original_filename=filename, source_fields=source_payload,
            )
            return _asset_payload(asset, asset.project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        finally:
            await file.close()
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)

    @application.post("/api/shots/{shot_id}/reused-media", status_code=status.HTTP_201_CREATED)
    async def import_shot_reused_media(
        shot_id: str,
        file: UploadFile = File(...),
        source: str = Form(...),
    ) -> dict[str, Any]:
        """Copy a user-selected local asset into one explicit REAL shot."""
        shot = service.database.get_shot(shot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail=f"shot not found: {shot_id}")
        filename = Path(file.filename or "upload").name
        staged_path: Path | None = None
        try:
            source_payload = json.loads(source)
            if not isinstance(source_payload, dict):
                raise ValueError("source metadata must be a JSON object")
            suffix = Path(filename).suffix.lower()
            service.temp_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="lvs-reused-media-", suffix=suffix, dir=service.temp_root, delete=False,
            ) as staged:
                staged_path = Path(staged.name)
                total = 0
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 5 * 1024 * 1024 * 1024:
                        raise ValueError("uploaded media exceeds the 5 GB local import limit")
                    staged.write(chunk)
            asset = service.import_reused_media(
                shot.scene_id,
                staged_path,
                original_filename=filename,
                source_fields=source_payload,
                shot_id=shot.id,
            )
            return _asset_payload(asset, asset.project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        finally:
            await file.close()
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)

    async def _import_generated_image_upload(
        *, scene_id: str, shot_id: str | None, file: UploadFile, source: str,
    ) -> dict[str, Any]:
        filename = Path(file.filename or "upload").name
        staged_path: Path | None = None
        try:
            source_payload = json.loads(source)
            if not isinstance(source_payload, dict):
                raise ValueError("source metadata must be a JSON object")
            suffix = Path(filename).suffix.lower()
            service.temp_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="lvs-imported-image-", suffix=suffix, dir=service.temp_root, delete=False,
            ) as staged:
                staged_path = Path(staged.name)
                total = 0
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 1024 * 1024 * 1024:
                        raise ValueError("uploaded image exceeds the 1 GB local import limit")
                    staged.write(chunk)
            asset = service.import_reused_media(
                scene_id,
                staged_path,
                original_filename=filename,
                source_fields=source_payload,
                shot_id=shot_id,
                generated_image=True,
            )
            return _asset_payload(asset, asset.project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        finally:
            await file.close()
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)

    @application.post("/api/scenes/{scene_id}/imported-image", status_code=status.HTTP_201_CREATED)
    async def import_scene_generated_image(
        scene_id: str,
        file: UploadFile = File(...),
        source: str = Form("{}"),
    ) -> dict[str, Any]:
        """Attach a user-selected AI-generated still to a legacy scene recipe."""
        return await _import_generated_image_upload(
            scene_id=scene_id, shot_id=None, file=file, source=source,
        )

    @application.post("/api/shots/{shot_id}/imported-image", status_code=status.HTTP_201_CREATED)
    async def import_shot_generated_image(
        shot_id: str,
        file: UploadFile = File(...),
        source: str = Form("{}"),
    ) -> dict[str, Any]:
        """Attach a user-selected AI-generated still to one explicit shot."""
        shot = service.database.get_shot(shot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail=f"shot not found: {shot_id}")
        return await _import_generated_image_upload(
            scene_id=shot.scene_id, shot_id=shot.id, file=file, source=source,
        )

    @application.post("/api/scenes/{scene_id}/generate", status_code=status.HTTP_201_CREATED)
    def generate_scene(scene_id: str) -> dict[str, Any]:
        try:
            return service.generate_scene(scene_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            # H3PolicyError (a ValueError subclass) and director policy
            # rejections are request failures, not server faults.
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except BackendError as exc:
            status_code = (
                status.HTTP_409_CONFLICT
                if exc.code in {
                    BackendErrorCode.MODEL_SELECTION_REQUIRED,
                    BackendErrorCode.INSUFFICIENT_VRAM,
                }
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post("/api/scenes/{scene_id}/regenerate", status_code=status.HTTP_201_CREATED)
    def regenerate_scene(scene_id: str) -> dict[str, Any]:
        try:
            return service.generate_scene(scene_id, force=True).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except BackendError as exc:
            status_code = (
                status.HTTP_409_CONFLICT
                if exc.code in {
                    BackendErrorCode.MODEL_SELECTION_REQUIRED,
                    BackendErrorCode.INSUFFICIENT_VRAM,
                }
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(status_code=status_code, detail=exc.as_dict()) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post("/api/scenes/{scene_id}/approve")
    def approve_scene(scene_id: str, request: ApproveRequest = ApproveRequest()) -> dict[str, Any]:
        try:
            return service.approve_scene(scene_id, lock=request.lock).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @application.get("/api/scenes/{scene_id}/graphic-screen")
    def graphic_screen_source(scene_id: str) -> dict[str, Any]:
        scene = service.database.get_scene(scene_id)
        if scene is None:
            raise HTTPException(status_code=404, detail="scene not found")
        project = service._project(scene.project_id)
        directory = service._scene_dir(project, scene)
        manifest_path = directory / "graphic-screen.json"
        source_path = directory / "graphic-screen.html"
        if not manifest_path.is_file() or not source_path.is_file():
            raise HTTPException(status_code=404, detail="Graphic Screen source is not available")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Graphic Screen manifest is invalid") from None
        # This is an explicit inspection endpoint. The frontend must place source with textContent,
        # never innerHTML; errors intentionally never include model-authored source.
        return {"manifest": manifest, "source": source_path.read_text(encoding="utf-8")}

    def _asset_payload(asset: Any, project_id: str | None = None) -> dict[str, Any]:
        payload = asset.model_dump(mode="json")
        payload["url"] = (
            f"/api/projects/{project_id or asset.project_id}/assets/{asset.id}/file"
        )
        return payload

    @application.get("/api/scenes/{scene_id}/shots")
    def get_scene_shots(scene_id: str) -> dict[str, Any]:
        try:
            return service.list_scene_shots(scene_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @application.post("/api/scenes/{scene_id}/shots", status_code=status.HTTP_201_CREATED)
    def create_scene_shot(scene_id: str, body: ShotCreate) -> dict[str, Any]:
        try:
            shot = service.create_shot(scene_id, body.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return shot.model_dump(mode="json")

    @application.patch("/api/shots/{shot_id}")
    def edit_shot(shot_id: str, body: ShotEdit) -> dict[str, Any]:
        try:
            shot = service.update_shot(shot_id, body.model_dump(exclude_unset=True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return shot.model_dump(mode="json")

    @application.delete("/api/shots/{shot_id}")
    def delete_shot(shot_id: str, archive_media: bool = True) -> dict[str, Any]:
        try:
            return service.delete_shot(shot_id, archive_media=archive_media)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @application.post("/api/shots/{shot_id}/approve")
    def approve_shot(
        shot_id: str, request: ApproveRequest = ApproveRequest(),
    ) -> dict[str, Any]:
        try:
            shot = service.approve_shot(shot_id, lock=request.lock)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return shot.model_dump(mode="json")

    @application.post(
        "/api/shots/{shot_id}/generate", status_code=status.HTTP_202_ACCEPTED,
    )
    def generate_shot(
        shot_id: str, background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Queue shot generation; unwired lanes are rejected before any row exists."""
        try:
            job = service.queue_shot_generation(shot_id, regenerate=False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except LaneResolutionRejected as exc:
            raise HTTPException(status_code=422, detail=exc.payload) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.run_shot_generation_job, job.id)
        return job_payload(job)

    @application.post(
        "/api/shots/{shot_id}/regenerate", status_code=status.HTTP_202_ACCEPTED,
    )
    def regenerate_shot(
        shot_id: str, background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Queue a forced regeneration (archives the current visual first)."""
        try:
            job = service.queue_shot_generation(shot_id, regenerate=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except LaneResolutionRejected as exc:
            raise HTTPException(status_code=422, detail=exc.payload) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.run_shot_generation_job, job.id)
        return job_payload(job)

    @application.post("/api/scenes/{scene_id}/render", status_code=status.HTTP_202_ACCEPTED)
    def render_scene(
        scene_id: str,
        background_tasks: BackgroundTasks,
        force: bool = False,
    ) -> dict[str, Any]:
        """Compile the scene's shots via ShotNormalizer + SceneAssembler."""
        try:
            job = service.queue_scene_render(scene_id, force=force)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        background_tasks.add_task(service.run_scene_render_job, job.id)
        return job_payload(job)

    @application.get("/api/projects/{project_id}/render/preflight")
    def project_render_preflight(project_id: str) -> dict[str, Any]:
        try:
            return service.render_preflight(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @application.post("/api/shots/{shot_id}/overlays", status_code=status.HTTP_201_CREATED)
    def add_shot_overlay(shot_id: str, body: OverlayCueRequest) -> dict[str, Any]:
        try:
            shot = service.add_shot_overlay(shot_id, body.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return shot.model_dump(mode="json")

    @application.patch("/api/shots/{shot_id}/overlays/{overlay_id}")
    def edit_shot_overlay(shot_id: str, overlay_id: str, body: OverlayPatch) -> dict[str, Any]:
        try:
            shot = service.patch_shot_overlay(
                shot_id, overlay_id, body.model_dump(exclude_unset=True),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return shot.model_dump(mode="json")

    @application.delete("/api/shots/{shot_id}/overlays/{overlay_id}")
    def delete_shot_overlay(shot_id: str, overlay_id: str) -> dict[str, Any]:
        try:
            shot = service.remove_shot_overlay(shot_id, overlay_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return shot.model_dump(mode="json")

    @application.patch("/api/overlays/{overlay_id}")
    def edit_project_overlay(
        overlay_id: str,
        body: OverlayPatch,
        project_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Overlay ids resolve within a project scope (they are embedded in shots)."""
        if not project_id:
            raise HTTPException(
                status_code=422,
                detail="project_id query parameter is required to resolve an overlay id",
            )
        try:
            shot = service.patch_project_overlay(
                project_id, overlay_id, body.model_dump(exclude_unset=True),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return shot.model_dump(mode="json")

    def job_payload(job: GenerationJob) -> dict[str, Any]:
        """Job row plus the two UI hints the Job Monitor needs for its buttons.

        ``executable`` mirrors PipelineService.job_is_executable (Retry only
        makes sense for rows the backend can re-run on its own); ``cancelable``
        is False for terminal rows and for pipeline bookkeeping rows
        (parameters.managed_by == "pipeline"), whose mid-operation cancel is a
        tolerated no-op the UI must not offer.
        """
        payload = job.model_dump(mode="json")
        payload["executable"] = service.job_is_executable(job)
        payload["cancelable"] = (
            job.status not in {
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
            }
            and job.parameters.get("managed_by") != "pipeline"
        )
        return payload

    @application.get("/api/jobs")
    def jobs() -> dict[str, Any]:
        return {"jobs": [job_payload(job) for job in service.jobs.list()]}

    @application.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            # Visual batches cascade to the children they created; each job's
            # media subprocesses are killed, unrelated jobs' survive.
            job = service.cancel_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except InvalidJobTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return job_payload(job)

    @application.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            target = service.jobs.get(job_id)
            if target is None:
                raise KeyError(f"job not found: {job_id}")
            if not service.job_is_executable(target):
                raise PipelineError(
                    f"stage '{target.stage}' runs inside its parent pipeline; rerun that stage instead"
                )
            job = service.jobs.retry(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        # Requeueing alone would strand the job: execution only happens here.
        background_tasks.add_task(service.execute_job, job.id)
        return job_payload(job)

    @application.get("/api/events")
    async def events(response: Response) -> StreamingResponse:
        del response

        async def stream():
            previous = ""
            while True:
                payload = json.dumps(
                    [job_payload(job) for job in service.jobs.list()],
                    default=str,
                    sort_keys=True,
                )
                if payload != previous:
                    yield f"event: jobs\ndata: {payload}\n\n"
                    previous = payload
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    frontend = Path(__file__).resolve().parents[2] / "frontend"
    if frontend.is_dir():
        application.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

    return application


app = create_app(initialize=False)


def run() -> None:
    settings = load_config()
    port = select_application_port(
        settings.network.bind_address,
        settings.ports.backend,
        allowed_range=settings.ports.allowed_range,
        reserved=set(settings.ports.reserved),
        auto_select=settings.ports.auto_select_free_port,
        allow_lan=settings.network.allow_lan,
    )
    uvicorn.run(
        "backend.api.main:app",
        host=settings.network.bind_address,
        port=port,
        reload=False,
    )
