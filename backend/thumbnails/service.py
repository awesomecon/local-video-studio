"""Project-scoped Thumbnail Studio orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PIL import Image, ImageOps

from backend.director.image_routing import (
    serialize_ideogram_prompt_json,
    validate_ideogram_prompt_json,
)
from backend.models import GenerationRequest, GenerationResult
from backend.models.errors import redact_secrets
from backend.rendering.process import media_process_scope
from backend.schemas import (
    AssetType,
    GenerationAttempt,
    GenerationJob,
    JobStatus,
    Project,
    ThumbnailCandidate,
    ThumbnailCandidateRequest,
    ThumbnailConcept,
    ThumbnailPlan,
    ThumbnailSelection,
    ThumbnailTextLayout,
    utc_now,
)

if TYPE_CHECKING:
    from backend.pipeline.service import PipelineService


_CANDIDATE_IDS = {"candidate-01", "candidate-02", "candidate-03"}

# Deterministic variation: each slot gets its own band of seeds and every
# regeneration attempt inside a slot advances by one, so three candidates and
# repeated regenerations never submit an identical prompt+seed pair again.
_SEED_SLOT_STRIDE = 1_000_000
_SEED_MODULUS = 2**63 - 1
logger = logging.getLogger(__name__)

_IDEOGRAM_THUMBNAIL_PALETTES: dict[str, list[str]] = {
    "sunset": ["#17191C", "#D59A4A", "#B23A2B", "#F2EEE5"],
    "electric": ["#08111F", "#00D9FF", "#FF2EA6", "#FFFFFF"],
    "midnight": ["#070B18", "#182A54", "#6E8BFF", "#F5F7FF"],
    "paper": ["#151515", "#E7D8BC", "#B83B2E", "#FFF8E8"],
}

_IDEOGRAM_THUMBNAIL_FONTS: dict[str, str] = {
    "impact": "very heavy condensed sans-serif display lettering, uppercase",
    "clean": "bold geometric sans-serif display lettering with clean open counters",
    "editorial": "bold high-contrast editorial serif display lettering",
}

_IDEOGRAM_THUMBNAIL_WIDTH = 1536
_IDEOGRAM_THUMBNAIL_HEIGHT = 864
_IDEOGRAM_THUMBNAIL_WORKFLOW_VERSION = "ideogram4-thumbnail-nf4-quality48-v2"
_IDEOGRAM_MAGIC_PROMPT_FILENAME = "ideogram-magic-prompt.json"


class ThumbnailStudioService:
    """Uses PipelineService resources without exposing raw ComfyUI calls to routes."""

    def __init__(self, pipeline: PipelineService) -> None:
        self.pipeline = pipeline

    @staticmethod
    def _magic_prompt_plan_fingerprint(plan: ThumbnailPlan) -> str:
        payload = plan.model_dump(mode="json")
        # Saving an unchanged form refreshes updated_at; it must not invalidate
        # an otherwise identical, already-generated Magic Prompt.
        payload.pop("updated_at", None)
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _magic_prompt_path(self, project: Project) -> Path:
        return (
            self.pipeline.store.project_path(project)
            / "thumbnails" / _IDEOGRAM_MAGIC_PROMPT_FILENAME
        )

    @staticmethod
    def _write_magic_prompt(path: Path, record: dict[str, Any]) -> None:
        """Atomically save without sorting the canonical caption's keys."""
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _magic_prompt_snapshot(
        self, project: Project, plan: ThumbnailPlan,
    ) -> dict[str, Any] | None:
        path = self._magic_prompt_path(project)
        if not path.is_file():
            self._migrate_magic_prompt_from_candidate(project, plan)
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            canonical = validate_ideogram_prompt_json(record["structured_prompt"])
            record["structured_prompt"] = canonical
            record["serialized_prompt"] = serialize_ideogram_prompt_json(canonical)
            record["stale"] = (
                record.get("plan_fingerprint")
                != self._magic_prompt_plan_fingerprint(plan)
            )
            record["status"] = "saved"
            return record
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            return {
                "schema_version": 1,
                "status": "invalid",
                "stale": True,
                "error": (
                    "The saved Magic Prompt file could not be validated. "
                    "Regenerate it before running Ideogram."
                ),
                "path": f"thumbnails/{_IDEOGRAM_MAGIC_PROMPT_FILENAME}",
            }

    def _migrate_magic_prompt_from_candidate(
        self, project: Project, plan: ThumbnailPlan,
    ) -> None:
        """Recover pre-feature prompt provenance from a current candidate."""
        if plan.image_model != "ideogram4_local":
            return
        root = self.pipeline.store.project_path(project) / "thumbnails"
        candidates: list[dict[str, Any]] = []
        for manifest_path in root.glob("candidate-*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    manifest.get("image_model") == "ideogram4_local"
                    and manifest.get("stale") is not True
                    and manifest.get("original_title") == plan.text_layout.title
                    and manifest.get("original_hook", "") == plan.text_layout.hook
                    and isinstance(manifest.get("ideogram_prompt_json"), dict)
                ):
                    candidates.append(manifest)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        if not candidates:
            return
        source = max(candidates, key=lambda item: str(item.get("created_at", "")))
        try:
            canonical = validate_ideogram_prompt_json(source["ideogram_prompt_json"])
        except ValueError:
            return
        record = {
            "schema_version": 1,
            "status": "saved",
            "path": f"thumbnails/{_IDEOGRAM_MAGIC_PROMPT_FILENAME}",
            "plan_fingerprint": self._magic_prompt_plan_fingerprint(plan),
            "prompt_mode": str(source.get("ideogram_prompt_mode", "quick")),
            "aspect_ratio": "16:9",
            "structured_prompt": canonical,
            "serialized_prompt": serialize_ideogram_prompt_json(canonical),
            "protected_text": list(source.get("ideogram_protected_text", [])),
            "warnings": list(source.get("ideogram_prompt_warnings", [])),
            "same_as_previous": False,
            "reused": True,
            "stale": False,
            "migrated_from_candidate": str(source.get("candidate_id", "")),
            "updated_at": str(source.get("created_at") or utc_now().isoformat()),
        }
        self._write_magic_prompt(self._magic_prompt_path(project), record)

    def prepare_ideogram_magic_prompt(
        self, project_id: str, *, regenerate: bool = False,
    ) -> dict[str, Any]:
        """Generate, persist, or reuse the current thumbnail Magic Prompt.

        This operation uses only the configured local LLM.  It deliberately
        finishes and writes the canonical JSON before any Ideogram model load
        or VRAM check, so an image-generation failure cannot erase the prompt.
        """
        project = self.pipeline._project(project_id)
        plan = self._plan(project)
        if plan.image_model != "ideogram4_local":
            raise ValueError("select Ideogram 4 and save the thumbnail plan first")
        with self.pipeline._lock:
            return self._prepare_ideogram_magic_prompt_for_plan(
                project, plan, regenerate=regenerate,
            )

    def _prepare_ideogram_magic_prompt_for_plan(
        self,
        project: Project,
        plan: ThumbnailPlan,
        *,
        regenerate: bool = False,
    ) -> dict[str, Any]:
        fingerprint = self._magic_prompt_plan_fingerprint(plan)
        previous = self._magic_prompt_snapshot(project, plan)
        if (
            not regenerate
            and previous is not None
            and previous.get("status") == "saved"
            and not previous.get("stale", True)
        ):
            return {**previous, "reused": True, "same_as_previous": True}

        if plan.ideogram_prompt_mode == "precise":
            if plan.ideogram_prompt_json is None:
                raise ValueError("Precise Ideogram thumbnail mode requires native prompt JSON")
            canonical = validate_ideogram_prompt_json(plan.ideogram_prompt_json)
            protected_text = [
                element["text"]
                for element in canonical["compositional_deconstruction"]["elements"]
                if element["type"] == "text"
            ]
            required_text = [
                value for value in (plan.text_layout.title, plan.text_layout.hook) if value
            ]
            missing = [value for value in required_text if value not in protected_text]
            if missing:
                raise ValueError(
                    "Precise Ideogram JSON is missing exact thumbnail text: "
                    + ", ".join(repr(value) for value in missing)
                )
            built = {
                "mode": "precise",
                "structured_prompt": canonical,
                "serialized_prompt": serialize_ideogram_prompt_json(canonical),
                "protected_text": protected_text,
                "warnings": [
                    "Precise thumbnail caption validated directly; Magic Prompt was bypassed."
                ],
            }
        else:
            built = self._build_ideogram_thumb_prompt(plan)
        canonical = validate_ideogram_prompt_json(built["structured_prompt"])
        serialized = serialize_ideogram_prompt_json(canonical)
        previous_prompt = (
            previous.get("structured_prompt")
            if previous and previous.get("status") == "saved" else None
        )
        now = utc_now().isoformat()
        record = {
            "schema_version": 1,
            "status": "saved",
            "path": f"thumbnails/{_IDEOGRAM_MAGIC_PROMPT_FILENAME}",
            "plan_fingerprint": fingerprint,
            "prompt_mode": plan.ideogram_prompt_mode,
            "aspect_ratio": "16:9",
            "structured_prompt": canonical,
            "serialized_prompt": serialized,
            "protected_text": list(built.get("protected_text", [])),
            "warnings": list(built.get("warnings", [])),
            "same_as_previous": previous_prompt == canonical,
            "reused": False,
            "stale": False,
            "updated_at": now,
        }
        self._write_magic_prompt(self._magic_prompt_path(project), record)
        return record

    def snapshot(self, project_id: str) -> dict[str, Any]:
        project = self.pipeline._project(project_id)
        plan = self._plan(project)
        selection = self.pipeline.store.load_thumbnail_selection(project.slug)
        root = self.pipeline.store.project_path(project)
        candidates: list[dict[str, Any]] = []
        for candidate_id in sorted(_CANDIDATE_IDS):
            manifest_path = root / "thumbnails" / candidate_id / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                candidate = ThumbnailCandidate.model_validate({
                    "candidate_id": candidate_id,
                    "artwork_path": f"thumbnails/{candidate_id}/artwork.png",
                    "composite_path": f"thumbnails/{candidate_id}/composite.png",
                    "manifest_path": f"thumbnails/{candidate_id}/manifest.json",
                    "artwork_hash": manifest["output_hashes"]["artwork"],
                    "composite_hash": manifest["output_hashes"]["composite"],
                    "selected": bool(
                        selection
                        and selection.candidate_id == candidate_id
                        and selection.composite_hash == manifest["output_hashes"]["composite"]
                    ),
                    "stale": bool(manifest.get("stale", False)),
                    "created_at": manifest["created_at"],
                })
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                continue
            payload = candidate.model_dump(mode="json")
            payload["file_url"] = (
                f"/api/projects/{project.id}/thumbnails/candidates/{candidate_id}/file"
            )
            payload["provenance"] = manifest
            candidates.append(payload)
        legacy = []
        for asset in self.pipeline.database.list_assets(project.id):
            if asset.type is AssetType.THUMBNAIL and asset.settings.get("role") == "thumbnail":
                payload = asset.model_dump(mode="json")
                payload["url"] = f"/api/projects/{project.id}/assets/{asset.id}/file"
                legacy.append(payload)
        return {
            "plan": plan.model_dump(mode="json"),
            "magic_prompt": self._magic_prompt_snapshot(project, plan),
            "candidates": candidates,
            "selection": selection.model_dump(mode="json") if selection else None,
            "legacy_frames": legacy,
            "jobs": [
                job.model_dump(mode="json")
                for job in self.pipeline.jobs.list(project.id)
                if job.stage.startswith("thumbnail:")
            ],
        }

    def save_plan(self, project_id: str, plan: ThumbnailPlan) -> ThumbnailPlan:
        project = self.pipeline._project(project_id)
        if plan.project_id != project.id:
            raise ValueError("thumbnail plan does not belong to this project")
        updated = plan.model_copy(update={"updated_at": utc_now()})
        self.pipeline.store.save_thumbnail_plan(project.slug, updated)
        self._mark_candidates_stale(project)
        self._archive_selection(project)
        return updated

    def invalidate(self, project_id: str) -> None:
        """Mark composites stale while retaining every completed local file."""
        project = self.pipeline._project(project_id)
        root = self.pipeline.store.project_path(project)
        manifests = {
            path: path.read_text(encoding="utf-8")
            for path in (root / "thumbnails").glob("candidate-*/manifest.json")
        }
        selected = root / "thumbnails" / "selected.json"
        selected_text = selected.read_text(encoding="utf-8") if selected.is_file() else None
        archive_dir = root / "variants" / "archive"
        archives_before = set(archive_dir.glob("thumbnail-selection-*.json"))
        try:
            self._mark_candidates_stale(project, raise_on_error=True)
            self._archive_selection(project)
        except Exception:
            rollback_errors: list[Exception] = []
            for path, original in manifests.items():
                try:
                    if path.read_text(encoding="utf-8") != original:
                        self.pipeline._atomic_json(path, json.loads(original))
                except Exception as rollback_error:  # pragma: no cover - catastrophic I/O
                    rollback_errors.append(rollback_error)
            try:
                if selected_text is not None:
                    self.pipeline._atomic_json(selected, json.loads(selected_text))
                for archive in set(archive_dir.glob("thumbnail-selection-*.json")) - archives_before:
                    archive.unlink(missing_ok=True)
            except Exception as rollback_error:  # pragma: no cover - catastrophic I/O
                rollback_errors.append(rollback_error)
            if rollback_errors:
                logger.error(
                    "thumbnail invalidation rollback was incomplete for project %s (%d errors)",
                    project.id,
                    len(rollback_errors),
                )
            raise

    def _candidate_seed(
        self, project: Project, base_seed: int, candidate_id: str,
    ) -> int:
        """Deterministic per-slot, per-attempt variation of the plan seed.

        The three slots draw from separated bands so simultaneous candidates
        differ, and every queued job for a slot advances by its persisted
        attempt count so regenerating never repeats a previous prompt+seed
        pair. The exact seed is stored on the job and in the manifest.
        """
        slots = sorted(_CANDIDATE_IDS)
        slot = slots.index(candidate_id) if candidate_id in slots else 0
        attempts = len(self._attempt_history(project, candidate_id))
        return (base_seed + slot * _SEED_SLOT_STRIDE + attempts) % _SEED_MODULUS

    def queue_candidate(
        self,
        project_id: str,
        request: ThumbnailCandidateRequest,
        *,
        candidate_id: str | None = None,
    ) -> GenerationJob:
        project = self.pipeline._project(project_id)
        plan = self._plan(project)
        chosen = candidate_id or request.candidate_id
        if chosen is None:
            existing = {
                path.parent.name
                for path in (self.pipeline.store.project_path(project) / "thumbnails").glob(
                    "candidate-*/manifest.json"
                )
            }
            chosen = next(
                (item for item in sorted(_CANDIDATE_IDS) if item not in existing), None,
            )
        if chosen not in _CANDIDATE_IDS:
            raise ValueError(
                "all three candidate slots are filled; delete a candidate to free a slot"
            )
        if any(
            job.stage == f"thumbnail:{chosen}"
            and job.status not in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}
            for job in self.pipeline.jobs.list(project.id)
        ):
            raise ValueError(f"{chosen} already has an active thumbnail job")
        if request.source_asset_id:
            source = self.pipeline.database.get_asset(request.source_asset_id)
            if source is None or source.project_id != project.id:
                raise ValueError("source thumbnail asset was not found in this project")
            if source.type not in {AssetType.IMAGE, AssetType.THUMBNAIL}:
                raise ValueError("source thumbnail asset must be an image")
        if request.source_candidate_id:
            source_candidate = (
                self.pipeline.store.project_path(project) / "thumbnails"
                / request.source_candidate_id / "artwork.png"
            )
            if not source_candidate.is_file():
                raise ValueError("source thumbnail candidate is not available")
        image_model = getattr(plan, "image_model", "krea") or "krea"
        if image_model == "ideogram4_local":
            default_backend = "ideogram4_local_comfyui"
        else:
            default_backend = "krea2_comfyui"
        return self.pipeline.jobs.enqueue(GenerationJob(
            project_id=project.id,
            stage=f"thumbnail:{chosen}",
            backend="mock" if self.pipeline.mock_mode else (
                "local_asset"
                if request.source_asset_id or request.source_candidate_id
                else default_backend
            ),
            parameters={
                "candidate_id": chosen,
                "source_asset_id": request.source_asset_id,
                "source_candidate_id": request.source_candidate_id,
                "plan": plan.model_dump(mode="json"),
                "seed": self._candidate_seed(project, plan.concept.seed, chosen),
            },
            max_attempts=1,
        ))

    def run_candidate_job(self, job_id: str) -> ThumbnailCandidate:
        job = self.pipeline.jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        # Attribute every media subprocess this job spawns (probe/render) to
        # the job so canceling it kills its in-flight work — and never an
        # unrelated job's processes.
        with media_process_scope(job.id):
            return self._run_candidate_job(job)

    def _run_candidate_job(self, job: GenerationJob) -> ThumbnailCandidate:
        plan = ThumbnailPlan.model_validate(job.parameters["plan"])
        candidate_id = str(job.parameters["candidate_id"])
        source_asset_id = job.parameters.get("source_asset_id")
        source_candidate_id = job.parameters.get("source_candidate_id")
        seed = int(job.parameters.get("seed", plan.concept.seed))
        project = self.pipeline._project(job.project_id)
        thumbnail_root = self.pipeline.store.project_path(project) / "thumbnails"
        temporary = Path(tempfile.mkdtemp(prefix=f".{candidate_id}-", dir=thumbnail_root))
        image_model = getattr(plan, "image_model", "krea") or "krea"
        is_ideogram = image_model == "ideogram4_local"
        try:
            self.pipeline.jobs.transition(job.id, JobStatus.PREPARING, progress=0.05)
            with self.pipeline._lock:
                current = self.pipeline.jobs.get(job.id)
                if current is None or current.status is JobStatus.CANCELED:
                    raise RuntimeError("thumbnail job canceled before generation")
                self.pipeline.jobs.transition(job.id, JobStatus.LOADING_MODEL, progress=0.1)
                if source_asset_id:
                    result = self._source_artwork(
                        project, str(source_asset_id), temporary / "artwork.png", plan, seed,
                    )
                elif source_candidate_id:
                    result = self._source_candidate_artwork(
                        project, str(source_candidate_id), temporary / "artwork.png", plan,
                        seed,
                    )
                elif self.pipeline.mock_mode:
                    result = self.pipeline._mock_generate(
                        project,
                        "image",
                        project_dir=temporary,
                        prompt=self._art_prompt(plan),
                        negative_prompt=plan.concept.avoid_prompt,
                        seed=seed,
                        width=1280,
                        height=720,
                    )
                    os.replace(result.outputs[0], temporary / "artwork.png")
                elif is_ideogram:
                    result = self._dispatch_ideogram4(project, plan, temporary, job.id, seed)
                    os.replace(result.outputs[0], temporary / "artwork.png")
                else:
                    result = self._dispatch_krea(project, plan, temporary, job.id, seed)
                    os.replace(result.outputs[0], temporary / "artwork.png")
                self.pipeline.jobs.transition(job.id, JobStatus.GENERATING, progress=0.55)
                self._normalize_artwork(temporary / "artwork.png")
                # Ideogram renders text natively — skip Pillow overlay.
                if is_ideogram:
                    composite_hash = hashlib.sha256(
                        (temporary / "artwork.png").read_bytes()
                    ).hexdigest()
                    shutil.copyfile(temporary / "artwork.png", temporary / "composite.png")
                    font_identity = "ideogram-native"
                    font_hash = None
                else:
                    composite_hash, font_identity, font_hash = (
                        self.pipeline.graphic_renderer.render_thumbnail(
                            temporary / "artwork.png",
                            temporary / "composite.png",
                            plan.text_layout,
                            text_side=plan.concept.text_placement,
                        )
                    )
            self.pipeline.jobs.transition(job.id, JobStatus.POSTPROCESSING, progress=0.85)
            artwork_hash = hashlib.sha256((temporary / "artwork.png").read_bytes()).hexdigest()
            metadata = dict(result.metadata)
            created_at = utc_now()
            manifest = {
                "schema_version": 1,
                "candidate_id": candidate_id,
                "output_hashes": {"artwork": artwork_hash, "composite": composite_hash},
                "original_title": plan.text_layout.title,
                "original_hook": plan.text_layout.hook,
                "krea_prompt": self._art_prompt(plan),
                "avoid_prompt": plan.concept.avoid_prompt,
                "seed": seed,
                "base_seed": plan.concept.seed,
                "canvas": [1280, 720],
                "text_layout": plan.text_layout.model_dump(mode="json"),
                "image_model": image_model,
                "ideogram_prompt_mode": (
                    metadata.get("settings", {}).get("prompt_mode")
                    if is_ideogram else None
                ),
                "ideogram_prompt_json": (
                    metadata.get("settings", {}).get("ideogram_prompt_json")
                    if is_ideogram else None
                ),
                "ideogram_protected_text": (
                    metadata.get("settings", {}).get("protected_text", [])
                    if is_ideogram else []
                ),
                "ideogram_prompt_warnings": (
                    metadata.get("settings", {}).get("prompt_warnings", [])
                    if is_ideogram else []
                ),
                "model": metadata.get("model", "local-source"),
                "model_version": metadata.get("model_version", "1"),
                "quantization": metadata.get("quantization"),
                "workflow_version": metadata.get("workflow_version", "thumbnail-v1"),
                "font_identity": font_identity,
                "font_hash": font_hash,
                "renderer_version": (
                    "ideogram-native-text-v1" if is_ideogram
                    else "graphic-screen-pillow-compositor-v2"
                ),
                "sanitizer_version": "typed-thumbnail-layout-v2",
                "source_asset_id": source_asset_id,
                "source_candidate_id": source_candidate_id,
                "stale": False,
                "created_at": created_at.isoformat(),
                "attempt_history": [
                    *self._attempt_history(project, candidate_id),
                    {"job_id": job.id, "status": "completed", "created_at": created_at.isoformat()},
                ],
            }
            self.pipeline._atomic_json(temporary / "manifest.json", manifest)
            self._publish(project, candidate_id, temporary)
            candidate_dir = thumbnail_root / candidate_id
            artwork_asset = self.pipeline._record_asset(
                project,
                None,
                candidate_dir / "artwork.png",
                AssetType.THUMBNAIL,
                result,
                role="thumbnail_artwork",
                job_id=job.id,
            )
            composite_result = GenerationResult(
                outputs=(candidate_dir / "composite.png",),
                metadata={
                    **metadata,
                    "seed": seed,
                    "prompt": plan.concept.prompt,
                    "negative_prompt": plan.concept.avoid_prompt,
                    "workflow_version": "thumbnail-composite-v2",
                    "settings": {
                        "candidate_id": candidate_id,
                        "artwork_asset_id": artwork_asset.id,
                        "text_layout": plan.text_layout.model_dump(mode="json"),
                    },
                },
                peak_vram_gb=result.peak_vram_gb,
            )
            self.pipeline._record_asset(
                project,
                None,
                candidate_dir / "composite.png",
                AssetType.THUMBNAIL,
                composite_result,
                role="thumbnail_candidate",
                job_id=job.id,
            )
            self._archive_selection(project)
            # A cancel that landed during generation wins: keep the row
            # canceled (the produced candidate is kept) instead of raising an
            # invalid CANCELED -> COMPLETED transition.
            current = self.pipeline.jobs.get(job.id)
            if current is not None and current.status is not JobStatus.CANCELED:
                self.pipeline.jobs.complete(job.id)
            return ThumbnailCandidate(
                candidate_id=candidate_id,
                artwork_path=f"thumbnails/{candidate_id}/artwork.png",
                composite_path=f"thumbnails/{candidate_id}/composite.png",
                manifest_path=f"thumbnails/{candidate_id}/manifest.json",
                artwork_hash=artwork_hash,
                composite_hash=composite_hash,
                created_at=created_at,
            )
        except Exception as exc:
            current = self.pipeline.jobs.get(job.id)
            if current is not None and current.status not in {
                JobStatus.FAILED, JobStatus.CANCELED, JobStatus.COMPLETED,
            }:
                self.pipeline.jobs.fail(job.id, redact_secrets(exc))
            if not any(
                attempt.success is False
                for attempt in self.pipeline.database.list_attempts(job_id=job.id)
            ):
                self.pipeline.database.save_attempt(GenerationAttempt(
                    job_id=job.id,
                    backend=job.backend or "thumbnail",
                    model="Krea 2 Turbo" if job.backend == "krea2_comfyui" else "local",
                    workflow_version="thumbnail-composite-v2",
                    parameters={"candidate_id": candidate_id},
                    seed=seed,
                    success=False,
                    error=redact_secrets(exc),
                ))
            self._append_failed_history(project, candidate_id, job.id, exc)
            raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def select(self, project_id: str, candidate_id: str) -> ThumbnailSelection:
        project = self.pipeline._project(project_id)
        if candidate_id not in _CANDIDATE_IDS:
            raise ValueError("invalid thumbnail candidate")
        composite = (
            self.pipeline.store.project_path(project)
            / "thumbnails" / candidate_id / "composite.png"
        )
        if not composite.is_file():
            raise FileNotFoundError("thumbnail candidate is not available")
        manifest_path = composite.with_name("manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = {}
        if manifest.get("stale") is True:
            raise ValueError("regenerate this stale candidate before selecting it")
        digest = hashlib.sha256(composite.read_bytes()).hexdigest()
        selection = ThumbnailSelection(
            project_id=project.id,
            candidate_id=candidate_id,
            composite_path=f"thumbnails/{candidate_id}/composite.png",
            composite_hash=digest,
        )
        self.pipeline.store.save_thumbnail_selection(project.slug, selection)
        result = GenerationResult(
            outputs=(composite,),
            metadata={
                "backend": "local-compositor",
                "model": manifest.get("model", "Graphic Screen renderer"),
                "model_version": manifest.get("model_version", "1"),
                "quantization": manifest.get("quantization"),
                "workflow_version": "thumbnail-selection-v1",
                "seed": manifest.get("seed", 0),
                "prompt": manifest.get("krea_prompt", ""),
                "negative_prompt": manifest.get("avoid_prompt", ""),
                "settings": {
                    "candidate_id": candidate_id,
                    "source_composite_hash": digest,
                    "source_manifest": str(
                        manifest_path.relative_to(self.pipeline.store.project_path(project))
                    ),
                },
            },
            peak_vram_gb=0,
        )
        self.pipeline._record_asset(
            project, None, composite, AssetType.THUMBNAIL, result,
            role="thumbnail_selected",
        )
        return selection

    def delete_candidate(self, project_id: str, candidate_id: str) -> dict[str, Any]:
        """Free a candidate slot by archiving its directory and index rows.

        The completed files stay in the project archive (portable, human
        readable); the slot simply becomes empty again. An export selection
        that pointed at this candidate is cleared first.
        """
        project = self.pipeline._project(project_id)
        if candidate_id not in _CANDIDATE_IDS:
            raise FileNotFoundError("thumbnail candidate not found")
        if any(
            job.stage == f"thumbnail:{candidate_id}"
            and job.status not in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}
            for job in self.pipeline.jobs.list(project.id)
        ):
            raise ValueError(
                f"{candidate_id} has an active thumbnail job; cancel it before deleting"
            )
        root = self.pipeline.store.project_path(project)
        candidate_dir = root / "thumbnails" / candidate_id
        if not candidate_dir.is_dir():
            raise FileNotFoundError("thumbnail candidate not found")
        selection = self.pipeline.store.load_thumbnail_selection(project.slug)
        if selection and selection.candidate_id == candidate_id:
            self._archive_selection(project)
        archive = (
            root / "variants" / "archive"
            / f"thumbnail-{candidate_id}-deleted-{utc_now().strftime('%Y%m%dT%H%M%S')}"
            f"-{uuid4().hex[:8]}"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate_dir, archive)
        self.pipeline.database.delete_assets_for_path(
            project.id, f"thumbnails/{candidate_id}/"
        )
        return {"candidate_id": candidate_id, "archived_to": str(archive.relative_to(root))}

    def candidate_file(self, project_id: str, candidate_id: str) -> Path:
        project = self.pipeline._project(project_id)
        if candidate_id not in _CANDIDATE_IDS:
            raise FileNotFoundError("thumbnail candidate not found")
        root = self.pipeline.store.project_path(project).resolve()
        path = (root / "thumbnails" / candidate_id / "composite.png").resolve()
        if root not in path.parents or not path.is_file():
            raise FileNotFoundError("thumbnail candidate not found")
        return path

    def _plan(self, project: Project) -> ThumbnailPlan:
        path = self.pipeline.store.project_path(project) / "thumbnails" / "thumbnail-plan.json"
        if path.is_file():
            return self.pipeline.store.load_thumbnail_plan(project.slug)
        hook = ""
        plan = ThumbnailPlan(
            project_id=project.id,
            proposed_title=project.title[:120],
            hook=hook,
            audience=project.audience[:120],
            topic=project.topic[:2000],
            style=project.style[:120],
            concept=ThumbnailConcept(
                prompt=(
                    "One concrete cinematic scene with a single recognizable focal subject from "
                    "the video's central question; one related iconic object or environment, "
                    f"dramatic {project.style} lighting, strong depth, no collage or document layout; "
                    "subject on the left with uncluttered negative space on the right"
                )[:4000],
                seed=40_000,
            ),
            text_layout=ThumbnailTextLayout(title=project.title[:120], hook=hook),
        )
        self.pipeline.store.save_thumbnail_plan(project.slug, plan)
        return plan

    @staticmethod
    def _art_prompt(plan: ThumbnailPlan) -> str:
        """Effective art prompt with the exact copy removed so Krea never paints the title."""
        prompt = plan.concept.prompt.strip()
        for literal in (plan.text_layout.title, plan.text_layout.hook):
            phrase = literal.strip()
            if phrase:
                prompt = re.sub(re.escape(phrase), "the core idea", prompt, flags=re.IGNORECASE)
        prompt = re.sub(r"\s{2,}", " ", prompt)
        return (
            f"{prompt}. Subject positioned {plan.concept.subject_position}; "
            f"leave the {plan.concept.text_placement} side empty for graphic copy. "
            "No text, no letters, no words, no logos, no watermarks."
        )

    def _dispatch_krea(
        self, project: Project, plan: ThumbnailPlan, directory: Path, job_id: str,
        seed: int,
    ) -> GenerationResult:
        reusing_resident = self.pipeline._prepare_comfy_backend("krea2_comfyui")
        if not reusing_resident:
            self.pipeline._check_krea2_vram()
        request = GenerationRequest(
            job_id=job_id,
            output_dir=directory,
            prompt=self._art_prompt(plan),
            negative_prompt=plan.concept.avoid_prompt,
            seed=seed,
            width=1280,
            height=720,
            settings={
                "kind": "image",
                "workflow": self.pipeline.KREA2_WORKFLOW,
                "workflow_version": "krea2-thumbnail-fp8-v1",
            },
        )
        backend = self.pipeline.registry.get("krea2_comfyui")
        try:
            backend.load()
            generated = backend.generate(request)
            self.pipeline._resident_comfy_backend = "krea2_comfyui"
            return GenerationResult(
                outputs=generated.outputs,
                metadata={
                    **dict(generated.metadata),
                    "prompt": request.prompt,
                    "negative_prompt": request.negative_prompt,
                    "seed": request.seed,
                    "workflow_version": "krea2-thumbnail-fp8-v1",
                    "settings": {"width": 1280, "height": 720, "steps": 8},
                },
                peak_vram_gb=generated.peak_vram_gb,
            )
        except Exception as exc:
            descriptor = backend.descriptor()
            self.pipeline.database.save_attempt(GenerationAttempt(
                job_id=job_id,
                backend=descriptor.backend_name,
                model=descriptor.model_name,
                model_version=descriptor.model_version,
                quantization=descriptor.quantization,
                workflow_version="krea2-thumbnail-fp8-v1",
                parameters={"width": 1280, "height": 720},
                seed=seed,
                success=False,
                error=redact_secrets(exc),
            ))
            self.pipeline._release_comfyui_memory(
                backend_name="krea2_comfyui", wait_for_vram=False, suppress_errors=True,
            )
            raise

    def _build_ideogram_thumb_prompt(self, plan: ThumbnailPlan) -> dict[str, Any]:
        """Build a valid Ideogram 4 structured JSON payload for a thumbnail.

        Quick mode first runs the official open-source Magic Prompt through the
        same pipeline entry point as scene generation. The normalized result is
        then specialized with deterministic thumbnail boxes and saved styling.
        """
        layout = plan.text_layout
        literals: list[str] = []
        for phrase in (layout.title, layout.hook):
            if phrase and phrase not in literals:
                literals.append(phrase)
        visual_prompt = plan.concept.prompt.strip()
        # Older auto-created plans copied the entire project summary into this
        # field. That strongly biases a text-capable model toward documents and
        # article layouts. Preserve authored prompts, but replace this known
        # legacy template with concise, visual-only direction.
        if (
            visual_prompt.startswith("A compelling ")
            and "YouTube thumbnail artwork about" in visual_prompt
            and "one strong expressive subject" in visual_prompt
        ):
            visual_prompt = (
                "A dramatic close-up composition centered on the main person or object named "
                "by the headline, paired with one iconic environment or object suggested by "
                "the topic, cinematic rim lighting and strong depth"
            )
        for phrase in (layout.title, layout.hook):
            if phrase:
                visual_prompt = re.sub(
                    re.escape(phrase), "the video's central idea", visual_prompt,
                    flags=re.IGNORECASE,
                )
        visual_prompt = re.sub(r"\s{2,}", " ", visual_prompt).strip()
        idea = (
            f"{visual_prompt}\n"
            f"Requested visual style: {plan.style or 'documentary'}.\n"
            "Create a polished landscape YouTube thumbnail with one dominant focal "
            "subject and distinct, uncluttered regions for the required lettering."
        )
        prompt_result = self.pipeline.build_ideogram_prompt(
            idea,
            mode="quick",
            aspect_ratio="16:9",
            text_literals=literals,
        )
        payload = prompt_result["structured_prompt"]
        palette = list(_IDEOGRAM_THUMBNAIL_PALETTES[layout.palette])
        font = _IDEOGRAM_THUMBNAIL_FONTS[layout.font_preset]
        elements = payload["compositional_deconstruction"]["elements"]
        # Magic Prompt must not introduce additional thumbnail copy. Keep only
        # protected title/hook text while retaining every visual object it
        # proposed; exact-text restoration guarantees these literals exist.
        elements[:] = [
            element for element in elements
            if element["type"] != "text" or element.get("text") in literals
        ]
        text_by_literal = {
            element["text"]: element
            for element in elements
            if element["type"] == "text" and element.get("text") in literals
        }
        text_elements = [text_by_literal[literal] for literal in literals]
        objects = [element for element in elements if element["type"] == "obj"][:2]
        subject = objects[0] if objects else None
        if subject is None:
            subject = {
                "type": "obj",
                "desc": "One dominant focal subject matching the thumbnail concept.",
            }
            objects.append(subject)

        if layout.layout_preset == "banner":
            title_box = [650, 80, 910, 920]
            hook_box = [540, 120, 640, 880]
            subject["bbox"] = [40, 60, 530, 940]
            objects[:] = objects[:1]
            placement = "in a full-width lower banner"
        else:
            if plan.concept.text_placement == "left":
                x_min, x_max = 50, 500
                subject["bbox"] = [100, 570, 920, 980]
                secondary_box = [270, 100, 560, 530]
                placement = "on the left side"
            else:
                x_min, x_max = 500, 950
                subject["bbox"] = [100, 20, 920, 430]
                secondary_box = [270, 470, 560, 900]
                placement = "on the right side"
            if len(objects) > 1:
                objects[1]["bbox"] = secondary_box
            title_box = [620 if len(text_elements) > 1 else 360, x_min, 850, x_max]
            hook_box = [80, x_min, 210, x_max]

        if text_elements:
            effects = []
            if layout.outline:
                effects.append("a thick contrasting outline")
            if layout.shadow:
                effects.append("a subtle compact drop shadow")
            effect_text = ", with " + " and ".join(effects) if effects else ""
            text_elements[0]["bbox"] = title_box
            text_elements[0]["desc"] = (
                f"Primary YouTube thumbnail headline {placement}; {font}; very large, "
                "high contrast, upright horizontal left-to-right lines with a level "
                "baseline parallel to the long bottom edge; zero rotation, zero tilt, "
                f"zero perspective distortion, generous safe margins{effect_text}"
            )
            text_elements[0]["color_palette"] = [palette[-1]]
        if len(text_elements) > 1:
            text_elements[1]["bbox"] = hook_box
            text_elements[1]["desc"] = (
                f"Short supporting kicker {placement}; {font}; smaller than the primary "
                "headline, upright horizontal left-to-right text with a level baseline; "
                f"zero rotation, zero tilt, zero perspective distortion{effect_text}"
            )
            text_elements[1]["color_palette"] = [
                palette[1] if len(palette) > 1 else palette[0]
            ]

        # Keep at most two visual objects and order typography from top to
        # bottom. The assigned regions do not intersect, so extra Magic Prompt
        # objects cannot silently collide with the title or kicker.
        ordered_text = (
            [text_elements[1], text_elements[0]]
            if len(text_elements) > 1 else text_elements
        )
        elements[:] = [*objects, *ordered_text]

        style_description = payload["style_description"]
        style_description["aesthetics"] = (
            f"polished cinematic thumbnail, {layout.layout_preset} layout, strong visual "
            "hierarchy, crisp edges, restrained detail, straight-on landscape composition"
        )
        style_description["lighting"] = (
            "dramatic directional key light, controlled highlights, deep dimensional contrast"
        )
        style_description.pop("art_style", None)
        style_description["photo"] = (
            "eye-level full-frame camera, 35mm lens, sharp focal subject, gentle background depth"
        )
        # Official Ideogram photo captions pair ``photo`` with the exact
        # medium category ``photograph``; camera/aesthetic detail belongs in
        # the neighboring prose fields.
        style_description["medium"] = "photograph"
        style_description["color_palette"] = palette
        payload["high_level_description"] = (
            "A polished cinematic 16:9 thumbnail composition with one large upright focal "
            f"subject and strong visual hierarchy. {visual_prompt}."
        )
        payload["compositional_deconstruction"]["background"] = (
            "A full-bleed cinematic environment related to the focal subject, with a level "
            f"horizon, controlled detail, rich depth, and calm contrast on the {plan.concept.text_placement}."
        )
        if not str(subject.get("desc", "")).strip():
            subject["desc"] = visual_prompt
        canonical = validate_ideogram_prompt_json(payload)
        return {
            **prompt_result,
            "structured_prompt": canonical,
            "serialized_prompt": serialize_ideogram_prompt_json(canonical),
        }

    def _ideogram_thumb_prompt_json(self, plan: ThumbnailPlan) -> dict[str, Any]:
        """Compatibility helper returning the final canonical thumbnail caption."""

        return self._build_ideogram_thumb_prompt(plan)["structured_prompt"]

    def _dispatch_ideogram4(
        self, project: Project, plan: ThumbnailPlan, directory: Path, job_id: str,
        seed: int,
    ) -> GenerationResult:
        """Dispatch a thumbnail generation to local Ideogram 4 via ComfyUI.

        Ideogram 4 natively renders text in images, so the Pillow compositor is
        skipped for this mode: the artwork IS the final composite. The structured
        prompt JSON uses the official open-source Magic Prompt with the user's
        configured local LLM, followed by canonical validation and deterministic
        thumbnail layout specialization. No prompt or image data leaves localhost.
        """
        # Persist/reuse the canonical caption before touching the Ideogram
        # worker or checking VRAM.  Failed image attempts therefore retain an
        # inspectable prompt for the next retry.
        prompt_result = self._prepare_ideogram_magic_prompt_for_plan(
            project, plan, regenerate=False,
        )
        prompt_json = prompt_result["structured_prompt"]
        serialized_prompt = prompt_result["serialized_prompt"]
        parameters = {
            "kind": "image",
            "width": _IDEOGRAM_THUMBNAIL_WIDTH,
            "height": _IDEOGRAM_THUMBNAIL_HEIGHT,
            "sampler_preset": "4.0 Quality 48",
            "prompt_mode": str(prompt_result.get("prompt_mode", "quick")),
            "aspect_ratio": "16:9",
            "ideogram_prompt_json": prompt_json,
            "protected_text": prompt_result["protected_text"],
            "prompt_warnings": prompt_result["warnings"],
        }
        backend_name = "ideogram4_local_comfyui"
        backend = self.pipeline.registry.get(backend_name)
        if self.pipeline.config.backends.ideogram4_local.managed:
            self.pipeline.ideogram_worker.ensure_running()
        reusing_resident = self.pipeline._prepare_comfy_backend(backend_name)
        if not reusing_resident:
            self.pipeline._check_ideogram4_vram()
        request = GenerationRequest(
            job_id=job_id,
            output_dir=directory,
            prompt=prompt_json["high_level_description"],
            # Ideogram4Generate has no negative-prompt input. All exclusions
            # are encoded in the structured JSON caption above.
            negative_prompt="",
            seed=seed,
            width=_IDEOGRAM_THUMBNAIL_WIDTH,
            height=_IDEOGRAM_THUMBNAIL_HEIGHT,
            settings={
                "kind": "image",
                "workflow": self.pipeline.IDEOGRAM4_THUMBNAIL_WORKFLOW,
                "workflow_version": _IDEOGRAM_THUMBNAIL_WORKFLOW_VERSION,
                "substitutions": {"prompt_json": serialized_prompt},
            },
        )
        try:
            backend.load()
            generated = backend.generate(request)
            self.pipeline._resident_comfy_backend = backend_name
            return GenerationResult(
                outputs=generated.outputs,
                metadata={
                    **dict(generated.metadata),
                    "prompt": request.prompt,
                    "negative_prompt": request.negative_prompt,
                    "seed": seed,
                    "workflow_version": _IDEOGRAM_THUMBNAIL_WORKFLOW_VERSION,
                    "settings": parameters,
                },
                peak_vram_gb=generated.peak_vram_gb,
            )
        except Exception as exc:
            descriptor = backend.descriptor()
            self.pipeline.database.save_attempt(GenerationAttempt(
                job_id=job_id,
                backend=descriptor.backend_name,
                model=descriptor.model_name,
                model_version=descriptor.model_version,
                quantization=descriptor.quantization,
                workflow_version=_IDEOGRAM_THUMBNAIL_WORKFLOW_VERSION,
                parameters=parameters,
                seed=seed,
                success=False,
                error=redact_secrets(exc),
            ))
            self.pipeline._release_comfyui_memory(
                backend_name=backend_name, wait_for_vram=False, suppress_errors=True,
            )
            raise

    def _source_artwork(
        self, project: Project, asset_id: str, output: Path, plan: ThumbnailPlan,
        seed: int,
    ) -> GenerationResult:
        asset = self.pipeline.database.get_asset(asset_id)
        if asset is None or asset.project_id != project.id:
            raise ValueError("source thumbnail asset was not found in this project")
        source = (self.pipeline.store.project_path(project) / asset.filepath).resolve()
        root = self.pipeline.store.project_path(project).resolve()
        if root not in source.parents or not source.is_file():
            raise ValueError("source thumbnail file is unavailable")
        with Image.open(source) as image:
            ImageOps.fit(image.convert("RGB"), (1280, 720), Image.Resampling.LANCZOS).save(
                output, format="PNG"
            )
        return GenerationResult(
            outputs=(output,),
            metadata={
                "backend": "local_asset",
                "model": asset.model,
                "model_version": asset.model_version,
                "quantization": asset.quantization,
                "workflow_version": "thumbnail-source-promotion-v1",
                "seed": seed,
                "prompt": plan.concept.prompt,
                "negative_prompt": plan.concept.avoid_prompt,
                "settings": {"source_asset_id": asset.id},
            },
            peak_vram_gb=0,
        )

    def _source_candidate_artwork(
        self, project: Project, candidate_id: str, output: Path, plan: ThumbnailPlan,
        seed: int,
    ) -> GenerationResult:
        source = (
            self.pipeline.store.project_path(project)
            / "thumbnails" / candidate_id / "artwork.png"
        )
        if candidate_id not in _CANDIDATE_IDS or not source.is_file():
            raise ValueError("source thumbnail candidate is not available")
        shutil.copyfile(source, output)
        return GenerationResult(
            outputs=(output,),
            metadata={
                "backend": "local_asset",
                "model": "Thumbnail candidate duplicate",
                "model_version": "1",
                "workflow_version": "thumbnail-duplicate-v1",
                "seed": seed,
                "prompt": plan.concept.prompt,
                "negative_prompt": plan.concept.avoid_prompt,
                "settings": {"source_candidate_id": candidate_id},
            },
            peak_vram_gb=0,
        )

    @staticmethod
    def _normalize_artwork(path: Path) -> None:
        with Image.open(path) as image:
            normalized = ImageOps.fit(
                image.convert("RGB"), (1280, 720), Image.Resampling.LANCZOS,
            )
            normalized.save(path, format="PNG", optimize=True)

    def _attempt_history(self, project: Project, candidate_id: str) -> list[dict[str, Any]]:
        manifest = (
            self.pipeline.store.project_path(project)
            / "thumbnails" / candidate_id / "manifest.json"
        )
        if not manifest.is_file():
            return []
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            history = payload.get("attempt_history", [])
            return history if isinstance(history, list) else []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _append_failed_history(
        self, project: Project, candidate_id: str, job_id: str, error: BaseException,
    ) -> None:
        manifest = (
            self.pipeline.store.project_path(project)
            / "thumbnails" / candidate_id / "manifest.json"
        )
        if not manifest.is_file():
            return
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            history = payload.get("attempt_history", [])
            payload["attempt_history"] = [
                *(history if isinstance(history, list) else []),
                {
                    "job_id": job_id,
                    "status": "failed",
                    "error": redact_secrets(error),
                    "created_at": utc_now().isoformat(),
                },
            ]
            self.pipeline._atomic_json(manifest, payload)
        except (OSError, ValueError, json.JSONDecodeError):
            return

    def _mark_candidates_stale(
        self, project: Project, *, raise_on_error: bool = False,
    ) -> None:
        root = self.pipeline.store.project_path(project) / "thumbnails"
        for manifest in root.glob("candidate-*/manifest.json"):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload["stale"] = True
                payload["stale_reason"] = "thumbnail plan changed"
                self.pipeline._atomic_json(manifest, payload)
            except (OSError, ValueError, json.JSONDecodeError):
                if raise_on_error:
                    raise
                continue

    def _publish(self, project: Project, candidate_id: str, temporary: Path) -> None:
        root = self.pipeline.store.project_path(project)
        target = root / "thumbnails" / candidate_id
        archived: Path | None = None
        if target.exists():
            archived = (
                root / "variants" / "archive"
                / f"thumbnail-{candidate_id}-{utc_now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
            )
            archived.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, archived)
        try:
            os.replace(temporary, target)
        except Exception:
            if archived is not None and archived.exists() and not target.exists():
                os.replace(archived, target)
            raise

    def _archive_selection(self, project: Project) -> None:
        selected = self.pipeline.store.project_path(project) / "thumbnails" / "selected.json"
        if not selected.is_file():
            return
        archive = (
            self.pipeline.store.project_path(project) / "variants" / "archive"
            / f"thumbnail-selection-{utc_now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}.json"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        os.replace(selected, archive)
