"""Shot-scope generation request builders for Krea 2, Qwen-Image-2512, and H3.

Each builder takes a validated ``backend.schemas.shots.Shot`` plus the project
canvas/fps context and produces a :class:`ShotGenerationPlan`: the
``GenerationRequest`` payload for its backend, the deterministic cache-key
payload (including typed reference-asset SHA-256 hashes), and provenance
metadata for the resulting asset row.

Typed ``shot.reference_assets`` roles (``source_evidence | composition | style
| character | first_frame | continuity``) are resolved to project-contained
files with content hashes. A backend that cannot condition on a reference
fails clearly instead of silently ignoring it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from backend.core.h3_policy import (
    CONTINUATION_WORKFLOW_VERSION,
    FIRST_SHOT_WORKFLOW_VERSION,
    h3_effective_duration,
    h3_frame_count,
    resolve_quality,
    validate_duration,
)
from backend.models.base import GenerationRequest
from backend.models.h3_shot_continuity import H3_NATIVE_AUDIO_MIX_POLICY
from backend.models.lane_resolver import LaneResolutionError, LaneErrorCode
from backend.schemas.models import Asset, Project, VisualType
from backend.schemas.shots import ReferenceRole, Shot

REPO_ROOT = Path(__file__).resolve().parents[2]

KREA2_WORKFLOW_PATH = REPO_ROOT / "workflows" / "comfyui" / "krea2-turbo.workflow.json"
QWEN_IMAGE_2512_WORKFLOW_PATH = REPO_ROOT / "workflows" / "comfyui" / "qwen-image-2512.workflow.json"
H3_WORKFLOW_PATH = REPO_ROOT / "workflows" / "comfyui" / "minimax-h3-av.workflow.json"
H3_FIRST_FRAME_WORKFLOW_PATH = (
    REPO_ROOT / "workflows" / "comfyui" / "minimax-h3-av-first-frame.workflow.json"
)

KREA2_WORKFLOW_VERSION = "krea2-turbo-fp8-v1"
QWEN_IMAGE_2512_WORKFLOW_VERSION = "qwen-image-2512-fp8-v1"

#: Reference roles that can condition an H3 generation through the first-frame
#: input slot. Every other role would be silently ignored by the wired
#: workflows and therefore must fail instead of passing validation.
H3_SUPPORTED_REFERENCE_ROLES = frozenset({
    ReferenceRole.FIRST_FRAME,
    ReferenceRole.CONTINUITY,
})
_STILL_IMAGE_REFERENCE_ROLES: frozenset[ReferenceRole] = frozenset()


class ShotRequestError(ValueError):
    """Structured failure while building a shot generation request."""

    def __init__(self, message: str, code: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            result["details"] = self.details
        return result


def _project_relative(path: Path, project_root: Path) -> Path:
    try:
        return path.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise ShotRequestError(
            f"Reference asset {path} escapes the project directory.",
            "reference_outside_project",
            details={"path": str(path)},
        ) from exc


def compute_file_sha256(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(block_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    """A typed reference asset resolved to a contained file plus its hash."""

    role: ReferenceRole
    asset_id: str
    path: Path  # absolute on disk; never persisted raw
    sha256: str

    @property
    def cache_entry(self) -> dict[str, str]:
        return {"role": self.role.value, "asset_id": self.asset_id, "sha256": self.sha256}


def resolve_reference_assets(
    shot: Shot,
    assets_by_id: Mapping[str, Asset],
    project_root: Path,
    *,
    hasher=compute_file_sha256,
) -> tuple[ResolvedReference, ...]:
    """Resolve typed reference roles to hashed, project-contained files."""
    resolved: list[ResolvedReference] = []
    for reference in shot.reference_assets:
        asset = assets_by_id.get(reference.asset_id)
        if asset is None or asset.project_id != shot.project_id:
            raise ShotRequestError(
                f"{reference.role.value} reference {reference.asset_id!r} is not an "
                "asset of this project.",
                "reference_unknown_asset",
                details={"role": reference.role.value, "asset_id": reference.asset_id},
            )
        absolute = (project_root / asset.filepath).resolve()
        if not absolute.is_file() or absolute.stat().st_size == 0:
            raise ShotRequestError(
                f"{reference.role.value} reference asset {reference.asset_id!r} has no "
                f"readable media file at {asset.filepath}.",
                "reference_missing_media",
                details={"role": reference.role.value, "asset_id": reference.asset_id},
            )
        _project_relative(absolute, project_root)
        resolved.append(ResolvedReference(
            role=reference.role,
            asset_id=reference.asset_id,
            path=absolute,
            sha256=hasher(absolute),
        ))
    resolved.sort(key=lambda item: (item.role.value, item.asset_id))
    return tuple(resolved)


@dataclass(frozen=True, slots=True)
class ShotGenerationPlan:
    """Everything a dispatcher needs to execute one shot's generation."""

    request: GenerationRequest
    cache_payload: dict[str, Any]
    provenance: dict[str, Any]


