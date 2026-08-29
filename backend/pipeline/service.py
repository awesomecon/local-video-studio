"""Persistent, scene-addressable video production pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
import textwrap
import threading
import time
import uuid
import wave
from dataclasses import asdict
from html import escape
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from backend.captions import CaptionWord, build_caption_cues, restore_authored_punctuation
from backend.core import AppConfig
from backend.director import DirectorEngine
from backend.director.image_routing import (
    IMAGE_MODEL_DIRNAMES,
    ROUTABLE_VISUAL_TYPES,
    ImageModelOption,
    build_qwen_prompt,
    scene_text_literals,
    storyboard_entry,
    validate_ideogram_prompt_json,
)
from backend.models import BackendRegistry, GenerationRequest, GenerationResult
from backend.models.h3_shot_continuity import (
    ShotContinuityError,
    effective_predecessor_shot_id,
    parse_shot_continuity,
    validate_continuity_chain,
)
from backend.models.lane_resolver import LaneResolutionError, resolve_lane_target
from backend.models.provenance import (
    apply_regeneration_staleness,
    current_visual_asset,
    plan_regeneration,
)
from backend.models.shot_requests import (
    ShotRequestError,
    build_shot_request,
    resolve_reference_assets,
)
from backend.models.local_llm import LocalLLMBackend
from backend.models.ideogram_prompt import (
    aspect_ratio_from_size,
    build_ideogram_v4_prompt,
)
from backend.models.errors import BackendError, BackendErrorCode, redact_secrets
from backend.graphics import GraphicScreenGenerator, GraphicScreenManifest, GraphicScreenRenderer
from backend.music import MovementPlan, plan_hash as music_plan_hash, plan_movements
from backend.music import stitch as music_stitch
from backend.core.h3_policy import (
    H3Quality, H3PolicyError, CONTINUATION_WORKFLOW_VERSION,
    FIRST_SHOT_WORKFLOW_VERSION, LAST_FRAME_EXTRACTOR_VERSION,
    resolve_quality, h3_frame_count, h3_effective_duration, validate_duration,
    parse_continuity, H3ContinuityBlock,
)
from backend.schemas.h3_continuity import (
    validate_continuity_graph, h3_continuity_status,
)
from backend.rendering.frames import extract_last_frame, compute_sha256
from backend.rendering.manifests import (
    SCENE_ASSEMBLY_WORKFLOW,
    load_manifest,
    sha256_file,
)
from backend.rendering.probe import probe_media
from backend.rendering.scenes import SceneAssembler, SceneEncodeOptions
from backend.rendering.shots import NormalizationInputs, ShotNormalizer
from backend.timeline.shots import compile_scene_plan
from backend.rendering import FFmpegRenderer, RenderOptions
from backend.rendering.binaries import require_ffmpeg
from backend.rendering.process import (
    cancel_media_processes_for_job,
    media_process_scope,
    run_media_process,
)
from backend.rendering.qc import MediaQC, QCReport
from backend.rendering.subtitles import write_ass, write_srt
from backend.editorial.models import EditPlan, EditPlanProvenance, EditPlanSourceKind
from backend.editorial.planner import EditorialPlanner
from backend.editorial.renderer import EditorialRenderer, compile_edit_plan_html
from backend.schemas import (
    Asset,
    AssetType,
    GenerationAttempt,
    GenerationJob,
    JobStatus,
    Project,
    ProjectCreate,
    ProjectPlan,
    ProjectStatus,
    VideoMode,
    Scene,
    SceneStatus,
    Shot,
    ShotStatus,
    OverlayCue,
    OverlayKind,
    MediaSource,
    VisualType,
    effective_shots,
    implicit_shot_from_scene,
    scene_rendered_duration,
    utc_now,
    validate_shot_sequence,
)
from backend.storage import PersistentJobQueue, ProjectStore, StudioDatabase, slugify
from backend.storage.generation_cache import CachedGeneration, GenerationCache
from backend.storage.jobs import InvalidJobTransition
from backend.timeline import (
    SceneTiming,
    SubtitleCue,
    Timeline,
    adjust_scene_durations,
    build_timeline,
)
from backend.thumbnails import ThumbnailStudioService
from backend.tts import NarrationRequest, TTSManager
from backend.tts.audio import wav_duration
from backend.workers.gpu import GPUSnapshot, query_nvidia_smi
from backend.workers.ideogram_process import IdeogramWorkerSupervisor
from backend.workers.tts_processes import TTSWorkerSupervisor

logger = logging.getLogger(__name__)

# Deterministic seed for the mock music path; per-movement seeds derive from it.
MUSIC_SEED_BASE = 30_001
# Fade-out/fade-in dip between stitched movements (keeps totals exact).
MOVEMENT_DIP_SECONDS = 1.5


class PipelineError(RuntimeError):
    pass


class LaneResolutionRejected(ValueError):
    """A shot's lane/visual type has no executable local target.

    Carries the lane resolver's structured payload so API callers receive a
    real JSON object (code + details) instead of a serialized string.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message", "lane resolution failed")))
        self.payload = payload