def _base_provenance(
    shot: Shot, project: Project, backend_identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "shot_id": shot.id,
        "scene_id": shot.scene_id,
        "lane": shot.lane.value if hasattr(shot.lane, "value") else str(shot.lane),
        "visual_type": (
            shot.visual_type.value if hasattr(shot.visual_type, "value") else str(shot.visual_type)
        ),
        "fps": project.fps,
        **dict(backend_identity),
    }


def _reference_cache_entries(references: tuple[ResolvedReference, ...]) -> list[dict[str, str]]:
    return [item.cache_entry for item in references]


def _reject_unsupported_references(
    shot: Shot,
    references: tuple[ResolvedReference, ...],
    supported: frozenset[ReferenceRole],
    backend_label: str,
) -> None:
    for reference in references:
        if reference.role in supported:
            continue
        raise ShotRequestError(
            f"{backend_label} cannot condition on {reference.role.value!r} references; "
            f"supported roles here are: "
            f"{', '.join(sorted(role.value for role in supported)) or 'none'}. Remove the "
            "reference or choose a backend that honors it.",
            "reference_role_unsupported",
            details={
                "role": reference.role.value,
                "asset_id": reference.asset_id,
                "backend": backend_label,
            },
        )


def _parse_wxh_override(
    value: str, label: str, *, min_side: int, max_megapixels: float | None,
) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text.strip()), int(height_text.strip())
    except (ValueError, AttributeError) as exc:
        raise ShotRequestError(
            f"{label} canvas override {value!r} is not a 'WIDTHxHEIGHT' pair.",
            "invalid_canvas",
        ) from exc
    if min(width, height) < min_side:
        raise ShotRequestError(
            f"{label} canvas override {value!r} must be at least {min_side} px per side.",
            "invalid_canvas",
        )
    if width % 16 or height % 16:
        raise ShotRequestError(
            f"{label} canvas override {value!r} must be aligned to 16 px.",
            "invalid_canvas",
        )
    if max_megapixels is not None and width * height > max_megapixels * 1_000_000:
        raise ShotRequestError(
            f"{label} canvas override {value!r} exceeds the safe "
            f"{max_megapixels:g}-megapixel preset cap.",
            "invalid_canvas",
        )
    return width, height


def krea2_canvas(settings: Mapping[str, Any], project_resolution: tuple[int, int]) -> tuple[int, int]:
    """Krea 2 Turbo canvas: explicit override or the project aspect rule."""
    override = settings.get("krea_canvas")
    if override and str(override).strip().lower() != "auto":
        return _parse_wxh_override(
            str(override), "Krea 2", min_side=256, max_megapixels=1.0,
        )
    width, height = project_resolution
    if width > height:
        return 1344, 768
    if height > width:
        return 768, 1344
    return 1024, 1024


def qwen_image_canvas(
    settings: Mapping[str, Any], project_resolution: tuple[int, int],
) -> tuple[int, int]:
    """Qwen-Image-2512 canvas: explicit override or the project aspect rule."""
    override = settings.get("qwen_image_canvas")
    if override and str(override).strip().lower() != "auto":
        return _parse_wxh_override(
            str(override), "Qwen Image", min_side=256, max_megapixels=1.8,
        )
    width, height = project_resolution
    if width > height:
        return 1664, 928
    if height > width:
        return 928, 1664
    return 1328, 1328


def build_krea2_request(
    shot: Shot,
    project: Project,
    output_dir: Path,
    *,
    backend_identity: Mapping[str, Any],
    job_id: str | None = None,
    references: tuple[ResolvedReference, ...] = (),
) -> ShotGenerationPlan:
    """Build the Krea 2 Turbo text-to-image request for one shot.

    The distilled Krea recipe runs at CFG 1.0 and cannot condition on image
    inputs; typed references therefore fail loudly instead of being dropped.
    """
    _reject_unsupported_references(shot, references, _STILL_IMAGE_REFERENCE_ROLES, "krea2_comfyui")
    width, height = krea2_canvas(shot.settings, project.resolution)
    prompt = shot.visual_prompt.strip()
    negative = shot.negative_prompt.strip()
    parameters = {
        "kind": "image",
        "shot_id": shot.id,
        "width": width,
        "height": height,
        "steps": 8,
        "cfg": 1.0,
        "sampler": "er_sde",
        "scheduler": "simple",
    }
    request = GenerationRequest(
        job_id=job_id or f"{project.id}:shot-krea2:{shot.id}:{shot.seed}",
        output_dir=output_dir,
        prompt=prompt,
        negative_prompt=negative,
        seed=shot.seed,
        width=width,
        height=height,
        settings={
            "kind": "image",
            "workflow": KREA2_WORKFLOW_PATH,
            "workflow_version": KREA2_WORKFLOW_VERSION,
            "shot_id": shot.id,
            "scene_id": shot.scene_id,
        },
    )
    cache_payload: dict[str, Any] = {
        "kind": "krea2_image",
        "workflow_version": KREA2_WORKFLOW_VERSION,
        **dict(backend_identity),
        "prompt": prompt,
        "negative_prompt": negative,
        "seed": shot.seed,
        "width": width,
        "height": height,
        "steps": parameters["steps"],
        "cfg": parameters["cfg"],
        "sampler": parameters["sampler"],
        "scheduler": parameters["scheduler"],
        "references": [],
    }
    provenance = {
        **_base_provenance(shot, project, backend_identity),
        **parameters,
        "workflow_version": KREA2_WORKFLOW_VERSION,
        "references": _reference_cache_entries(()),
    }
    return ShotGenerationPlan(request=request, cache_payload=cache_payload, provenance=provenance)


def build_qwen_image_request(
    shot: Shot,
    project: Project,
    output_dir: Path,
    *,
    backend_identity: Mapping[str, Any],
    job_id: str | None = None,
    references: tuple[ResolvedReference, ...] = (),
) -> ShotGenerationPlan:
    """Build the Qwen-Image-2512 request for one shot.

    ``settings.on_screen_text`` entries are appended as exact quoted render
    instructions, mirroring scene-scope behavior; mission-critical wording
    should still travel through HTML overlays.
    """
    _reject_unsupported_references(
        shot, references, _STILL_IMAGE_REFERENCE_ROLES, "qwen_image_2512_comfyui",
    )
    width, height = qwen_image_canvas(shot.settings, project.resolution)
    prompt = shot.visual_prompt.strip()
    raw_text = shot.settings.get("on_screen_text", [])
    if not isinstance(raw_text, list):
        raw_text = []
    requested_text = [str(item).strip() for item in raw_text if str(item).strip()]
    if requested_text:
        literals = "\n".join(
            f"- {json.dumps(item, ensure_ascii=False)}" for item in requested_text
        )
        prompt = (
            f"{prompt}\nRender each of these quoted strings exactly once, with clear, "
            f"legible spelling:\n{literals}"
        )
    negative = shot.negative_prompt.strip()
    parameters = {
        "kind": "image",
        "shot_id": shot.id,
        "width": width,
        "height": height,
        "steps": 50,
        "cfg": 4.0,
        "sampler": "euler",
        "scheduler": "simple",
        "model_sampling_shift": 3.1,
        "on_screen_text": requested_text,
    }
    request = GenerationRequest(
        job_id=job_id or f"{project.id}:shot-qwen-image-2512:{shot.id}:{shot.seed}",
        output_dir=output_dir,
        prompt=prompt,
        negative_prompt=negative,
        seed=shot.seed,
        width=width,
        height=height,
        settings={
            "kind": "image",
            "workflow": QWEN_IMAGE_2512_WORKFLOW_PATH,
            "workflow_version": QWEN_IMAGE_2512_WORKFLOW_VERSION,
            "shot_id": shot.id,
            "scene_id": shot.scene_id,
        },
    )
    cache_payload: dict[str, Any] = {
        "kind": "qwen_image_2512",
        "workflow_version": QWEN_IMAGE_2512_WORKFLOW_VERSION,
        **dict(backend_identity),
        "prompt": prompt,
        "negative_prompt": negative,
        "seed": shot.seed,
        "width": width,
        "height": height,
        "steps": parameters["steps"],
        "cfg": parameters["cfg"],
        "sampler": parameters["sampler"],
        "scheduler": parameters["scheduler"],
        "model_sampling_shift": parameters["model_sampling_shift"],
        "on_screen_text": requested_text,
        "references": [],
    }
    provenance = {
        **_base_provenance(shot, project, backend_identity),
        **parameters,
        "workflow_version": QWEN_IMAGE_2512_WORKFLOW_VERSION,
        "references": _reference_cache_entries(()),
    }
    return ShotGenerationPlan(request=request, cache_payload=cache_payload, provenance=provenance)