class PipelineService:
    """Coordinates stateful stages while keeping artifacts portable and reproducible."""

    H3_WORKFLOW = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / "comfyui"
        / "minimax-h3-av.workflow.json"
    )
    H3_FIRST_FRAME_WORKFLOW = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / "comfyui"
        / "minimax-h3-av-first-frame.workflow.json"
    )
    KREA2_WORKFLOW = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / "comfyui"
        / "krea2-turbo.workflow.json"
    )
    QWEN_IMAGE_2512_WORKFLOW = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / "comfyui"
        / "qwen-image-2512.workflow.json"
    )
    # Ideogram 4 runs locally through its ComfyUI workflow template. Added to
    # TEST against Qwen Image for embedded-text scenes; Qwen stays available.
    IDEOGRAM4_WORKFLOW = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / "comfyui"
        / "ideogram4-local.workflow.json"
    )
    IDEOGRAM4_THUMBNAIL_WORKFLOW = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / "comfyui"
        / "ideogram4-thumbnail-local.workflow.json"
    )
    # All voice-cloning providers reachable through the dashboard.
    _TTS_PROVIDER_NAMES: tuple[str, ...] = (
        "qwen_tts", "step_audio_editx", "chatterbox",
        "fish_s2_pro", "voxcpm2", "omnivoice", "index_tts_2_5",
        "breeze_tts_2",
    )
    # Providers that ship with an isolated worker process supervised by
    # TTSWorkerSupervisor. ComfyUI-backed providers (fish/voxcpm/index) are
    # intentionally excluded: they share the image/video ComfyUI on 8188.
    _TTS_ISOLATED_WORKER_NAMES: frozenset[str] = frozenset({
        "qwen_tts", "step_audio_editx", "chatterbox", "omnivoice", "breeze_tts_2",
    })

    def __init__(
        self,
        config: AppConfig,
        *,
        database_path: str | Path | None = None,
        project_root: str | Path | None = None,
        temp_root: str | Path | None = None,
        mock_mode: bool | None = None,
        snapshot_provider: Callable[[], tuple[GPUSnapshot, ...]] | None = None,
        initialize: bool = True,
    ) -> None:
        self.config = config
        self.mock_mode = (
            os.environ.get("LOCAL_VIDEO_STUDIO_MOCK_MODE", "").lower() in {"1", "true", "yes"}
            if mock_mode is None
            else mock_mode
        )
        app_data = config.paths.app_data
        self.database = StudioDatabase(database_path or app_data / "studio.sqlite3")
        self.store = ProjectStore(project_root or config.paths.project_root)
        # API upload staging and FFmpeg may both need a working directory.
        # Keep it concrete even when callers rely on the configured default.
        self.temp_root = Path(temp_root or config.paths.temp_root).expanduser()
        self.jobs = PersistentJobQueue(self.database)
        self.registry = BackendRegistry.from_config(config.model_dump(mode="python"), mock_mode=self.mock_mode)
        self.generation_cache = self._build_generation_cache(config)
        self._snapshot_provider = snapshot_provider or query_nvidia_smi
        self.director = DirectorEngine(
            None if self.mock_mode else self.registry.get("local_llm")  # type: ignore[arg-type]
        )
        self.editorial_planner = EditorialPlanner(self.director.llm)
        # Chromium discovery stays lazy so Classic Mode startup and tests never
        # acquire an Editorial rendering dependency they do not use.
        self._editorial_renderer: EditorialRenderer | None = None
        self.renderer = FFmpegRenderer(temp_root=temp_root or config.paths.temp_root)
        self.graphic_renderer = GraphicScreenRenderer()
        self.thumbnails = ThumbnailStudioService(self)
        self.qc = MediaQC(self.renderer.binaries)
        self.tts_workers = TTSWorkerSupervisor.from_config(config, output_root=self.store.root)
        self.ideogram_worker = IdeogramWorkerSupervisor.from_config(config)
        self.tts = TTSManager(self)
        # Locking order (never acquire in the opposite order):
        #   1. self._lock          service/project-state serialization
        #                          (create/delete/update_project, narration, scene generation)
        #   2. self._gpu_lock      GPU backend-call serialization. Acquired around
        #                          every GPU-heavy backend section (TTS, ComfyUI
        #                          visuals/music, Whisper alignment). Code holding
        #                          _gpu_lock must never acquire _lock.
        #   3. self._stage_state_lock  leaf lock guarding read-modify-write of
        #                          per-project stage-state.json; it acquires
        #                          nothing else and may be taken under either lock above.
        self._lock = threading.RLock()
        self._gpu_lock = threading.RLock()
        self._stage_state_lock = threading.Lock()
        self._resident_comfy_backend: str | None = None
        self._initialized = False
        if initialize:
            self.initialize()

    def _build_generation_cache(self, config: AppConfig) -> GenerationCache | None:
        if self.mock_mode:
            return None
        cap_gb = float(config.gpu.generation_cache_max_gb)
        return GenerationCache(
            config.paths.generation_cache_root,
            max_bytes=int(cap_gb * (1024 ** 3)) if cap_gb > 0 else None,
        )

    def initialize(self) -> None:
        """Create lightweight local state at application startup, never at module import."""
        with self._lock:
            if self._initialized:
                return
            self.database.initialize()
            self.jobs.recover_interrupted()
            self._initialized = True

    def create_project(self, request: ProjectCreate) -> Project:
        with self._lock:
            base = slugify(request.title)
            slug = base
            suffix = 2
            while self.database.get_project_by_slug(slug) or self.store.project_path(slug).exists():
                slug = f"{base}-{suffix}"
                suffix += 1
            project = Project(
                **request.model_dump(),
                slug=slug,
                settings={"mock_mode": self.mock_mode, "pipeline_version": "1"},
            )
            self.store.create_project(project)
            try:
                self.database.create_project(project)
            except Exception:
                # Preserve the portable directory for recovery rather than deleting user-visible data.
                raise
            return project

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """Delete a project permanently: portable directory and index rows.

        Refuses while non-terminal jobs exist so a running render cannot be
        destroyed underneath itself. The database row goes first: if removing
        files then fails, listing re-indexes the surviving directory instead
        of reporting an orphaned row, and no user data was lost either way.
        """
        with self._lock:
            project = self._project(project_id)
            active = [
                job for job in self.jobs.list(project.id)
                if job.status not in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}
            ]
            if active:
                raise PipelineError(
                    "cancel this project's queued or running jobs before deleting it "
                    f"({len(active)} active)"
                )
            directory = self.store.project_path(project)
            self.database.delete_project(project.id)
            if directory.is_dir():
                shutil.rmtree(directory)
            logger.info("deleted project %s (%s)", project.slug, project.id)
            return {
                "deleted": True,
                "project_id": project.id,
                "slug": project.slug,
                "directory": str(directory),
            }

    def project_snapshot(self, project_id: str) -> dict[str, Any]:
        project, recovery = self._project_with_recovery(project_id)
        scenes = self.database.list_scenes(project_id)
        if not scenes and self._stage_complete(project, "plan"):
            plan = self.store.load_plan(project.slug)
            for scene in plan.scenes:
                self.database.save_scene(scene)
            scenes = self.database.list_scenes(project_id)
        scenes_by_id = {scene.id: scene for scene in scenes}

        assets = []
        assets_by_scene: dict[str, list[Asset]] = {}
        for asset in self.database.list_assets(project_id):
            payload = asset.model_dump(mode="json")
            payload["url"] = f"/api/projects/{project.id}/assets/{asset.id}/file"
            asset_scene = scenes_by_id.get(asset.scene_id or "")
            payload["current"] = (
                self._is_current_visual_asset(asset_scene, asset)
                if asset_scene is not None and asset.settings.get("role") == "visual"
                else True
            )
            assets.append(payload)
            if asset.scene_id:
                assets_by_scene.setdefault(asset.scene_id, []).append(asset)

        asset_summaries: dict[str, dict[str, Any]] = {}
        for s in scenes:
            candidates = [
                a for a in assets_by_scene.get(s.id, [])
                if self._is_current_visual_asset(s, a)
            ]
            current = candidates[-1] if candidates else None
            asset_summaries[s.id] = {
                "id": current.id if current else None,
                "hash": current.hash if current else None,
                "settings": current.settings if current else {},
            }

        scene_payloads: list[dict[str, Any]] = []
        shots_by_scene: dict[str, list[Shot]] = {}
        for shot in self.database.list_shots(project_id):
            shots_by_scene.setdefault(shot.scene_id, []).append(shot)
        for scene in scenes:
            payload = scene.model_dump(mode="json")
            shot_view = self.shots_snapshot(scene)
            payload["shots"] = shot_view.pop("shots")
            payload["shot_summary"] = shot_view
            if scene.visual_type is VisualType.H3_AUDIOVISUAL:
                try:
                    payload["h3"] = h3_continuity_status(
                        scene, scenes, asset_summaries, project.resolution
                    )
                except Exception as exc:
                    payload["h3"] = {
                        "status": "error",
                        "detail": f"Could not compute H3 continuity status: {exc}",
                    }
            scene_payloads.append(payload)

        snapshot = {
            "project": project.model_dump(mode="json"),
            "scenes": scene_payloads,
            "assets": assets,
            "jobs": [job.model_dump(mode="json") for job in self.jobs.list(project_id)],
            "directory": str(self.store.project_path(project)),
            "stage_state": self._read_stage_state(project),
        }
        if project.video_mode is VideoMode.EDITORIAL:
            plan_status = self._editorial_plan_status(project)
            snapshot["editorial"] = {
                "has_edit_plan": plan_status["has_edit_plan"],
                "plan_status": plan_status["plan_status"],
                "stale": plan_status["stale"],
                "stale_reasons": plan_status["stale_reasons"],
                "edit_plan_url": f"/api/projects/{project.id}/editorial/edit-plan",
                "generate_url": f"/api/projects/{project.id}/editorial/plan",
                "preview_url": f"/api/projects/{project.id}/editorial/preview",
            }
        if recovery:
            snapshot["recovery"] = recovery
        return snapshot

    def save_edit_plan(self, project_id: str, plan: EditPlan) -> EditPlan:
        """Validate project ownership/mode and atomically publish an edit plan."""
        with self._lock:
            project = self._project(project_id)
            provenance = self._editorial_plan_provenance(
                project, source_kind=EditPlanSourceKind.MANUAL,
            )
            self.store.save_edit_plan(project.slug, plan)
            self.store.save_edit_plan_provenance(project.slug, provenance)
            self._invalidate_stages(project, {
                "editorial_visual", "timeline", "render_preview",
                "quality_control", "render_final", "thumbnails",
            })
            return plan

    def load_edit_plan(self, project_id: str) -> EditPlan:
        project = self._project(project_id)
        if project.video_mode is not VideoMode.EDITORIAL:
            raise ValueError("project is not in Editorial Mode")
        return self.store.load_edit_plan(project.slug)

    def ensure_edit_plan(self, project_id: str) -> EditPlan:
        """Generate the first Edit Plan from the stored script and narration clock."""
        project = self._project(project_id)
        if project.video_mode is not VideoMode.EDITORIAL:
            raise PipelineError("project is not in Editorial Mode")
        if self.store.edit_plan_exists(project.slug):
            return self.store.load_edit_plan(project.slug)
        try:
            script = self.store.load_plan(project.slug)
        except FileNotFoundError as exc:
            raise PipelineError(
                "Generate the project script before generating an Editorial Edit Plan."
            ) from exc
        self._require_selected_llm_model(project)
        assets = self.database.list_assets(project.id)
        words = self._editorial_word_timings(project)

        def operation() -> tuple[EditPlan, list[Path]]:
            # Keep the planner tied to the same externally managed local model
            # object even when tests or runtime configuration replace it.
            self.editorial_planner.llm = self.director.llm
            edit_plan, draft = self.editorial_planner.plan_with_draft(
                project,
                script,
                assets=assets,
                word_timings=words,
                mock_mode=self.mock_mode,
            )
            edit_plan_path = self.store.save_edit_plan(project.slug, edit_plan)
            provenance = self._editorial_plan_provenance(
                project,
                source_kind=EditPlanSourceKind.PLANNER,
                script=script,
                word_timings=words,
            )
            provenance_path = self.store.save_edit_plan_provenance(
                project.slug, provenance,
            )
            self._invalidate_stages(project, {
                "editorial_visual", "timeline", "render_preview",
                "quality_control", "render_final", "thumbnails",
            })
            outputs = [edit_plan_path, provenance_path]
            if draft is not None:
                draft_path = self.store.project_path(project) / "editorial" / "planner-draft.json"
                self._atomic_json(draft_path, draft.model_dump(mode="json"))
                outputs.append(draft_path)
            return edit_plan, outputs

        return self._execute_stage(
            project,
            "editorial_plan",
            operation,
            backend="mock" if self.mock_mode else "local_llm",
        )[0]

    @staticmethod
    def _editorial_hash(payload: Any) -> str:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _editorial_plan_provenance(
        self,
        project: Project,
        *,
        source_kind: EditPlanSourceKind,
        script: ProjectPlan | None = None,
        word_timings: list[CaptionWord] | None = None,
    ) -> EditPlanProvenance:
        if script is None:
            try:
                script = self.store.load_plan(project.slug)
            except (FileNotFoundError, ValueError):
                script = None
        if word_timings is None:
            try:
                word_timings = self._editorial_word_timings(project)
            except PipelineError:
                word_timings = None

        project_payload = {
            "title": project.title,
            "topic": project.topic,
            "style": project.style,
            "audience": project.audience,
            "instructions": project.instructions,
            "resolution": list(project.resolution),
            "fps": project.fps,
        }
        script_payload = None if script is None else {
            "project_id": script.project_id,
            "scenes": [
                {
                    "id": scene.id,
                    "index": scene.index,
                    "duration": scene.duration,
                    "narration": scene.narration,
                }
                for scene in script.scenes
            ],
        }
        timing_payload = None if word_timings is None else [
            word.to_dict() for word in word_timings
        ]
        return EditPlanProvenance(
            project_id=project.id,
            source_kind=source_kind,
            project_sha256=self._editorial_hash(project_payload),
            script_sha256=(
                self._editorial_hash(script_payload) if script_payload is not None else None
            ),
            word_timings_sha256=(
                self._editorial_hash(timing_payload) if timing_payload is not None else None
            ),
        )

    def _editorial_plan_status(self, project: Project) -> dict[str, Any]:
        if not self.store.edit_plan_exists(project.slug):
            return {
                "has_edit_plan": False,
                "plan_status": "missing",
                "stale": None,
                "stale_reasons": [],
            }
        try:
            recorded = self.store.load_edit_plan_provenance(project.slug)
        except (FileNotFoundError, OSError, ValueError):
            # Plans created before provenance tracking stay valid and usable;
            # the application does not infer that they are stale.
            return {
                "has_edit_plan": True,
                "plan_status": "untracked",
                "stale": None,
                "stale_reasons": [],
            }
        current = self._editorial_plan_provenance(
            project, source_kind=recorded.source_kind,
        )
        comparisons = {
            "project": (recorded.project_sha256, current.project_sha256),
            "script": (recorded.script_sha256, current.script_sha256),
            "word_timings": (
                recorded.word_timings_sha256, current.word_timings_sha256,
            ),
        }
        reasons = [name for name, pair in comparisons.items() if pair[0] != pair[1]]
        return {
            "has_edit_plan": True,
            "plan_status": "stale" if reasons else "current",
            "stale": bool(reasons),
            "stale_reasons": reasons,
        }

    def _editorial_word_timings(self, project: Project) -> list[CaptionWord]:
        path = self.store.project_path(project) / "subtitles" / "word-timings.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [
                CaptionWord(
                    start_seconds=float(item["start_seconds"]),
                    end_seconds=float(item["end_seconds"]),
                    text=str(item["text"]),
                )
                for item in payload["words"]
            ]
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineError(
                "Stored narration word timings are invalid; regenerate caption alignment."
            ) from exc

    def list_projects(self) -> tuple[list[Project], list[dict[str, Any]]]:
        """List projects reconciling the SQLite index with on-disk directories.

        Returns (projects, recovery_issues). The recovery path never discards
        files or creates duplicates: a directory without a database row is
        re-indexed (index only), a database row without a directory is reported
        and kept, and id/slug disagreements are surfaced for repair.
        """
        with self._lock:
            sqlite_projects = self.database.list_projects()
            by_slug = {p.slug: p for p in sqlite_projects}
            recovery: list[dict[str, Any]] = []

            for dir_slug in self.store.list_project_slugs():
                try:
                    disk = self.store.load_project(dir_slug)
                except Exception:
                    recovery.append({
                        "type": "unreadable",
                        "slug": dir_slug,
                        "detail": "project.json could not be parsed; files were left untouched",
                    })
                    continue
                # The directory name must match the slug inside project.json,
                # otherwise the project would be listed under a slug whose
                # directory does not exist. Surface it and do not index it.
                if disk.slug != dir_slug:
                    recovery.append({
                        "type": "conflict",
                        "slug": dir_slug,
                        "project_id": disk.id,
                        "detail": "directory name does not match the project slug in project.json; not indexed",
                    })
                    continue
                existing = by_slug.get(disk.slug)
                if existing is None:
                    try:
                        self.database.create_project(disk)
                        # Rebuild the SQLite scene index from the portable
                        # scene.json files so a planned project is not reported
                        # with zero scenes.
                        scene_result = self._reindex_scenes_from_disk(disk)
                        if scene_result["total"] > 0 and scene_result["failed"] > 0:
                            recovery.append({
                                "type": "partial_recovery",
                                "slug": disk.slug,
                                "project_id": disk.id,
                                "detail": f"recovered {scene_result['succeeded']} of {scene_result['total']} scenes; {scene_result['failed']} failed",
                            })
                    except Exception:
                        recovery.append({
                            "type": "recovery_failed",
                            "slug": disk.slug,
                            "project_id": disk.id,
                            "detail": "project directory found but could not be re-indexed; files left untouched",
                        })
                        continue
                    by_slug[disk.slug] = disk
                    recovery.append({
                        "type": "recovered",
                        "slug": disk.slug,
                        "project_id": disk.id,
                        "detail": "project directory found without a database row; re-indexed",
                    })
                elif existing.id == disk.id:
                    # Same id/slug but content diverged: surface it instead of
                    # silently keeping the database row.
                    if existing.model_dump() != disk.model_dump():
                        recovery.append({
                            "type": "diverged",
                            "slug": disk.slug,
                            "project_id": disk.id,
                            "detail": "database and directory disagree on project content; database row kept, files untouched",
                        })
                else:
                    recovery.append({
                        "type": "conflict",
                        "slug": disk.slug,
                        "project_id": disk.id,
                        "detail": "directory and database disagree on project id; database row kept, files untouched",
                    })

            for project in list(by_slug.values()):
                if not self.store.project_json_exists(project.slug):
                    recovery.append({
                        "type": "orphaned",
                        "slug": project.slug,
                        "project_id": project.id,
                        "detail": "database row has no project directory; row kept, nothing deleted",
                    })

            projects = sorted(by_slug.values(), key=lambda p: p.created_at, reverse=True)
            return projects, recovery

    def _recover_project_by_id(
        self, project_id: str,
    ) -> tuple[Project | None, list[dict[str, Any]]]:
        for dir_slug in self.store.list_project_slugs():
            try:
                disk = self.store.load_project(dir_slug)
            except Exception:
                continue
            if disk.id != project_id:
                continue
            # Only index when the directory name matches the project slug and the
            # database row can actually be created; otherwise return None so the
            # endpoint reports 404 consistently instead of an unindexed 200.
            if disk.slug != dir_slug:
                continue
            try:
                self.database.create_project(disk)
            except Exception:
                return None, []
            recovery: list[dict[str, Any]] = []
            scene_result = self._reindex_scenes_from_disk(disk)
            if scene_result["failed"] > 0:
                recovery.append({
                    "type": "partial_recovery",
                    "slug": disk.slug,
                    "project_id": disk.id,
                    "detail": (
                        f"recovered {scene_result['succeeded']} of "
                        f"{scene_result['total']} scenes; {scene_result['failed']} failed"
                    ),
                })
            return disk, recovery
        return None, []

    def _reindex_scenes_from_disk(self, project: Project) -> dict[str, int]:
        """Rebuild the SQLite scene index from portable scene.json files.

        Used by the recovery path so a planned project keeps its scenes without
        touching or duplicating any on-disk files. Scenes whose project id does
        not match are ignored so a copied/inconsistent directory cannot insert
        or overwrite records belonging to another project. Returns a count of
        attempted / succeeded / failed reindexes so the caller can report
        partial recovery instead of silently claiming success.
        """
        result = {"total": 0, "succeeded": 0, "failed": 0}
        directory = self.store.project_path(project.slug) / "scenes"
        if not directory.is_dir():
            return result
        for entry in sorted(directory.iterdir()):
            match = re.fullmatch(r"(\d{3})", entry.name)
            if not match or not entry.is_dir():
                continue
            index = int(match.group(1)) - 1
            result["total"] += 1
            try:
                scene = self.store.load_scene(project.slug, index)
            except Exception:
                result["failed"] += 1
                continue
            if scene.project_id != project.id:
                result["failed"] += 1
                continue
            try:
                self.database.save_scene(scene)
                result["succeeded"] += 1
            except Exception:
                result["failed"] += 1
                continue
            try:
                disk_shots = [
                    shot for shot in self.store.load_shots(project.slug, index)
                    if shot.project_id == project.id and shot.scene_id == scene.id
                ]
            except Exception:
                continue
            known_ids = set()
            for shot in disk_shots:
                self.database.save_shot(shot)
                known_ids.add(shot.id)
            # A partially indexed database may hold shot rows that no longer
            # exist on disk; the portable files are authoritative here.
            for stale in self.database.list_shots(project.id, scene.id):
                if stale.id not in known_ids:
                    self.database.delete_shot(stale.id)
        return result

    def update_project(self, project_id: str, changes: dict[str, Any]) -> tuple[Project, set[str]]:
        with self._lock:
            return self._update_project_locked(project_id, changes)

    def _update_project_locked(
        self, project_id: str, changes: dict[str, Any],
    ) -> tuple[Project, set[str]]:
        project = self._project(project_id)
        changes = dict(changes)
        # Control flag, not a project field.
        mark_scenes_stale = bool(changes.pop("mark_scenes_stale", False))
        payload = project.model_dump()
        settings_changes = changes.pop("settings", None)
        if settings_changes is not None:
            payload["settings"] = {**project.settings, **settings_changes}
        payload.update(changes)
        payload["updated_at"] = utc_now()
        updated = Project.model_validate(payload)

        # Field-to-stage invalidation. Only fields whose values actually changed
        # invalidate downstream work; sending an unchanged key must be a no-op.
        brief_fields = {
            "title", "topic", "style", "audience", "visual_quality", "instructions",
            # Switching who owns the runtime changes how the director plans.
            "duration_mode", "video_mode",
        }
        dimension_fields = {"target_duration", "aspect_ratio", "fps", "resolution"}
        changed_fields = {
            key for key in changes
            if getattr(project, key, None) != getattr(updated, key, None)
        }
        changed_setting_keys: set[str] = set()
        if settings_changes is not None and project.settings != updated.settings:
            changed_fields.add("settings")
            missing = object()
            changed_setting_keys = {
                key for key in settings_changes
                if project.settings.get(key, missing) != updated.settings.get(key, missing)
            }
        if not changed_fields:
            return project, set()
        invalidated: set[str] = set()
        if brief_fields.intersection(changed_fields):
            invalidated.update(
                {
                    "plan",
                    "references",
                    "visuals",
                    "narration",
                    "music",
                    "subtitles",
                    "timeline",
                    "render_preview",
                    "quality_control",
                    "render_final",
                    "thumbnails",
                    "metadata",
                }
            )
        if dimension_fields.intersection(changed_fields):
            invalidated.update(
                {
                    "timeline",
                    "render_preview",
                    "quality_control",
                    "render_final",
                    "thumbnails",
                    "metadata",
                }
            )
        if "narrator_preference" in changed_fields or "voice" in changed_setting_keys:
            invalidated.update(
                {
                    "narration",
                    "subtitles",
                    "timeline",
                    "render_preview",
                    "quality_control",
                    "render_final",
                    "thumbnails",
                    "metadata",
                }
            )
        if "music" in changed_setting_keys:
            invalidated.update(
                {
                    "music",
                    "timeline",
                    "render_preview",
                    "quality_control",
                    "render_final",
                    "metadata",
                }
            )
        known_settings = {"voice", "music"}
        if not changed_setting_keys.issubset(known_settings):
            invalidated.update(
                {
                    "references",
                    "visuals",
                    "narration",
                    "music",
                    "subtitles",
                    "timeline",
                    "render_preview",
                    "quality_control",
                    "render_final",
                    "thumbnails",
                    "metadata",
                }
            )

        # Capture rollback state before any mutation so a failed update leaves
        # the previous project and stage state intact.
        project_path = self.store.project_path(project) / "project.json"
        previous_project_text = project_path.read_text(encoding="utf-8") if project_path.is_file() else None
        previous_project = project
        stage_path = self._state_path(project)
        previous_stage_text = stage_path.read_text(encoding="utf-8") if stage_path.is_file() else None

        try:
            self._save_project(updated)
            if invalidated:
                self._invalidate_stages(updated, invalidated)
            # Explicit stale-marking for brief or dimension edits (the user's
            # "mark scenes stale" choice): reset non-locked scenes to draft so they
            # re-plan, without rewriting their prompts/narration. Scene writes
            # are rolled back as a group if any portable or SQLite write fails.
            scene_affecting = brief_fields | dimension_fields
            if mark_scenes_stale and scene_affecting.intersection(changed_fields):
                self._mark_scenes_stale(updated)
            # Thumbnail invalidation is transactional and intentionally last, so
            # no later project-edit step can fail after it commits.
            if "thumbnails" in invalidated:
                self.thumbnails.invalidate(updated.id)
        except Exception:
            if previous_project_text is not None:
                try:
                    self.store._atomic_json(project_path, json.loads(previous_project_text))
                    self.database.update_project(previous_project)
                except Exception:
                    pass
            if previous_stage_text is not None:
                try:
                    self.store._atomic_json(stage_path, json.loads(previous_stage_text))
                except Exception:
                    pass
            raise

        return updated, invalidated

    def _mark_scenes_stale(self, project: Project) -> None:
        originals = [
            scene for scene in self.database.list_scenes(project.id)
            if not scene.locked and scene.status is not SceneStatus.DRAFT
        ]
        try:
            for scene in originals:
                stale = scene.model_copy(update={"status": SceneStatus.DRAFT})
                self.store.save_scene(project.slug, stale)
                self.database.save_scene(stale)
        except Exception:
            rollback_errors: list[Exception] = []
            for original in originals:
                try:
                    self.store.save_scene(project.slug, original)
                    self.database.save_scene(original)
                except Exception as rollback_error:  # pragma: no cover - catastrophic I/O
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                logger.error(
                    "scene stale-mark rollback was incomplete for project %s (%d errors)",
                    project.id,
                    len(rollback_errors),
                )
            raise

    def queue_narration(self, project_id: str, request: NarrationRequest) -> GenerationJob:
        """Persist a restartable narration job; API background execution is separate."""
        self._project(project_id)
        return self.jobs.enqueue(GenerationJob(
            project_id=project_id,
            stage="narration",
            backend=request.provider,
            parameters=request.model_dump(mode="json"),
        ))

    def run_narration_job(self, job_id: str) -> Path:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        request = NarrationRequest.model_validate(job.parameters)
        with media_process_scope(job_id):
            return self._run_narration_job(job, request)

    def _run_narration_job(self, job: GenerationJob, request: NarrationRequest) -> Path:
        job_id = job.id
        try:
            self.jobs.transition(job_id, JobStatus.PREPARING, progress=0.05)
            self.jobs.transition(job_id, JobStatus.LOADING_MODEL, progress=0.1)
            self.jobs.transition(job_id, JobStatus.GENERATING, progress=0.2)
            # TTS synthesis is GPU-heavy: _gpu_lock serializes it against
            # visuals/music GPU sections (order: _lock -> _gpu_lock).
            with self._lock, self._gpu_lock:
                output = self.tts.generate(
                    job.project_id, request, job_id=job_id, activate=False,
                )
            # A cancel that landed during synthesis keeps the completed take for
            # inspection but must not replace the project's active narration.
            current = self.jobs.get(job_id)
            if current is not None and current.status is JobStatus.CANCELED:
                return output
            self.jobs.transition(job_id, JobStatus.POSTPROCESSING, progress=0.9)
            output = self.tts.activate_take_path(
                job.project_id, output, stage_job_id=job_id,
            )
            # A cancel that landed during synthesis wins: the row stays
            # canceled (the audio file is kept) instead of raising an invalid
            # CANCELED -> COMPLETED transition.
            current = self.jobs.get(job_id)
            if current is None or current.status is not JobStatus.CANCELED:
                self.jobs.complete(job_id)
            return output
        except Exception as exc:
            current = self.jobs.get(job_id)
            if current is not None and current.status not in {
                JobStatus.FAILED, JobStatus.CANCELED, JobStatus.COMPLETED,
            }:
                self.jobs.fail(job_id, redact_secrets(exc))
            raise

    def select_project_llm_model(self, project_id: str, model: str) -> Project:
        project = self._project(project_id)
        updated = project.model_copy(
            update={"selected_llm_model": model, "updated_at": utc_now()}
        )
        self._save_project(updated)
        self._invalidate_stages(updated, {"plan", "metadata"})
        return updated

    def _require_selected_llm_model(self, project: Project) -> None:
        """Require a deliberate, still-served router model for real LLM work.

        The router remains the owner of loading and unloading.  This check only
        verifies a project selection against its safe model listing before any
        prompt is sent, preventing the historical ``auto -> first model``
        fallback from silently choosing a model for a script.
        """
        if self.mock_mode or self.director.llm is None:
            return
        model = project.selected_llm_model.strip()
        if not model or model == "auto":
            raise BackendError(
                BackendErrorCode.MODEL_SELECTION_REQUIRED,
                "Choose a local LLM model for this project before generating a script.",
            )
        self.director.llm.model = model
        # selected_model performs a model-list request and validates the exact
        # project selection without asking the external router to load anything.
        self.director.llm.selected_model()

    def ensure_plan(self, project_id: str, *, force: bool = False) -> ProjectPlan:
        project = self._project(project_id)
        if not force and self._stage_complete(project, "plan"):
            return self.store.load_plan(project.slug)
        if force:
            self._invalidate_stages(
                project,
                {
                    "narration",
                    "references",
                    "visuals",
                    "music",
                    "subtitles",
                    "timeline",
                    "render_preview",
                    "quality_control",
                    "render_final",
                    "thumbnails",
                    "metadata",
                },
            )

        def operation() -> tuple[ProjectPlan, list[Path]]:
            self._require_selected_llm_model(project)
            comparison_mode = (
                self.config.image_generation.comparison_mode
                or bool(project.settings.get("comparison_mode", False))
            )
            plan, director_draft = self.director.plan_with_draft(
                project, mock_mode=self.mock_mode, comparison_mode=comparison_mode,
            )
            existing_by_index = {
                scene.index: scene for scene in self.database.list_scenes(project.id)
            }
            if existing_by_index:
                # The director materializes h3_continuity links against the
                # fresh in-memory scene ids; rewriting each scene id to the
                # persisted identity must rewrite those links too, otherwise
                # every continuation scene keeps a dangling predecessor.
                fresh_to_persisted = {
                    scene.id: existing_by_index[scene.index].id
                    for scene in plan.scenes
                    if scene.index in existing_by_index
                    and existing_by_index[scene.index].id != scene.id
                }
                remapped: list[Scene] = []
                for scene in plan.scenes:
                    existing = existing_by_index.get(scene.index)
                    if existing is None:
                        remapped.append(scene)
                        continue
                    settings = scene.settings
                    continuity = settings.get("h3_continuity")
                    updates: dict[str, Any] = {"id": existing.id}
                    if (
                        isinstance(continuity, dict)
                        and continuity.get("predecessor_scene_id") in fresh_to_persisted
                    ):
                        settings = {
                            **settings,
                            "h3_continuity": {
                                **continuity,
                                "predecessor_scene_id": fresh_to_persisted[
                                    continuity["predecessor_scene_id"]
                                ],
                            },
                        }
                        updates["settings"] = settings
                    remapped.append(scene.model_copy(update=updates))
                plan = plan.model_copy(update={"scenes": remapped})
            project_dir = self.store.project_path(project)
            outline = project_dir / "script" / "outline.md"
            script = project_dir / "script" / "script.md"
            storyboard = project_dir / "script" / "storyboard.json"
            outline.write_text(
                f"# {project.title}\n\n" + "\n".join(f"- {item}" for item in plan.outline) + "\n",
                encoding="utf-8",
            )
            script.write_text(
                f"# {project.title}\n\n" + "\n\n".join(scene.narration for scene in plan.scenes) + "\n",
                encoding="utf-8",
            )
            # Storyboard artifact: per-scene routing metadata plus the exact
            # model-specific prompts (krea / qwen / ideogram structured JSON)
            # the image stage will consume. Human-readable and portable.
            self._atomic_json(storyboard, {
                "project_id": project.id,
                "title": project.title,
                "comparison_mode": comparison_mode,
                "scenes": [
                    storyboard_entry(scene, style=project.style) for scene in plan.scenes
                ],
            })
            for scene in plan.scenes:
                self.database.save_scene(scene)
                self.store.save_scene(project.slug, scene)
                self.database.record_prompt(
                    project.id,
                    "director",
                    scene.visual_prompt,
                    scene_id=scene.id,
                    negative_prompt=scene.negative_prompt,
                    seed=scene.seed,
                    settings=scene.settings,
                    created_at=utc_now(),
                )
            self.store.save_plan(project.slug, plan)
            artifacts = [
                outline, script, storyboard, project_dir / "plan.json",
            ]
            if director_draft is not None:
                # Persist the normalized director output so an incomplete model
                # response (empty titles/prompts repaired by fallback) stays
                # diagnosable instead of vanishing behind normalization.
                draft_path = project_dir / "script" / "director-draft.json"
                self._atomic_json(draft_path, director_draft.model_dump(mode="json"))
                artifacts.append(draft_path)
            updates: dict[str, Any] = {
                "status": ProjectStatus.PLANNING,
                "updated_at": utc_now(),
            }
            # duration_mode="llm": the director authored the runtime, so adopt it
            # as the project target. Downstream consumers (music generation,
            # mock narration, UI) read project.target_duration; real renders
            # follow the actual TTS audio either way.
            if abs(plan.target_duration - project.target_duration) > 1e-6:
                updates["target_duration"] = plan.target_duration
            updated = project.model_copy(update=updates)
            self._save_project(updated)
            return plan, artifacts

        return self._execute_stage(project, "plan", operation, backend="mock" if self.mock_mode else "local_llm")[0]

    def run_project(
        self,
        project_id: str,
        *,
        force: bool = False,
        parent_job_id: str | None = None,
    ) -> Path:
        # Attribute every media subprocess this run spawns to the parent job so
        # canceling it never kills an unrelated job's ffmpeg/ffprobe.
        with media_process_scope(parent_job_id):
            return self._run_project(project_id, force=force, parent_job_id=parent_job_id)

    def _run_project(
        self,
        project_id: str,
        *,
        force: bool,
        parent_job_id: str | None,
    ) -> Path:
        project = self._project(project_id)
        if parent_job_id:
            self._start_parent_job(parent_job_id)
        try:
            self.ensure_plan(project_id, force=force)
            project = self._project(project_id)
            self._save_project(project.model_copy(update={"status": ProjectStatus.GENERATING, "updated_at": utc_now()}))
            self._ensure_narration(project, force=force)
            self._check_parent_job(parent_job_id)
            self._ensure_references(project, force=force)
            self._check_parent_job(parent_job_id)
            self._ensure_visuals(project, force=force)
            self._check_parent_job(parent_job_id)
            self._ensure_music(project, force=force)
            self._ensure_subtitles(project, force=force)
            self._check_parent_job(parent_job_id)
            if project.video_mode is VideoMode.EDITORIAL:
                self.ensure_edit_plan(project.id)
                self._ensure_editorial_visual(project, force=force)
                self._check_parent_job(parent_job_id)
            self._ensure_timeline(project, force=force)
            self._ensure_preview(project, force=force)
            self._ensure_qc(project, force=force)
            self._check_parent_job(parent_job_id)
            final = self._ensure_final(project, force=force)
            self._ensure_thumbnails(project, force=force)
            self._ensure_metadata(project, force=force)
            self._check_parent_job(parent_job_id)
            current = self._project(project_id)
            self._save_project(current.model_copy(update={"status": ProjectStatus.COMPLETED, "updated_at": utc_now()}))
            if parent_job_id:
                self.jobs.transition(parent_job_id, JobStatus.POSTPROCESSING, progress=0.95)
                self.jobs.complete(parent_job_id)
            return final
        except Exception as exc:
            current = self._project(project_id)
            parent = self.jobs.get(parent_job_id) if parent_job_id else None
            failed_status = (
                ProjectStatus.CANCELED
                if parent and parent.status is JobStatus.CANCELED
                else ProjectStatus.FAILED
            )
            self._save_project(current.model_copy(update={"status": failed_status, "updated_at": utc_now()}))
            if parent_job_id:
                active = self.jobs.get(parent_job_id)
                if active and active.status not in {JobStatus.FAILED, JobStatus.CANCELED}:
                    self.jobs.fail(parent_job_id, redact_secrets(exc))
            raise

    def queue_render(self, project_id: str, *, force: bool = False) -> GenerationJob:
        project = self._project(project_id)  # 404 before any conflict/validation error
        # One in-flight render/pipeline per project: both run_render and
        # run_project write preview.mp4/final.mp4 and race _archive_output.
        active = next(
            (
                j for j in self.jobs.list(project_id)
                if j.stage in {"render", "pipeline"}
                and j.status not in {
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                }
            ),
            None,
        )
        if active is not None:
            raise PipelineError(
                "A render or pipeline job is already running for this project. "
                "Wait for it to finish, or cancel it, before queueing another render."
            )
        self.validate_render_inputs(project_id)
        job = GenerationJob(
            project_id=project_id,
            stage="render",
            backend="ffmpeg",
            parameters={
                "force": force,
                "current_stage": "queued",
                "stages": (["editorial_visual"] if project.video_mode is VideoMode.EDITORIAL else []) + [
                    "timeline",
                    "render_preview",
                    "quality_control",
                    "render_final",
                    "thumbnails",
                ],
            },
        )
        return self.jobs.enqueue(job)

    def queue_caption_alignment(self, project_id: str) -> GenerationJob:
        """Queue a standalone rebuild of captions from the active narration."""
        project = self._project(project_id)
        narration = self.store.project_path(project) / "narration" / "master.wav"
        if not narration.is_file():
            raise PipelineError("Generate or select narration before aligning captions.")
        if not self.mock_mode:
            health = self.registry.get("whisper").health()
            if not self.config.backends.whisper.enabled or health.get("status") != "healthy":
                guidance = health.get("install_guidance") or "Enable the configured local Whisper backend."
                raise PipelineError(str(guidance))
        active = next(
            (
                job for job in self.jobs.list(project_id)
                if job.stage in {"pipeline", "render", "narration", "caption_alignment"}
                and job.status not in {
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                }
            ),
            None,
        )
        if active is not None:
            raise PipelineError("Another narration, caption, or render job is already running for this project.")
        return self.jobs.enqueue(GenerationJob(
            project_id=project_id,
            stage="caption_alignment",
            backend="mock" if self.mock_mode else "whisper",
            parameters={"force": True},
        ))

    def run_caption_alignment_job(self, job_id: str) -> list[SubtitleCue]:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        try:
            self.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
            self.jobs.transition(job.id, JobStatus.LOADING_MODEL, progress=0.10)
            self.jobs.transition(job.id, JobStatus.GENERATING, progress=0.20)
            project = self._project(job.project_id)
            cues = self._ensure_subtitles(project, force=True)
            self._invalidate_stages(
                project,
                {"timeline", "render_preview", "quality_control", "render_final", "thumbnails", "metadata"},
            )
            current = self.jobs.get(job.id)
            if current is not None and current.status is JobStatus.CANCELED:
                return cues
            self.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.95)
            self.jobs.complete(job.id)
            return cues
        except Exception as exc:
            current = self.jobs.get(job.id)
            if current is not None and current.status not in {
                JobStatus.FAILED, JobStatus.CANCELED, JobStatus.COMPLETED,
            }:
                self.jobs.fail(job.id, redact_secrets(exc))
            raise

    def validate_render_inputs(self, project_id: str) -> Timeline | EditPlan:
        """Validate existing media without invoking any generation backend."""
        project = self._project(project_id)
        if project.video_mode is VideoMode.EDITORIAL:
            return self._validate_editorial_render_inputs(project)
        scenes = self.database.list_scenes(project.id)
        if not scenes:
            raise PipelineError(
                "Cannot render because this project has no scenes. Create the script and scenes first."
            )
        return self._build_timeline(project)

    def run_render(
        self,
        project_id: str,
        *,
        force: bool = False,
        parent_job_id: str | None = None,
    ) -> Path:
        """Assemble existing project media without planning or generating content."""
        with media_process_scope(parent_job_id):
            return self._run_render_impl(project_id, force=force, parent_job_id=parent_job_id)

    def _run_render_impl(
        self,
        project_id: str,
        *,
        force: bool,
        parent_job_id: str | None,
    ) -> Path:
        project = self._project(project_id)
        if parent_job_id:
            self._start_parent_job(parent_job_id)
        try:
            self._update_parent_job(parent_job_id, progress=0.08, current_stage="validating_inputs")
            self.validate_render_inputs(project_id)
            if force:
                invalidated = {
                        "timeline",
                        "render_preview",
                        "quality_control",
                        "render_final",
                        "thumbnails",
                    }
                if project.video_mode is VideoMode.EDITORIAL:
                    invalidated.add("editorial_visual")
                self._invalidate_stages(project, invalidated)
            self._save_project(
                project.model_copy(update={"status": ProjectStatus.RENDERING, "updated_at": utc_now()})
            )

            if project.video_mode is VideoMode.EDITORIAL:
                self._update_parent_job(
                    parent_job_id, progress=0.12, current_stage="editorial_visual",
                )
                self._ensure_editorial_visual(project, force=force)
                self._check_parent_job(parent_job_id)

            self._update_parent_job(parent_job_id, progress=0.15, current_stage="timeline")
            self._ensure_timeline(project, force=force)
            self._check_parent_job(parent_job_id)

            self._update_parent_job(parent_job_id, progress=0.20, current_stage="render_preview")
            self._update_parent_job(parent_job_id, progress=0.25, current_stage="render_preview")
            self._ensure_preview(project, force=force)
            self._check_parent_job(parent_job_id)

            self._update_parent_job(parent_job_id, progress=0.45, current_stage="quality_control")
            self._update_parent_job(parent_job_id, progress=0.50, current_stage="quality_control")
            self._ensure_qc(project, force=force)
            self._check_parent_job(parent_job_id)

            self._update_parent_job(parent_job_id, progress=0.60, current_stage="render_final")
            self._update_parent_job(parent_job_id, progress=0.65, current_stage="render_final")
            final = self._ensure_final(project, force=force)
            self._check_parent_job(parent_job_id)

            self._update_parent_job(parent_job_id, progress=0.80, current_stage="thumbnails")
            self._update_parent_job(parent_job_id, progress=0.85, current_stage="thumbnails")
            self._ensure_thumbnails(project, force=force)
            self._check_parent_job(parent_job_id)

            current = self._project(project_id)
            self._save_project(
                current.model_copy(update={"status": ProjectStatus.COMPLETED, "updated_at": utc_now()})
            )
            if parent_job_id:
                self.jobs.transition(parent_job_id, JobStatus.POSTPROCESSING, progress=0.98)
                self.jobs.complete(parent_job_id)
            return final
        except Exception as exc:
            current = self._project(project_id)
            parent = self.jobs.get(parent_job_id) if parent_job_id else None
            failed_status = (
                ProjectStatus.CANCELED
                if parent and parent.status is JobStatus.CANCELED
                else ProjectStatus.FAILED
            )
            self._save_project(
                current.model_copy(update={"status": failed_status, "updated_at": utc_now()})
            )
            if parent_job_id:
                active = self.jobs.get(parent_job_id)
                if active and active.status not in {
                    JobStatus.FAILED,
                    JobStatus.CANCELED,
                    JobStatus.COMPLETED,
                }:
                    self.jobs.fail(parent_job_id, redact_secrets(exc))
            raise

    _EXECUTABLE_STAGES = {
        "pipeline", "render", "narration", "narration_chunk",
        "visual_batch", "caption_alignment", "shot_generate", "scene_render",
    }

    def job_is_executable(self, job: GenerationJob) -> bool:
        """True when a job can be executed standalone (and therefore retried).

        Child-stage jobs (plan, visuals, music, ...) are bookkeeping rows
        driven inline by their parent pipeline/render/narration job.
        ``scene_visual`` and ``shot_generate`` rows are executable only when
        they are standalone: batch/pipeline children carry a ``parent_job_id``
        and are re-run by retrying that parent.
        """
        if job.stage in {"scene_visual", "shot_generate"}:
            return not job.parameters.get("parent_job_id")
        return job.stage in self._EXECUTABLE_STAGES or job.stage.startswith("thumbnail:")

    def execute_job(self, job_id: str) -> None:
        """Run a queued top-level job to completion; used by retry after restarts.

        Every runner re-enters safely: completed stages are kept and only the
        missing ones are rebuilt.
        """
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        if not self.job_is_executable(job):
            raise PipelineError(
                f"stage '{job.stage}' runs inside its parent pipeline; rerun that stage instead"
            )
        if job.status is JobStatus.CANCELED:
            return
        try:
            if job.stage == "pipeline":
                self.run_project(
                    job.project_id,
                    force=bool(job.parameters.get("force")),
                    parent_job_id=job.id,
                )
            elif job.stage == "render":
                self.run_render(
                    job.project_id,
                    force=bool(job.parameters.get("force")),
                    parent_job_id=job.id,
                )
            elif job.stage == "narration":
                self.run_narration_job(job.id)
            elif job.stage == "narration_chunk":
                self.tts.run_chunk_regeneration_job(job.id)
            elif job.stage == "caption_alignment":
                self.run_caption_alignment_job(job.id)
            elif job.stage == "visual_batch":
                self.run_visual_batch_job(job.id)
            elif job.stage == "scene_visual":
                # Standalone row (no parent_job_id): re-run its scene. The
                # force flag is preserved; a visual produced by a canceled
                # earlier attempt satisfies the retry without new GPU work.
                if not job.scene_id:
                    raise PipelineError("scene_visual job has no scene to generate")
                self.generate_scene(job.scene_id, job=self.jobs.get(job_id) or job)
            elif job.stage == "shot_generate":
                if not job.shot_id:
                    raise PipelineError("shot_generate job has no shot to generate")
                self.generate_shot(
                    job.shot_id,
                    force=bool(job.parameters.get("force")),
                    job=self.jobs.get(job_id) or job,
                )
            elif job.stage == "scene_render":
                if not job.scene_id:
                    raise PipelineError("scene_render job has no scene to compile")
                self.render_scene(
                    job.scene_id,
                    force=bool(job.parameters.get("force")),
                    job=self.jobs.get(job_id) or job,
                )
            else:
                self.thumbnails.run_candidate_job(job.id)
        except Exception as exc:
            # Runners record their own failures; guarantee a terminal state even
            # when one escapes before their handling, and never crash the caller.
            current = self.jobs.get(job_id)
            if current is not None and current.status not in {
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
            }:
                self.jobs.fail(job_id, redact_secrets(exc))

    def generate_scene(
        self,
        scene_id: str,
        *,
        force: bool = False,
        job: GenerationJob | None = None,
    ) -> Asset:
        """Generate (or regenerate) one scene's visual.

        Every real run is tracked as a ``scene_visual`` job row so the Job
        Monitor, storyboard, and scene editor can follow it live. Callers
        that pre-created a row (visual batches, pipeline runs) pass it via
        ``job``; standalone callers get one enqueued here. Canceling the row
        is honored at the completion boundary: like render stages, work that
        already finished is kept, the row stays canceled, and the caller sees
        an error instead of a success that would contradict the monitor.
        """
        # Scene generation shares the same serialization lock as other GPU-heavy stages.
        with self._lock:
            scene = self.database.get_scene(scene_id)
            if scene is None:
                raise KeyError(f"scene not found: {scene_id}")
            project = self._project(scene.project_id)
            existing = [
                asset for asset in self.database.list_assets(project.id, scene.id)
                if self._is_current_visual_asset(scene, asset)
            ]
            if existing and not force:
                if job is None:
                    # A standalone no-op still leaves a row: the user asked
                    # for this scene's visual, and the monitor should show it
                    # was satisfied without new generation.
                    job = self.jobs.enqueue(GenerationJob(
                        project_id=project.id,
                        scene_id=scene.id,
                        stage="scene_visual",
                        backend="mock" if self.mock_mode else "automatic",
                        parameters={"force": force},
                    ))
                if job.status is not JobStatus.COMPLETED:
                    # The pre-created row is already satisfied by the current
                    # visual (the scene was generated after the batch was
                    # queued): complete it without touching any media. A row
                    # that is already COMPLETED (a retried batch reusing a
                    # child that succeeded on an earlier run) stays as-is.
                    self.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
                    self.jobs.transition(job.id, JobStatus.GENERATING, progress=0.2)
                    self.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.9)
                    self.jobs.complete(job.id)
                return existing[-1]
            if force and scene.visual_type not in {
                VisualType.GRAPHIC_SCREEN,
                VisualType.H3_AUDIOVISUAL,
            }:
                for asset in existing:
                    path = self.store.project_path(project) / asset.filepath
                    if path.is_file():
                        self.store.archive_variant(project.slug, asset.filepath)
            if job is None:
                job = self.jobs.enqueue(GenerationJob(
                    project_id=project.id,
                    scene_id=scene.id,
                    stage="scene_visual",
                    backend="mock" if self.mock_mode else "automatic",
                    parameters={"force": force},
                ))
            # Attribute any spawned media subprocesses to this job so canceling
            # the row kills its in-flight probes/renders — never another job's.
            with media_process_scope(job.id):
                asset = self._run_scene_visual_job(project, scene, job)
            self._invalidate_stages(
                project,
                {"timeline", "render_preview", "quality_control", "render_final"},
            )
            return asset

    def _run_scene_visual_job(
        self,
        project: Project,
        scene: Scene,
        job: GenerationJob,
    ) -> Asset:
        """Drive a ``scene_visual`` job row around the actual generation.

        Shared by standalone generation, visual batches, and pipeline runs so
        every path transitions the row identically. The row always reaches a
        terminal state, even when generation raises; a cancel that lands
        mid-generation wins at the completion boundary (the produced visual is
        kept — "completed stages are kept" — and the row stays canceled).
        """
        try:
            self.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
            self.jobs.transition(job.id, JobStatus.GENERATING, progress=0.2)
            asset = self._generate_visual(
                project, scene, use_cache=not bool(job.parameters.get("force")),
            )
            current = self.jobs.get(job.id)
            if current is not None and current.status is JobStatus.CANCELED:
                raise PipelineError("job was canceled")
            self.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.9)
            self.jobs.complete(job.id)
            return asset
        except Exception as exc:
            current = self.jobs.get(job.id)
            if current is not None and current.status not in {
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
            }:
                self.jobs.fail(job.id, redact_secrets(exc))
            raise

    # Non-image batch ordering. Routed image scenes are ordered separately by
    # their effective model so Krea source frames stay adjacent even when some
    # are Image Motion and others are stills.
    _VISUAL_BATCH_ORDER = (
        VisualType.TEXT_OVERLAY_STILL,
        VisualType.QWEN_IMAGE_STILL,
        VisualType.IDEOGRAM4_STILL,
        VisualType.KREA2_STILL,
        VisualType.IMAGE_MOTION,
        VisualType.GRAPHIC_SCREEN,
        VisualType.H3_AUDIOVISUAL,
    )

    def queue_visual_batch(
        self,
        project_id: str,
        *,
        visual_type: VisualType | None = None,
        image_model: str | None = None,
    ) -> GenerationJob:
        """Queue sequential generation for every scene still missing a visual.

        Only unlocked scenes without a current visual asset are selected.
        Still-image work is grouped by its effective image model before other
        types, preserving the resident Krea/Ideogram/Qwen family between
        compatible scenes. Existing visuals are never archived, which also
        makes retries cheap: a rerun skips everything that already succeeded.
        """
        project = self._project(project_id)  # 404 before any conflict error
        active = next(
            (
                j for j in self.jobs.list(project.id)
                if j.stage == "visual_batch"
                and j.status not in {
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                }
            ),
            None,
        )
        if active is not None:
            raise PipelineError(
                "A visual batch job is already running for this project. "
                "Wait for it to finish, or cancel it, before queueing another one."
            )
        scenes = self.database.list_scenes(project.id)
        scenes_by_id = {scene.id: scene for scene in scenes}
        root = self.store.project_path(project)
        has_visual: set[str] = set()
        for asset in self.database.list_assets(project.id):
            scene = scenes_by_id.get(asset.scene_id or "")
            path = root / asset.filepath
            if (
                scene is not None
                and self._is_current_visual_asset(scene, asset)
                and path.is_file()
                and path.stat().st_size > 0
            ):
                has_visual.add(asset.scene_id)

        if image_model is not None and image_model not in {
            option.value for option in ImageModelOption
        }:
            raise ValueError("image_model must be krea, qwen_image, or ideogram4_local")

        def effective_image_model(scene: Scene) -> str | None:
            if scene.visual_type.value not in ROUTABLE_VISUAL_TYPES:
                return None
            routed = self._resolve_scene_image_model(scene)
            if routed is not None:
                return routed.value
            if scene.visual_type is VisualType.QWEN_IMAGE_STILL:
                return ImageModelOption.QWEN_IMAGE.value
            if (
                scene.visual_type is VisualType.IMAGE_MOTION
                and self._image_motion_source(scene) == "qwen_image_2512"
            ):
                return ImageModelOption.QWEN_IMAGE.value
            return ImageModelOption.KREA.value

        image_order = {
            ImageModelOption.KREA.value: 0,
            ImageModelOption.IDEOGRAM4_LOCAL.value: 1,
            ImageModelOption.QWEN_IMAGE.value: 2,
        }
        image_type_order = {
            VisualType.TEXT_OVERLAY_STILL: 0,
            VisualType.KREA2_STILL: 0,
            VisualType.IDEOGRAM4_STILL: 0,
            VisualType.QWEN_IMAGE_STILL: 0,
            VisualType.IMAGE_MOTION: 1,
            VisualType.FLUX_STILL: 2,
        }

        def order_key(scene: Scene) -> tuple[int, int, int, int]:
            model = effective_image_model(scene)
            if model is not None:
                return (
                    0, image_order[model],
                    image_type_order.get(scene.visual_type, len(image_type_order)),
                    scene.index,
                )
            group = (
                self._VISUAL_BATCH_ORDER.index(scene.visual_type)
                if scene.visual_type in self._VISUAL_BATCH_ORDER
                else len(self._VISUAL_BATCH_ORDER)
            )
            return (1, group, 0, scene.index)

        selected = sorted(
            (
                s for s in scenes
                if not s.locked
                and s.id not in has_visual
                and (visual_type is None or s.visual_type is visual_type)
                and (image_model is None or effective_image_model(s) == image_model)
            ),
            key=order_key,
        )
        if not selected:
            scope = f" of type {visual_type.value}" if visual_type else ""
            if image_model:
                scope += f" using {image_model}"
            raise PipelineError(f"No unlocked scenes{scope} need a visual.")
        backend = "mock" if self.mock_mode else "automatic"
        parent = self.jobs.enqueue(GenerationJob(
            project_id=project.id,
            stage="visual_batch",
            backend=backend,
            parameters={
                "visual_type": visual_type.value if visual_type else None,
                "image_model": image_model,
                "scene_ids": [scene.id for scene in selected],
            },
        ))
        # One child row per scene up front: storyboard cards show live queued/
        # generating state from these rows, and each can be canceled alone.
        for scene in selected:
            self.jobs.enqueue(GenerationJob(
                project_id=project.id,
                scene_id=scene.id,
                stage="scene_visual",
                backend=backend,
                parameters={"parent_job_id": parent.id},
            ))
        return parent

    def run_visual_batch_job(self, job_id: str) -> None:
        # Attribute any spawned media subprocesses to this job so canceling it
        # never kills an unrelated job's processes (same contract as renders).
        with media_process_scope(job_id):
            self._run_visual_batch(job_id)

    def _run_visual_batch(self, job_id: str) -> None:
        try:
            self._start_parent_job(job_id)
            self._execute_visual_batch(job_id)
        except Exception as exc:
            # Mirror execute_job: guarantee a terminal state and never crash
            # the caller once the HTTP response has returned; the job row
            # carries the redacted error for the Job Monitor.
            message = redact_secrets(exc)
            current = self.jobs.get(job_id)
            if current is not None and current.status not in {
                JobStatus.FAILED, JobStatus.CANCELED, JobStatus.COMPLETED,
            }:
                self.jobs.fail(job_id, message)
            logger.warning("Visual batch job %s did not complete: %s", job_id, message)

    def _execute_visual_batch(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        scene_ids = [str(item) for item in job.parameters.get("scene_ids", [])]
        total = len(scene_ids)
        children = {
            child.scene_id: child
            for child in self.jobs.list(job.project_id)
            if child.stage == "scene_visual"
            and child.parameters.get("parent_job_id") == job_id
        }

        # A retried batch re-queues its failed children (and children canceled
        # along with a canceled parent) so every selected scene gets another
        # attempt. Individually canceled children stay opted out.
        for key, child in list(children.items()):
            if child.status is JobStatus.FAILED or (
                child.status is JobStatus.CANCELED
                and child.parameters.get("canceled_with_parent")
            ):
                try:
                    children[key] = self.jobs.retry(child.id)
                except InvalidJobTransition:
                    pass

        def cancel_remaining(first_position: int) -> None:
            for scene_id in scene_ids[first_position:]:
                child = children.get(scene_id)
                if child is None or child.status is not JobStatus.QUEUED:
                    continue
                try:
                    updated = self.jobs.cancel(child.id)
                except InvalidJobTransition:
                    continue
                self.database.save_job(updated.model_copy(update={
                    "parameters": {
                        **updated.parameters, "canceled_with_parent": True,
                    },
                }))

        failures: list[str] = []
        for position, scene_id in enumerate(scene_ids):
            current = self.jobs.get(job_id)
            if current is None or current.status is JobStatus.CANCELED:
                cancel_remaining(position)
                return
            child = children.get(scene_id)
            if child is not None and child.status is JobStatus.CANCELED:
                continue  # user opted this scene out of the batch
            scene = self.database.get_scene(scene_id)
            label = (
                f"S{int(scene.index or 0) + 1} · {scene.visual_type.value}"
                if scene is not None else scene_id
            )
            try:
                self._update_parent_job(
                    job_id,
                    progress=round((position / max(total, 1)) * 0.95 + 0.01, 4),
                    current_stage=f"{position + 1}/{total} · {label}",
                )
            except PipelineError:
                cancel_remaining(position)
                raise
            # The child row drives the whole lifecycle (queued → … → terminal)
            # inside generate_scene; a mid-flight cancel of the child is
            # honored there and surfaces as a canceled row, not a failure.
            try:
                self.generate_scene(scene_id, job=child)
            except Exception as exc:
                child_now = self.jobs.get(child.id) if child is not None else None
                if child_now is not None and child_now.status is JobStatus.CANCELED:
                    continue  # user canceled this scene mid-flight; stays opted out
                if (
                    child_now is not None
                    and child_now.status not in {
                        JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                    }
                ):
                    # The exception escaped before the child's row reached a
                    # terminal state (a failure before generation even
                    # started): the Job Monitor must show it failed, not queued.
                    self.jobs.fail(child.id, redact_secrets(exc))
                failures.append(f"{label}: {redact_secrets(exc)}")
                continue
        if failures:
            detail = "; ".join(failures[:5])
            if len(failures) > 5:
                detail += f"; (+{len(failures) - 5} more)"
            raise PipelineError(
                f"{len(failures)} of {total} queued visuals failed: {detail}. "
                "Retry the batch to attempt only what is still missing."
            )
        self._update_parent_job(job_id, progress=0.99, current_stage="done")
        self.jobs.complete(job_id)

    def cancel_job(self, job_id: str) -> GenerationJob:
        """Cancel one job (and, for visual batches, every job it created).

        Batch children are canceled with the parent: queued rows immediately,
        the in-flight child's row at once, with its generation honored at the
        completion boundary (the produced visual is kept, the row stays
        canceled — the same contract as single-scene cancels). Each job's
        media subprocesses are killed so in-flight probes/renders stop;
        unrelated jobs' processes survive.
        """
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        canceled = self.jobs.cancel(job_id)
        cancel_media_processes_for_job(job_id)
        if job.stage == "visual_batch":
            self._cancel_batch_children(canceled)
        return canceled

    def cancel_all_jobs(self, project_id: str) -> list[GenerationJob]:
        """Cancel every active job for a project (storyboard's Cancel all).

        Visual batches are canceled parent-first so their children carry
        ``canceled_with_parent`` (a batch retry re-queues them) instead of
        being treated as individually opted out. Terminal jobs are left
        alone; an unknown project is a 404 before any cancel work.
        """
        self._project(project_id)
        terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}
        active = [
            job for job in self.jobs.list(project_id)
            if job.status not in terminal
        ]
        active.sort(key=lambda job: 0 if job.stage == "visual_batch" else 1)
        canceled: list[GenerationJob] = []
        for job in active:
            current = self.jobs.get(job.id)
            if current is None or current.status in terminal:
                # Already handled via its batch parent, or raced to terminal.
                continue
            try:
                canceled_job = self.jobs.cancel(job.id)
            except InvalidJobTransition:
                continue
            cancel_media_processes_for_job(job.id)
            if job.stage == "visual_batch":
                canceled.append(canceled_job)
                canceled.extend(self._cancel_batch_children(canceled_job))
            else:
                canceled.append(canceled_job)
        return canceled

    def _cancel_batch_children(self, parent: GenerationJob) -> list[GenerationJob]:
        """Cancel the active children a visual batch created and tag them.

        ``canceled_with_parent`` tells the retry path to re-queue these
        children; children the user canceled individually stay opted out.
        Killing each child's media processes stops its in-flight subprocesses
        (the row itself is honored at the completion boundary).
        """
        canceled: list[GenerationJob] = []
        for child in self.jobs.list(parent.project_id):
            if child.stage != "scene_visual":
                continue
            if child.parameters.get("parent_job_id") != parent.id:
                continue
            if child.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
                continue
            try:
                updated = self.jobs.cancel(child.id)
            except InvalidJobTransition:
                continue
            cancel_media_processes_for_job(child.id)
            self.database.save_job(updated.model_copy(update={
                "parameters": {**updated.parameters, "canceled_with_parent": True},
            }))
            canceled.append(updated)
        return canceled

    def approve_scene(self, scene_id: str, *, lock: bool = False) -> Scene:
        scene = self.database.get_scene(scene_id)
        if scene is None:
            raise KeyError(f"scene not found: {scene_id}")
        status = SceneStatus.LOCKED if lock else SceneStatus.APPROVED
        updated = scene.model_copy(update={"status": status, "locked": lock, "updated_at": utc_now()})
        self.database.save_scene(updated)
        self.store.save_scene(self._project(scene.project_id).slug, updated)
        self._invalidate_stages(
            self._project(scene.project_id),
            {
                "subtitles",
                "timeline",
                "render_preview",
                "quality_control",
                "render_final",
                "thumbnails",
                "metadata",
            },
        )
        return updated

    # ------------------------------------------------------------------
    # Shots (multi-shot scenes)

    _SHOT_TIMELINE_STAGES = frozenset({
        "timeline", "render_preview", "quality_control", "render_final", "thumbnails",
    })
    _SHOT_GENERATION_FIELDS = frozenset({
        "lane", "visual_type", "selected_backend", "visual_prompt", "negative_prompt",
        "camera_instruction", "seed", "references", "reference_assets", "source_asset_id",
        "source_in_seconds", "source_out_seconds", "settings",
    })

    def _scene_or_key_error(self, scene_id: str) -> Scene:
        scene = self.database.get_scene(scene_id)
        if scene is None:
            raise KeyError(f"scene not found: {scene_id}")
        return scene

    def shots_for_scene(self, scene: Scene) -> list[Shot]:
        return self.database.list_shots(scene.project_id, scene.id)

    def shots_snapshot(self, scene: Scene) -> dict[str, Any]:
        """Stored shots plus the implicit legacy projection for empty scenes."""
        stored = self.shots_for_scene(scene)
        effective = effective_shots(scene, stored)
        payloads = []
        for shot in effective:
            payload = shot.model_dump(mode="json")
            payload["implicit"] = not stored
            payloads.append(payload)
        statuses = [shot.status for shot in effective]
        return {
            "shots": payloads,
            "materialized": bool(stored),
            "count": len(effective),
            "ready": sum(status in {ShotStatus.READY, ShotStatus.APPROVED} for status in statuses),
            "approved": sum(status is ShotStatus.APPROVED for status in statuses),
            "failed": sum(status is ShotStatus.FAILED for status in statuses),
            "rendered_duration_seconds": round(scene_rendered_duration(effective), 6),
        }

    def list_scene_shots(self, scene_id: str) -> dict[str, Any]:
        scene = self._scene_or_key_error(scene_id)
        payload = self.shots_snapshot(scene)
        payload["scene_id"] = scene.id
        payload["scene_duration"] = scene.duration
        return payload

    def _invalidate_compiled_scene(self, project: Project, scene: Scene) -> None:
        """Retire the scene's compiled render after any contributing change.

        The media is archived (never deleted in place) and every
        ``role=="scene_render"`` asset row is repointed at its archived copy,
        so stored hashes always describe the bytes actually on disk. The
        manifest describes the retired publication and is removed. Forced
        replacement renders use ``_copy_compiled_scene_history`` instead so a
        failed encoder cannot retire the last good publication.
        """
        root = self.store.project_path(project)
        directory = root / "scenes" / f"{scene.index + 1:03d}"
        media = directory / "rendered.mp4"
        if media.is_file():
            destination = self.store.archive_variant(
                project.slug, media.relative_to(root),
            )
            archived_relative = destination.relative_to(root)
            for asset in self.database.list_assets(project.id):
                if asset.settings.get("role") == "scene_render" \
                        and Path(asset.filepath) == Path(media.relative_to(root)):
                    self.database.save_asset(asset.model_copy(update={
                        "filepath": archived_relative,
                        "settings": {
                            **asset.settings,
                            "archived_at": utc_now().isoformat(),
                        },
                    }))
        (directory / "render-manifest.json").unlink(missing_ok=True)

    def _copy_compiled_scene_history(
        self, project: Project, scene: Scene,
    ) -> tuple[Path, list[Asset]] | None:
        """Copy the live compile before a forced replacement.

        The live publication and its database rows remain untouched until the
        replacement has passed QC and been atomically published. This lets a
        failed force render leave the last good scene fully usable.
        """
        root = self.store.project_path(project)
        media = root / "scenes" / f"{scene.index + 1:03d}" / "rendered.mp4"
        if not media.is_file():
            return None
        relative = media.relative_to(root)
        rows = [
            asset for asset in self.database.list_assets(project.id)
            if asset.settings.get("role") == "scene_render"
            and Path(asset.filepath) == relative
        ]
        archived = self.store.copy_to_archive(project.slug, relative)
        return archived, rows

    def _commit_compiled_scene_history(
        self, project: Project, copied: tuple[Path, list[Asset]],
    ) -> None:
        root = self.store.project_path(project)
        destination, rows = copied
        archived_relative = destination.relative_to(root)
        archived_at = utc_now().isoformat()
        for asset in rows:
            self.database.save_asset(asset.model_copy(update={
                "filepath": archived_relative,
                "settings": {**asset.settings, "archived_at": archived_at},
            }))

    def _migrate_numbered_shot_media(self, project: Project, scene: Scene) -> None:
        """Move legacy index-numbered shot media into stable ID directories.

        Runs before the numbered ``shot.json`` directories are re-synced so
        renumbering can never delete or strand a surviving shot's media.
        """
        shots_root = self._scene_media_dir(project, scene)
        root = self.store.project_path(project)
        by_shot: dict[str, list[Asset]] = {}
        for asset in self.database.list_assets(project.id):
            if asset.shot_id:
                by_shot.setdefault(asset.shot_id, []).append(asset)
        for shot in self.database.list_shots(project.id, scene.id):
            legacy_assets = []
            for asset in by_shot.get(shot.id, []):
                parts = Path(asset.filepath).parts
                if (
                    len(parts) > 4
                    and parts[0] == "scenes"
                    and parts[2] == "shots"
                    and re.fullmatch(r"\d{3}", parts[3])
                ):
                    legacy_assets.append(asset)
            if not legacy_assets:
                continue
            stable_dir = shots_root / shot.id
            stable_dir.mkdir(parents=True, exist_ok=True)
            for asset in legacy_assets:
                source = root / asset.filepath
                if not source.is_file():
                    continue
                destination = stable_dir / Path(*Path(asset.filepath).parts[4:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                self.database.save_asset(asset.model_copy(update={
                    "filepath": destination.relative_to(root),
                }))

    def _persist_scene_shots(self, project: Project, scene: Scene,
                             shots: list[Shot]) -> list[Shot]:
        """Persist an ordered shot list through both storage layers.

        Callers pass shots in their intended sequence; indexes are assigned
        from list position here because callers may carry stale index values
        while proposing a reorder. Reassignment happens inside one immediate
        transaction with a negative-index parking pass so the
        ``(project_id, scene_id, shot_index)`` uniqueness holds at every
        statement even when shots swap positions.
        """
        # Relocate any index-numbered media first: the sync below prunes
        # numbered directories, and survivors' assets must already point at
        # their stable ID-owned locations.
        self._migrate_numbered_shot_media(project, scene)
        persisted: list[Shot] = []
        known_ids = {shot.id for shot in shots}
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                def _upsert(shot_row: Shot) -> None:
                    connection.execute(
                        """INSERT INTO shots
                           (id, project_id, scene_id, shot_index, status, payload_json)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET
                           project_id=excluded.project_id,
                           scene_id=excluded.scene_id,
                           shot_index=excluded.shot_index,
                           status=excluded.status,
                           payload_json=excluded.payload_json""",
                        (shot_row.id, shot_row.project_id, shot_row.scene_id,
                         shot_row.index, shot_row.status.value,
                         shot_row.model_dump_json()),
                    )

                # Pass one parks every row at a unique negative index so the
                # final pass can assign contiguous positions without ever
                # colliding with an index another shot still holds.
                for position, shot in enumerate(shots):
                    _upsert(shot.model_copy(update={
                        "index": -(position + 1),
                        "project_id": scene.project_id,
                        "scene_id": scene.id,
                    }))
                for position, shot in enumerate(shots):
                    final = shot.model_copy(update={
                        "index": position,
                        "project_id": scene.project_id,
                        "scene_id": scene.id,
                        "updated_at": utc_now(),
                    })
                    _upsert(final)
                    persisted.append(final)
                for stale in connection.execute(
                    "SELECT id FROM shots WHERE project_id=? AND scene_id=?",
                    (scene.project_id, scene.id),
                ).fetchall():
                    if stale["id"] not in known_ids:
                        connection.execute("DELETE FROM shots WHERE id=?", (stale["id"],))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.store.sync_scene_shots(project.slug, scene.index, persisted)
        validate_shot_sequence(persisted)
        self._invalidate_compiled_scene(project, scene)
        return persisted

    def _materialize_implicit_shot(self, project: Project, scene: Scene) -> Shot:
        """Turn a legacy single-visual scene into one concrete stored shot.

        The scene's current ``role=="visual"`` asset is attached by setting its
        ``shot_id``; the asset itself is never replaced or deleted.
        """
        implicit = implicit_shot_from_scene(scene)
        self.database.save_shot(implicit)
        candidates = [
            asset for asset in self.database.list_assets(project.id, scene.id)
            if asset.settings.get("role") == "visual"
        ]
        if candidates:
            current = max(candidates, key=lambda asset: asset.created_at)
            self.database.save_asset(current.model_copy(update={"shot_id": implicit.id}))
        self.store.save_shot(project.slug, scene.index, implicit)
        self._invalidate_compiled_scene(project, scene)
        return implicit

    def create_shot(self, scene_id: str, fields: dict[str, Any]) -> Shot:
        with self._lock:
            return self._create_shot_locked(scene_id, fields)

    def _create_shot_locked(self, scene_id: str, fields: dict[str, Any]) -> Shot:
        fields = dict(fields)
        scene = self._scene_or_key_error(scene_id)
        if scene.locked:
            raise PipelineError("unlock the scene before editing its shots")
        project = self._project(scene.project_id)
        stored = self.shots_for_scene(scene)
        if not stored:
            stored = [self._materialize_implicit_shot(project, scene)]
        requested_index = fields.pop("index", None)
        if requested_index is None:
            position = len(stored)
        else:
            position = int(requested_index)
            if not 0 <= position <= len(stored):
                raise ValueError(
                    f"shot index must be between 0 and {len(stored)} for this scene"
                )
        allowed = {
            "title", "duration_seconds", "start_mode", "lane", "visual_type",
            "selected_backend", "visual_prompt", "negative_prompt", "camera_instruction",
            "source_asset_id", "source_in_seconds", "source_out_seconds", "transition_in",
            "references", "reference_assets", "seed", "overlays", "audio_cues", "source",
            "settings",
        }
        payload = {key: value for key, value in fields.items() if key in allowed}
        try:
            shot = Shot(
                project_id=scene.project_id,
                scene_id=scene.id,
                index=position,
                **payload,
            )
        except Exception as exc:
            raise ValueError(f"invalid shot: {exc}") from None
        inserted = stored[:position] + [shot] + stored[position:]
        persisted = self._persist_scene_shots(project, scene, inserted)
        self._invalidate_compiled_scene(project, scene)
        self._invalidate_stages(project, {"visuals"} | set(self._SHOT_TIMELINE_STAGES))
        return next(item for item in persisted if item.id == shot.id)

    def update_shot(self, shot_id: str, changes: dict[str, Any]) -> Shot:
        with self._lock:
            return self._update_shot_locked(shot_id, changes)

    def _update_shot_locked(self, shot_id: str, changes: dict[str, Any]) -> Shot:
        shot = self.database.get_shot(shot_id)
        if shot is None:
            raise KeyError(f"shot not found: {shot_id}")
        if shot.locked:
            raise PipelineError("unlock the shot before editing it")
        scene = self._scene_or_key_error(shot.scene_id)
        if scene.locked:
            raise PipelineError("unlock the scene before editing its shots")
        project = self._project(scene.project_id)
        stored = self.shots_for_scene(scene)
        mutable_fields = {
            "title", "duration_seconds", "start_mode", "lane", "visual_type",
            "selected_backend", "visual_prompt", "negative_prompt", "camera_instruction",
            "source_asset_id", "source_in_seconds", "source_out_seconds", "transition_in",
            "references", "reference_assets", "seed", "status", "locked", "overlays",
            "audio_cues", "source", "settings",
        }
        updates = {key: value for key, value in changes.items() if key in mutable_fields}
        if "index" in changes and changes["index"] != shot.index:
            target = int(changes["index"])
            if not 0 <= target < len(stored):
                raise ValueError(
                    f"shot index must be between 0 and {len(stored) - 1} for this scene"
                )
            others = [item for item in stored if item.id != shot.id]
            others.insert(target, shot)
            stored = others
        elif shot.id not in {item.id for item in stored}:
            stored.append(shot)
        merged_payload = {**shot.model_dump(mode="json"), **updates}
        try:
            merged = Shot.model_validate(merged_payload)
        except Exception as exc:
            raise ValueError(f"invalid shot edit: {exc}") from None
        merged = merged.model_copy(update={"updated_at": utc_now()})
        next_shots = [
            merged if item.id == shot.id else item
            for item in stored
        ]
        generation_changed = any(
            getattr(merged, field) != getattr(shot, field) if field != "settings"
            else merged.settings != shot.settings
            for field in self._SHOT_GENERATION_FIELDS
        )
        persisted = self._persist_scene_shots(project, scene, next_shots)
        stages = set(self._SHOT_TIMELINE_STAGES)
        if generation_changed:
            stages.add("visuals")
        self._invalidate_stages(project, stages)
        return next(item for item in persisted if item.id == shot.id)

    def delete_shot(self, shot_id: str, *, archive_media: bool = True) -> dict[str, Any]:
        with self._lock:
            return self._delete_shot_locked(shot_id, archive_media=archive_media)

    def _delete_shot_locked(self, shot_id: str, *, archive_media: bool) -> dict[str, Any]:
        shot = self.database.get_shot(shot_id)
        if shot is None:
            raise KeyError(f"shot not found: {shot_id}")
        if shot.locked:
            raise PipelineError("unlock the shot before archiving it")
        scene = self._scene_or_key_error(shot.scene_id)
        if scene.locked:
            raise PipelineError("unlock the scene before archiving its shots")
        project = self._project(scene.project_id)
        archived: list[str] = []
        root = self.store.project_path(project)
        shots_directory = self._scene_media_dir(project, scene)
        assets_by_path: dict[Path, list[Asset]] = {}
        for asset in self.database.list_assets(project.id, scene.id, shot_id=shot.id):
            assets_by_path.setdefault(Path(asset.filepath), []).append(asset)
        for relative_path, assets in assets_by_path.items():
            path = root / relative_path
            if archive_media and path.is_file():
                destination = self.store.archive_variant(
                    project.slug, relative_path,
                )
                relative = destination.relative_to(root)
                for asset in assets:
                    self.database.save_asset(asset.model_copy(update={"filepath": relative}))
                archived.append(str(relative))
            elif not archive_media and (
                path == shots_directory or shots_directory in path.parents
            ):
                for asset in assets:
                    self.database.delete_asset(asset.id)
        remaining = [
            item for item in self.shots_for_scene(scene) if item.id != shot.id
        ]
        self.database.delete_shot(shot.id)
        persisted = self._persist_scene_shots(project, scene, remaining)
        self._remove_shot_media_dir(project, scene, shot.id)
        self._invalidate_compiled_scene(project, scene)
        self._invalidate_stages(project, {"visuals"} | set(self._SHOT_TIMELINE_STAGES))
        return {
            "deleted_shot_id": shot.id,
            "archived_assets": archived,
            "remaining_shots": len(persisted),
            "scene_reverted_to_implicit": not persisted,
        }

    def _remove_shot_media_dir(self, project: Project, scene: Scene,
                               shot_id: str) -> None:
        """Drop a deleted shot's stable media directory.

        Tracked assets were archived and repointed, or their rows were removed
        when deletion explicitly disabled archival. Anything left here is
        regenerable sidecar output such as overlay PNG metadata.
        """
        directory = self._scene_media_dir(project, scene) / shot_id
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)

    def approve_shot(self, shot_id: str, *, lock: bool = False) -> Shot:
        with self._lock:
            shot = self.database.get_shot(shot_id)
            if shot is None:
                # The deterministic implicit id may refer to a legacy scene
                # whose single shot was never materialized.
                scene_id = shot_id[: -len("-implicit")] if shot_id.endswith("-implicit") \
                    else None
                scene = (
                    self._scene_or_key_error(scene_id)
                    if scene_id else None
                )
                if scene is None or self.shots_for_scene(scene):
                    raise KeyError(f"shot not found: {shot_id}")
                self._materialize_implicit_shot(
                    self._project(scene.project_id), scene,
                )
                shot = self.database.get_shot(shot_id)
                assert shot is not None
            scene = self._scene_or_key_error(shot.scene_id)
            project = self._project(scene.project_id)
            status = ShotStatus.APPROVED
            updated = shot.model_copy(update={
                "status": status,
                "locked": bool(lock),
                "updated_at": utc_now(),
            })
            self.database.save_shot(updated)
            self.store.save_shot(project.slug, scene.index, updated)
            self._invalidate_compiled_scene(project, scene)
            self._invalidate_stages(project, set(self._SHOT_TIMELINE_STAGES))
            return updated

    def _editable_shot_context(self, shot_id: str) -> tuple[Shot, Scene, Project]:
        shot = self.database.get_shot(shot_id)
        if shot is None:
            raise KeyError(f"shot not found: {shot_id}")
        if shot.locked:
            raise PipelineError("unlock the shot before editing it")
        scene = self._scene_or_key_error(shot.scene_id)
        if scene.locked:
            raise PipelineError("unlock the scene before editing its shots")
        return shot, scene, self._project(scene.project_id)

    def add_shot_overlay(self, shot_id: str, fields: dict[str, Any]) -> Shot:
        """Attach one overlay cue, validating it against the stored shot."""
        with self._lock:
            shot, _, _ = self._editable_shot_context(shot_id)
            try:
                overlay = OverlayCue.model_validate(
                    {**fields, "start_seconds": fields.get("start_seconds", 0)}
                )
            except Exception as exc:
                raise ValueError(f"invalid overlay: {exc}") from None
            overlays = [item.model_dump(mode="json") for item in shot.overlays]
            overlays.append(overlay.model_dump(mode="json"))
            return self._update_shot_locked(shot_id, {"overlays": overlays})

    def find_overlay(self, project_id: str, overlay_id: str) -> tuple[Shot, OverlayCue]:
        """Locate an embedded overlay cue within one project's shots."""
        self._project(project_id)
        for shot in self.database.list_shots(project_id):
            for overlay in shot.overlays:
                if overlay.id == overlay_id:
                    return shot, overlay
        raise KeyError(f"overlay not found: {overlay_id}")

    def patch_shot_overlay(self, shot_id: str, overlay_id: str,
                           changes: dict[str, Any]) -> Shot:
        with self._lock:
            shot, _, _ = self._editable_shot_context(shot_id)
            if all(item.id != overlay_id for item in shot.overlays):
                raise KeyError(f"overlay not found: {overlay_id}")
            overlays = [
                {**item.model_dump(mode="json"), **changes}
                if item.id == overlay_id else item.model_dump(mode="json")
                for item in shot.overlays
            ]
            try:
                validated = [OverlayCue.model_validate(item) for item in overlays]
            except Exception as exc:
                raise ValueError(f"invalid overlay edit: {exc}") from None
            return self._update_shot_locked(
                shot_id,
                {"overlays": [item.model_dump(mode="json") for item in validated]},
            )

    def patch_project_overlay(
        self, project_id: str, overlay_id: str, changes: dict[str, Any],
    ) -> Shot:
        with self._lock:
            shot, overlay = self.find_overlay(project_id, overlay_id)
            self._editable_shot_context(shot.id)
            payload = overlay.model_dump(mode="json")
            payload.update(changes)
            try:
                patched = OverlayCue.model_validate(payload)
            except Exception as exc:
                raise ValueError(f"invalid overlay edit: {exc}") from None
            overlays = [
                patched.model_dump(mode="json") if item.id == overlay_id
                else item.model_dump(mode="json")
                for item in shot.overlays
            ]
            return self._update_shot_locked(shot.id, {"overlays": overlays})

    def remove_shot_overlay(self, shot_id: str, overlay_id: str) -> Shot:
        with self._lock:
            shot, _, _ = self._editable_shot_context(shot_id)
            remaining = [
                item.model_dump(mode="json") for item in shot.overlays
                if item.id != overlay_id
            ]
            if len(remaining) == len(shot.overlays):
                raise KeyError(f"overlay not found: {overlay_id}")
            return self._update_shot_locked(shot_id, {"overlays": remaining})

    def _begin_job_attempt(self, job_id: str) -> GenerationJob:
        """Atomically count one execution attempt; enforce the retry ceiling.

        Runs inside BEGIN IMMEDIATE so concurrent triggers cannot both pass
        the limit, and a failure rolls the counter back.
        """
        def _apply(job: GenerationJob) -> GenerationJob:
            if job.attempt_count >= job.max_attempts:
                raise PipelineError(
                    f"job exceeded its {job.max_attempts} allowed attempts"
                )
            return job.model_copy(update={"attempt_count": job.attempt_count + 1})

        return self.database.update_job_in_transaction(job_id, _apply)

    # ------------------------------------------------------------------
    # Shot generation dispatch (production lanes)

    def _scene_media_dir(self, project: Project, scene: Scene) -> Path:
        return (
            self.store.project_path(project) / "scenes" / f"{scene.index + 1:03d}"
            / "shots"
        )

    def _shot_output_dir(self, project: Project, scene: Scene, shot: Shot) -> Path:
        """Stable shot-ID-owned media directory.

        Media lives under ``shots/<shot_id>/`` so insertion, deletion, and
        reordering never rename, misassociate, or overwrite another shot's
        assets; only the tiny ``shot.json`` metadata directories are numbered.
        """
        directory = self._scene_media_dir(project, scene) / shot.id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def _render_cache_root(self) -> Path:
        return self.store.root / ".render-cache"

    @property
    def shot_normalizer(self) -> ShotNormalizer:
        if getattr(self, "_shot_normalizer_handle", None) is None:
            self._shot_normalizer_handle = ShotNormalizer(
                cache_root=self._render_cache_root,
                temp_root=self.temp_root,
            )
        return self._shot_normalizer_handle

    @property
    def scene_assembler(self) -> SceneAssembler:
        if getattr(self, "_scene_assembler_handle", None) is None:
            self._scene_assembler_handle = SceneAssembler(
                cache_root=self._render_cache_root,
                temp_root=self.temp_root,
            )
        return self._scene_assembler_handle

    def _shot_visual_family(self, shot: Shot) -> str:
        if shot.visual_type in {
            VisualType.H3_AUDIOVISUAL, VisualType.H3_REFERENCE, VisualType.WAN_VIDEO,
        }:
            return "video"
        return "image"

    def generate_shot(
        self, shot_id: str, *, force: bool = False, job: GenerationJob | None = None,
    ) -> Asset:
        """Generate (or regenerate) one shot's visual through its production lane."""
        with self._lock:
            shot = self.database.get_shot(shot_id)
            if shot is None:
                raise KeyError(f"shot not found: {shot_id}")
            if shot.locked:
                raise PipelineError("unlock the shot before regenerating it")
            scene = self._scene_or_key_error(shot.scene_id)
            project = self._project(scene.project_id)
            assets = self.database.list_assets(project.id)
            current = current_visual_asset(shot, assets)
            if shot.visual_type is VisualType.REUSED_MEDIA:
                if current is None:
                    raise PipelineError(
                        "attach a local image or video "
                        "before using this reused-media shot"
                    )
                if force:
                    raise PipelineError(
                        "reused media is replaced by importing a new local file, not regeneration"
                    )
                return current
            if current is not None and not force:
                if job is None:
                    job = self.jobs.enqueue(GenerationJob(
                        project_id=project.id, scene_id=scene.id, shot_id=shot.id,
                        stage="shot_generate",
                        backend="mock" if self.mock_mode else "automatic",
                        parameters={"force": force},
                    ))
                if job.status is not JobStatus.COMPLETED:
                    self.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
                    self.jobs.transition(job.id, JobStatus.GENERATING, progress=0.2)
                    self.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.9)
                    self.jobs.complete(job.id)
                return current
            if force and current is not None:
                previous = self.store.project_path(project) / current.filepath
                if previous.is_file():
                    destination = self.store.archive_variant(
                        project.slug, current.filepath,
                    )
                    # The archived file is still a valid asset record; point it
                    # at its new home so history stays resolvable.
                    self.database.save_asset(current.model_copy(update={
                        "filepath": destination.relative_to(
                            self.store.project_path(project),
                        ),
                        "settings": {
                            **current.settings,
                            "archived_for": shot.id,
                            "archived_at": utc_now().isoformat(),
                        },
                    }))
                all_shots = self.database.list_shots(project.id)
                impact = plan_regeneration(
                    shot.id, all_shots, assets, scenes=self.database.list_scenes(project.id),
                )
                for updated_shot in apply_regeneration_staleness(all_shots, impact):
                    if updated_shot.id != shot.id:
                        self.database.save_shot(updated_shot)
                        owning_scene = next(
                            (item for item in self.database.list_scenes(project.id)
                             if item.id == updated_shot.scene_id),
                            None,
                        )
                        if owning_scene is not None:
                            self.store.save_shot(
                                project.slug, owning_scene.index, updated_shot,
                            )
            if job is None:
                job = self.jobs.enqueue(GenerationJob(
                    project_id=project.id, scene_id=scene.id, shot_id=shot.id,
                    stage="shot_generate",
                    backend="mock" if self.mock_mode else "automatic",
                    parameters={"force": force},
                ))
            # Count this execution attempt atomically before any work; a
            # ceiling-exceeded failure lands in the row like any other.
            job = self._begin_job_attempt(job.id)
            with media_process_scope(job.id):
                try:
                    self.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
                    self.jobs.transition(job.id, JobStatus.GENERATING, progress=0.2)
                    asset = self._dispatch_shot_visual(
                        project, scene, shot,
                        use_cache=not force, job_id=job.id,
                    )
                    refreshed = self.jobs.get(job.id)
                    if refreshed is not None and refreshed.status is JobStatus.CANCELED:
                        raise PipelineError("job was canceled")
                    self.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.9)
                    self.jobs.complete(job.id)
                except Exception as exc:
                    failed = self.database.get_shot(shot_id)
                    if failed is not None and not failed.locked:
                        self.database.save_shot(failed.model_copy(update={
                            "status": ShotStatus.FAILED, "updated_at": utc_now(),
                        }))
                    current_job = self.jobs.get(job.id)
                    if current_job is not None and current_job.status not in {
                        JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                    }:
                        self.jobs.fail(job.id, redact_secrets(exc))
                    raise
            self._invalidate_compiled_scene(project, scene)
            self._invalidate_stages(project, set(self._SHOT_TIMELINE_STAGES))
            return asset

    def _dispatch_shot_visual(
        self,
        project: Project,
        scene: Scene,
        shot: Shot,
        *,
        use_cache: bool,
        job_id: str | None,
    ) -> Asset:
        """Route one shot to its resolved lane target and record the result."""
        directory = self._shot_output_dir(project, scene, shot)
        use_video = self._shot_visual_family(shot) == "video"
        destination = directory / ("video.mp4" if use_video else "visual.png")
        if shot.visual_type is VisualType.TEXT_OVERLAY_STILL:
            background_model = str(
                shot.settings.get("text_overlay_background_model", "krea")
            )
            try:
                preferred = ImageModelOption(background_model)
            except ValueError as exc:
                raise PipelineError(
                    "text overlay background model must be krea, ideogram4_local, "
                    "or qwen_image"
                ) from exc
            shot_scene = scene.model_copy(update={
                "title": shot.title or scene.title,
                "visual_prompt": shot.visual_prompt,
                "negative_prompt": shot.negative_prompt,
                "visual_type": VisualType.TEXT_OVERLAY_STILL,
                "selected_backend": shot.selected_backend,
                "seed": shot.seed,
                "needs_embedded_text": True,
                "text_in_image": str(shot.settings.get("text_in_image", "")),
                "preferred_image_model": preferred.value,
                "settings": dict(shot.settings),
            })
            result = self._dispatch_text_overlay_still(
                project, shot_scene, directory, use_cache=use_cache,
            )
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
            asset = self._record_asset(
                project, scene, destination, AssetType.IMAGE, result,
                role="visual", job_id=job_id,
                extra_settings={
                    "shot_id": shot.id,
                    "visual_type": shot.visual_type.value,
                    "lane": shot.lane.value,
                },
                shot_id=shot.id,
            )
            self._mark_shot_ready(shot)
            return asset
        if self.mock_mode:
            result = self._mock_generate(
                project,
                "video" if use_video else "image",
                project_dir=directory,
                prompt=shot.visual_prompt,
                negative_prompt=shot.negative_prompt,
                seed=shot.seed,
                duration=shot.duration_seconds if use_video else None,
                width=min(project.resolution[0], 640),
                height=min(project.resolution[1], 360),
            )
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
            asset = self._record_asset(
                project, scene, destination,
                AssetType.VIDEO if use_video else AssetType.IMAGE,
                result,                 role="visual", job_id=job_id,
                extra_settings={
                    "shot_id": shot.id,
                    "visual_type": shot.visual_type.value,
                    "lane": shot.lane.value if hasattr(shot.lane, "value") else str(shot.lane),
                },
                shot_id=shot.id,
            )
            self._mark_shot_ready(shot)
            return asset

        resolved = resolve_lane_target(shot, self.registry, mock_mode=False)
        if resolved.kind == "deterministic":
            if resolved.handler == "imported_media":
                existing = current_visual_asset(
                    shot, self.database.list_assets(project.id),
                )
                if existing is None:
                    raise PipelineError(
                        "attach a local image or video before using this reused-media shot"
                    )
                return existing
            graphic_scene = scene.model_copy(update={
                "visual_type": VisualType.GRAPHIC_SCREEN,
                "visual_prompt": shot.visual_prompt,
                "negative_prompt": shot.negative_prompt,
                "seed": shot.seed,
                "settings": {
                    **scene.settings,
                    **shot.settings,
                    "graphic_screen": {
                        **(shot.settings.get("graphic_screen") or {}),
                    },
                },
            })
            asset = self._dispatch_graphic_screen(
                project, graphic_scene, directory, use_cache=use_cache,
            )
            asset = self.database.save_asset(asset.model_copy(update={"shot_id": shot.id}))
            self._mark_shot_ready(shot)
            return asset

        if shot.visual_type is VisualType.IDEOGRAM4_STILL:
            if shot.reference_assets:
                raise ShotRequestError(
                    "Ideogram 4 stills do not currently support shot reference assets; "
                    "remove the references before generating this shot.",
                    "reference_role_unsupported",
                    details={"backend": "ideogram4_local_comfyui", "shot_id": shot.id},
                )
            # Reuse the exact same prompt builder, workflow, cache, and VRAM
            # policy as scene-level Ideogram stills.  Shot settings own Quick /
            # Precise mode and exact text, so no legacy scene recipe leaks in.
            shot_scene = scene.model_copy(update={
                "title": shot.title or scene.title,
                "visual_prompt": shot.visual_prompt,
                "negative_prompt": shot.negative_prompt,
                "visual_type": VisualType.IDEOGRAM4_STILL,
                "seed": shot.seed,
                "text_in_image": str(shot.settings.get("text_in_image", "")),
                "settings": dict(shot.settings),
            })
            with self._gpu_lock:
                result = self._dispatch_ideogram4(
                    project, shot_scene, directory, use_cache=use_cache,
                )
            output = result.outputs[0] if result.outputs else None
            if output is None:
                raise PipelineError("Ideogram 4 returned no still image for the shot.")
            if Path(output) != destination:
                os.replace(output, destination)
            asset = self._record_asset(
                project, scene, destination, AssetType.IMAGE, result,
                role="visual", job_id=job_id,
                extra_settings={
                    "shot_id": shot.id,
                    "visual_type": shot.visual_type.value,
                    "lane": shot.lane.value,
                },
                shot_id=shot.id,
            )
            self._mark_shot_ready(shot)
            return asset

        assets_by_id = {
            asset.id: asset for asset in self.database.list_assets(project.id)
        }
        references = resolve_reference_assets(
            shot, assets_by_id, self.store.project_path(project),
        )
        identity = self._backend_identity(resolved.backend)
        plan = build_shot_request(
            shot, project, directory,
            backend_identity=identity, references=references, job_id=job_id,
        )
        request = plan.request
        cache_key = self._generation_cache_key(
            resolved.backend_name, plan.cache_payload,
        )
        if cache_key is not None and use_cache:
            cached = (
                self.generation_cache.lookup(resolved.backend_name, cache_key)
                if self.generation_cache else None
            )
            if cached is not None:
                staged = destination.with_name(f".{destination.name}.cached")
                shutil.copyfile(cached.path, staged)
                os.replace(staged, destination)
                metadata = self._cache_hit_metadata(cached, cache_key)
                metadata = {**metadata, "provenance": plan.provenance}
                asset = self._record_asset(
                    project, scene, destination,
                    AssetType.VIDEO if use_video else AssetType.IMAGE,
                    GenerationResult(
                        outputs=(destination,), metadata=metadata, peak_vram_gb=0.0,
                    ),
                    role="visual", job_id=job_id,
                    extra_settings={"shot_id": shot.id, "visual_type": shot.visual_type.value},
                    shot_id=shot.id,
                )
                self._mark_shot_ready(shot)
                return asset
        try:
            def _execute() -> GenerationResult:
                if resolved.uses_gpu:
                    with self._gpu_lock:
                        resolved.backend.load()
                        return resolved.backend.generate(request)
                return resolved.backend.generate(request)

            result = _execute()
            metadata = {**dict(result.metadata), "settings": plan.provenance}
            output = result.outputs[0] if result.outputs else None
            self._store_generation_result(resolved.backend_name, cache_key, output, metadata)
            if output is not None and Path(output) != destination:
                os.replace(output, destination)
            asset = self._record_asset(
                project, scene, destination,
                AssetType.VIDEO if use_video else AssetType.IMAGE,
                GenerationResult(
                    outputs=result.outputs, metadata=metadata,
                    peak_vram_gb=result.peak_vram_gb,
                ),
                role="visual", job_id=job_id,
                extra_settings={"shot_id": shot.id, "visual_type": shot.visual_type.value},
                shot_id=shot.id,
            )
        except Exception as exc:
            descriptor = resolved.backend.descriptor()
            self.database.save_attempt(GenerationAttempt(
                asset_id=None, job_id=job_id, scene_id=scene.id, shot_id=shot.id,
                backend=descriptor.backend_name, model=descriptor.model_name,
                model_version=descriptor.model_version,
                quantization=descriptor.quantization,
                workflow_version=str(plan.provenance.get("workflow_version", "shot-v1")),
                parameters=dict(plan.cache_payload),
                seed=shot.seed, success=False, error=redact_secrets(exc),
            ))
            raise PipelineError(
                f"Shot generation failed on {resolved.backend_name}: {redact_secrets(exc)}"
            ) from exc
        self._mark_shot_ready(shot)
        return asset

    _IMPORTED_IMAGE_EXTENSIONS = frozenset({
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff",
    })
    _IMPORTED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm"})

    def import_reused_media(
        self,
        scene_id: str,
        source_path: Path,
        *,
        original_filename: str,
        source_fields: Mapping[str, Any],
        shot_id: str | None = None,
        generated_image: bool = False,
    ) -> Asset:
        """Attach a user-selected local visual to a REAL scene or explicit shot.

        This deliberately copies only a local file supplied by the user.  It
        never fetches the source URL; any optional rights note is durable
        provenance for a later review or portable project handoff.
        """
        with self._lock:
            scene = self._scene_or_key_error(scene_id)
            if scene.locked:
                raise PipelineError("unlock the scene before replacing its reused media")
            if (
                not generated_image
                and shot_id is None
                and scene.visual_type is not VisualType.REUSED_MEDIA
            ):
                raise ValueError("local media can only be attached to a reused_media scene")
            if not source_path.is_file() or source_path.stat().st_size <= 0:
                raise ValueError("uploaded media is empty or unavailable")
            filename = Path(original_filename).name
            suffix = Path(filename).suffix.lower()
            allowed_extensions = (
                self._IMPORTED_IMAGE_EXTENSIONS
                if generated_image
                else self._IMPORTED_IMAGE_EXTENSIONS | self._IMPORTED_VIDEO_EXTENSIONS
            )
            if suffix not in allowed_extensions:
                raise ValueError(
                    "unsupported media type; use PNG, JPEG, WebP, BMP, GIF, or TIFF"
                    + ("" if generated_image else ", MP4, MOV, MKV, or WebM")
                )
            normalized_source = dict(source_fields)
            if generated_image:
                normalized_source.setdefault("title", filename)
                normalized_source.setdefault("classification", "illustration")
            try:
                source = MediaSource.model_validate(normalized_source)
            except Exception as exc:
                raise ValueError(f"invalid media source metadata: {exc}") from None
            if not generated_image and not source.title.strip():
                raise ValueError("source title is required for reused media")

            project = self._project(scene.project_id)
            stored = self.shots_for_scene(scene)
            if shot_id is None:
                if not stored:
                    stored = [self._materialize_implicit_shot(project, scene)]
                shot = stored[0]
            else:
                shot = next((item for item in stored if item.id == shot_id), None)
                if shot is None:
                    raise KeyError(f"shot not found in scene: {shot_id}")
                if shot.locked:
                    raise PipelineError("unlock the shot before replacing its reused media")
            if not generated_image and shot.visual_type is not VisualType.REUSED_MEDIA:
                raise ValueError("local media can only be attached to a reused_media shot")
            if generated_image and shot.visual_type not in {
                VisualType.KREA2_STILL,
                VisualType.IDEOGRAM4_STILL,
                VisualType.QWEN_IMAGE_STILL,
                VisualType.FLUX_STILL,
                VisualType.IMAGE_MOTION,
            }:
                raise ValueError(
                    "an imported AI image can only be attached to an image-generation or image-motion shot"
                )

            root = self.store.project_path(project)
            asset_id = str(uuid.uuid4())
            relative = Path("scenes") / f"{scene.index + 1:03d}" / "imports" / f"{asset_id}{suffix}"
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged = destination.with_name(f".{destination.name}.pending")
            shutil.copyfile(source_path, staged)
            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(staged, destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            source = source.model_copy(update={"sha256": digest})
            asset = Asset(
                id=asset_id,
                project_id=project.id,
                scene_id=scene.id,
                shot_id=shot.id,
                type=(AssetType.IMAGE if suffix in self._IMPORTED_IMAGE_EXTENSIONS else AssetType.VIDEO),
                filepath=relative,
                backend="imported_ai_image" if generated_image else "imported_local",
                model="external-ai-image" if generated_image else "user-supplied",
                model_version="original",
                workflow_version="imported-ai-image-v1" if generated_image else "imported-media-v1",
                seed=0,
                prompt=shot.visual_prompt,
                negative_prompt=shot.negative_prompt,
                settings={
                    "role": "visual",
                    "visual_type": shot.visual_type.value,
                    "visual_revision": int(shot.settings.get("visual_revision", 0)),
                    "source": source.model_dump(mode="json"),
                    "original_filename": filename,
                },
                hash=digest,
            )
            self.database.save_asset(asset)
            updated_shot = shot.model_copy(update={
                "source": source,
                "status": ShotStatus.READY,
                "updated_at": utc_now(),
            })
            self._persist_scene_shots(
                project, scene,
                [updated_shot if item.id == shot.id else item for item in stored],
            )
            self.database.save_scene(scene.model_copy(update={
                "status": SceneStatus.GENERATED, "updated_at": utc_now(),
            }))
            self._invalidate_compiled_scene(project, scene)
            self._invalidate_stages(project, {"timeline", "render_preview", "quality_control", "render_final"})
            return asset

    def _mark_shot_ready(self, shot: Shot) -> None:
        if shot.locked:
            updated = shot.model_copy(update={"updated_at": utc_now()})
        else:
            updated = shot.model_copy(update={
                "status": ShotStatus.READY, "updated_at": utc_now(),
            })
        self.database.save_shot(updated)

    def validate_shot_lane(self, shot_id: str) -> None:
        """Reject unwired lanes/visual types with structured errors before queueing.

        Raises :class:`LaneResolutionRejected` carrying the resolver's payload
        (code + details) so callers surface a precise structured reason
        without ever falling through to mock generation in real mode.
        """
        if self.mock_mode:
            return
        shot = self.database.get_shot(shot_id)
        if shot is None:
            raise KeyError(f"shot not found: {shot_id}")
        try:
            resolve_lane_target(shot, self.registry, mock_mode=False)
        except LaneResolutionError as exc:
            raise LaneResolutionRejected(exc.as_dict()) from None

    def queue_shot_generation(self, shot_id: str, *, regenerate: bool) -> GenerationJob:
        """Validate the lane, then enqueue a ``shot_generate`` row (no execution).

        Unwired lanes raise here, so a rejected request never leaves a doomed
        job row behind.
        """
        self.validate_shot_lane(shot_id)
        shot = self.database.get_shot(shot_id)
        if shot is None:
            raise KeyError(f"shot not found: {shot_id}")
        scene = self._scene_or_key_error(shot.scene_id)
        project = self._project(scene.project_id)
        active = next(
            (
                j for j in self.jobs.list(project.id)
                if j.stage == "shot_generate" and j.shot_id == shot_id
                and j.status not in {
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                }
            ),
            None,
        )
        if active is not None:
            raise PipelineError(
                "A generation job for this shot is already queued or running."
            )
        return self.jobs.enqueue(GenerationJob(
            project_id=project.id, scene_id=scene.id, shot_id=shot_id,
            stage="shot_generate",
            backend="mock" if self.mock_mode else "automatic",
            parameters={"force": regenerate},
        ))

    def run_shot_generation_job(self, job_id: str) -> None:
        """Background runner: drive one queued shot_generate row to a terminal state."""
        job = self.jobs.get(job_id)
        if job is None or job.shot_id is None:
            return
        with media_process_scope(job_id):
            try:
                self.generate_shot(
                    job.shot_id,
                    force=bool(job.parameters.get("force")),
                    job=self.jobs.get(job_id) or job,
                )
            except Exception as exc:
                current = self.jobs.get(job_id)
                if current is not None and current.status not in {
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                }:
                    self.jobs.fail(job_id, redact_secrets(exc))
                logger.warning(
                    "Shot generation job %s failed: %s", job_id, redact_secrets(exc),
                )

    # ------------------------------------------------------------------
    # Scene compilation (ShotNormalizer + SceneAssembler)

    def _overlay_media_paths(
        self, project: Project, shot: Shot,
    ) -> dict[str, Path]:
        root = self.store.project_path(project)
        assets_by_id = {
            asset.id: asset for asset in self.database.list_assets(project.id)
        }
        resolved: dict[str, Path] = {}
        for cue in shot.overlays:
            if cue.kind is OverlayKind.EXACT_TEXT:
                resolved[cue.id] = self._render_exact_text_overlay(project, shot, cue)
                continue
            asset = assets_by_id.get(cue.asset_id or "")
            path = root / asset.filepath if asset is not None else None
            if path is None or not path.is_file() or path.stat().st_size == 0:
                raise PipelineError(
                    f"overlay {cue.id!r} references missing media "
                    f"(asset {cue.asset_id!r}); attach the media before rendering"
                )
            resolved[cue.id] = path
        return resolved

    _OVERLAY_TEMPLATES: dict[str, str] = {
        # {font} is replaced with a canvas-height-derived pixel size; vh units
        # round to zero on small canvases and silently produce blank overlays.
        "lower_third": "left:6%;bottom:8%;font-size:{font}px;text-align:left;",
        "caption_line": "left:50%;bottom:7%;transform:translateX(-50%);font-size:{font}px;text-align:center;",
        "verse_card": "left:50%;top:50%;transform:translate(-50%,-50%);font-size:{font}px;text-align:center;max-width:80%;",
        "verse_reference": "right:6%;top:7%;font-size:{font}px;text-align:right;",
        "comparison_label_left": "left:4%;top:50%;transform:translateY(-50%);font-size:{font}px;",
        "comparison_label_right": "right:4%;top:50%;transform:translateY(-50%);font-size:{font}px;",
        "date_label": "left:50%;top:12%;transform:translateX(-50%);font-size:{font}px;letter-spacing:0.18em;",
    }

    _OVERLAY_FALLBACK_GEOMETRY: dict[str, tuple[float, float, str]] = {
        # (x fraction, y fraction, horizontal alignment) of the text block.
        "lower_third": (0.06, 0.92, "left"),
        "caption_line": (0.5, 0.93, "center"),
        "verse_card": (0.5, 0.5, "center"),
        "verse_reference": (0.94, 0.07, "right"),
        "comparison_label_left": (0.04, 0.5, "left"),
        "comparison_label_right": (0.96, 0.5, "right"),
        "date_label": (0.5, 0.12, "center"),
    }

    def _exact_overlay_document(
        self, cue: OverlayCue, width: int, height: int,
    ) -> str:
        """Backend-authored static HTML for one exact-text cue.

        Opacity and fades are intentionally excluded: FFmpeg composition owns
        them (colorchannelmixer/alpha fades), so rasterized pixels stay fully
        opaque and the effect is applied exactly once downstream.
        """
        template = self._OVERLAY_TEMPLATES.get(
            cue.template, self._OVERLAY_TEMPLATES["caption_line"],
        ).format(font=max(9, round(height * 0.048)))
        style = template
        color = "#ffffff"
        text_color = str(cue.style.get("color", color)) if isinstance(cue.style, dict) else color
        safe_text = escape(cue.exact_text or "")
        return (
            '<!doctype html><html><head><meta charset="utf-8"><style>'
            "*{margin:0;padding:0;box-sizing:border-box;} html,body{width:100%;height:100%;"
            "background:transparent;overflow:hidden;}"
            "body{font-family:'Noto Sans','DejaVu Sans',sans-serif;font-weight:700;}"
            ".lvs-overlay{position:absolute;color:" + escape(text_color) + ";"
            "text-shadow:0 2px 10px rgba(0,0,0,0.85);white-space:pre-wrap;" + style + "}"
            "</style></head><body>"
            '<div class="lvs-overlay">' + safe_text + "</div>"
            "</body></html>"
        )

    def _render_exact_text_overlay(
        self, project: Project, shot: Shot, cue: OverlayCue,
    ) -> Path:
        """Render one exact-text cue through the sanitized local HTML renderer.

        The document is fully backend-authored static HTML with the user text
        inserted as escaped text content only; nothing model-authored reaches
        this path. When the local Chromium binary is unavailable (headless
        machines), the same layout spec is rasterized locally with Pillow so
        overlay rendering never requires network access either way.
        """
        scene = self._scene_or_key_error(shot.scene_id)
        directory = self._shot_output_dir(project, scene, shot)
        overlays_dir = directory / "overlays"
        overlays_dir.mkdir(parents=True, exist_ok=True)
        output = overlays_dir / f"{cue.id}.png"
        meta_path = overlays_dir / f"{cue.id}.json"
        document = self._exact_overlay_document(
            cue, project.resolution[0], project.resolution[1],
        )
        expected_meta = {
            "document_sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(),
            "renderer_version": self.graphic_renderer.version,
            "resolution": list(project.resolution),
        }
        try:
            cached_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached_meta = None
        if isinstance(cached_meta, dict) \
                and output.is_file() and output.stat().st_size > 0 \
                and all(cached_meta.get(k) == v for k, v in expected_meta.items()) \
                and cached_meta.get("png_sha256") == sha256_file(output):
            # Unchanged cue plus a file that still hashes to its recorded PNG:
            # never invoke Chromium/Pillow again.
            return output
        png_hash: str | None = None
        if self.graphic_renderer.executable:
            for _attempt in range(2):
                try:
                    self.graphic_renderer.render_transparent(
                        document, output,
                        width=project.resolution[0], height=project.resolution[1],
                    )
                except RuntimeError:
                    break
                if self._overlay_png_has_content(output):
                    png_hash = hashlib.sha256(output.read_bytes()).hexdigest()
                    break
        if png_hash is None:
            # Headless Chromium intermittently captures before first paint on
            # transparent pages; the local Pillow rasterization is fully
            # deterministic and offline.
            png_hash = self._rasterize_overlay_with_pillow(project, cue, output)
        self._atomic_json(meta_path, {**expected_meta, "png_sha256": png_hash})
        return output

    def _overlay_png_has_content(self, path: Path) -> bool:
        from PIL import Image

        try:
            with Image.open(path) as image:
                if image.mode != "RGBA":
                    return True
                return max(image.getchannel("A").getdata()) > 0
        except OSError:
            return False

    def _rasterize_overlay_with_pillow(
        self, project: Project, cue: OverlayCue, output: Path,
    ) -> str:
        """Deterministic offline fallback matching the HTML template geometry."""
        from PIL import Image, ImageDraw, ImageFont

        width, height = project.resolution
        x_fraction, y_fraction, alignment = self._OVERLAY_FALLBACK_GEOMETRY.get(
            cue.template, self._OVERLAY_FALLBACK_GEOMETRY["caption_line"],
        )
        font_size = max(9, int(height * 0.052))
        font = None
        for candidate in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        ):
            path = Path(candidate)
            if path.is_file():
                font = ImageFont.truetype(str(path), font_size)
                break
        if font is None:
            font = ImageFont.load_default()
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        lines = (cue.exact_text or "").splitlines() or [""]
        line_spacing = int(font_size * 1.25)
        block_height = line_spacing * len(lines)
        anchor_x = x_fraction * width
        color = cue.style.get("color", "#ffffff") if isinstance(cue.style, dict) else "#ffffff"
        rgb = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) \
            if color.startswith("#") and len(color) == 7 else (255, 255, 255)
        anchor = {"center": "ma", "right": "ra"}.get(alignment, "la")
        for offset, line in enumerate(lines):
            text = line[:200]
            y = y_fraction * height - block_height / 2 + offset * line_spacing
            draw.text((anchor_x + 2, y + 2), text, font=font,
                      fill=(*rgb, 140), anchor=anchor)
            # Full alpha here too: composition owns cue opacity/fades.
            draw.text((anchor_x, y), text, font=font, fill=(*rgb, 255), anchor=anchor)
        staged = output.with_name(f".{output.name}.pillow.tmp")
        image.save(staged, format="PNG")
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(staged, output)
        return hashlib.sha256(output.read_bytes()).hexdigest()

    # Preflight issues that make compiling the scene impossible right now.
    # scene_unrendered is the reason to render, not a blocker; scene_stale and
    # stale_dependency are advisory (the compiled artifact itself is
    # invalidated eagerly on every contributing change).
    _SCENE_RENDER_BLOCKERS = frozenset({
        "failed_shot", "missing_visual", "corrupt_visual",
        "missing_source_media", "continuity_invalid",
    })

    def render_scene(
        self, scene_id: str, *, force: bool = False, job: GenerationJob | None = None,
    ) -> dict[str, Any]:
        """Compile one scene's shots into scenes/<NNN>/rendered.mp4.

        Shot composites and identical scene compiles come from the content-
        addressed cache. ``force=True`` bypasses only the second-level scene
        cache (it never re-generates shot media); shot composites are still
        reused because their inputs are unchanged.
        """
        scene = self._scene_or_key_error(scene_id)
        project = self._project(scene.project_id)
        stored = self.shots_for_scene(scene)
        if not stored:
            self._materialize_implicit_shot(project, scene)
            stored = self.shots_for_scene(scene)
        issues = self._scene_render_issues(project, scene, stored)
        blocking = [
            issue for issue in issues if issue["code"] in self._SCENE_RENDER_BLOCKERS
        ]
        if blocking:
            raise PipelineError(
                "Scene render preflight failed: "
                + "; ".join(f"{issue['code']}: {issue['detail']}" for issue in blocking[:5])
            )
        measured = self.tts.active_scene_durations(project.id) or {}
        target = measured.get(scene.id)
        plan = compile_scene_plan(
            scene, stored, fps=project.fps,
            target_duration_seconds=float(target) if target else None,
        )
        root = self.store.project_path(project)
        intermediates: dict[str, Path] = {}
        shot_keys: dict[str, str] = {}
        for compiled_shot, frames in zip(plan.shots, plan.frame_counts, strict=True):
            shot = next(item for item in stored if item.id == compiled_shot.shot_id)
            source_asset = current_visual_asset(shot, self.database.list_assets(project.id))
            if source_asset is None:
                raise PipelineError(
                    f"shot {compiled_shot.shot_id} has no generated visual to compile"
                )
            inputs = NormalizationInputs(
                shot=shot,
                source_path=root / source_asset.filepath,
                overlay_paths=self._overlay_media_paths(project, shot),
                canvas_width=project.resolution[0],
                canvas_height=project.resolution[1],
                fps=plan.fps,
            )
            normalized = self.shot_normalizer.normalize(
                inputs, duration_seconds=frames / plan.fps,
                job_id=job.id if job else None,
            )
            intermediates[normalized.shot_id] = normalized.path
            shot_keys[normalized.shot_id] = normalized.cache_key
        destination = root / "scenes" / f"{scene.index + 1:03d}" / "rendered.mp4"
        manifest_target = destination.with_name("render-manifest.json")
        assembler = (
            self.scene_assembler if not force
            else SceneAssembler(cache_root=None, temp_root=self.temp_root)
        )
        copied_history = self._copy_compiled_scene_history(project, scene) if force else None
        try:
            result = assembler.render(
                plan, intermediates, destination,
                shot_keys=shot_keys, manifest_path=manifest_target,
                options=SceneEncodeOptions(), job_id=job.id if job else None,
            )
        except Exception:
            # This file is only a precautionary copy made by this attempt. The
            # live media, manifest, and asset rows still describe the prior
            # successful publication.
            if copied_history is not None:
                copied_history[0].unlink(missing_ok=True)
            raise
        if copied_history is not None:
            self._commit_compiled_scene_history(project, copied_history)
        render_result = GenerationResult(
            outputs=(result.path,),
            metadata={
                "backend": "ffmpeg", "model": "ffmpeg-scene-assembly",
                "model_version": assembler.renderer_version,
                "workflow_version": SCENE_ASSEMBLY_WORKFLOW,
                "seed": 0,
                "settings": {
                    "role": "scene_render",
                    "cache_hit": result.cache_hit,
                    "cache_key": result.cache_key,
                    "total_frames": result.total_frames,
                },
            },
            peak_vram_gb=0.0,
        )
        self._record_asset(
            project, scene, result.path, AssetType.VIDEO, render_result,
            role="scene_render", job_id=job.id if job else None,
        )
        self._invalidate_stages(project, set(self._SHOT_TIMELINE_STAGES))
        return {
            "scene_id": scene.id,
            "path": str(destination.relative_to(root)),
            "total_frames": result.total_frames,
            "duration_seconds": result.duration_seconds,
            "cache_hit": result.cache_hit,
            "cache_key": result.cache_key,
        }

    def queue_scene_render(self, scene_id: str, *, force: bool = False) -> GenerationJob:
        scene = self._scene_or_key_error(scene_id)
        project = self._project(scene.project_id)
        active = next(
            (
                j for j in self.jobs.list(project.id)
                if j.stage == "scene_render" and j.scene_id == scene_id
                and j.status not in {
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                }
            ),
            None,
        )
        if active is not None:
            raise PipelineError("A scene render job is already queued or running.")
        return self.jobs.enqueue(GenerationJob(
            project_id=project.id, scene_id=scene.id,
            stage="scene_render",
            backend="ffmpeg",
            parameters={"force": force, "scene_index": scene.index},
        ))

    def run_scene_render_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.scene_id is None:
            return
        with media_process_scope(job_id):
            try:
                self._start_parent_job(job_id)
                self._begin_job_attempt(job_id)
                summary = self.render_scene(
                    job.scene_id, force=bool(job.parameters.get("force")), job=job,
                )
                # Re-fetch before writing: the row moved through PREPARING/
                # GENERATING above and a stale payload would reset its status.
                current = self.jobs.get(job_id)
                if current is not None:
                    self.database.save_job(current.model_copy(update={
                        "parameters": {**current.parameters, "result": summary},
                    }))
                self.jobs.transition(job_id, JobStatus.POSTPROCESSING, progress=0.95)
                self.jobs.complete(job_id)
            except Exception as exc:
                current = self.jobs.get(job_id)
                if current is not None and current.status not in {
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
                }:
                    self.jobs.fail(job_id, redact_secrets(exc))
                logger.warning("Scene render job %s failed: %s", job_id, redact_secrets(exc))

    # ------------------------------------------------------------------
    # Render preflight

    def _scene_render_issues(
        self, project: Project, scene: Scene, stored: list[Shot],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        assets = self.database.list_assets(project.id)
        root = self.store.project_path(project)
        shots_by_id = {item.id: item for item in self.database.list_shots(project.id)}
        scene_label = f"S{scene.index + 1}"
        for shot in effective_shots(scene, stored):
            label = f"{scene_label}/{shot.index + 1}"
            if shot.status is ShotStatus.FAILED:
                issues.append({
                    "scope": label, "shot_id": shot.id, "code": "failed_shot",
                    "detail": "the last generation attempt failed; regenerate it",
                })
            visual = current_visual_asset(shot, assets)
            if visual is None:
                issues.append({
                    "scope": label, "shot_id": shot.id, "code": "missing_visual",
                    "detail": "no generated visual asset is attached to this shot",
                })
            elif not (root / visual.filepath).is_file() \
                    or (root / visual.filepath).stat().st_size == 0:
                issues.append({
                    "scope": label, "shot_id": shot.id, "code": "corrupt_visual",
                    "detail": f"visual file {visual.filepath} is missing or empty",
                })
            if shot.source_asset_id is not None:
                source = next(
                    (item for item in assets if item.id == shot.source_asset_id), None,
                )
                source_path = root / source.filepath if source is not None else None
                if (
                    source_path is None
                    or not source_path.is_file()
                    or source_path.stat().st_size == 0
                ):
                    issues.append({
                        "scope": label, "shot_id": shot.id,
                        "code": "missing_source_media",
                        "detail": (
                            f"source asset {shot.source_asset_id!r} has no readable "
                            "media file; attach it before rendering"
                        ),
                    })
            if shot.settings.get("staleness"):
                issues.append({
                    "scope": label, "shot_id": shot.id, "code": "stale_dependency",
                    "detail": (
                        "marked stale because an upstream shot regenerated; "
                        "regenerate before final export"
                    ),
                })
            if shot.id in shots_by_id \
                    and parse_shot_continuity(dict(shot.settings)).enabled:
                try:
                    validate_continuity_chain(shot, shots_by_id)
                except ShotContinuityError as exc:
                    issues.append({
                        "scope": label, "shot_id": shot.id,
                        "code": "continuity_invalid",
                        "detail": redact_secrets(exc),
                    })
        manifest_path = root / "scenes" / f"{scene.index + 1:03d}" / "render-manifest.json"
        media_path = manifest_path.with_name("rendered.mp4")
        manifest = load_manifest(manifest_path) if manifest_path.is_file() else None
        if manifest is None or not media_path.is_file() or media_path.stat().st_size == 0:
            issues.append({
                "scope": scene_label, "shot_id": None,
                "code": "scene_unrendered",
                "detail": "this scene has no compiled render yet",
            })
        elif any(
            item.settings.get("staleness") or item.status is ShotStatus.FAILED
            for item in effective_shots(scene, stored)
        ):
            issues.append({
                "scope": scene_label, "shot_id": None,
                "code": "scene_stale",
                "detail": "a shot changed after this compiled render was built; "
                          "re-render the scene before the project export",
            })
        return issues

    def render_preflight(self, project_id: str) -> dict[str, Any]:
        """Group per-scene render blockers without touching any media."""
        project = self._project(project_id)
        report: list[dict[str, Any]] = []
        all_ready = True
        for scene in self.database.list_scenes(project.id):
            stored = self.shots_for_scene(scene)
            issues = self._scene_render_issues(project, scene, stored)
            blocking = [
                issue for issue in issues if issue["code"] in self._SCENE_RENDER_BLOCKERS
            ]
            ready = bool(stored) and not blocking and not issues
            if not ready:
                all_ready = False
            report.append({
                "scene_id": scene.id,
                "index": scene.index,
                "title": scene.title,
                "materialized": bool(stored),
                "ready": ready,
                "issues": issues,
            })
        if not report:
            all_ready = False
        return {
            "project_id": project.id,
            "scenes": report,
            "all_ready": all_ready,
        }

    def _check_parent_job(self, job_id: str | None) -> None:
        if not job_id:
            return
        job = self.jobs.get(job_id)
        if job is None or job.status is JobStatus.CANCELED:
            raise PipelineError("job was canceled")

    def update_scene(self, scene_id: str, changes: dict[str, Any]) -> Scene:
        scene = self.database.get_scene(scene_id)
        if scene is None:
            raise KeyError(f"scene not found: {scene_id}")
        if scene.locked:
            raise PipelineError("unlock the scene before editing it")
        project = self._project(scene.project_id)
        switching_to_h3 = (
            changes.get("visual_type") == VisualType.H3_AUDIOVISUAL.value
            and scene.visual_type is not VisualType.H3_AUDIOVISUAL
        )
        # Canvas selections are stage-specific metadata; keep them in scene settings.
        stage_settings = dict(scene.settings)
        for field in ("h3_canvas", "krea_canvas", "qwen_image_canvas"):
            value = changes.pop(field, None)
            if value is not None:
                stage_settings[field] = value
        ideogram_prompt_mode = changes.pop("ideogram_prompt_mode", None)
        if ideogram_prompt_mode is not None:
            if ideogram_prompt_mode not in {"quick", "precise"}:
                raise ValueError("ideogram_prompt_mode must be 'quick' or 'precise'")
            stage_settings["ideogram_prompt_mode"] = ideogram_prompt_mode
        ideogram_prompt_json = changes.pop("ideogram_prompt_json", None)
        if ideogram_prompt_json is not None:
            stage_settings["ideogram_prompt_json"] = validate_ideogram_prompt_json(
                ideogram_prompt_json
            )
        if (
            stage_settings.get("ideogram_prompt_mode", "quick") == "precise"
            and not isinstance(stage_settings.get("ideogram_prompt_json"), dict)
        ):
            raise ValueError("precise Ideogram prompt mode requires ideogram_prompt_json")
        image_motion_source = changes.pop("image_motion_source", None)
        if image_motion_source is not None:
            if image_motion_source not in {"krea2", "qwen_image_2512"}:
                raise ValueError(
                    "image_motion_source must be 'krea2' or 'qwen_image_2512'"
                )
            stage_settings["image_motion_source"] = image_motion_source
        text_overlay_layout = changes.pop("text_overlay_layout", None)
        if text_overlay_layout is not None:
            if text_overlay_layout not in {"auto", "hook", "reveal", "quote", "cta"}:
                raise ValueError(
                    "text_overlay_layout must be auto, hook, reveal, quote, or cta"
                )
            stage_settings["text_overlay_layout"] = text_overlay_layout
        h3_quality = changes.pop("h3_quality", None)
        if h3_quality is not None:
            try:
                h3_quality = H3Quality(h3_quality)
            except (TypeError, ValueError):
                valid = ", ".join(quality.value for quality in H3Quality)
                raise ValueError(
                    f"Invalid h3_quality {h3_quality!r}. Valid values: {valid}."
                ) from None
            stage_settings["h3_quality"] = h3_quality
        elif switching_to_h3 and not (
            isinstance(stage_settings.get("h3_canvas"), str)
            and stage_settings["h3_canvas"].strip().lower() != "auto"
        ):
            stage_settings["h3_quality"] = H3Quality.STANDARD
        h3_long_shot = changes.pop("h3_long_shot", None)
        if h3_long_shot is not None:
            stage_settings["h3_long_shot"] = bool(h3_long_shot)
        h3_continuity = changes.pop("h3_continuity", None)
        if h3_continuity is not None:
            if isinstance(h3_continuity, dict):
                stage_settings["h3_continuity"] = h3_continuity
            else:
                stage_settings.pop("h3_continuity", None)
        graphic_instructions = changes.pop("graphic_instructions", None)
        graphic_text = changes.pop("graphic_text", None)
        on_screen_text = changes.pop("on_screen_text", None)
        if graphic_instructions is not None or graphic_text is not None:
            graphic = stage_settings.get("graphic_screen", {})
            graphic = dict(graphic) if isinstance(graphic, dict) else {}
            original_graphic = dict(graphic)
            if graphic_instructions is not None:
                graphic["instructions"] = graphic_instructions
            if graphic_text is not None:
                graphic["exact_text"] = graphic_text
            if graphic != original_graphic:
                graphic["revision"] = int(original_graphic.get("revision", 0)) + 1
            stage_settings["graphic_screen"] = graphic
        if on_screen_text is not None:
            stage_settings["on_screen_text"] = on_screen_text
        if stage_settings != scene.settings:
            changes["settings"] = stage_settings
        candidate = Scene.model_validate({**scene.model_dump(), **changes, "updated_at": utc_now()})
        visual_direct_fields = (
            "visual_prompt", "negative_prompt", "visual_type", "selected_backend",
            "seed", "references",
            "needs_embedded_text", "text_in_image", "preferred_image_model",
            "test_generate_with_qwen", "test_generate_with_ideogram",
        )
        visual_setting_fields = (
            "h3_canvas", "h3_quality", "h3_long_shot", "h3_continuity",
            "krea_canvas", "qwen_image_canvas", "image_motion_source",
            "ideogram_prompt_mode", "ideogram_prompt_json",
            "on_screen_text", "graphic_screen", "text_overlay_layout",
        )
        visual_content_changed = (
            any(getattr(candidate, field) != getattr(scene, field) for field in visual_direct_fields)
            or any(
                candidate.settings.get(field) != scene.settings.get(field)
                for field in visual_setting_fields
            )
        )
        h3_visual_changed = (
            candidate.visual_type is VisualType.H3_AUDIOVISUAL
            and (visual_content_changed or candidate.duration != scene.duration)
        )
        if visual_content_changed or h3_visual_changed:
            stage_settings["visual_revision"] = int(
                scene.settings.get("visual_revision", 0)
            ) + 1
            changes["settings"] = stage_settings
            updated = Scene.model_validate({
                **scene.model_dump(), **changes, "updated_at": utc_now(),
            })
        else:
            updated = candidate
        if updated.visual_type is VisualType.H3_AUDIOVISUAL:
            resolution = resolve_quality(updated.settings, project.resolution)
            validate_duration(resolution, updated.duration)
            project_scenes = [
                updated if item.id == updated.id else item
                for item in self.database.list_scenes(project.id)
            ]
            validate_continuity_graph(updated, project_scenes)
        self.database.save_scene(updated)
        self.store.save_scene(project.slug, updated)
        plan_path = self.store.project_path(project) / "plan.json"
        if plan_path.is_file():
            plan = self.store.load_plan(project.slug)
            plan_scenes = [
                updated if item.id == updated.id else item
                for item in plan.scenes
            ]
            if any(item.id == updated.id for item in plan.scenes):
                self.store.save_plan(
                    project.slug,
                    plan.model_copy(update={"scenes": plan_scenes}),
                )
        # Field-aware stage invalidation. Dependency summary per edited field:
        #   narration          -> narration (mock joins all scene text into
        #                         master.wav), subtitles (audio-derived word
        #                         timings / text cues), then the timeline chain.
        #   duration / camera_instruction -> the timeline chain only.
        #   graphic settings / visual_type / H3 generation fields -> visuals,
        #                         then the timeline chain (existing behavior).
        # The timeline chain is timeline -> render_preview -> quality_control
        # -> render_final -> thumbnails (music/metadata do not read scenes).
        invalidated: set[str] = set()
        if updated.narration != scene.narration:
            invalidated |= {
                "narration", "subtitles", "timeline", "render_preview",
                "quality_control", "render_final", "thumbnails",
            }
        if (
            updated.duration != scene.duration
            or updated.camera_instruction != scene.camera_instruction
        ):
            invalidated |= {
                "timeline", "render_preview", "quality_control",
                "render_final", "thumbnails",
            }
        graphic_changed = stage_settings.get("graphic_screen") != scene.settings.get("graphic_screen")
        qwen_text_changed = stage_settings.get("on_screen_text") != scene.settings.get("on_screen_text")
        qwen_canvas_changed = (
            stage_settings.get("qwen_image_canvas")
            != scene.settings.get("qwen_image_canvas")
        )
        motion_source_changed = (
            stage_settings.get("image_motion_source", "krea2")
            != scene.settings.get("image_motion_source", "krea2")
        )
        visual_type_changed = updated.visual_type != scene.visual_type
        if (
            graphic_changed or qwen_text_changed or qwen_canvas_changed or motion_source_changed
            or visual_type_changed or h3_visual_changed
        ):
            invalidated |= {
                "visuals", "timeline", "render_preview", "quality_control",
                "render_final", "thumbnails",
            }
        if invalidated:
            self._invalidate_stages(project, invalidated)
        return updated

    def _start_parent_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        if job.status is JobStatus.CANCELED:
            raise PipelineError("job was canceled")
        if job.status is JobStatus.QUEUED:
            self.jobs.transition(job_id, JobStatus.PREPARING, progress=0.01)
        self.jobs.transition(job_id, JobStatus.GENERATING, progress=0.05)

    def _update_parent_job(
        self,
        job_id: str | None,
        *,
        progress: float,
        current_stage: str,
    ) -> None:
        if not job_id:
            return
        self._check_parent_job(job_id)
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        parameters = {**job.parameters, "current_stage": current_stage}
        self.database.save_job(
            job.model_copy(
                update={
                    "progress": progress,
                    "parameters": parameters,
                    "updated_at": utc_now(),
                }
            )
        )

    def _ensure_narration(self, project: Project, *, force: bool) -> Path:
        output = self.store.project_path(project) / "narration" / "master.wav"

        if not force and self._stage_complete(project, "narration"):
            return output

        if not self.mock_mode:
            voice = project.settings.get("voice", {})
            built_in_voice = (
                isinstance(voice, dict)
                and voice.get("provider") in {"chatterbox", "qwen_tts"}
                and not voice.get("voice_profile_id")
            )
            if not isinstance(voice, dict) or (
                not voice.get("voice_profile_id") and not built_in_voice
            ):
                raise PipelineError(
                    "Local narration requires a voice profile or a built-in "
                    "Qwen/Chatterbox voice."
                )
            allowed = set(NarrationRequest.model_fields)
            payload = {key: value for key, value in voice.items() if key in allowed}
            request = NarrationRequest.model_validate(payload)
            job = self.queue_narration(project.id, request)
            return self.run_narration_job(job.id)

        def operation() -> tuple[Path, list[Path]]:
            plan = self.store.load_plan(project.slug)
            script = " ".join(scene.narration for scene in plan.scenes)
            self._archive_output(project, output)
            result = self._mock_generate(
                project,
                "narration",
                project_dir=output.parent,
                prompt=script,
                duration=project.target_duration,
                seed=20_001,
            )
            generated = result.outputs[0]
            if generated != output:
                os.replace(generated, output)
            self._record_asset(project, None, output, AssetType.NARRATION, result, role="narration")
            return output, [output]

        return self._execute_stage(project, "narration", operation, backend="mock")[0]

    def _ensure_references(self, project: Project, *, force: bool) -> list[Path]:
        if not force and self._stage_complete(project, "references"):
            return self._stage_paths(project, "references")

        # The reference-image stage predates the scene-addressable real backends.
        # Krea and Image Motion consume the scene prompt directly, while any
        # explicit user references live on the scene itself. Do not invoke the
        # mock image generator merely to satisfy legacy stage bookkeeping.
        if not self.mock_mode:
            return self._execute_stage(
                project,
                "references",
                lambda: ([], []),
                backend="not_required",
            )[0]

        def operation() -> tuple[list[Path], list[Path]]:
            outputs: list[Path] = []
            for scene in self.database.list_scenes(project.id):
                directory = self._scene_dir(project, scene)
                destination = directory / "reference.png"
                self._archive_output(project, destination)
                result = self._mock_generate(
                    project,
                    "image",
                    project_dir=directory,
                    prompt=scene.visual_prompt,
                    seed=scene.seed,
                    width=min(project.resolution[0], 640),
                    height=min(project.resolution[1], 360),
                )
                generated = result.outputs[0]
                if generated != destination:
                    os.replace(generated, destination)
                self._record_asset(project, scene, destination, AssetType.IMAGE, result, role="reference")
                outputs.append(destination)
            return outputs, outputs

        return self._execute_stage(project, "references", operation, backend="mock")[0]

    def _ensure_visuals(self, project: Project, *, force: bool) -> list[Asset]:
        if not force and self._stage_complete(project, "visuals"):
            return [
                asset for asset in self.database.list_assets(project.id)
                if asset.settings.get("role") == "visual"
            ]

        # Pre-create the stage job so the per-scene child rows can reference
        # it (the Job Monitor links children to their parent stage by id).
        backend = "mock" if self.mock_mode else "automatic"
        stage_job = GenerationJob(
            project_id=project.id, stage="visuals", backend=backend,
        )

        def operation() -> tuple[list[Asset], list[Path]]:
            root = self.store.project_path(project)
            scenes = self.database.list_scenes(project.id)
            scenes_by_id = {scene.id: scene for scene in scenes}
            existing_by_scene: dict[str, Asset] = {}
            if not force:
                for asset in self.database.list_assets(project.id):
                    asset_scene = scenes_by_id.get(asset.scene_id or "")
                    path = root / asset.filepath
                    if (
                        asset_scene is not None
                        and self._is_current_visual_asset(asset_scene, asset)
                        and path.is_file()
                        and path.stat().st_size > 0
                    ):
                        # Assets are returned oldest-first, so the latest valid
                        # variant naturally replaces an earlier database entry.
                        existing_by_scene[asset.scene_id] = asset
            assets: list[Asset] = []
            for scene in scenes:
                existing = existing_by_scene.get(scene.id)
                if existing is not None:
                    assets.append(existing)
                    continue
                # One child row per generated scene: the Job Monitor shows
                # live per-scene progress instead of a silent stage bar, and
                # a canceled row opts that scene out (like visual batches).
                child = self.jobs.enqueue(GenerationJob(
                    project_id=project.id,
                    scene_id=scene.id,
                    stage="scene_visual",
                    backend=backend,
                    parameters={"parent_job_id": stage_job.id},
                ))
                try:
                    assets.append(self._run_scene_visual_job(project, scene, child))
                except PipelineError:
                    current = self.jobs.get(child.id)
                    if current is not None and current.status is JobStatus.CANCELED:
                        continue  # user opted this scene out; the rest continue
                    raise
            paths = [self.store.project_path(project) / asset.filepath for asset in assets]
            return assets, paths

        return self._execute_stage(project, "visuals", operation, backend=backend, job=stage_job)[0]

    @staticmethod
    def _is_current_visual_asset(scene: Scene, asset: Asset) -> bool:
        if asset.settings.get("role") != "visual":
            return False
        if int(asset.settings.get("visual_revision", 0)) != int(
            scene.settings.get("visual_revision", 0)
        ):
            return False
        recorded_type = asset.settings.get("visual_type")
        if recorded_type is not None and recorded_type != scene.visual_type.value:
            return False
        if scene.visual_type is VisualType.IMAGE_MOTION:
            recorded_source = asset.settings.get("image_motion_source", "krea2")
            if recorded_source != PipelineService._image_motion_source(scene):
                return False
        # Graphic Screen assets created before visual-type provenance was added still carry a
        # graphic revision. Never reuse one after the scene has switched to another backend.
        if recorded_type is None and scene.visual_type is not VisualType.GRAPHIC_SCREEN:
            if "graphic_revision" in asset.settings:
                return False
        if scene.visual_type is not VisualType.GRAPHIC_SCREEN:
            return True
        settings = scene.settings.get("graphic_screen", {})
        revision = int(settings.get("revision", 0)) if isinstance(settings, dict) else 0
        return asset.settings.get("graphic_revision") == revision

    def _generate_visual(self, project: Project, scene: Scene, *, use_cache: bool = True) -> Asset:
        if scene.visual_type is VisualType.GRAPHIC_SCREEN:
            return self._dispatch_graphic_screen(
                project, scene, self._scene_dir(project, scene), use_cache=use_cache,
            )
        directory = self._scene_dir(project, scene)
        use_video = scene.visual_type in {VisualType.WAN_VIDEO, VisualType.H3_AUDIOVISUAL, VisualType.H3_REFERENCE}
        kind = "video" if use_video else "image"
        destination = directory / ("video.mp4" if use_video else "visual.png")
        # Image-model routing (Ideogram 4 addition). Resolving an explicit or
        # detected preferred model redirects eligible still-image scenes to
        # Ideogram/Qwen/Krea; everything else keeps the legacy dispatch below.
        route_model = self._resolve_scene_image_model(scene)
        comparison_models = (
            self._comparison_variant_models(scene, route_model)
            if not self.mock_mode
            and kind == "image"
            and scene.visual_type is not VisualType.TEXT_OVERLAY_STILL
            else []
        )
        if scene.visual_type is not VisualType.H3_AUDIOVISUAL:
            self._archive_output(project, destination)
        if scene.visual_type is VisualType.TEXT_OVERLAY_STILL:
            result = self._dispatch_text_overlay_still(
                project, scene, directory, use_cache=use_cache,
            )
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
        elif not self.mock_mode and route_model is not None:
            with self._gpu_lock:
                result = self._dispatch_image_model(
                    project, scene, directory, route_model, use_cache=use_cache,
                )
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
        elif not self.mock_mode and scene.visual_type is VisualType.H3_AUDIOVISUAL:
            # GPU-heavy ComfyUI section: serialized by _gpu_lock (see lock-order
            # comment in __init__); callers may already hold _lock (generate_scene).
            with self._gpu_lock:
                result = self._dispatch_h3(project, scene, directory, use_cache=use_cache)
            pending = directory / ".video.mp4.pending"
            if result.outputs[0] != pending:
                os.replace(result.outputs[0], pending)
            if not pending.is_file() or pending.stat().st_size == 0:
                pending.unlink(missing_ok=True)
                raise PipelineError("H3 output was empty before publication.")
            try:
                probe_media(pending, self.renderer.binaries)
            except Exception as exc:
                pending.unlink(missing_ok=True)
                raise PipelineError(f"H3 output failed QC before publication: {exc}") from exc
            self._publish_pending_file(project, pending, destination)
        elif not self.mock_mode and scene.visual_type is VisualType.QWEN_IMAGE_STILL:
            with self._gpu_lock:
                result = self._dispatch_qwen_image_2512(
                    project, scene, directory, use_cache=use_cache,
                )
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
        elif not self.mock_mode and scene.visual_type is VisualType.IMAGE_MOTION:
            with self._gpu_lock:
                if self._image_motion_source(scene) == "qwen_image_2512":
                    result = self._dispatch_qwen_image_2512(
                        project, scene, directory, use_cache=use_cache,
                    )
                else:
                    result = self._dispatch_krea2(
                        project, scene, directory, use_cache=use_cache,
                    )
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
        elif not self.mock_mode and scene.visual_type is VisualType.KREA2_STILL:
            with self._gpu_lock:
                result = self._dispatch_krea2(project, scene, directory, use_cache=use_cache)
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
        else:
            result = self._mock_generate(
                project,
                kind,
                project_dir=directory,
                prompt=scene.visual_prompt,
                negative_prompt=scene.negative_prompt,
                seed=scene.seed,
                duration=scene.duration if use_video else None,
                width=min(project.resolution[0], 640) if self.mock_mode else project.resolution[0],
                height=min(project.resolution[1], 360) if self.mock_mode else project.resolution[1],
            )
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
        if comparison_models and route_model is not None:
            # Side-by-side testing (Qwen vs Ideogram): render the requested
            # variants into comparisons/<model>/ subfolders. They are recorded
            # as role="comparison" assets so the timeline keeps using the
            # primary visual.png while both candidates stay reviewable.
            self._generate_comparison_variants(
                project, scene, directory, route_model, comparison_models,
                use_cache=use_cache,
            )
        if scene.visual_type is VisualType.IMAGE_MOTION:
            metadata = dict(result.metadata)
            result_settings = dict(metadata.get("settings", {}))
            result_settings["image_motion_source"] = self._image_motion_source(scene)
            metadata["settings"] = result_settings
            result = GenerationResult(
                outputs=result.outputs,
                metadata=metadata,
                peak_vram_gb=result.peak_vram_gb,
            )
        asset = self._record_asset(
            project,
            scene,
            destination,
            AssetType.VIDEO if use_video else AssetType.IMAGE,
            result,
            role="visual",
        )
        updated = scene.model_copy(update={"status": SceneStatus.GENERATED, "updated_at": utc_now()})
        self.database.save_scene(updated)
        self.store.save_scene(project.slug, updated)
        return asset

    def _resolve_scene_image_model(self, scene: Scene) -> ImageModelOption | None:
        """Pick the routed image model for a still-image scene, if any.

        Only explicit preferences and detected embedded-text needs reroute;
        every legacy scene keeps its historical dispatch path untouched:
          * preferred_image_model set → that generator (user/director override)
          * needs_embedded_text and automatic → config text_model (Ideogram 4),
            because Qwen's in-image lettering was not strong enough and Ideogram
            is the candidate under test
          * otherwise None → visual_type dispatch exactly as before
        """

        if scene.visual_type.value not in ROUTABLE_VISUAL_TYPES:
            return None
        preference = str(
            scene.preferred_image_model.value
            if isinstance(scene.preferred_image_model, ImageModelOption)
            else scene.preferred_image_model
        )
        if preference != "automatic":
            try:
                return ImageModelOption(preference)
            except ValueError:
                return None
        if scene.visual_type is VisualType.TEXT_OVERLAY_STILL:
            return ImageModelOption.KREA
        if scene.visual_type is VisualType.IDEOGRAM4_STILL:
            return ImageModelOption.IDEOGRAM4_LOCAL
        if scene.needs_embedded_text:
            try:
                return ImageModelOption(self.config.image_generation.text_model)
            except ValueError:
                return ImageModelOption.IDEOGRAM4_LOCAL
        return None

    def _comparison_variant_models(
        self, scene: Scene, primary: ImageModelOption | None,
    ) -> list[ImageModelOption]:
        """Models to render alongside the primary for side-by-side review."""

        if primary is None:
            return []
        mode = (
            self.config.image_generation.comparison_mode
            or bool(scene.settings.get("comparison_mode", False))
        )
        wants_qwen = scene.test_generate_with_qwen or (mode and scene.needs_embedded_text)
        wants_ideogram = scene.test_generate_with_ideogram or (mode and scene.needs_embedded_text)
        variants: list[ImageModelOption] = []
        if wants_qwen and primary is not ImageModelOption.QWEN_IMAGE:
            variants.append(ImageModelOption.QWEN_IMAGE)
        if wants_ideogram and primary is not ImageModelOption.IDEOGRAM4_LOCAL:
            variants.append(ImageModelOption.IDEOGRAM4_LOCAL)
        return variants

    def _dispatch_image_model(
        self,
        project: Project,
        scene: Scene,
        directory: Path,
        model: ImageModelOption,
        *,
        use_cache: bool = True,
    ) -> GenerationResult:
        """Send one image job to the selected generator backend."""

        if model is ImageModelOption.KREA:
            return self._dispatch_krea2(project, scene, directory, use_cache=use_cache)
        if model is ImageModelOption.QWEN_IMAGE:
            return self._dispatch_qwen_image_2512(project, scene, directory, use_cache=use_cache)
        if model is ImageModelOption.IDEOGRAM4_LOCAL:
            return self._dispatch_ideogram4(project, scene, directory, use_cache=use_cache)
        raise PipelineError(f"Unsupported image model {model!r}.")

    def _generate_comparison_variants(
        self,
        project: Project,
        scene: Scene,
        directory: Path,
        primary: ImageModelOption,
        variants: list[ImageModelOption],
        *,
        use_cache: bool = True,
    ) -> list[Asset]:
        """Render and record comparison variants under comparisons/<model>/.

        Output layout inside each scene folder mirrors the documented structure:
        the primary stays scenes/<NNN>/visual.png while test renders land in
        scenes/<NNN>/comparisons/{qwen,ideogram,krea}/visual.png.
        """

        recorded: list[Asset] = []
        for model in variants:
            dirname = IMAGE_MODEL_DIRNAMES.get(model.value, model.value)
            variant_dir = directory / "comparisons" / dirname
            variant_dir.mkdir(parents=True, exist_ok=True)
            destination = variant_dir / "visual.png"
            self._archive_output(project, destination)
            with self._gpu_lock:
                result = self._dispatch_image_model(
                    project, scene, variant_dir, model, use_cache=use_cache,
                )
            if result.outputs[0] != destination:
                os.replace(result.outputs[0], destination)
            metadata = dict(result.metadata)
            settings = dict(metadata.get("settings", {}))
            settings["comparison_for_scene"] = True
            settings["primary_image_model"] = primary.value
            metadata["settings"] = settings
            recorded.append(self._record_asset(
                project, scene, destination, AssetType.IMAGE,
                GenerationResult(
                    outputs=result.outputs,
                    metadata=metadata,
                    peak_vram_gb=result.peak_vram_gb,
                ),
                role="comparison",
            ))
        return recorded

    @staticmethod
    def _service_error_parameters(exc: BaseException) -> dict[str, Any]:
        """Bounded redacted transport diagnostics for generation_attempts rows.

        BackendError.details already carries a truncated, secret-redacted server
        response body; persisting it makes 5xx bursts diagnosable from the
        attempt history alone instead of requiring live server logs.
        """
        details = getattr(exc, "details", None)
        if not details:
            return {}
        return {"service_details": redact_secrets(details)[:400]}

    def _dispatch_graphic_screen(
        self, project: Project, scene: Scene, directory: Path, *, use_cache: bool = True,
    ) -> Asset:
        """Generate a static, validated local Graphic Screen without touching GPU state."""
        destination = directory / "visual.png"
        source_path = directory / "graphic-screen.html"
        manifest_path = directory / "graphic-screen.json"
        if self.mock_mode:
            result = self._mock_generate(
                project, "image", project_dir=directory, prompt=scene.visual_prompt,
                seed=scene.seed, width=min(project.resolution[0], 640),
                height=min(project.resolution[1], 360),
            )
            pending = directory / ".visual.png.pending"
            if result.outputs[0] != pending:
                os.replace(result.outputs[0], pending)
            document = (
                "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
                "<main>LOCAL VIDEO STUDIO — MOCK GRAPHIC SCREEN</main></body></html>"
            )
            visible = ["LOCAL VIDEO STUDIO — MOCK GRAPHIC SCREEN"]
            title = scene.title or "Mock Graphic Screen"
            summary = "Deterministic mock Graphic Screen fixture"
            model = "deterministic-placeholder-v1"
            attempt_number = 1
            renderer_version = "mock"
        else:
            self._require_selected_llm_model(project)
            backend = self.registry.get("local_llm")
            if not isinstance(backend, LocalLLMBackend):
                raise PipelineError("Graphic Screen requires the configured local LLM backend")
            generator = GraphicScreenGenerator(
                backend, cache=self.generation_cache if use_cache else None,
            )
            try:
                response, document, visible, attempt_number = generator.generate(project, scene)
            except Exception as exc:
                count = int(getattr(exc, "attempt_count", 1))
                recorded = [redact_secrets(item) for item in generator.attempt_errors]
                while len(recorded) < count:
                    recorded.append(redact_secrets(exc))
                for message in recorded[:count]:
                    self.database.save_attempt(GenerationAttempt(
                        backend="local_graphic", model=project.selected_llm_model,
                        model_version="server-managed", workflow_version="graphic-screen-v1",
                        parameters={
                            "sanitizer_version": "graphic-screen-sanitizer-v1",
                            **self._service_error_parameters(exc),
                        },
                        seed=scene.seed, success=False,
                        error=message,
                ))
                if isinstance(exc, BackendError):
                    raise PipelineError(
                        f"Graphic Screen local LLM failed: {redact_secrets(exc)} "
                        "The approved visual was kept."
                    ) from exc
                raise PipelineError(
                    f"{redact_secrets(exc)} "
                    "The approved visual was kept."
                ) from exc
            repair_errors = [redact_secrets(item) for item in generator.attempt_errors]
            while len(repair_errors) < attempt_number - 1:
                repair_errors.append("Graphic Screen response required structural repair.")
            for message in repair_errors[: attempt_number - 1]:
                self.database.save_attempt(GenerationAttempt(
                    backend="local_graphic", model=project.selected_llm_model,
                    model_version="server-managed", workflow_version="graphic-screen-v1",
                    parameters={"sanitizer_version": "graphic-screen-sanitizer-v1"},
                    seed=scene.seed, success=False,
                    error=message,
                ))
            pending = directory / ".visual.png.pending"
            try:
                png_hash = self.graphic_renderer.render(
                    document, pending, width=project.resolution[0], height=project.resolution[1],
                )
            except Exception as exc:
                self.database.save_attempt(GenerationAttempt(
                    backend="local_graphic", model=project.selected_llm_model,
                    model_version="server-managed", workflow_version="graphic-screen-v1",
                    parameters={
                        "renderer": "chromium-headless",
                        **self._service_error_parameters(exc),
                    }, seed=scene.seed,
                    success=False, error=redact_secrets(exc),
                ))
                raise PipelineError("Graphic Screen rendering failed; the approved visual was kept.") from exc
            title, summary = response.title, response.design_summary
            model = generator.selected_model
            renderer_version = self.graphic_renderer.version

        # Publish only after a complete candidate exists. A failed regeneration leaves the current
        # source, manifest, and PNG untouched; a successful one archives all three as a variant.
        source_pending = directory / ".graphic-screen.html.pending"
        manifest_pending = directory / ".graphic-screen.json.pending"
        source_pending.write_text(document, encoding="utf-8")
        source_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
        png_hash = hashlib.sha256(pending.read_bytes()).hexdigest()
        settings = scene.settings.get("graphic_screen", {})
        instructions = str(settings.get("instructions", scene.visual_prompt)) if isinstance(settings, dict) else scene.visual_prompt
        font_identity, font_hash = (
            ("Noto Sans (mock)", None) if self.mock_mode else self.graphic_renderer.font_metadata()
        )
        manifest = GraphicScreenManifest(
            project_resolution=project.resolution, design_instructions=instructions[:8_000],
            visible_text=visible, title=title, design_summary=summary, model=model,
            renderer_version=renderer_version, attempt_number=attempt_number,
            source_hash=source_hash, png_hash=png_hash,
            font_identity=font_identity, font_hash=font_hash,
        )
        self._atomic_json(manifest_pending, manifest.model_dump(mode="json"))
        self._publish_graphic_artifacts(project, {
            source_path: source_pending,
            manifest_path: manifest_pending,
            destination: pending,
        })
        result = GenerationResult(
            outputs=(destination,),
            metadata={
                "backend": "local_graphic" if not self.mock_mode else "mock",
                "model": model, "model_version": "server-managed" if not self.mock_mode else "1",
                "workflow_version": "graphic-screen-v1", "seed": scene.seed,
                "prompt": instructions, "negative_prompt": "",
                "settings": {
                    "renderer": "chromium-headless" if not self.mock_mode else "mock",
                    "sanitizer_version": "graphic-screen-sanitizer-v1", "source_hash": source_hash,
                    "png_hash": png_hash, "manifest": str(manifest_path.relative_to(self.store.project_path(project))),
                    "cache_hit": generator.cache_hit if not self.mock_mode else False,
                    "cache_key": generator.cache_key if not self.mock_mode else None,
                    "graphic_revision": (
                        int(settings.get("revision", 0)) if isinstance(settings, dict) else 0
                    ),
                },
            }, peak_vram_gb=0.0,
        )
        asset = self._record_asset(project, scene, destination, AssetType.IMAGE, result, role="visual")
        updated = scene.model_copy(update={"status": SceneStatus.GENERATED, "updated_at": utc_now()})
        self.database.save_scene(updated)
        self.store.save_scene(project.slug, updated)
        return asset

    @staticmethod
    def _backend_identity(backend: Any) -> dict[str, Any]:
        descriptor = backend.descriptor()
        return {
            "model": descriptor.model_name,
            "model_version": descriptor.model_version,
            "quantization": descriptor.quantization,
        }

    def _generation_cache_key(self, backend_name: str, payload: dict[str, Any]) -> str | None:
        if self.generation_cache is None:
            return None
        try:
            return self.generation_cache.key_hash({"backend": backend_name, **payload})
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _reference_hashes(references: tuple[Path, ...]) -> list[str] | None:
        hashes: list[str] = []
        for reference in references:
            try:
                hashes.append(compute_sha256(reference))
            except (OSError, ValueError):
                return None
        return hashes

    @staticmethod
    def _cache_hit_metadata(cached: CachedGeneration, cache_key: str) -> dict[str, Any]:
        stored = dict(cached.metadata.get("result_metadata", {}))
        settings = dict(stored.get("settings", {}))
        settings.update({"cache_hit": True, "cache_key": cache_key})
        stored["settings"] = settings
        return stored

    def _store_generation_result(
        self,
        backend_name: str,
        cache_key: str | None,
        output: Path | None,
        metadata: Mapping[str, Any],
    ) -> None:
        if cache_key is None or self.generation_cache is None or output is None:
            return
        try:
            if not output.is_file() or output.stat().st_size == 0:
                return
        except OSError:
            return
        self.generation_cache.store(
            backend_name, cache_key, output,
            metadata={"result_metadata": dict(metadata)},
        )

    @staticmethod
    def _text_overlay_literals(scene: Scene) -> list[str]:
        """Return distinct, non-empty literal regions in authored order."""
        literals = [line for line in scene.text_in_image.splitlines() if line != ""]
        if not literals:
            configured = scene.settings.get("on_screen_text", [])
            if isinstance(configured, list):
                literals = [str(item) for item in configured if str(item) != ""]
        if not literals:
            raise PipelineError(
                "Generated background + exact text requires at least one Text inside image line."
            )
        if len(literals) > 6:
            raise PipelineError(
                "Generated background + exact text supports at most six text regions per still."
            )
        return literals

    @staticmethod
    def _text_overlay_model(scene: Scene) -> ImageModelOption:
        configured = scene.settings.get("text_overlay_background_model")
        if configured is None:
            preference = scene.preferred_image_model
            configured = preference.value if hasattr(preference, "value") else str(preference)
        if configured in {None, "", "automatic"}:
            configured = ImageModelOption.KREA.value
        try:
            return ImageModelOption(str(configured))
        except ValueError as exc:
            raise PipelineError(
                "text overlay background model must be krea, ideogram4_local, or qwen_image"
            ) from exc

    @staticmethod
    def _text_overlay_positions(count: int, layout: str = "auto") -> list[float]:
        if layout == "quote" and count == 2:
            return [0.29, 0.70]
        if layout == "cta":
            if count == 2:
                return [0.18, 0.74]
            if count == 3:
                return [0.14, 0.46, 0.75]
        if layout == "hook" and count == 2:
            return [0.18, 0.74]
        if count == 1:
            return [0.50]
        if count == 2:
            return [0.20, 0.78]
        if count == 3:
            return [0.16, 0.48, 0.80]
        step = 0.68 / (count - 1)
        return [0.14 + step * index for index in range(count)]

    @staticmethod
    def _text_overlay_layout(scene: Scene, literals: list[str]) -> str:
        configured = str(scene.settings.get("text_overlay_layout", "auto")).strip().lower()
        supported = {"auto", "hook", "reveal", "quote", "cta"}
        if configured not in supported:
            raise PipelineError(
                "text overlay layout must be auto, hook, reveal, quote, or cta"
            )
        if configured != "auto":
            return configured
        if any("FULL VIDEO" in literal.upper() for literal in literals) or len(literals) >= 3:
            return "cta"
        if len(literals) == 2 and max(len(literal) for literal in literals) > 30:
            return "quote"
        if len(literals) == 1 and len(literals[0]) <= 18:
            return "reveal"
        return "hook"

    @staticmethod
    def _overlay_font(size: int):
        from PIL import ImageFont

        for candidate in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ):
            path = Path(candidate)
            if path.is_file():
                return ImageFont.truetype(str(path), size)
        return ImageFont.load_default()

    @classmethod
    def _fit_overlay_text(
        cls, draw: Any, text: str, *, width: int, height: int,
        max_width_fraction: float = 0.86,
        max_height_fraction: float = 0.22,
        minimum_size_fraction: float = 0.028,
        maximum_size_fraction: float = 0.09,
    ) -> tuple[str, Any, tuple[int, int, int, int]]:
        """Fit verbatim words into a mobile-safe region using visual line wraps."""
        max_width = int(width * max_width_fraction)
        max_height = int(height * max_height_fraction)
        minimum = max(18, int(height * minimum_size_fraction))
        maximum = max(minimum, int(height * maximum_size_fraction))
        words = text.split(" ")
        for size in range(maximum, minimum - 1, -2):
            font = cls._overlay_font(size)
            lines: list[str] = []
            current = ""
            for word in words:
                candidate = word if not current else f"{current} {word}"
                bounds = draw.textbbox((0, 0), candidate, font=font, stroke_width=max(1, size // 28))
                if current and bounds[2] - bounds[0] > max_width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
            rendered = "\n".join(lines)
            bounds = draw.multiline_textbbox(
                (0, 0), rendered, font=font, spacing=max(4, size // 5),
                align="center", stroke_width=max(1, size // 28),
            )
            if bounds[2] - bounds[0] <= max_width and bounds[3] - bounds[1] <= max_height:
                return rendered, font, bounds
        font = cls._overlay_font(minimum)
        rendered = "\n".join(words)
        bounds = draw.multiline_textbbox(
            (0, 0), rendered, font=font, spacing=max(4, minimum // 5), align="center",
            stroke_width=max(1, minimum // 28),
        )
        return rendered, font, bounds

    @classmethod
    def _composite_exact_text(
        cls, background: Path, output: Path, *, resolution: tuple[int, int],
        literals: list[str], colors: list[str], layout: str = "auto",
    ) -> str:
        """Flatten exact local typography over a generated text-free background."""
        from PIL import Image, ImageDraw, ImageOps

        width, height = resolution
        with Image.open(background) as source:
            base = ImageOps.fit(
                source.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS,
            ).convert("RGBA")
        draw = ImageDraw.Draw(base, "RGBA")
        for index, (literal, y_fraction) in enumerate(
            zip(
                literals,
                cls._text_overlay_positions(len(literals), layout),
                strict=True,
            )
        ):
            if layout == "quote":
                max_width_fraction = 0.82
                max_height_fraction = 0.26 if index == 0 else 0.15
                maximum_size_fraction = 0.056 if index == 0 else 0.052
                panel_opacity = 52
            elif layout == "cta":
                max_width_fraction = 0.78
                max_height_fraction = 0.15
                maximum_size_fraction = 0.056
                panel_opacity = 68
            elif layout == "hook":
                max_width_fraction = 0.82
                max_height_fraction = 0.18
                maximum_size_fraction = 0.068
                panel_opacity = 62
            else:  # reveal
                max_width_fraction = 0.84
                max_height_fraction = 0.20
                maximum_size_fraction = 0.084
                panel_opacity = 48
            rendered, font, bounds = cls._fit_overlay_text(
                draw, literal, width=width, height=height,
                max_width_fraction=max_width_fraction,
                max_height_fraction=max_height_fraction,
                maximum_size_fraction=maximum_size_fraction,
            )
            text_width = bounds[2] - bounds[0]
            text_height = bounds[3] - bounds[1]
            center_x = width / 2
            center_y = height * y_fraction
            pad_x = max(12, int(width * 0.018))
            pad_y = max(8, int(height * 0.006))
            box = (
                int(center_x - text_width / 2 - pad_x),
                int(center_y - text_height / 2 - pad_y),
                int(center_x + text_width / 2 + pad_x),
                int(center_y + text_height / 2 + pad_y),
            )
            draw.rounded_rectangle(
                box,
                radius=max(12, int(height * 0.010)),
                fill=(6, 8, 12, panel_opacity),
            )
            color = colors[index % len(colors)] if colors else "#F2EEE5"
            stroke = max(2, int(height * 0.0025))
            draw.multiline_text(
                (center_x, center_y), rendered, font=font, fill=color,
                anchor="mm", align="center",
                spacing=max(4, int(getattr(font, "size", height * 0.05)) // 5),
                stroke_width=stroke, stroke_fill="#080A0E",
            )
        staged = output.with_name(f".{output.name}.tmp")
        base.convert("RGB").save(staged, format="PNG")
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(staged, output)
        return hashlib.sha256(output.read_bytes()).hexdigest()

    def _dispatch_text_overlay_still(
        self, project: Project, scene: Scene, directory: Path, *, use_cache: bool = True,
    ) -> GenerationResult:
        """Generate a text-free local background, then flatten exact local typography."""
        literals = self._text_overlay_literals(scene)
        model = self._text_overlay_model(scene)
        layout = self._text_overlay_layout(scene, literals)
        background_dir = directory / "generated-background"
        background_dir.mkdir(parents=True, exist_ok=True)
        background_path = directory / "generated-background.png"
        background_settings = {
            key: value for key, value in scene.settings.items()
            if key not in {
                "ideogram_prompt_json", "ideogram_prompt_mode", "image_prompts",
                "on_screen_text", "graphic_screen", "text_overlay_colors",
                "text_overlay_layout",
            }
        }
        authored_background = scene.visual_prompt.strip()
        if not authored_background:
            authored_background = ". ".join(
                part.strip().rstrip(".")
                for part in (scene.title, scene.narration)
                if part.strip()
            )
        text_suppression = (
            "words, letters, numbers, typography, captions, labels, logos, signs, "
            "interfaces, watermarks, pseudo-text, illegible writing, book-page writing"
        )
        negative_prompt = scene.negative_prompt.strip()
        if text_suppression not in negative_prompt:
            negative_prompt = ", ".join(
                part for part in (negative_prompt, text_suppression) if part
            )
        background_scene = scene.model_copy(update={
            "visual_type": (
                VisualType.KREA2_STILL if model is ImageModelOption.KREA
                else VisualType.IDEOGRAM4_STILL if model is ImageModelOption.IDEOGRAM4_LOCAL
                else VisualType.QWEN_IMAGE_STILL
            ),
            "visual_prompt": (
                authored_background
                + "\nCreate only the cinematic visual background. Include no words, letters, "
                  "numbers, captions, labels, logos, signs, interfaces, or watermarks; reserve "
                  "clean negative space for typography added later. Any document or book page "
                  "must be out of focus or turned away so it contains no readable or fake text."
            ),
            "negative_prompt": negative_prompt,
            "needs_embedded_text": False,
            "text_in_image": "",
            "preferred_image_model": model.value,
            "settings": background_settings,
        })
        if self.mock_mode:
            background_result = self._mock_generate(
                project, "image", project_dir=background_dir,
                prompt=background_scene.visual_prompt,
                negative_prompt=background_scene.negative_prompt,
                seed=background_scene.seed,
                width=project.resolution[0], height=project.resolution[1],
            )
        else:
            with self._gpu_lock:
                background_result = self._dispatch_image_model(
                    project, background_scene, background_dir, model, use_cache=use_cache,
                )
        source = Path(background_result.outputs[0])
        if source != background_path:
            os.replace(source, background_path)
        colors_raw = scene.settings.get("text_overlay_colors", ["#F2EEE5", "#E78A2E"])
        colors = [
            str(color) for color in colors_raw
            if isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color)
        ] if isinstance(colors_raw, list) else []
        pending = directory / ".visual.png.pending"
        png_hash = self._composite_exact_text(
            background_path, pending, resolution=project.resolution,
            literals=literals, colors=colors or ["#F2EEE5", "#E78A2E"],
            layout=layout,
        )
        metadata = dict(background_result.metadata)
        settings = dict(metadata.get("settings", {}))
        settings.update({
            "text_overlay_workflow": "generated-background-exact-text-v3",
            "text_overlay_background_model": model.value,
            "text_overlay_layout": layout,
            "text_overlay_literals": literals,
            "text_overlay_colors": colors or ["#F2EEE5", "#E78A2E"],
            "text_overlay_png_sha256": png_hash,
            "generated_background": str(
                background_path.relative_to(self.store.project_path(project))
            ),
        })
        metadata["settings"] = settings
        metadata["workflow_version"] = "generated-background-exact-text-v3"
        return GenerationResult(
            outputs=(pending,), metadata=metadata,
            peak_vram_gb=background_result.peak_vram_gb,
        )

    def _dispatch_krea2(
        self, project: Project, scene: Scene, directory: Path, *, use_cache: bool = True,
    ) -> GenerationResult:
        """Send a still image to the local native ComfyUI Krea 2 Turbo workflow."""
        width, height = self._krea2_scene_canvas(project, scene)
        prompt = scene.visual_prompt.strip()
        # Negative prompt is sent separately to the backend; do not embed it
        # as a positive "Avoid:" clause, which image models may render literally.
        negative = scene.negative_prompt.strip()
        parameters = {
            "kind": "image",
            "scene_id": scene.id,
            "width": width,
            "height": height,
            "steps": 8,
            "cfg": 1.0,
            "sampler": "er_sde",
            "scheduler": "simple",
        }
        backend = self.registry.get("krea2_comfyui")
        sampler_settings = {
            key: value for key, value in parameters.items() if key != "scene_id"
        }
        cache_key = self._generation_cache_key("krea2_comfyui", {
            "kind": "krea2_image",
            "workflow_version": "krea2-turbo-fp8-v1",
            **self._backend_identity(backend),
            "prompt": prompt,
            "negative_prompt": negative,
            "seed": scene.seed,
            "width": width,
            "height": height,
            **sampler_settings,
        })
        if cache_key is not None and use_cache:
            cached = self.generation_cache.lookup("krea2_comfyui", cache_key)
            if cached is not None:
                staged = directory / ".visual.png.cached"
                shutil.copyfile(cached.path, staged)
                return GenerationResult(
                    outputs=(staged,),
                    metadata=self._cache_hit_metadata(cached, cache_key),
                    peak_vram_gb=0.0,
                )
        reusing_resident = self._prepare_comfy_backend("krea2_comfyui")
        if not reusing_resident:
            self._check_krea2_vram()
        request = GenerationRequest(
            job_id=f"{project.id}:krea2:{scene.seed}",
            output_dir=directory,
            prompt=prompt,
            negative_prompt=negative,
            seed=scene.seed,
            width=width,
            height=height,
            settings={
                "kind": "image",
                "workflow": self.KREA2_WORKFLOW,
                "workflow_version": "krea2-turbo-fp8-v1",
            },
        )
        try:
            backend.load()
            result = backend.generate(request)
            self._resident_comfy_backend = "krea2_comfyui"
            metadata = {**dict(result.metadata), "settings": parameters}
            output = result.outputs[0] if result.outputs else None
            self._store_generation_result("krea2_comfyui", cache_key, output, metadata)
            return GenerationResult(
                outputs=result.outputs,
                metadata=metadata,
                peak_vram_gb=result.peak_vram_gb,
            )
        except Exception as exc:
            descriptor = backend.descriptor()
            self.database.save_attempt(
                GenerationAttempt(
                    backend=descriptor.backend_name,
                    model=descriptor.model_name,
                    model_version=descriptor.model_version,
                    quantization=descriptor.quantization,
                    workflow_version="krea2-turbo-fp8-v1",
                    parameters=parameters,
                    seed=scene.seed,
                    success=False,
                    error=redact_secrets(exc),
                )
            )
            self._release_comfyui_memory(
                backend_name="krea2_comfyui",
                wait_for_vram=False,
                suppress_errors=True,
            )
            raise
        except BaseException:
            self._release_comfyui_memory(
                backend_name="krea2_comfyui",
                wait_for_vram=False,
                suppress_errors=True,
            )
            raise

    def _check_krea2_vram(self) -> None:
        """Require enough system-wide VRAM before loading the Krea image stack."""
        required = self.config.gpu.minimum_free_vram_gb_for_heavy_job
        try:
            snapshots = self._snapshot_provider()
        except BackendError as exc:
            raise PipelineError(
                "System-wide GPU memory cannot be inspected before the Krea 2 job; "
                "the nvidia-smi diagnostic failed."
            ) from exc
        free = max(snapshot.free_gb for snapshot in snapshots)
        if free < required:
            raise PipelineError(
                f"Free system VRAM {free:.1f} GiB is below the {required:g} GiB required "
                "for Krea 2 Turbo. Release cached ComfyUI models from Models & System Status, "
                "or unload the externally managed LLM in its router UI, then retry."
            )

    def _dispatch_qwen_image_2512(
        self, project: Project, scene: Scene, directory: Path, *, use_cache: bool = True,
    ) -> GenerationResult:
        """Generate a text-capable still with the native Qwen-Image-2512 workflow."""
        width, height = self._qwen_image_scene_canvas(project, scene)
        # Prompt recipe preserved verbatim from the original inline logic
        # (Qwen stays unchanged as the fallback/A-B test generator).
        prompt = build_qwen_prompt(
            scene.visual_prompt.strip(),
            text_in_image=scene.settings.get("on_screen_text", []),
        )
        negative = scene.negative_prompt.strip()
        requested_text = scene_text_literals(scene.settings)
        parameters = {
            "kind": "image",
            "scene_id": scene.id,
            "width": width,
            "height": height,
            "steps": 50,
            "cfg": 4.0,
            "sampler": "euler",
            "scheduler": "simple",
            "model_sampling_shift": 3.1,
            "on_screen_text": requested_text,
        }
        backend_name = "qwen_image_2512_comfyui"
        backend = self.registry.get(backend_name)
        sampler_settings = {
            key: value for key, value in parameters.items() if key != "scene_id"
        }
        cache_key = self._generation_cache_key(backend_name, {
            "kind": "qwen_image_2512",
            "workflow_version": "qwen-image-2512-fp8-v1",
            **self._backend_identity(backend),
            "prompt": prompt,
            "negative_prompt": negative,
            "seed": scene.seed,
            **sampler_settings,
        })
        if cache_key is not None and use_cache:
            cached = self.generation_cache.lookup(backend_name, cache_key)
            if cached is not None:
                staged = directory / ".visual.png.cached"
                shutil.copyfile(cached.path, staged)
                return GenerationResult(
                    outputs=(staged,),
                    metadata=self._cache_hit_metadata(cached, cache_key),
                    peak_vram_gb=0.0,
                )
        reusing_resident = self._prepare_comfy_backend(backend_name)
        if not reusing_resident:
            self._check_qwen_image_vram()
        request = GenerationRequest(
            job_id=f"{project.id}:qwen-image-2512:{scene.seed}",
            output_dir=directory,
            prompt=prompt,
            negative_prompt=negative,
            seed=scene.seed,
            width=width,
            height=height,
            settings={
                "kind": "image",
                "workflow": self.QWEN_IMAGE_2512_WORKFLOW,
                "workflow_version": "qwen-image-2512-fp8-v1",
            },
        )
        try:
            backend.load()
            result = backend.generate(request)
            self._resident_comfy_backend = backend_name
            metadata = {**dict(result.metadata), "settings": parameters}
            output = result.outputs[0] if result.outputs else None
            self._store_generation_result(backend_name, cache_key, output, metadata)
            return GenerationResult(
                outputs=result.outputs,
                metadata=metadata,
                peak_vram_gb=result.peak_vram_gb,
            )
        except Exception as exc:
            descriptor = backend.descriptor()
            self.database.save_attempt(GenerationAttempt(
                backend=descriptor.backend_name,
                model=descriptor.model_name,
                model_version=descriptor.model_version,
                quantization=descriptor.quantization,
                workflow_version="qwen-image-2512-fp8-v1",
                parameters=parameters,
                seed=scene.seed,
                success=False,
                error=redact_secrets(exc),
            ))
            self._release_comfyui_memory(
                backend_name=backend_name, wait_for_vram=False, suppress_errors=True,
            )
            raise
        except BaseException:
            self._release_comfyui_memory(
                backend_name=backend_name, wait_for_vram=False, suppress_errors=True,
            )
            raise

    def _check_qwen_image_vram(self) -> None:
        required = self.config.gpu.minimum_free_vram_gb_for_heavy_job
        try:
            snapshots = self._snapshot_provider()
        except BackendError as exc:
            raise PipelineError(
                "System-wide GPU memory cannot be inspected before the Qwen-Image-2512 job."
            ) from exc
        free = max(snapshot.free_gb for snapshot in snapshots)
        if free < required:
            raise PipelineError(
                f"Free system VRAM {free:.1f} GiB is below the {required:g} GiB required for "
                "Qwen-Image-2512. Release cached ComfyUI models or unload the externally "
                "managed LLM in its router UI, then retry."
            )

    def build_ideogram_prompt(
        self,
        prompt: str | Mapping[str, Any] | None,
        *,
        mode: str = "quick",
        aspect_ratio: str | None = None,
        precise_json: str | Mapping[str, Any] | None = None,
        text_literals: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Build the canonical caption used by every Ideogram generation path.

        Keeping local-LLM selection here prevents scene, comparison, thumbnail,
        and future Ideogram callers from accidentally bypassing Magic Prompt.
        Mock mode and an unavailable local LLM use the prompt builder's
        deterministic, schema-valid fallback.
        """

        return build_ideogram_v4_prompt(
            prompt,
            mode=mode,  # type: ignore[arg-type]
            aspect_ratio=aspect_ratio,
            precise_json=precise_json,
            llm=None if self.mock_mode else self.director.llm,
            text_literals=text_literals,
        )

    def _dispatch_ideogram4(
        self, project: Project, scene: Scene, directory: Path, *, use_cache: bool = True,
    ) -> GenerationResult:
        """Render a scene still with local Ideogram 4 via its ComfyUI workflow.

        Quick mode expands the natural-language scene prompt with the official
        open-source Magic Prompt instructions through the configured local LLM.
        Precise mode validates a user-authored native/KJNodes caption. Both
        converge on the same canonical JSON immediately before ComfyUI encoding.
        """
        width, height = self._ideogram_scene_canvas(project, scene)
        literals = scene_text_literals(scene.settings, scene.text_in_image)
        prompt_mode = str(scene.settings.get("ideogram_prompt_mode", "quick"))
        precise_json = scene.settings.get("ideogram_prompt_json")
        idea = scene.visual_prompt.strip() or scene.title.strip()
        if project.style.strip():
            idea = f"{idea}\nRequested visual style: {project.style.strip()}."
        prompt_result = self.build_ideogram_prompt(
            idea if prompt_mode == "quick" else None,
            mode=prompt_mode,  # type: ignore[arg-type]
            aspect_ratio=aspect_ratio_from_size(width, height),
            precise_json=precise_json,
            text_literals=literals,
        )
        prompt_json = prompt_result["structured_prompt"]
        serialized_prompt = prompt_result["serialized_prompt"]
        validate_ideogram_prompt_json(prompt_json)
        negative = scene.negative_prompt.strip()
        parameters = {
            "kind": "image",
            "scene_id": scene.id,
            "width": width,
            "height": height,
            "on_screen_text": literals,
            "prompt_mode": prompt_mode,
            "aspect_ratio": aspect_ratio_from_size(width, height),
            "ideogram_prompt_json": prompt_json,
            "protected_text": prompt_result["protected_text"],
            "prompt_warnings": prompt_result["warnings"],
        }
        backend_name = "ideogram4_local_comfyui"
        backend = self.registry.get(backend_name)
        cache_key = self._generation_cache_key(backend_name, {
            "kind": "ideogram4_local",
            "workflow_version": "ideogram4-nf4-v1",
            **self._backend_identity(backend),
            "prompt": serialized_prompt,
            "negative_prompt": negative,
            "seed": scene.seed,
            **{k: v for k, v in parameters.items() if k != "scene_id"},
        })
        if cache_key is not None and use_cache:
            cached = self.generation_cache.lookup(backend_name, cache_key)
            if cached is not None:
                staged = directory / ".visual.png.cached"
                shutil.copyfile(cached.path, staged)
                return GenerationResult(
                    outputs=(staged,),
                    metadata=self._cache_hit_metadata(cached, cache_key),
                    peak_vram_gb=0.0,
                )
        if self.config.backends.ideogram4_local.managed:
            self.ideogram_worker.ensure_running()
        reusing_resident = self._prepare_comfy_backend(backend_name)
        if not reusing_resident:
            self._check_ideogram4_vram()
        request = GenerationRequest(
            job_id=f"{project.id}:ideogram4:{scene.seed}",
            output_dir=directory,
            # The structured payload travels via substitutions; the flat string
            # stays provenance-only so job logs remain readable.
            prompt=str(
                prompt_json.get("high_level_description")
                or prompt_json["compositional_deconstruction"]["background"]
                or "Ideogram 4 structured caption"
            ),
            negative_prompt=negative,
            seed=scene.seed,
            width=width,
            height=height,
            settings={
                "kind": "image",
                "workflow": self.IDEOGRAM4_WORKFLOW,
                "workflow_version": "ideogram4-nf4-v1",
                "substitutions": {"prompt_json": serialized_prompt},
            },
        )
        try:
            backend.load()
            result = backend.generate(request)
            self._resident_comfy_backend = backend_name
            metadata = {**dict(result.metadata), "settings": parameters}
            output = result.outputs[0] if result.outputs else None
            self._store_generation_result(backend_name, cache_key, output, metadata)
            return GenerationResult(
                outputs=result.outputs,
                metadata=metadata,
                peak_vram_gb=result.peak_vram_gb,
            )
        except Exception as exc:
            descriptor = backend.descriptor()
            self.database.save_attempt(GenerationAttempt(
                backend=descriptor.backend_name,
                model=descriptor.model_name,
                model_version=descriptor.model_version,
                quantization=descriptor.quantization,
                workflow_version="ideogram4-nf4-v1",
                parameters=parameters,
                seed=scene.seed,
                success=False,
                error=redact_secrets(exc),
            ))
            self._release_comfyui_memory(
                backend_name=backend_name, wait_for_vram=False, suppress_errors=True,
            )
            raise
        except BaseException:
            self._release_comfyui_memory(
                backend_name=backend_name, wait_for_vram=False, suppress_errors=True,
            )
            raise

    def _check_ideogram4_vram(self) -> None:
        required = self.config.gpu.minimum_free_vram_gb_for_heavy_job
        try:
            snapshots = self._snapshot_provider()
        except BackendError as exc:
            raise PipelineError(
                "System-wide GPU memory cannot be inspected before the Ideogram 4 job."
            ) from exc
        free = max(snapshot.free_gb for snapshot in snapshots)
        if free < required:
            raise PipelineError(
                f"Free system VRAM {free:.1f} GiB is below the {required:g} GiB required for "
                "Ideogram 4. Release cached ComfyUI models or unload the externally managed "
                "LLM in its router UI, then retry."
            )

    @staticmethod
    def _ideogram_scene_canvas(project: Project, scene: Scene) -> tuple[int, int]:
        override = scene.settings.get("ideogram_canvas")
        if override and str(override).strip().lower() != "auto":
            return PipelineService._parse_ideogram_canvas(str(override))
        width, height = project.resolution
        if width > height:
            return 1344, 768
        if height > width:
            return 768, 1344
        return 1024, 1024

    @staticmethod
    def _parse_ideogram_canvas(value: str) -> tuple[int, int]:
        try:
            width_text, height_text = value.lower().split("x", 1)
            width, height = int(width_text.strip()), int(height_text.strip())
        except (ValueError, AttributeError) as exc:
            raise PipelineError(
                f"Ideogram canvas override {value!r} is not a 'WIDTHxHEIGHT' pair."
            ) from exc
        if min(width, height) < 256:
            raise PipelineError("Ideogram canvas must be at least 256 px per side.")
        if width % 16 or height % 16:
            raise PipelineError("Ideogram canvas must be aligned to 16 px.")
        if width * height > 1_800_000:
            raise PipelineError("Ideogram canvas exceeds the safe 1.8-megapixel preset cap.")
        return width, height

    @property
    def resident_comfy_backend(self) -> str | None:
        """ComfyUI model family retained by this Studio process for same-type reuse."""
        return self._resident_comfy_backend

    def release_comfyui_memory(self) -> dict[str, Any]:
        """Safely release ComfyUI models, serialized against generation."""
        with self._lock:
            previous = self._resident_comfy_backend
            self._release_comfyui_memory(
                wait_for_vram=previous is not None,
                suppress_errors=False,
            )
            return {"status": "released", "previous_backend": previous}

    def unload_ideogram4(self) -> dict[str, Any]:
        """Release Ideogram's model cache and stop only our owned worker.

        Ideogram can run in a dedicated, app-managed ComfyUI process.  Asking
        ComfyUI to free models does not terminate that worker, so it may retain
        memory until the process exits.  This explicit action never stops an
        externally owned service: ``IdeogramWorkerSupervisor.stop`` is a no-op
        unless this Studio instance started the process.
        """
        backend_name = "ideogram4_local_comfyui"
        with self._lock:
            previous = self._resident_comfy_backend
            unload_error: BackendError | None = None
            try:
                self.registry.get(backend_name).unload()
            except BackendError as exc:
                # Still stop an owned worker even if its HTTP unload endpoint
                # has already gone away; the action's primary goal is VRAM.
                unload_error = exc
            finally:
                if self._resident_comfy_backend == backend_name:
                    self._resident_comfy_backend = None
                stopped_owned_worker = self.ideogram_worker.stop()
            if unload_error is not None and not stopped_owned_worker:
                raise unload_error
            return {
                "status": "unloaded",
                "previous_backend": previous,
                "stopped_owned_worker": stopped_owned_worker,
            }

    def unload_tts_provider(self, provider: str) -> dict[str, Any]:
        """Release a TTS provider's loaded model and stop only the owned worker.

        Covers both the isolated-worker providers (qwen_tts, step_audio_editx,
        chatterbox, omnivoice, breeze_tts_2) and the ComfyUI workflow adapters
        (fish_s2_pro, voxcpm2, index_tts_2_5). For isolated workers, ``unload``
        asks the worker process to drop its loaded weights; for ComfyUI-backed
        providers, it posts to ComfyUI's ``/free`` to release cached models and
        allocator memory. When this Studio owns the isolated worker process it
        is also stopped, mirroring the Ideogram4 unload path.
        """
        if provider not in self._TTS_PROVIDER_NAMES:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"unknown TTS provider: {provider!r}",
            )
        try:
            backend = self.registry.get(provider)
        except KeyError as exc:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                f"{provider} is not registered.",
            ) from exc
        unload_error: BackendError | None = None
        try:
            backend.unload()
        except BackendError as exc:
            unload_error = exc
        stopped_owned_worker = False
        if provider in self._TTS_ISOLATED_WORKER_NAMES:
            try:
                stopped_owned_worker = bool(self.tts_workers.stop(provider))
            except BackendError as exc:
                if unload_error is None:
                    unload_error = exc
        if unload_error is not None and not stopped_owned_worker:
            raise unload_error
        return {
            "provider": provider,
            "status": "unloaded",
            "stopped_owned_worker": stopped_owned_worker,
        }

    def _prepare_comfy_backend(self, backend_name: str) -> bool:
        """Reuse a matching resident model, or unload before changing families."""
        if self._resident_comfy_backend == backend_name:
            return True
        if self._resident_comfy_backend is not None:
            self._release_comfyui_memory(wait_for_vram=True, suppress_errors=False)
            return False
        # Ideogram runs in a dedicated ComfyUI process and its custom loader
        # owns a cache outside ComfyUI's normal model manager. Reconcile our
        # process-local marker after cancellation, reload, or other state drift
        # before applying the cold-load VRAM gate.
        backend = self.registry.get(backend_name)
        resident_probe = getattr(backend, "has_resident_pipeline", None)
        if callable(resident_probe) and resident_probe():
            self._resident_comfy_backend = backend_name
            return True
        return False

    def h3_vram_readiness(self) -> dict[str, Any]:
        required = self.config.gpu.minimum_free_vram_gb_for_heavy_job
        snapshots: tuple[GPUSnapshot, ...] = ()
        free_gb: float | None = None
        total_gb: float | None = None
        error_detail: str | None = None
        try:
            snapshots = self._snapshot_provider()
            free_gb = max(s.free_gb for s in snapshots)
            total_gb = max(s.total_gb for s in snapshots)
        except BackendError as exc:
            error_detail = redact_secrets(exc)
        cold_load_required = self._resident_comfy_backend != "comfyui"
        return {
            "free_gib": free_gb,
            "total_gib": total_gb,
            "threshold_gib": required,
            "resident_comfy_family": self._resident_comfy_backend,
            "cold_load_required": cold_load_required,
            "must_free_vram": (
                cold_load_required and free_gb is not None and free_gb < required
            ),
            "error": error_detail,
        }

    def _release_comfyui_memory(
        self,
        *,
        backend_name: str | None = None,
        wait_for_vram: bool,
        suppress_errors: bool,
    ) -> None:
        required = self.config.gpu.minimum_free_vram_gb_for_heavy_job
        target = backend_name or self._resident_comfy_backend or "comfyui"
        try:
            self.registry.get(target).unload()
        except BackendError:
            if not suppress_errors:
                raise
        finally:
            self._resident_comfy_backend = None
        if not wait_for_vram:
            return
        deadline = time.monotonic() + 10.0
        while True:
            try:
                free = max(snapshot.free_gb for snapshot in self._snapshot_provider())
            except BackendError:
                return
            if free >= required or time.monotonic() >= deadline:
                return
            time.sleep(0.25)

    @staticmethod
    def _krea2_scene_canvas(project: Project, scene: Scene) -> tuple[int, int]:
        override = scene.settings.get("krea_canvas")
        if override and str(override).strip().lower() != "auto":
            return PipelineService._parse_krea2_canvas(str(override))
        width, height = project.resolution
        if width > height:
            return 1344, 768
        if height > width:
            return 768, 1344
        return 1024, 1024

    @staticmethod
    def _qwen_image_scene_canvas(project: Project, scene: Scene) -> tuple[int, int]:
        override = scene.settings.get("qwen_image_canvas")
        if override and str(override).strip().lower() != "auto":
            return PipelineService._parse_qwen_image_canvas(str(override))
        width, height = project.resolution
        if width > height:
            return 1664, 928
        if height > width:
            return 928, 1664
        return 1328, 1328

    @staticmethod
    def _image_motion_source(scene: Scene) -> str:
        source = str(scene.settings.get("image_motion_source", "krea2"))
        if source not in {"krea2", "qwen_image_2512"}:
            raise PipelineError(
                f"Unsupported Image Motion source {source!r}; choose Krea 2 or Qwen-Image-2512."
            )
        return source

    @staticmethod
    def _parse_qwen_image_canvas(value: str) -> tuple[int, int]:
        try:
            width_text, height_text = value.lower().split("x", 1)
            width, height = int(width_text.strip()), int(height_text.strip())
        except (ValueError, AttributeError) as exc:
            raise PipelineError(
                f"Qwen Image canvas override {value!r} is not a 'WIDTHxHEIGHT' pair."
            ) from exc
        if min(width, height) < 256:
            raise PipelineError("Qwen Image canvas must be at least 256 px per side.")
        if width % 16 or height % 16:
            raise PipelineError("Qwen Image canvas must be aligned to 16 px.")
        if width * height > 1_800_000:
            raise PipelineError("Qwen Image canvas exceeds the safe 1.8-megapixel preset cap.")
        return width, height

    @staticmethod
    def _parse_krea2_canvas(value: str) -> tuple[int, int]:
        try:
            width_text, height_text = value.lower().split("x", 1)
            width, height = int(width_text.strip()), int(height_text.strip())
        except (ValueError, AttributeError) as exc:
            raise PipelineError(
                f"Krea 2 canvas override {value!r} is not a 'WIDTHxHEIGHT' pair."
            ) from exc
        if min(width, height) < 256:
            raise PipelineError(
                f"Krea 2 canvas override {value!r} must be at least 256 px per side."
            )
        if width % 16 or height % 16:
            raise PipelineError(
                f"Krea 2 canvas override {value!r} must be aligned to 16 px."
            )
        if width * height > 1024 * 1024:
            raise PipelineError(
                f"Krea 2 canvas override {value!r} exceeds the safe one-megapixel preset cap."
            )
        return width, height

    def _dispatch_h3(
        self, project: Project, scene: Scene, directory: Path, *, use_cache: bool = True,
    ) -> GenerationResult:
        """Validate preset policy, resolve continuity, and submit the real ComfyUI H3 workflow."""
        resolution = resolve_quality(scene.settings, project.resolution)
        validate_duration(resolution, scene.duration)
        frames = h3_frame_count(scene.duration)
        block, references, continuity_meta = self._prepare_h3_continuity(project, scene, resolution)

        workflow_path = self.H3_FIRST_FRAME_WORKFLOW if references else self.H3_WORKFLOW
        workflow_version = (
            CONTINUATION_WORKFLOW_VERSION if references else FIRST_SHOT_WORKFLOW_VERSION
        )
        provenance = {
            "h3_quality": resolution.quality,
            "h3_canvas": f"{resolution.canvas[0]}x{resolution.canvas[1]}",
            "resolution_canvas": list(resolution.canvas),
            "h3_long_shot": resolution.long_shot,
            "requested_seconds": scene.duration,
            "effective_frames": frames,
            "effective_seconds": h3_effective_duration(frames),
            "workflow_version": workflow_version,
            "preset_label": resolution.label,
            "preset_max_seconds": resolution.max_seconds,
            "long_shot_allowed": resolution.preset.long_shot_allowed,
        }
        if references:
            provenance["h3_continuity"] = {
                "enabled": True,
                "group": continuity_meta.get("group", block.group),
                "predecessor_scene_id": block.predecessor_scene_id,
                "predecessor_asset_id": continuity_meta.get("predecessor_asset_id"),
                "predecessor_video_sha256": continuity_meta.get("predecessor_video_sha256"),
                "keyframe_path": continuity_meta.get("keyframe_path"),
                "keyframe_sha256": continuity_meta.get("keyframe_sha256"),
            }

        backend = self.registry.get("comfyui")
        cache_key: str | None = None
        if use_cache:
            reference_hashes = self._reference_hashes(references)
            if reference_hashes is not None:
                cache_key = self._generation_cache_key("comfyui", {
                    "kind": "h3_video",
                    "workflow_version": workflow_version,
                    **self._backend_identity(backend),
                    "prompt": scene.visual_prompt,
                    "negative_prompt": scene.negative_prompt,
                    "seed": scene.seed,
                    "requested_seconds": scene.duration,
                    "frames": frames,
                    "canvas": list(resolution.canvas),
                    "preset": resolution.quality,
                    "long_shot": resolution.long_shot,
                    "references": reference_hashes,
                })
                cached = (
                    self.generation_cache.lookup("comfyui", cache_key)
                    if cache_key is not None else None
                )
                if cached is not None:
                    pending = directory / ".video.mp4.pending"
                    shutil.copyfile(cached.path, pending)
                    return GenerationResult(
                        outputs=(pending,),
                        metadata=self._cache_hit_metadata(cached, cache_key),
                        peak_vram_gb=0.0,
                    )

        reusing_resident = self._prepare_comfy_backend("comfyui")
        if not reusing_resident:
            self._check_h3_vram()
        request = GenerationRequest(
            job_id=f"{project.id}:h3:{scene.seed}",
            output_dir=directory,
            prompt=scene.visual_prompt,
            negative_prompt=scene.negative_prompt,
            seed=scene.seed,
            duration_seconds=scene.duration,
            width=resolution.canvas[0],
            height=resolution.canvas[1],
            references=references,
            settings={
                "kind": "video",
                "workflow": workflow_path,
                "substitutions": {"length": frames},
                "workflow_version": workflow_version,
                "preset": resolution.quality,
                "long_shot": resolution.long_shot,
                **({"h3_continuity": provenance["h3_continuity"]} if references else {}),
            },
        )
        try:
            backend.load()
            result = backend.generate(request)
            self._resident_comfy_backend = "comfyui"
            metadata = {
                **dict(result.metadata),
                "settings": {**dict(result.metadata.get("settings", {})), **provenance},
            }
            output = result.outputs[0] if result.outputs else None
            self._store_generation_result("comfyui", cache_key, output, metadata)
            return GenerationResult(
                outputs=result.outputs,
                metadata=metadata,
                peak_vram_gb=result.peak_vram_gb,
            )
        except Exception as exc:
            descriptor = backend.descriptor()
            self.database.save_attempt(
                GenerationAttempt(
                    backend=descriptor.backend_name,
                    model=descriptor.model_name,
                    model_version=descriptor.model_version,
                    quantization=descriptor.quantization,
                    workflow_version=workflow_version,
                    parameters={
                        "kind": "video",
                        "scene_id": scene.id,
                        "preset": resolution.quality,
                        "effective_frames": frames,
                        "resolution": resolution.canvas,
                        **({"h3_continuity": provenance.get("h3_continuity", {})} if references else {}),
                    },
                    seed=scene.seed,
                    success=False,
                    error=redact_secrets(exc),
                )
            )
            self._release_comfyui_memory(
                backend_name="comfyui",
                wait_for_vram=False,
                suppress_errors=True,
            )
            raise
        except BaseException:
            self._release_comfyui_memory(
                backend_name="comfyui",
                wait_for_vram=False,
                suppress_errors=True,
            )
            raise

    def _prepare_h3_continuity(
        self, project: Project, scene: Scene, resolution: H3Resolution
    ) -> tuple[H3ContinuityBlock | None, tuple[Path, ...], dict]:
        block = parse_continuity(scene.settings)
        validate_continuity_graph(scene, self.database.list_scenes(project.id))
        references: list[Path] = []
        continuity_meta: dict = {}
        if block and block.enabled and block.predecessor_scene_id:
            pred_id = block.predecessor_scene_id
            pred_scene = self.database.get_scene(pred_id)
            if pred_scene is None or pred_scene.project_id != project.id:
                raise PipelineError(
                    f"H3 continuity predecessor scene {pred_id!r} is not in this project."
                )
            pred_assets = [
                a for a in self.database.list_assets(project.id, pred_scene.id)
                if self._is_current_visual_asset(pred_scene, a)
            ]
            if not pred_assets:
                raise PipelineError(
                    f"Predecessor scene {pred_scene.index + 1} has no current visual asset for continuity."
                )
            pred_asset = pred_assets[-1]
            pred_path = self.store.project_path(project) / pred_asset.filepath
            if not pred_path.is_file() or pred_path.stat().st_size == 0:
                raise PipelineError(
                    f"Predecessor video file is missing: {pred_path}"
                )
            pred_hash = compute_sha256(pred_path)
            pred_resolution = resolve_quality(pred_scene.settings, project.resolution)
            if pred_resolution.canvas != resolution.canvas:
                raise PipelineError(
                    f"Continuity canvas mismatch: predecessor {pred_scene.index + 1} uses "
                    f"{pred_resolution.canvas[0]}x{pred_resolution.canvas[1]}, this scene uses "
                    f"{resolution.canvas[0]}x{resolution.canvas[1]}. Align presets or disable continuity."
                )
            keyframe_dir = self._scene_dir(project, scene) / "continuity"
            keyframe_dir.mkdir(parents=True, exist_ok=True)
            keyframe_path = keyframe_dir / "first-frame.png"
            manifest_path = keyframe_dir / "first-frame.json"
            keyframe_meta = None
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    manifest = None
                if (
                    manifest
                    and manifest.get("predecessor_asset_id") == pred_asset.id
                    and manifest.get("predecessor_video_sha256") == pred_hash
                    and manifest.get("canvas") == list(resolution.canvas)
                    and manifest.get("extractor_version") == LAST_FRAME_EXTRACTOR_VERSION
                ):
                    try:
                        cached_info = probe_media(keyframe_path, self.renderer.binaries)
                        cached_hash = compute_sha256(keyframe_path)
                    except Exception:
                        cached_info = None
                        cached_hash = None
                    if (
                        cached_info is not None
                        and cached_info.has_video
                        and (cached_info.width, cached_info.height) == resolution.canvas
                        and cached_hash == manifest.get("keyframe_sha256")
                    ):
                        keyframe_meta = manifest
            if keyframe_meta is None:
                extract_last_frame(
                    pred_path,
                    keyframe_path,
                    timeout=60.0,
                )
                info = probe_media(keyframe_path, self.renderer.binaries)
                if (
                    info.width is not None
                    and info.height is not None
                    and (info.width, info.height) != resolution.canvas
                ):
                    raise PipelineError(
                        f"Extracted keyframe {info.width}x{info.height} does not match requested "
                        f"H3 canvas {resolution.canvas[0]}x{resolution.canvas[1]}."
                    )
                actual_frame_hash = compute_sha256(keyframe_path)
                keyframe_meta = {
                    "predecessor_asset_id": pred_asset.id,
                    "predecessor_video_sha256": pred_hash,
                    "canvas": list(resolution.canvas),
                    "extractor_version": LAST_FRAME_EXTRACTOR_VERSION,
                    "keyframe_sha256": actual_frame_hash,
                    "extracted_at": utc_now().isoformat(),
                }
                self._atomic_json(manifest_path, keyframe_meta)
            # Ensure hash is available whether manifest was reused or newly extracted
            actual_frame_hash = keyframe_meta.get("keyframe_sha256") if isinstance(keyframe_meta, dict) else None
            if actual_frame_hash is None:
                actual_frame_hash = compute_sha256(keyframe_path)
            references = [keyframe_path]
            block = H3ContinuityBlock(
                enabled=True,
                group=block.group or f"h3-chain-{pred_scene.index + 1:03d}",
                predecessor_scene_id=pred_id,
            )
            continuity_meta = {
                "group": block.group,
                "predecessor_asset_id": pred_asset.id,
                "predecessor_video_sha256": pred_hash,
                "keyframe_path": str(keyframe_path.relative_to(self.store.project_path(project))),
                "keyframe_sha256": actual_frame_hash,
            }
        return block, tuple(references), continuity_meta

    def _check_h3_vram(self) -> None:
        """System-wide free-VRAM gate before a heavy H3 job.

        The llama.cpp router on 127.0.0.1:1234 may hold most of the VRAM. Local
        Video Studio never stops it, so a shortfall is a retryable structured error.
        """
        required = self.config.gpu.minimum_free_vram_gb_for_heavy_job
        try:
            snapshots = self._snapshot_provider()
        except BackendError as exc:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "System-wide GPU memory cannot be inspected before the H3 job; the nvidia-smi diagnostic failed.",
                retryable=True,
                details=exc.details,
            ) from exc
        free = max(snapshot.free_gb for snapshot in snapshots)
        if free < required:
            raise BackendError(
                BackendErrorCode.INSUFFICIENT_VRAM,
                (
                    f"Free system VRAM {free:.1f} GiB is below the {required:g} GiB required "
                    "for MiniMax H3. Release cached ComfyUI models from Models & System Status, "
                    "or unload the externally managed LLM in its router UI, then retry."
                ),
                retryable=True,
                details={
                    "free_gib": free,
                    "required_gib": required,
                    "actions": [
                        "release cached ComfyUI models from Models & System Status",
                        "unload the externally managed LLM in its router UI",
                        "retry after VRAM is free",
                    ],
                },
            )

    @staticmethod
    def _h3_canvas(width: int, height: int) -> tuple[int, int]:
        """768-short-edge H3 canvas with the 768x1344 area cap, rounded to 32 px."""
        base = 768
        cap = base * 1344
        if width >= height:
            nominal_w, nominal_h = base * width / height, base
        else:
            nominal_w, nominal_h = base, base * height / width
        if nominal_w * nominal_h > cap:
            scale = (cap / (nominal_w * nominal_h)) ** 0.5
            nominal_w, nominal_h = nominal_w * scale, nominal_h * scale
        return max(32, round(nominal_w / 32) * 32), max(32, round(nominal_h / 32) * 32)

    @staticmethod
    def _h3_scene_canvas(project: Project, scene: Scene) -> tuple[int, int]:
        """Resolve the H3 canvas using the preset-aware policy.

        ``settings.h3_canvas`` still wins when it is not ``"auto"``; the new
        ``settings.h3_quality`` selects the preset when no explicit override is set.
        """
        from backend.core.h3_policy import resolve_quality
        resolution = resolve_quality(scene.settings, project.resolution)
        return resolution.canvas

    @staticmethod
    def _parse_h3_canvas(value: str) -> tuple[int, int]:
        """Parse a scene canvas override like ``"1152x640"`` into (width, height)."""
        try:
            width_text, height_text = value.lower().split("x", 1)
            width, height = int(width_text.strip()), int(height_text.strip())
        except (ValueError, AttributeError) as exc:
            raise PipelineError(
                f"H3 canvas override {value!r} is not a 'WIDTHxHEIGHT' pair."
            ) from exc
        if min(width, height) < 256:
            raise PipelineError(
                f"H3 canvas override {value!r} must be at least 256 px per side."
            )
        if width % 32 or height % 32:
            raise PipelineError(
                f"H3 canvas override {value!r} must be aligned to 32 px."
            )
        return width, height

    @staticmethod
    def _h3_frame_count(seconds: float) -> int:
        """Snap a duration to the model's 17k+5 frame grid at 24 fps."""
        from backend.core.h3_policy import h3_frame_count as policy_frame_count
        return policy_frame_count(seconds)

    def _ensure_music(
        self,
        project: Project,
        *,
        force: bool,
        regenerate_movement: int | None = None,
    ) -> Path | None:
        output = self.store.project_path(project) / "music" / "background.wav"
        music_settings = (project.settings or {}).get("music", {})

        if self.mock_mode:
            if not force and self._stage_complete(project, "music"):
                return output if output.is_file() else None
            def operation() -> tuple[Path, list[Path]]:
                self._archive_output(project, output)
                music_root = output.parent
                total_duration = self._effective_music_duration(project)
                plans = self._movement_plans(project, total_duration)
                movement_payload = [
                    {
                        "index": plan.index,
                        "start": plan.start_seconds,
                        "duration": plan.duration_seconds,
                        "energy": plan.energy,
                        "mood": plan.mood,
                        "scenes": list(plan.scene_indices),
                    }
                    for plan in plans
                ]
                result = self._mock_generate(
                    project,
                    "music",
                    project_dir=music_root,
                    prompt=f"instrumental {project.style} background, restrained, no vocals",
                    duration=total_duration,
                    seed=MUSIC_SEED_BASE,
                    extra_settings={
                        "movements": movement_payload,
                        "bpm": music_settings.get("bpm", 90),
                        "key_scale": music_settings.get("key_scale", "C major"),
                        "time_signature": str(music_settings.get("time_signature", "4")),
                    },
                )
                if result.outputs[0] != output:
                    os.replace(result.outputs[0], output)
                movement_files = sorted((music_root / "movements").glob("movement-*.wav"))
                entries: list[dict[str, Any]] = []
                for index, movement_path in enumerate(movement_files):
                    plan = plans[min(index, len(plans) - 1)]
                    seed = MUSIC_SEED_BASE + 7919 * index
                    movement_result = GenerationResult(
                        outputs=(movement_path,),
                        metadata={
                            **result.metadata,
                            "seed": seed,
                            "settings": {
                                **dict(result.metadata.get("settings", {})),
                                "movement_index": index,
                            },
                        },
                        peak_vram_gb=0,
                    )
                    asset = self._record_asset(
                        project,
                        None,
                        movement_path,
                        AssetType.MUSIC,
                        movement_result,
                        role="music_movement",
                        extra_settings={
                            "movement_index": index,
                            "start_seconds": round(plan.start_seconds, 3),
                            "duration_seconds": round(plan.duration_seconds, 3),
                            "mood": plan.mood,
                            "energy": round(plan.energy, 3),
                            "plan_hash": music_plan_hash(plans),
                        },
                    )
                    entries.append(self._movement_manifest_entry(asset, plan, seed))
                manifest_path = music_root / "manifest.json"
                manifest = {
                    "version": 1,
                    "backend": "mock",
                    "model": None,
                    "fingerprint": self._music_fingerprint(project),
                    "plan_hash": music_plan_hash(plans),
                    "total_duration": round(total_duration, 3),
                    "dip_seconds": MOVEMENT_DIP_SECONDS,
                    "movements": entries,
                }
                self._atomic_json(manifest_path, manifest)
                self._record_asset(
                    project,
                    None,
                    output,
                    AssetType.MUSIC,
                    result,
                    role="music",
                    extra_settings={
                        "fingerprint": self._music_fingerprint(project),
                        "plan_hash": music_plan_hash(plans),
                        "movement_asset_ids": [entry["asset_id"] for entry in entries],
                    },
                )
                return output, [output, manifest_path, *movement_files]
            return self._execute_stage(project, "music", operation, backend="mock")[0]

        ace_enabled = (
            self.config.backends.ace_step.enabled
            and music_settings.get("backend") == "ace_step_comfyui"
            and music_settings.get("mood") != "none"
        )
        if not ace_enabled:
            return self._execute_stage(
                project,
                "music",
                lambda: (None, []),
                backend="not_configured_optional",
            )[0]

        current_fingerprint = self._music_fingerprint(project)
        stage_complete = self._stage_complete(project, "music")

        if not force and stage_complete and output.is_file():
            stored = self._get_last_music_attempt(project)
            if stored and stored.parameters.get("fingerprint") == current_fingerprint:
                return output
            self._invalidate_stages(project, {"music"})

        # Pre-created so the operation can attribute attempt records to the
        # real stage job; _execute_stage enqueues it.
        stage_job = GenerationJob(
            project_id=project.id, stage="music", backend="ace_step_comfyui"
        )

        def operation() -> tuple[Path, list[Path]]:
            backend = self.registry.get("ace_step_comfyui")
            readiness = backend.readiness()
            # Validate the preset that will actually be submitted — checking
            # turbo here would let an uninstalled SFT model reach ComfyUI,
            # where the prompt fails validation ("Value not in list").
            preset = music_settings.get("model", "xl_turbo")
            preset_key = "sft" if preset == "xl_sft" else "turbo"
            if not readiness.get(preset_key, {}).get("ready"):
                raise PipelineError(
                    f"ACE-Step preset '{preset}' is not installed in ComfyUI. "
                    + self._format_readiness_errors(readiness)
                )

            total_duration = self._effective_music_duration(project)
            plans = self._movement_plans(project, total_duration)
            plan_signature = music_plan_hash(plans)

            duration_range = readiness.get("duration_range", {})
            max_duration = duration_range.get("max")
            if max_duration is not None:
                exceeding = next(
                    (p for p in plans if p.duration_seconds > float(max_duration)), None
                )
                if exceeding is not None:
                    raise PipelineError(
                        f"Music movement {exceeding.index + 1} lasts "
                        f"{exceeding.duration_seconds:.0f}s but the installed ACE maximum is "
                        f"{float(max_duration):.0f}s. Reduce 'Movement length' in the Music settings."
                    )

            vram = self.h3_vram_readiness()
            if vram.get("must_free_vram"):
                raise PipelineError(
                    f"Free system VRAM {vram['free_gib']:.1f} GiB is below the "
                    f"{vram['threshold_gib']} GiB required for ACE-Step XL. "
                    "Release cached ComfyUI models or unload the externally managed LLM, then retry."
                )

            manifest_path = output.parent / "manifest.json"
            if regenerate_movement is not None:
                stored_manifest = self._load_music_manifest(manifest_path)
                if (
                    stored_manifest is None
                    or stored_manifest.get("fingerprint") != current_fingerprint
                    or stored_manifest.get("plan_hash") != plan_signature
                ):
                    raise PipelineError(
                        "Cannot regenerate a single movement because music settings or scene "
                        "timings changed. Regenerate the whole soundtrack first."
                    )

            workflow = backend._load_workflow_for_preset(preset)
            workflow_hash = hashlib.sha256(
                json.dumps(workflow, sort_keys=True).encode()
            ).hexdigest()[:16]
            _, metadata = backend._resolve_workflow_from_info({}, preset)
            output_node_id = str(metadata.get("output_node_id", ""))
            workflow_version = str(metadata.get("workflow_version", f"ace-step-1.5-xl-{preset}-comfy-v1"))
            sampling = metadata.get("sampling", {})
            model_filename = (
                "acestep_v1.5_xl_sft_bf16.safetensors"
                if preset == "xl_sft"
                else "acestep_v1.5_xl_turbo_bf16.safetensors"
            )

            self._prepare_comfy_backend("ace_step_comfyui")

            music_root = output.parent
            movements_dir = music_root / "movements"
            attempts_root = music_root / "attempts"
            project_root = self.store.project_path(project)

            entries: list[dict[str, Any]] = []
            last_peak_vram = 0.0
            try:
                for plan in plans:
                    destination = movements_dir / f"movement-{plan.index + 1:02d}.wav"
                    try:
                        self.jobs.transition(
                            stage_job.id,
                            JobStatus.GENERATING,
                            progress=0.2 + 0.6 * plan.index / max(1, len(plans)),
                        )
                    except Exception:
                        pass

                    # Completed movements are reused so a failed multi-movement
                    # run resumes instead of paying GPU cost again. A forced
                    # full regeneration ignores reuse; a single-movement
                    # regeneration forces exactly that one movement.
                    allow_reuse = not force or regenerate_movement is not None
                    reusable = None
                    targets_one_movement = (
                        regenerate_movement is None or plan.index != regenerate_movement
                    )
                    if allow_reuse and targets_one_movement:
                        reusable = self._find_reusable_movement_asset(
                            project, plan, plan_signature, current_fingerprint
                        )
                    if reusable is not None:
                        entries.append(
                            {
                                "index": plan.index,
                                "file": destination.relative_to(project_root).as_posix(),
                                "start": round(plan.start_seconds, 3),
                                "duration": round(plan.duration_seconds, 3),
                                "mood": plan.mood,
                                "energy": round(plan.energy, 3),
                                "seed": int(reusable.seed),
                                "prompt": reusable.prompt,
                                "asset_id": reusable.id,
                                "reused": True,
                            }
                        )
                        continue

                    generation_duration = self._ace_generation_duration(
                        plan.duration_seconds, readiness
                    )
                    substitutions = self._build_ace_substitutions(
                        project, music_settings, plan, len(plans)
                    )
                    substitutions["duration"] = generation_duration
                    substitutions["plan_hash"] = plan_signature
                    # Capture the prompt before popping so attempt records keep it
                    # (AGENTS.md: persist prompts per attempt).
                    prompt_text = substitutions.pop("prompt")
                    request_job_id = str(uuid.uuid4())
                    attempts_dir = attempts_root / f"{request_job_id}-movement-{plan.index + 1:02d}"
                    attempts_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        request = GenerationRequest(
                            job_id=request_job_id,
                            output_dir=attempts_dir,
                            prompt=prompt_text,
                            seed=substitutions["seed"],
                            duration_seconds=substitutions["duration"],
                            settings={
                                "workflow": workflow,
                                "workflow_metadata": metadata,
                                "substitutions": substitutions,
                                "output_node_id": output_node_id,
                                "output_category": "audio",
                                "model_filename": model_filename,
                                "workflow_version": workflow_version,
                                "fingerprint": current_fingerprint,
                                "sampler": sampling,
                                "ace_model": model_filename,
                                "generate_audio_codes": substitutions["generate_audio_codes"],
                            },
                        )

                        result = backend.generate(request)

                        audio_outputs = [
                            p for p in result.outputs if p.suffix in (".wav", ".flac", ".mp3", ".ogg")
                        ]
                        if not audio_outputs:
                            raise BackendError(
                                BackendErrorCode.INVALID_RESPONSE,
                                "ACE workflow produced no audio output.",
                            )

                        normalized = attempts_dir / "normalized.wav"
                        self._normalize_audio(audio_outputs[0], normalized, plan.duration_seconds)
                        self._archive_output(project, destination)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(normalized, destination)
                        last_peak_vram = max(last_peak_vram, float(result.peak_vram_gb or 0.0))

                        asset = self._record_asset(
                            project,
                            None,
                            destination,
                            AssetType.MUSIC,
                            result,
                            role="music_movement",
                            job_id=stage_job.id,
                            record_attempt=False,
                            extra_settings={
                                "movement_index": plan.index,
                                "start_seconds": round(plan.start_seconds, 3),
                                "duration_seconds": round(plan.duration_seconds, 3),
                                "mood": plan.mood,
                                "energy": round(plan.energy, 3),
                                "plan_hash": plan_signature,
                                "fingerprint": current_fingerprint,
                            },
                        )
                        self.database.save_attempt(
                            GenerationAttempt(
                                asset_id=asset.id,
                                job_id=stage_job.id,
                                backend="ace_step_comfyui",
                                model=model_filename,
                                model_version="ace-step-1.5-xl-comfy-v1",
                                quantization="bf16",
                                workflow_version=workflow_version,
                                parameters={
                                    "prompt": prompt_text,
                                    **substitutions,
                                    "fingerprint": current_fingerprint,
                                    "workflow_hash": workflow_hash,
                                    "sampler": sampling,
                                    "output_node_id": output_node_id,
                                    "comfy_prompt_id": result.metadata.get("prompt_id"),
                                    "request_job_id": request_job_id,
                                    "requested_duration": plan.duration_seconds,
                                    "generated_duration": generation_duration,
                                    "movement_index": plan.index,
                                    "plan_hash": plan_signature,
                                },
                                seed=substitutions["seed"],
                                success=True,
                                peak_vram_gb=result.peak_vram_gb,
                            )
                        )
                        entries.append(
                            {
                                "index": plan.index,
                                "file": destination.relative_to(project_root).as_posix(),
                                "start": round(plan.start_seconds, 3),
                                "duration": round(plan.duration_seconds, 3),
                                "mood": plan.mood,
                                "energy": round(plan.energy, 3),
                                "seed": int(substitutions["seed"]),
                                "prompt": prompt_text,
                                "asset_id": asset.id,
                                "reused": False,
                            }
                        )
                    except BackendError as exc:
                        # Persist failed generations per movement (AGENTS.md);
                        # finished movements remain on disk so a retry only
                        # pays GPU cost for what is missing.
                        self.database.save_attempt(
                            GenerationAttempt(
                                asset_id=None,
                                job_id=stage_job.id,
                                backend="ace_step_comfyui",
                                model=model_filename,
                                model_version="ace-step-1.5-xl-comfy-v1",
                                quantization="bf16",
                                workflow_version=workflow_version,
                                parameters={
                                    "prompt": prompt_text,
                                    **substitutions,
                                    "fingerprint": current_fingerprint,
                                    "workflow_hash": workflow_hash,
                                    "sampler": sampling,
                                    "output_node_id": output_node_id,
                                    "request_job_id": request_job_id,
                                    "movement_index": plan.index,
                                    "plan_hash": plan_signature,
                                    "error": str(exc),
                                },
                                seed=substitutions["seed"],
                                success=False,
                                error=str(exc),
                            )
                        )
                        raise

                movement_files = [project_root / entry["file"] for entry in entries]
                missing = [
                    str(path) for path in movement_files
                    if not path.is_file() or path.stat().st_size == 0
                ]
                if missing:
                    raise PipelineError(f"Music movement audio is missing: {', '.join(missing)}")

                stitched_tmp = music_root / ".background.stitched.wav"
                try:
                    music_stitch.stitch_movements(
                        movement_files, stitched_tmp, dip_seconds=MOVEMENT_DIP_SECONDS
                    )
                    self._archive_output(project, output)
                    os.replace(stitched_tmp, output)
                except Exception:
                    stitched_tmp.unlink(missing_ok=True)
                    raise

                final_result = GenerationResult(
                    outputs=(output,),
                    metadata={
                        "backend": "ace_step_comfyui",
                        "model": model_filename,
                        "model_version": "ace-step-1.5-xl-comfy-v1",
                        "quantization": "bf16",
                        "workflow_version": workflow_version,
                        "seed": (
                            entries[-1]["seed"]
                            if entries else int(music_settings.get("seed", MUSIC_SEED_BASE))
                        ),
                        "prompt": (
                            f"instrumental {project.style} background music, "
                            f"{len(entries)} movement(s)"
                        ),
                        "settings": {},
                    },
                    peak_vram_gb=last_peak_vram,
                )
                final_asset = self._record_asset(
                    project,
                    None,
                    output,
                    AssetType.MUSIC,
                    final_result,
                    role="music",
                    job_id=stage_job.id,
                    record_attempt=False,
                    extra_settings={
                        "fingerprint": current_fingerprint,
                        "plan_hash": plan_signature,
                        "movement_asset_ids": [entry["asset_id"] for entry in entries],
                    },
                )
                self.database.save_attempt(
                    GenerationAttempt(
                        asset_id=final_asset.id,
                        job_id=stage_job.id,
                        backend="ace_step_comfyui",
                        model=model_filename,
                        model_version="ace-step-1.5-xl-comfy-v1",
                        quantization="bf16",
                        workflow_version=workflow_version,
                        parameters={
                            "fingerprint": current_fingerprint,
                            "plan_hash": plan_signature,
                            "movements": [entry["file"] for entry in entries],
                            "dip_seconds": MOVEMENT_DIP_SECONDS,
                            "requested_duration": total_duration,
                        },
                        seed=int(music_settings.get("seed", MUSIC_SEED_BASE)),
                        success=True,
                        peak_vram_gb=last_peak_vram,
                    )
                )
                manifest = {
                    "version": 1,
                    "backend": "ace_step_comfyui",
                    "model": model_filename,
                    "workflow_version": workflow_version,
                    "fingerprint": current_fingerprint,
                    "plan_hash": plan_signature,
                    "total_duration": round(total_duration, 3),
                    "dip_seconds": MOVEMENT_DIP_SECONDS,
                    "movements": entries,
                }
                self._atomic_json(manifest_path, manifest)
                return output, [output, manifest_path, *movement_files]
            finally:
                self._release_comfyui_memory(
                    backend_name="ace_step_comfyui",
                    wait_for_vram=False,
                    suppress_errors=True,
                )
                self._resident_comfy_backend = None

        # GPU-heavy ACE-Step section (readiness, ComfyUI generate, audio
        # normalization) is serialized against other GPU work via _gpu_lock.
        with self._gpu_lock:
            return self._execute_stage(
                project, "music", operation, backend="ace_step_comfyui", job=stage_job
            )[0]

    @staticmethod
    def _incremental_hash(path: Path, *, block_size: int = 1024 * 1024) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(block_size)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _music_fingerprint(self, project: Project) -> str:
        music = (project.settings or {}).get("music", {})
        total_duration = self._effective_music_duration(project)
        plans = self._movement_plans(project, total_duration)
        components = (
            music.get("backend", ""),
            music.get("model", "xl_turbo"),
            music.get("seed", 30001),
            music.get("bpm", 90),
            music.get("key_scale", "C major"),
            music.get("time_signature", "4"),
            music.get("language", "en"),
            project.style,
            music.get("mood", ""),
            music.get("instrumental", True),
            music.get("generate_audio_codes", True),
            project.target_duration,
            # Movement boundaries/energies follow scene moods, so plan edits
            # and scene re-planning must invalidate the soundtrack too.
            music_plan_hash(plans),
            float(music.get("movement_seconds", 60) or 60),
        )
        return hashlib.sha256(json.dumps(components).encode()).hexdigest()[:16]

    @staticmethod
    def _movement_energy_words(energy: float) -> str:
        if energy < 0.4:
            return "sparse and gentle"
        if energy < 0.65:
            return "flowing and curious"
        return "driving and full"

    def _build_ace_substitutions(
        self,
        project: Project,
        music_settings: dict[str, Any],
        movement: MovementPlan | None = None,
        movement_count: int | None = None,
    ) -> dict[str, Any]:
        instrumental = music_settings.get("instrumental", True)
        base_seed = int(music_settings.get("seed", MUSIC_SEED_BASE))
        prompt_parts = [music_settings.get("style", project.style)]
        mood = music_settings.get("mood", "")
        if mood:
            prompt_parts.append(mood)
        if movement is not None:
            seed = base_seed + 1000 * (movement.index + 1)
            if movement.mood:
                prompt_parts.append(movement.mood)
            total = movement_count if movement_count is not None else movement.index + 1
            prompt_parts.append(
                f"instrumental background music section {movement.index + 1} of {total}, "
                f"about {movement.duration_seconds:.0f} seconds, "
                f"{self._movement_energy_words(movement.energy)}"
            )
        else:
            scenes = self.database.list_scenes(project.id)
            scene_moods = [s.music_mood for s in scenes if s.music_mood]
            if scene_moods:
                prompt_parts.extend(scene_moods)
            prompt_parts.append(
                "instrumental background music, restrained, no vocals, no spoken words, no chanting, no vocalizations"
            )
            prompt_parts.append(f"suitable for {project.target_duration:.0f} seconds")
            seed = base_seed
        if movement is None or not any("no vocals" in part for part in prompt_parts):
            prompt_parts.append("no vocals, no spoken words, no chanting, no vocalizations")
        preset = music_settings.get("model", "xl_turbo")
        if preset == "xl_sft":
            model_filename = "acestep_v1.5_xl_sft_bf16.safetensors"
        else:
            model_filename = "acestep_v1.5_xl_turbo_bf16.safetensors"

        duration = (
            movement.duration_seconds
            if movement is not None
            else project.target_duration
        )
        return {
            "prompt": ", ".join(prompt_parts),
            "lyrics": "" if instrumental else music_settings.get("lyrics", ""),
            "seed": seed,
            "duration": duration,
            "bpm": int(music_settings.get("bpm", 90)),
            "time_signature": str(music_settings.get("time_signature", "4")),
            "language": str(music_settings.get("language", "en")),
            "key_scale": str(music_settings.get("key_scale", "C major")),
            "generate_audio_codes": bool(music_settings.get("generate_audio_codes", music_settings.get("thinking", True))),
            "model_filename": model_filename,
            "filename_prefix": "local-video-studio/ace-step-music",
        }

    def _format_readiness_errors(self, readiness: dict[str, Any]) -> str:
        parts = []
        for preset in ("turbo", "sft"):
            preset_data = readiness.get(preset, {})
            missing_nodes = preset_data.get("missing_nodes", [])
            missing_files = preset_data.get("missing_files", [])
            if missing_nodes or missing_files:
                parts.append(f"{preset}: missing nodes {missing_nodes}, missing files {missing_files}")
        return " ".join(parts) if parts else "check ComfyUI readiness for details."

    @staticmethod
    def _ace_generation_duration(target_duration: float, readiness: dict[str, Any]) -> float:
        """Apply the installed ACE duration limits before workflow submission."""
        duration_range = readiness.get("duration_range", {})
        min_duration = duration_range.get("min")
        max_duration = duration_range.get("max")
        if max_duration is not None and target_duration > float(max_duration):
            raise PipelineError(
                f"Project duration {target_duration}s exceeds ACE maximum {max_duration}s. "
                "Multi-segment composition is not supported in v1."
            )
        if min_duration is not None:
            return max(target_duration, float(min_duration))
        return target_duration

    def _get_last_music_attempt(self, project: Project) -> Any | None:
        assets = self.database.list_assets(project.id)
        music_assets = [a for a in assets if a.type == AssetType.MUSIC]
        if not music_assets:
            return None
        last_asset = music_assets[-1]
        attempts = self.database.list_attempts(asset_id=last_asset.id)
        successful = [a for a in attempts if a.success]
        return successful[-1] if successful else None

    def _effective_music_duration(self, project: Project) -> float:
        """Total scored length: narration length, else the planned video length."""
        narration_path = self.store.project_path(project) / "narration" / "master.wav"
        if narration_path.is_file():
            try:
                return wav_duration(narration_path)
            except Exception:
                pass
        scenes = self.database.list_scenes(project.id)
        scene_total = sum(scene.duration for scene in scenes)
        return max(project.target_duration, scene_total)

    def _movement_plans(self, project: Project, total_duration: float) -> list[MovementPlan]:
        music_settings = (project.settings or {}).get("music", {})
        scenes = self.database.list_scenes(project.id)
        movement_seconds = max(30.0, float(music_settings.get("movement_seconds", 60) or 60))
        raw_cap = music_settings.get("max_movement_seconds")
        return plan_movements(
            [scene.duration for scene in scenes],
            [scene.music_mood for scene in scenes],
            total_duration,
            movement_seconds=movement_seconds,
            max_movement_seconds=float(raw_cap) if raw_cap else None,
        )

    def _load_music_manifest(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _find_reusable_movement_asset(
        self,
        project: Project,
        plan: MovementPlan,
        plan_signature: str,
        fingerprint: str,
    ) -> Asset | None:
        """Return a still-valid generated movement for this plan slot, if any."""
        for asset in self.database.list_assets(project.id):
            if asset.type is not AssetType.MUSIC:
                continue
            settings = asset.settings or {}
            if settings.get("role") != "music_movement":
                continue
            if settings.get("movement_index") != plan.index:
                continue
            if settings.get("plan_hash") != plan_signature:
                continue
            if settings.get("fingerprint") != fingerprint:
                continue
            path = self.store.project_path(project) / asset.filepath
            if not path.is_file() or path.stat().st_size == 0:
                continue
            if compute_sha256(path) != asset.hash:
                continue
            return asset
        return None

    def _movement_manifest_entry(
        self,
        asset: Asset,
        plan: MovementPlan,
        seed: int,
    ) -> dict[str, Any]:
        return {
            "index": plan.index,
            "file": Path(asset.filepath).as_posix(),
            "start": round(plan.start_seconds, 3),
            "duration": round(plan.duration_seconds, 3),
            "mood": plan.mood,
            "energy": round(plan.energy, 3),
            "seed": int(seed),
            "prompt": asset.prompt,
            "asset_id": asset.id,
        }

    def _normalize_audio(self, source: Path, destination: Path, target_duration: float) -> None:
        source_info = probe_media(source)
        source_duration = source_info.duration_seconds or 0.0
        ffmpeg = require_ffmpeg()
        argv = [
            str(ffmpeg),
            "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-ar", "44100",
            "-ac", "2",
            "-c:a", "pcm_s16le",
        ]
        if target_duration > 0:
            if source_duration < target_duration:
                pad = target_duration - source_duration
                argv.extend(["-af", f"apad=pad_dur={pad:.3f}"])
            argv.extend(["-t", str(target_duration)])
        argv.extend([str(destination)])
        run_media_process(argv, timeout=max(60.0, target_duration * 5))

    def queue_music_generation(
        self,
        project_id: str,
        *,
        force: bool = False,
        movement_index: int | None = None,
    ) -> GenerationJob:
        project = self._project(project_id)
        music_settings = (project.settings or {}).get("music", {})
        if not self.config.backends.ace_step.enabled:
            raise BackendError(
                BackendErrorCode.BACKEND_UNAVAILABLE,
                "ACE-Step is not enabled in configuration.",
            )
        if music_settings.get("backend") != "ace_step_comfyui":
            raise BackendError(
                BackendErrorCode.MODEL_SELECTION_REQUIRED,
                "Select ACE-Step ComfyUI as the music backend in project settings.",
            )
        if music_settings.get("mood") == "none":
            raise PipelineError("Project music mood is set to none.")
        backend = self.registry.get("ace_step_comfyui")
        readiness = backend.readiness()
        preset = music_settings.get("model", "xl_turbo")
        preset_key = "sft" if preset == "xl_sft" else "turbo"
        preset_readiness = readiness.get(preset_key, {})
        if not preset_readiness.get("ready"):
            raise PipelineError(
                "ACE-Step preset is not ready. "
                + self._format_readiness_errors(readiness)
            )
        job = GenerationJob(
            project_id=project.id,
            stage="music",
            backend="ace_step_comfyui",
            parameters={
                "settings": music_settings,
                **({"movement_index": movement_index} if movement_index is not None else {}),
            },
        )
        return self.jobs.enqueue(job)

    def run_music_generation(
        self,
        project_id: str,
        *,
        force: bool = False,
        parent_job_id: str | None = None,
        movement_index: int | None = None,
    ) -> None:
        job = self.jobs.get(parent_job_id) if parent_job_id else None
        if job is None:
            return
        with media_process_scope(job.id):
            self.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
            self.jobs.transition(job.id, JobStatus.LOADING_MODEL, progress=0.1)
            self.jobs.transition(job.id, JobStatus.GENERATING, progress=0.3)
            try:
                self._ensure_music(
                    self._project(project_id),
                    force=force,
                    regenerate_movement=movement_index,
                )
                # A cancel that landed during generation wins: keep the row
                # canceled (the produced track is kept) instead of raising an
                # invalid CANCELED -> COMPLETED transition.
                current = self.jobs.get(job.id)
                if current is not None and current.status is not JobStatus.CANCELED:
                    self.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.9)
                    self.jobs.complete(job.id)
            except Exception as exc:
                current = self.jobs.get(job.id)
                if current is not None and current.status not in {
                    JobStatus.FAILED, JobStatus.CANCELED, JobStatus.COMPLETED,
                }:
                    self.jobs.fail(job.id, redact_secrets(exc))
                raise

    def _ensure_subtitles(self, project: Project, *, force: bool) -> list[SubtitleCue]:
        root = self.store.project_path(project) / "subtitles"
        srt = root / "captions.srt"
        ass = root / "captions.ass"
        word_timings = root / "word-timings.json"
        if not force and self._stage_complete(project, "subtitles"):
            return self._subtitle_cues(project)

        def operation() -> tuple[list[SubtitleCue], list[Path]]:
            self._archive_output(project, srt)
            self._archive_output(project, ass)
            self._archive_output(project, word_timings)
            if self.mock_mode:
                cues = self._subtitle_cues(project)
                result = GenerationResult(
                    outputs=(srt, ass),
                    metadata={
                        "backend": "mock",
                        "model": "deterministic-audio-alignment-v1",
                        "model_version": "1",
                        "seed": 0,
                        "settings": {"audio_derived": False, "mock_mode": True},
                    },
                    peak_vram_gb=0,
                )
                outputs = [srt, ass]
            else:
                result = self._align_narration(project, word_timings)
                cues = self._audio_derived_cues(word_timings)
                outputs = [word_timings, srt, ass]
            write_srt(cues, srt)
            write_ass(cues, ass, width=project.resolution[0], height=project.resolution[1])
            for output in (srt, ass):
                self._record_asset(project, None, output, AssetType.SUBTITLE, result, role="captions")
            if not self.mock_mode:
                self._record_asset(
                    project, None, word_timings, AssetType.METADATA, result, role="caption_timing",
                )
            return cues, outputs

        return self._execute_stage(
            project, "subtitles", operation, backend="mock" if self.mock_mode else "whisper",
        )[0]

    def _align_narration(self, project: Project, destination: Path) -> GenerationResult:
        """Write portable word timings from the finalized narration audio."""
        config = self.config.backends.whisper
        if not config.enabled:
            raise PipelineError(
                "Accurate captions require the local Whisper alignment backend. Set "
                "backends.whisper.enabled=true after installing the optional captions dependency "
                "and placing the configured model locally."
            )
        narration = self.store.project_path(project) / "narration" / "master.wav"
        if not narration.is_file():
            raise PipelineError("Cannot align captions because narration/master.wav is missing.")
        backend = self.registry.get("whisper")
        descriptor = backend.descriptor()
        if descriptor.device == "cuda":
            snapshots = self._snapshot_provider()
            free_gb = max((device.free_gb for device in snapshots), default=0.0)
            if free_gb < descriptor.vram_required_gb:
                raise BackendError(
                    BackendErrorCode.INSUFFICIENT_VRAM,
                    "Caption alignment needs more free GPU memory. Local Video Studio did not "
                    "stop the external local LLM server.",
                    retryable=True,
                    details=(
                        f"required={descriptor.vram_required_gb:.2f} GiB, "
                        f"available={free_gb:.2f} GiB"
                    ),
                )
        voice = project.settings.get("voice")
        language = voice.get("language") if isinstance(voice, dict) else None
        request = GenerationRequest(
            job_id=f"{project.id}:caption-alignment",
            output_dir=destination.parent,
            prompt="Align the generated local narration audio with word timestamps.",
            seed=0,
            references=(narration,),
            settings={"language": language},
        )
        # Whisper alignment is GPU work: _gpu_lock serializes it against
        # visuals/music GPU sections (order: _lock -> _gpu_lock).
        with self._lock, self._gpu_lock:
            backend.load()
            try:
                result = backend.generate(request)
            finally:
                backend.unload()
        generated = result.outputs[0]
        if generated != destination:
            os.replace(generated, destination)
        audio_hash = hashlib.sha256(narration.read_bytes()).hexdigest()
        metadata = dict(result.metadata)
        settings = dict(metadata.get("settings", {}))
        settings["input_audio"] = str(narration.relative_to(self.store.project_path(project)))
        settings["input_audio_sha256"] = audio_hash
        metadata["settings"] = settings
        timing_payload = json.loads(destination.read_text(encoding="utf-8"))
        # Captions align against clean authored scene narration. Performance
        # cues live in a separate artifact and never modify these records.
        authored_transcript = " ".join(
            scene.narration for scene in self.database.list_scenes(project.id)
        )
        raw_words = timing_payload.get("words")
        if isinstance(raw_words, list):
            try:
                aligned_words = tuple(
                    CaptionWord(
                        start_seconds=float(item["start_seconds"]),
                        end_seconds=float(item["end_seconds"]),
                        text=str(item["text"]),
                    )
                    for item in raw_words
                )
            except (KeyError, TypeError, ValueError):
                # _audio_derived_cues reports malformed alignment data with the
                # pipeline's existing structured error after metadata is saved.
                pass
            else:
                restored_words = restore_authored_punctuation(
                    aligned_words, authored_transcript,
                )
                timing_payload["words"] = [word.to_dict() for word in restored_words]
                settings["punctuation_source"] = "authored_scene_narration"
                settings["punctuation_workflow_version"] = "authored-punctuation-v1"
        timing_payload["input_audio"] = settings["input_audio"]
        timing_payload["input_audio_sha256"] = audio_hash
        timing_payload["backend"] = metadata.get("backend")
        timing_payload["model"] = metadata.get("model")
        timing_payload["model_version"] = metadata.get("model_version")
        timing_payload["quantization"] = metadata.get("quantization")
        timing_payload["punctuation_source"] = settings.get("punctuation_source")
        timing_payload["punctuation_workflow_version"] = settings.get(
            "punctuation_workflow_version"
        )
        self._atomic_json(destination, timing_payload)
        return GenerationResult(outputs=(destination,), metadata=metadata, peak_vram_gb=result.peak_vram_gb)

    @staticmethod
    def _audio_derived_cues(word_timings: Path) -> list[SubtitleCue]:
        try:
            payload = json.loads(word_timings.read_text(encoding="utf-8"))
            raw_words = payload["words"]
            words = [
                CaptionWord(
                    start_seconds=float(item["start_seconds"]),
                    end_seconds=float(item["end_seconds"]),
                    text=str(item["text"]),
                )
                for item in raw_words
            ]
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineError("Caption alignment produced invalid word timing data.") from exc
        cues = build_caption_cues(words)
        if not cues:
            raise PipelineError("Caption alignment produced no readable subtitle cues.")
        return cues

    def _ensure_timeline(self, project: Project, *, force: bool) -> Timeline:
        destination = self.store.project_path(project) / "timeline.json"
        if not force and self._stage_complete(project, "timeline"):
            return self._build_timeline(project)

        def operation() -> tuple[Timeline, list[Path]]:
            timeline = self._build_timeline(project)
            payload = timeline.to_dict()
            root = self.store.project_path(project)
            for clip in payload["clips"]:
                clip["path"] = str(Path(clip["path"]).relative_to(root))
            for track in payload["audio_tracks"]:
                track["path"] = str(Path(track["path"]).relative_to(root))
            self._atomic_json(destination, payload)
            return timeline, [destination]

        return self._execute_stage(project, "timeline", operation, backend="ffmpeg")[0]

    def _editorial_renderer_instance(self) -> EditorialRenderer:
        """Create the Chromium renderer only when an Editorial export needs it."""
        if self._editorial_renderer is None:
            self._editorial_renderer = EditorialRenderer()
        return self._editorial_renderer

    def _validate_editorial_render_inputs(self, project: Project) -> EditPlan:
        """Validate an existing Edit Plan and narration without generating either."""
        try:
            plan = self.store.load_edit_plan(project.slug)
        except FileNotFoundError as exc:
            raise PipelineError(
                "Cannot render Editorial Mode because no Edit Plan exists. "
                "Generate or save an Edit Plan first."
            ) from exc
        # Compilation is a cheap validation step which rejects templates that
        # the deterministic renderer does not implement before a job is queued.
        try:
            compile_edit_plan_html(plan)
        except ValueError as exc:
            raise PipelineError(f"Cannot render this Editorial Edit Plan: {exc}") from exc
        root = self.store.project_path(project)
        narration = root / "narration" / "master.wav"
        if not narration.is_file():
            raise PipelineError("Narration audio is missing: narration/master.wav")
        try:
            narration_duration = wav_duration(narration)
        except (OSError, EOFError, ValueError, ZeroDivisionError, wave.Error) as exc:
            raise PipelineError("Narration audio is not a valid PCM WAV file.") from exc
        if not math.isfinite(narration_duration) or narration_duration <= 0:
            raise PipelineError("Narration audio must have a positive, finite duration.")
        # Narration remains the master clock. A plan may intentionally leave a
        # short visual tail, but it must never truncate spoken audio.
        frame_tolerance = max(1.0 / plan.fps, 0.05)
        if narration_duration > plan.duration + frame_tolerance:
            raise PipelineError(
                "The Editorial Edit Plan ends before narration finishes. "
                "Regenerate or extend the plan using the current narration timings."
            )
        return plan

    def _ensure_editorial_visual(self, project: Project, *, force: bool) -> Path:
        """Render the deterministic, silent Editorial canvas for common FFmpeg finishing."""
        if project.video_mode is not VideoMode.EDITORIAL:
            raise PipelineError("Editorial visual rendering requires an Editorial Mode project.")
        output = self.store.project_path(project) / "editorial" / "master.mp4"
        if not force and self._stage_complete(project, "editorial_visual"):
            return output

        def operation() -> tuple[Path, list[Path]]:
            plan = self._validate_editorial_render_inputs(project)
            self._archive_output(project, output)
            self._editorial_renderer_instance().render(
                plan,
                output,
                asset_root=self.store.project_path(project),
            )
            return output, [output]

        return self._execute_stage(
            project, "editorial_visual", operation, backend="chromium",
        )[0]

    def _ensure_preview(self, project: Project, *, force: bool) -> Path:
        output = self.store.project_path(project) / "renders" / "preview.mp4"
        if not force and self._stage_complete(project, "render_preview"):
            return output

        def operation() -> tuple[Path, list[Path]]:
            timeline = self._build_timeline(project)
            width, height = self.config.render.preview_resolution
            self._archive_output(project, output)
            info = self.renderer.render_preview(
                timeline,
                output,
                RenderOptions(
                    width=width,
                    height=height,
                    fps=project.fps,
                    burn_subtitles=True,
                    embed_subtitle_track=True,
                ),
            )
            self.database.record_render_metadata(
                project.id, str(output.relative_to(self.store.project_path(project))),
                self._jsonable(asdict(info)), utc_now(),
            )
            self._record_render_asset(project, output, role="preview_render")
            return output, [output]

        return self._execute_stage(project, "render_preview", operation, backend="ffmpeg")[0]

    def _ensure_qc(self, project: Project, *, force: bool) -> QCReport:
        output = self.store.project_path(project) / "renders" / "qc.json"
        if not force and self._stage_complete(project, "quality_control"):
            return self.qc.check_timeline(self._build_timeline(project))

        def operation() -> tuple[QCReport, list[Path]]:
            timeline = self._build_timeline(project)
            report = self.qc.check_timeline(timeline)
            report.extend(
                self.qc.check_file(
                    self.store.project_path(project) / "renders" / "preview.mp4",
                    expected_duration_seconds=timeline.duration_seconds,
                    expected_resolution=self.config.render.preview_resolution,
                    require_audio=True,
                )
            )
            payload = {
                "passed": report.passed,
                "inspected_files": report.inspected_files,
                "issues": [self._jsonable(asdict(issue)) for issue in report.issues],
            }
            self._atomic_json(output, payload)
            errors = [issue for issue in report.issues if issue.severity.value == "error"]
            if errors:
                raise PipelineError(f"quality control failed with {len(errors)} error(s)")
            return report, [output]

        return self._execute_stage(project, "quality_control", operation, backend="ffmpeg")[0]

    def _ensure_final(self, project: Project, *, force: bool) -> Path:
        output = self.store.project_path(project) / "renders" / "final.mp4"
        if not force and self._stage_complete(project, "render_final"):
            return output

        def operation() -> tuple[Path, list[Path]]:
            timeline = self._build_timeline(project)
            self._archive_output(project, output)
            info = self.renderer.render_final(
                timeline,
                output,
                RenderOptions(
                    width=project.resolution[0],
                    height=project.resolution[1],
                    fps=project.fps,
                    burn_subtitles=True,
                    embed_subtitle_track=True,
                ),
            )
            self.database.record_render_metadata(
                project.id, str(output.relative_to(self.store.project_path(project))),
                self._jsonable(asdict(info)), utc_now(),
            )
            self._record_render_asset(project, output, role="final_render")
            return output, [output]

        self._save_project(project.model_copy(update={"status": ProjectStatus.RENDERING, "updated_at": utc_now()}))
        return self._execute_stage(project, "render_final", operation, backend="ffmpeg")[0]

    def _ensure_thumbnails(self, project: Project, *, force: bool) -> list[Path]:
        if not force and self._stage_complete(project, "thumbnails"):
            return self._stage_paths(project, "thumbnails")

        def operation() -> tuple[list[Path], list[Path]]:
            outputs: list[Path] = []
            root = self.store.project_path(project) / "thumbnails"
            for index in range(3):
                directory = root / f"candidate-{index + 1:02d}"
                output = directory / "thumbnail.png"
                self._archive_output(project, output)
                final = self.store.project_path(project) / "renders" / "final.mp4"
                duration = self._build_timeline(project).duration_seconds
                timestamp = duration * (0.2 + index * 0.3)
                self.renderer.extract_frame(
                    final,
                    output,
                    timestamp_seconds=timestamp,
                    width=640,
                    height=360,
                )
                result = GenerationResult(
                    outputs=(output,),
                    metadata={
                        "backend": "ffmpeg",
                        "model": "frame-extraction",
                        "model_version": self.renderer.binaries.source,
                        "workflow_version": "thumbnail-v1",
                        "seed": 40_000 + index,
                        "settings": {"timestamp_seconds": timestamp},
                    },
                    peak_vram_gb=0,
                )
                self._record_asset(project, None, output, AssetType.THUMBNAIL, result, role="thumbnail")
                outputs.append(output)
            return outputs, outputs

        return self._execute_stage(project, "thumbnails", operation, backend="ffmpeg")[0]

    def _ensure_metadata(self, project: Project, *, force: bool) -> Path:
        output = self.store.project_path(project) / "publishing-metadata.json"
        if not force and self._stage_complete(project, "metadata"):
            return output

        def operation() -> tuple[Path, list[Path]]:
            plan = self.store.load_plan(project.slug)
            payload = {
                "titles": [project.title, f"How {project.title} Works", f"{project.title}: Explained"],
                "description": (
                    f"A locally produced {project.style} video about {project.topic}.\n\n"
                    "Generated without cloud inference in Local Video Studio."
                ),
                "tags": [word.lower().strip(".,:;!?()") for word in project.topic.split()[:12]],
                "llm_model": project.selected_llm_model,
                "director": DirectorEngine.metadata_prompt(project, plan),
            }
            self._atomic_json(output, payload)
            return output, [output]

        return self._execute_stage(project, "metadata", operation, backend="mock")[0]

    def _build_timeline(self, project: Project) -> Timeline:
        if project.video_mode is VideoMode.EDITORIAL:
            return self._build_editorial_timeline(project)
        root = self.store.project_path(project)
        narration = root / "narration" / "master.wav"
        if not narration.is_file():
            raise PipelineError("Narration audio is missing: narration/master.wav")
        try:
            narration_duration = wav_duration(narration)
        except (OSError, EOFError, ValueError, ZeroDivisionError, wave.Error) as exc:
            raise PipelineError("Narration audio is not a valid PCM WAV file.") from exc
        if not math.isfinite(narration_duration) or narration_duration <= 0:
            raise PipelineError("Narration audio must have a positive, finite duration.")

        scenes = self.database.list_scenes(project.id)
        assets = self.database.list_assets(project.id)
        scenes_by_id = {scene.id: scene for scene in scenes}
        materialized_scenes = {shot.scene_id for shot in self.database.list_shots(project.id)}
        compiled_scenes = {
            scene.id: path
            for scene in scenes
            if (path := self._compiled_scene_media(project, scene)) is not None
        }
        visuals = {
            asset.scene_id: asset for asset in assets
            if asset.scene_id
            and asset.scene_id in scenes_by_id
            and self._is_current_visual_asset(scenes_by_id[asset.scene_id], asset)
            and (root / asset.filepath).is_file()
            and (root / asset.filepath).stat().st_size > 0
        }
        selected: list[tuple[Scene, Asset | None, Path, str]] = []
        for scene in scenes:
            compiled = compiled_scenes.get(scene.id)
            if compiled is not None:
                # A current multi-shot compilation exists: consume it as-is.
                # Project rendering never regenerates shot media here.
                selected.append((scene, None, compiled, "video"))
                continue
            if scene.id in materialized_scenes:
                # Auto-compile multi-shot scenes before assembling the project render.
                render_result = self.render_scene(scene.id)
                compiled_path = root / render_result.get("path", f"scenes/{scene.index + 1:03d}/rendered.mp4")
                selected.append((scene, None, compiled_path, "video"))
                continue
            asset = visuals.get(scene.id)
            if asset is None:
                raise PipelineError(
                    f"Cannot render because scene {scene.index + 1} has no current generated visual. "
                    "Generate or regenerate that scene from the Storyboard first."
                )
            media_kind = "video" if asset.type is AssetType.VIDEO else (
                "title" if scene.visual_type is VisualType.TITLE_CARD else
                "diagram" if scene.visual_type is VisualType.DIAGRAM else "image"
            )
            selected.append((scene, asset, root / asset.filepath, media_kind))
        planned_durations = [scene.duration for scene, _asset, _path, _kind in selected]
        planned_total = sum(planned_durations)
        if not math.isfinite(planned_total):
            raise PipelineError("Planned scene durations must be finite.")
        durations = list(planned_durations)
        measured_scene_durations = self.tts.active_scene_durations(project.id)
        scene_audio_synced = bool(
            measured_scene_durations
            and all(scene.id in measured_scene_durations for scene, _a, _p, _k in selected)
        )
        if scene_audio_synced:
            assert measured_scene_durations is not None
            durations = [measured_scene_durations[scene.id] for scene, _a, _p, _k in selected]
            # WAV frame rounding can leave a sub-frame aggregate difference.
            durations[-1] += narration_duration - sum(durations)
            if any(duration <= 0 for duration in durations):
                raise PipelineError("Measured narration scene timing contains a non-positive duration.")
        elif narration_duration > planned_total:
            durations = adjust_scene_durations(durations, narration_duration)
        visuals_extended = any(
            actual > planned + 1e-6
            for actual, planned in zip(durations, planned_durations)
        )
        transitions: list[tuple[str, float]] = [("cut", 0.0)]
        for index in range(1, len(selected)):
            scene, _asset, _path, media_kind = selected[index]
            _s, _a2, _p2, previous_kind = selected[index - 1]
            overlap = min(0.35, durations[index] / 4)
            if scene.transition == "cut" or (previous_kind == "video" and media_kind == "video"):
                transitions.append(("cut", 0.0))
                continue
            # Add visual handles to a still so transition overlap does not shorten narration.
            if previous_kind != "video":
                durations[index - 1] += overlap
            elif media_kind != "video":
                durations[index] += overlap
            else:
                transitions.append(("cut", 0.0))
                continue
            transitions.append((scene.transition, overlap))
        timings = []
        for index, (scene, asset, source_path, media_kind) in enumerate(selected):
            transition, overlap = transitions[index]
            timings.append(
                SceneTiming(
                    scene.id,
                    source_path,
                    durations[index],
                    media_kind,
                    transition,
                    overlap,
                    (
                        None
                        if asset is None
                        else self._camera_motion(
                            scene.camera_instruction,
                            media_kind,
                            scene.visual_type,
                        )
                    ),
                )
            )
        music = root / "music" / "background.wav"
        narration_gain_db = self.tts.active_narration_gain(project.id)
        timeline = build_timeline(
            timings,
            width=project.resolution[0],
            height=project.resolution[1],
            fps=project.fps,
            narration_path=narration,
            narration_gain_db=narration_gain_db,
            music_path=music if music.is_file() else None,
            subtitles=self._subtitle_cues(project),
        )
        timeline.metadata.update(
            {
                "workflow_version": "timeline-v2",
                "duration_policy": (
                    "scene_aligned_narration_v1"
                    if scene_audio_synced else "extend_visuals_to_narration_v1"
                ),
                "planned_scene_duration_seconds": planned_total,
                "narration_duration_seconds": narration_duration,
                "narration_gain_db": narration_gain_db,
                "visuals_extended": visuals_extended,
                "scene_audio_synced": scene_audio_synced,
            }
        )
        return timeline

    def _build_editorial_timeline(self, project: Project) -> Timeline:
        """Wrap one deterministic Editorial master in the shared audio/caption timeline."""
        plan = self._validate_editorial_render_inputs(project)
        root = self.store.project_path(project)
        visual = root / "editorial" / "master.mp4"
        if not visual.is_file() or visual.stat().st_size <= 0:
            raise PipelineError(
                "Editorial visual master is missing; render the Editorial composition first."
            )
        narration = root / "narration" / "master.wav"
        music = root / "music" / "background.wav"
        timeline = build_timeline(
            [SceneTiming(
                scene_id="editorial-master",
                asset_path=visual,
                duration_seconds=plan.duration,
                media_kind="video",
            )],
            width=project.resolution[0],
            height=project.resolution[1],
            fps=project.fps,
            narration_path=narration,
            narration_gain_db=self.tts.active_narration_gain(project.id),
            music_path=music if music.is_file() else None,
            subtitles=self._subtitle_cues(project) if plan.captions_enabled else (),
        )
        timeline.metadata.update({
            "workflow_version": "editorial-timeline-v1",
            "duration_policy": "narration_clock_with_editorial_tail_v1",
            "edit_plan_duration_seconds": plan.duration,
            "captions_enabled": plan.captions_enabled,
            "editorial_text_enabled": plan.editorial_text_enabled,
        })
        return timeline

    @staticmethod
    def _camera_motion(
        instruction: str,
        media_kind: str,
        visual_type: VisualType | str | None = None,
    ) -> str | None:
        """Return still motion only for visual types that support animation."""
        if media_kind == "video":
            return None
        # Camera movement is an explicit property of IMAGE_MOTION. Krea 2 stills,
        # graphics, diagrams, and other still modes must remain truly static even
        # when an older plan happens to contain a camera instruction.
        if visual_type is not None and visual_type != VisualType.IMAGE_MOTION:
            return None
        normalized = instruction.strip().lower().replace("_", " ").replace("-", " ")
        if normalized in {"", "locked", "locked off", "none", "no motion", "static"}:
            return None
        return instruction

    def _subtitle_cues(self, project: Project) -> list[SubtitleCue]:
        word_timings = self.store.project_path(project) / "subtitles" / "word-timings.json"
        if word_timings.is_file():
            return self._audio_derived_cues(word_timings)
        cues: list[SubtitleCue] = []
        cursor = 0.0
        for scene in self.database.list_scenes(project.id):
            lines = textwrap.wrap(scene.narration, width=42, break_long_words=False) or [""]
            chunks = ["\n".join(lines[index:index + 2]) for index in range(0, len(lines), 2)]
            chunk_duration = scene.duration / len(chunks)
            for index, text in enumerate(chunks):
                start = cursor + index * chunk_duration
                cues.append(SubtitleCue(start, start + chunk_duration, text))
            cursor += scene.duration
        return cues

    def _mock_generate(
        self,
        project: Project,
        kind: str,
        *,
        project_dir: Path,
        prompt: str,
        seed: int,
        negative_prompt: str = "",
        duration: float | None = None,
        width: int | None = None,
        height: int | None = None,
        references: tuple[Path, ...] = (),
        extra_settings: Mapping[str, Any] | None = None,
    ) -> GenerationResult:
        if not self.mock_mode:
            raise PipelineError(
                f"Real backend routing for {kind!r} is not configured; enable a compatible local worker."
            )
        backend = self.registry.get("mock")
        settings: dict[str, Any] = {"kind": kind}
        if extra_settings:
            settings.update(extra_settings)
        request = GenerationRequest(
            job_id=f"{project.id}:{kind}:{seed}",
            output_dir=project_dir,
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            duration_seconds=duration,
            width=width,
            height=height,
            references=references,
            settings=settings,
        )
        backend.load()
        try:
            return backend.generate(request)
        except Exception as exc:
            descriptor = backend.descriptor()
            self.database.save_attempt(
                GenerationAttempt(
                    backend=descriptor.backend_name,
                    model=descriptor.model_name,
                    model_version=descriptor.model_version,
                    quantization=descriptor.quantization,
                    workflow_version="mock-v1",
                    parameters={"kind": kind},
                    seed=seed,
                    success=False,
                    error=redact_secrets(exc),
                )
            )
            raise
        finally:
            backend.unload()

    def _record_asset(
        self,
        project: Project,
        scene: Scene | None,
        output: Path,
        asset_type: AssetType,
        result: GenerationResult,
        *,
        role: str,
        job_id: str | None = None,
        record_attempt: bool = True,
        extra_settings: Mapping[str, Any] | None = None,
        shot_id: str | None = None,
    ) -> Asset:
        descriptor = self.registry.get("mock").descriptor() if self.mock_mode else None
        metadata = dict(result.metadata)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        asset = Asset(
            project_id=project.id,
            scene_id=scene.id if scene else None,
            shot_id=shot_id,
            type=asset_type,
            filepath=output.relative_to(self.store.project_path(project)),
            backend=str(metadata.get("backend", descriptor.backend_name if descriptor else "unknown")),
            model=str(metadata.get("model", descriptor.model_name if descriptor else "unknown")),
            model_version=str(metadata.get("model_version", descriptor.model_version if descriptor else "unknown")),
            quantization=(
                str(metadata["quantization"])
                if metadata.get("quantization") is not None
                else descriptor.quantization if descriptor else None
            ),
            workflow_version=str(metadata.get("workflow_version", "mock-v1")),
            seed=int(metadata.get("seed", scene.seed if scene else 0)),
            prompt=str(metadata.get("prompt", scene.visual_prompt if scene else "")),
            negative_prompt=str(metadata.get("negative_prompt", scene.negative_prompt if scene else "")),
            settings={
                **dict(metadata.get("settings", {})),
                **(dict(extra_settings) if extra_settings else {}),
                **(
                    {"visual_type": (
                        self.database.get_shot(shot_id).visual_type.value
                        if shot_id and self.database.get_shot(shot_id) is not None
                        else scene.visual_type.value
                    )}
                    if role == "visual" and scene is not None else {}
                ),
                **(
                    {"visual_revision": int(scene.settings.get("visual_revision", 0))}
                    if role == "visual"
                    and scene is not None
                    and int(scene.settings.get("visual_revision", 0)) > 0
                    else {}
                ),
                "role": role,
            },
            hash=digest,
        )
        self.database.save_asset(asset)
        if record_attempt:
            self.database.save_attempt(
                GenerationAttempt(
                    asset_id=asset.id,
                    job_id=job_id,
                    scene_id=asset.scene_id,
                    shot_id=asset.shot_id,
                    backend=asset.backend,
                    model=asset.model,
                    model_version=asset.model_version,
                    quantization=asset.quantization,
                    workflow_version=asset.workflow_version,
                    parameters=asset.settings,
                    seed=asset.seed,
                    success=True,
                    duration_seconds=0,
                    peak_vram_gb=result.peak_vram_gb,
                )
            )
        return asset

    def _record_render_asset(self, project: Project, output: Path, *, role: str) -> Asset:
        result = GenerationResult(
            outputs=(output,),
            metadata={
                "backend": "ffmpeg",
                "model": "ffmpeg",
                "model_version": self.renderer.binaries.source,
                "workflow_version": "render-v2",
                "seed": 0,
                "settings": {"role": role},
            },
            peak_vram_gb=0,
        )
        return self._record_asset(
            project,
            None,
            output,
            AssetType.VIDEO,
            result,
            role=role,
        )

    def _execute_stage(
        self,
        project: Project,
        stage: str,
        operation: Callable[[], tuple[Any, list[Path]]],
        *,
        backend: str,
        job: GenerationJob | None = None,
    ) -> tuple[Any, list[Path]]:
        # A caller may pre-create the stage job when the operation closure
        # needs the real job id (e.g. to attribute generation attempts; the
        # generation_attempts.job_id column references jobs(id)).
        if job is None:
            job = GenerationJob(project_id=project.id, stage=stage, backend=backend)
        if not job.parameters:
            # Stage rows are bookkeeping driven by their caller: execution has
            # no per-stage cancel checks, so the marker lets the API report
            # them as non-cancelable instead of offering a no-op button.
            job = job.model_copy(update={"parameters": {"managed_by": "pipeline"}})
        job = self.jobs.enqueue(job)
        self.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
        self.jobs.transition(job.id, JobStatus.GENERATING, progress=0.2)
        try:
            result, outputs = operation()
            # A cancel from the Job Monitor can land on this row mid-operation.
            # Keep the produced outputs and the stage record, and leave the row
            # canceled instead of raising an invalid CANCELED -> COMPLETED
            # transition that would fail the whole parent pipeline.
            current = self.jobs.get(job.id)
            canceled_midflight = current is not None and current.status is JobStatus.CANCELED
            if not canceled_midflight:
                self.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.9)
            self._mark_stage(project, stage, outputs, job.id)
            if not canceled_midflight:
                self.jobs.complete(job.id)
            return result, outputs
        except Exception as exc:
            current = self.jobs.get(job.id)
            if current is None or current.status not in {
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED,
            }:
                self.jobs.fail(job.id, redact_secrets(exc))
            raise

    def _project_with_recovery(
        self, project_id: str,
    ) -> tuple[Project, list[dict[str, Any]]]:
        project = self.database.get_project(project_id)
        recovery: list[dict[str, Any]] = []
        if project is None:
            # Recovery path: an on-disk directory with no database row can still
            # be resolved and re-indexed without discarding files.
            project, recovery = self._recover_project_by_id(project_id)
        if project is None:
            raise KeyError(f"project not found: {project_id}")
        return project, recovery

    def _project(self, project_id: str) -> Project:
        return self._project_with_recovery(project_id)[0]

    def _save_project(self, project: Project) -> None:
        # Keep the portable project.json and the database row consistent. Write
        # the portable file first, then the database; if the database update
        # fails, roll the portable file back to its prior content so a failed
        # update leaves neither source changed.
        path = self.store.project_path(project) / "project.json"
        previous = path.read_text(encoding="utf-8") if path.is_file() else None
        self.store.save_project(project)
        try:
            self.database.update_project(project)
        except Exception:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                self.store._atomic_json(path, json.loads(previous))
            raise

    def _scene_dir(self, project: Project, scene: Scene) -> Path:
        directory = self.store.project_path(project) / "scenes" / f"{scene.index + 1:03d}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _compiled_scene_media(self, project: Project, scene: Scene) -> Path | None:
        """Return the scene's compiled render when a current manifest vouches for it."""
        directory = self.store.project_path(project) / "scenes" / f"{scene.index + 1:03d}"
        media = directory / "rendered.mp4"
        if not media.is_file() or media.stat().st_size == 0:
            return None
        manifest = load_manifest(directory / "render-manifest.json")
        if manifest is None:
            return None
        if manifest.get("workflow") != SCENE_ASSEMBLY_WORKFLOW:
            return None
        if manifest.get("scene_id") != scene.id:
            return None
        try:
            if sha256_file(media) != manifest.get("media_sha256"):
                return None
        except OSError:
            return None
        return media

    def _state_path(self, project: Project) -> Path:
        return self.store.project_path(project) / "stage-state.json"

    def _read_stage_state(self, project: Project) -> dict[str, Any]:
        path = self._state_path(project)
        if not path.is_file():
            return {"version": 1, "stages": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "stages": {}}
        return payload if isinstance(payload, dict) else {"version": 1, "stages": {}}

    def _stage_complete(self, project: Project, stage: str) -> bool:
        record = self._read_stage_state(project).get("stages", {}).get(stage, {})
        if record.get("status") != "completed":
            return False
        root = self.store.project_path(project)
        return all((root / path).is_file() and (root / path).stat().st_size > 0 for path in record.get("outputs", []))

    def _stage_paths(self, project: Project, stage: str) -> list[Path]:
        root = self.store.project_path(project)
        record = self._read_stage_state(project).get("stages", {}).get(stage, {})
        return [root / path for path in record.get("outputs", [])]

    def _invalidate_stages(self, project: Project, stages: set[str]) -> None:
        # Leaf lock: stage-state.json read-modify-write is safe against
        # concurrent request threads and background jobs (see lock order).
        with self._stage_state_lock:
            state = self._read_stage_state(project)
            records = state.setdefault("stages", {})
            changed = False
            for stage in stages:
                if stage in records:
                    records.pop(stage)
                    changed = True
            if changed:
                self._atomic_json(self._state_path(project), state)

    def _archive_output(self, project: Project, path: Path) -> Path | None:
        if not path.is_file():
            return None
        return self.store.archive_variant(
            project.slug,
            path.relative_to(self.store.project_path(project)),
        )

    def _publish_pending_file(
        self, project: Project, pending: Path, destination: Path,
    ) -> None:
        """Archive the approved file and atomically replace it only after generation/QC."""
        archived: Path | None = None
        backup: Path | None = None
        try:
            if destination.is_file():
                descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.", suffix=".rollback", dir=destination.parent
                )
                os.close(descriptor)
                backup = Path(backup_name)
                shutil.copy2(destination, backup)
                archived = self.store.archive_variant(
                    project.slug,
                    backup.relative_to(self.store.project_path(project)),
                )
            os.replace(pending, destination)
        except Exception:
            if archived is not None and archived.is_file() and not destination.is_file():
                shutil.copy2(archived, destination)
            raise
        finally:
            pending.unlink(missing_ok=True)
            if backup is not None:
                backup.unlink(missing_ok=True)

    def _publish_graphic_artifacts(
        self, project: Project, replacements: dict[Path, Path],
    ) -> None:
        """Publish the source, manifest, and PNG together, restoring the old set on failure."""

        root = self.store.project_path(project)
        directory = next(iter(replacements)).parent
        rollback_dir = Path(tempfile.mkdtemp(prefix=".graphic-screen-rollback-", dir=directory))
        archived: dict[Path, Path] = {}
        try:
            # Archive copies first. The approved targets remain in place until every rollback copy
            # is durable in the normal variants directory.
            try:
                for target in replacements:
                    if not target.is_file():
                        continue
                    backup = rollback_dir / target.name
                    shutil.copy2(target, backup)
                    archived[target] = self.store.archive_variant(
                        project.slug, backup.relative_to(root),
                    )
            except Exception:
                for archive in archived.values():
                    archive.unlink(missing_ok=True)
                raise

            try:
                for target, pending in replacements.items():
                    os.replace(pending, target)
            except Exception as publish_error:
                restore_errors: list[Exception] = []
                for target in replacements:
                    archive = archived.get(target)
                    try:
                        if archive is not None and archive.is_file():
                            os.replace(archive, target)
                        elif archive is None:
                            target.unlink(missing_ok=True)
                    except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
                        restore_errors.append(exc)
                if restore_errors:
                    raise PipelineError(
                        "Graphic Screen publication failed and its prior artifacts could not be fully restored."
                    ) from publish_error
                raise
        finally:
            for pending in replacements.values():
                pending.unlink(missing_ok=True)
            shutil.rmtree(rollback_dir, ignore_errors=True)

    def _mark_stage(self, project: Project, stage: str, outputs: Iterable[Path], job_id: str) -> None:
        # Same leaf lock as _invalidate_stages: request threads and background
        # jobs both mutate stage-state.json.
        with self._stage_state_lock:
            state = self._read_stage_state(project)
            root = self.store.project_path(project)
            state.setdefault("stages", {})[stage] = {
                "status": "completed",
                "job_id": job_id,
                "completed_at": utc_now().isoformat(),
                "outputs": [str(path.relative_to(root)) for path in outputs],
            }
            self._atomic_json(self._state_path(project), state)

    @staticmethod
    def _atomic_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {key: cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._jsonable(item) for item in value]
        return value.value if hasattr(value, "value") else value