def build_h3_request(
    shot: Shot,
    project: Project,
    output_dir: Path,
    *,
    backend_identity: Mapping[str, Any],
    job_id: str | None = None,
    references: tuple[ResolvedReference, ...] = (),
) -> ShotGenerationPlan:
    """Build the MiniMax H3 audiovisual request for one shot.

    Continuity is expressed with typed references: ``first_frame`` /
    ``continuity`` feed the first-frame conditioning slot of the continuation
    workflow. Native H3 audio stays muted (``mix_policy: mute``); enabling it
    is an explicit per-shot edit, never a builder default.
    """
    _reject_unsupported_references(shot, references, H3_SUPPORTED_REFERENCE_ROLES, "minimax_h3")
    image_references = [
        item for item in references
        if item.role in H3_SUPPORTED_REFERENCE_ROLES
    ]
    if len(image_references) > 1:
        raise ShotRequestError(
            "An H3 shot accepts at most one first-frame reference; got "
            f"{len(image_references)} ({', '.join(item.role.value for item in image_references)}).",
            "too_many_first_frame_references",
            details={"roles": [item.role.value for item in image_references]},
        )
    resolution = resolve_quality(shot.settings, project.resolution)
    validate_duration(resolution, shot.duration_seconds)
    frames = h3_frame_count(shot.duration_seconds)
    conditioned = bool(image_references)
    workflow_path = H3_FIRST_FRAME_WORKFLOW_PATH if conditioned else H3_WORKFLOW_PATH
    workflow_version = (
        CONTINUATION_WORKFLOW_VERSION if conditioned else FIRST_SHOT_WORKFLOW_VERSION
    )
    prompt = shot.visual_prompt.strip()
    negative = shot.negative_prompt.strip()
    provenance: dict[str, Any] = {
        **_base_provenance(shot, project, backend_identity),
        "kind": "video",
        "preset": resolution.quality,
        "canvas": list(resolution.canvas),
        "requested_seconds": shot.duration_seconds,
        "effective_frames": frames,
        "effective_seconds": h3_effective_duration(frames),
        "long_shot": resolution.long_shot,
        "workflow_version": workflow_version,
        "native_audio_mix_policy": H3_NATIVE_AUDIO_MIX_POLICY.value,
        "conditioned": conditioned,
        "references": _reference_cache_entries(references),
    }
    request = GenerationRequest(
        job_id=job_id or f"{project.id}:shot-h3:{shot.id}:{shot.seed}",
        output_dir=output_dir,
        prompt=prompt,
        negative_prompt=negative,
        seed=shot.seed,
        duration_seconds=shot.duration_seconds,
        width=resolution.canvas[0],
        height=resolution.canvas[1],
        references=tuple(item.path for item in image_references),
        settings={
            "kind": "video",
            "workflow": workflow_path,
            "substitutions": {"length": frames},
            "workflow_version": workflow_version,
            "preset": resolution.quality,
            "long_shot": resolution.long_shot,
            "shot_id": shot.id,
            "scene_id": shot.scene_id,
            "native_audio_mix_policy": H3_NATIVE_AUDIO_MIX_POLICY.value,
        },
    )
    cache_payload: dict[str, Any] = {
        "kind": "h3_video",
        "workflow_version": workflow_version,
        **dict(backend_identity),
        "prompt": prompt,
        "negative_prompt": negative,
        "seed": shot.seed,
        "requested_seconds": shot.duration_seconds,
        "frames": frames,
        "canvas": list(resolution.canvas),
        "preset": resolution.quality,
        "long_shot": resolution.long_shot,
        "native_audio_mix_policy": H3_NATIVE_AUDIO_MIX_POLICY.value,
        "references": _reference_cache_entries(references),
    }
    return ShotGenerationPlan(request=request, cache_payload=cache_payload, provenance=provenance)


BUILDER_BACKEND_NAMES: dict[VisualType, str] = {
    VisualType.KREA2_STILL: "krea2_comfyui",
    VisualType.IMAGE_MOTION: "krea2_comfyui",
    VisualType.QWEN_IMAGE_STILL: "qwen_image_2512_comfyui",
    VisualType.H3_AUDIOVISUAL: "comfyui",
    VisualType.H3_REFERENCE: "comfyui",
}


def build_shot_request(
    shot: Shot,
    project: Project,
    output_dir: Path,
    *,
    backend_identity: Mapping[str, Any],
    references: tuple[ResolvedReference, ...] = (),
    job_id: str | None = None,
) -> ShotGenerationPlan:
    """Dispatch to the builder matching the shot's resolved visual type."""
    visual_type = (
        shot.visual_type
        if isinstance(shot.visual_type, VisualType)
        else VisualType(shot.visual_type)
    )
    builder = {
        VisualType.KREA2_STILL: build_krea2_request,
        VisualType.IMAGE_MOTION: build_krea2_request,
        VisualType.QWEN_IMAGE_STILL: build_qwen_image_request,
        VisualType.H3_AUDIOVISUAL: build_h3_request,
        VisualType.H3_REFERENCE: build_h3_request,
    }.get(visual_type)
    if builder is None:
        raise LaneResolutionError(
            f"No shot-scope request builder exists for visual type {visual_type.value!r}.",
            LaneErrorCode.UNRESOLVED_VISUAL_TYPE,
            details={"visual_type": visual_type.value, "shot_id": shot.id},
        )
    return builder(
        shot, project, output_dir,
        backend_identity=backend_identity, job_id=job_id, references=references,
    )
