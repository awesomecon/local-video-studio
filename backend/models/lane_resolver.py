"""Shot-scope lane/backend capability resolution.

Maps an editorial ``(ShotLane, VisualType)`` pair onto the enabled local
backend or deterministic handler that implements it, validating declared
capabilities and readiness BEFORE any job would queue. In real mode every
selectable visual type must resolve to a real implementation or fail with a
structured error; unwired modes never fall through to mock generation.

This module is self-contained so ``PipelineService`` can call it from the
``POST /api/shots/{id}/generate|regenerate`` dispatch path:

    target = resolve_lane_target(shot, service.registry, mock_mode=service.mock_mode)
    # LaneResolutionError carries code/details for a structured HTTP 409/422.

Mock mode keeps the mock pipeline working: every visual type resolves to the
registered ``mock`` backend without network or GPU access.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from backend.models.base import Capability, GeneratorBackend
from backend.schemas.models import VisualType
from backend.schemas.shots import Shot, ShotLane


class LaneErrorCode(StrEnum):
    LANE_VISUAL_TYPE_MISMATCH = "lane_visual_type_mismatch"
    UNRESOLVED_VISUAL_TYPE = "unresolved_visual_type"
    BACKEND_NOT_FOUND = "backend_not_found"
    CAPABILITY_MISMATCH = "capability_mismatch"
    BACKEND_NOT_READY = "backend_not_ready"
    HANDLER_UNAVAILABLE = "handler_unavailable"


class LaneResolutionError(RuntimeError):
    """Structured, pre-queue failure of lane/backend resolution."""

    def __init__(
        self,
        message: str,
        code: LaneErrorCode | str,
        *,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code.value if isinstance(code, LaneErrorCode) else str(code)
        self.retryable = retryable
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True, slots=True)
class LaneTarget:
    """A wired implementation for one visual type inside one lane."""

    backend_name: str | None  # None => deterministic handler
    capability: Capability | None
    handler: str | None = None  # e.g. "graphic_screen", "imported_media"


#: Deterministic, local, generation-free handlers that are actually executable
#: today. A LaneTarget naming a handler absent from this table resolves as
#: unavailable with a structured pre-queue error rather than being advertised.
DETERMINISTIC_HANDLERS: dict[str, str] = {
    "graphic_screen": (
        "Local sanitized HTML/Chromium Graphic Screen renderer (deterministic; "
        "no model weights)."
    ),
    "imported_media": (
        "Local, user-attached image or video with a source title and optional rights metadata; "
        "normalized by FFmpeg only during scene rendering."
    ),
}

#: Handler targets that are declared but NOT executable yet. They stay in
#: LANE_TARGETS so resolution fails with a precise, structured error instead
#: of silently pretending the lane works.
PENDING_HANDLERS: dict[str, str] = {}

#: The wired real-mode targets per lane. Only visual types with a working
#: backend target AND a shot-scope request builder are listed; anything absent
#: fails clearly instead of falling through to mock generation.
LANE_TARGETS: dict[ShotLane, dict[VisualType, LaneTarget]] = {
    ShotLane.REAL: {
        VisualType.REUSED_MEDIA: LaneTarget(None, None, "imported_media"),
    },
    ShotLane.IMAGE: {
        VisualType.TEXT_OVERLAY_STILL: LaneTarget(
            "krea2_comfyui", Capability.TEXT_TO_IMAGE,
        ),
        VisualType.KREA2_STILL: LaneTarget("krea2_comfyui", Capability.TEXT_TO_IMAGE),
        VisualType.IDEOGRAM4_STILL: LaneTarget(
            "ideogram4_local_comfyui", Capability.TEXT_TO_IMAGE,
        ),
        VisualType.QWEN_IMAGE_STILL: LaneTarget(
            "qwen_image_2512_comfyui", Capability.TEXT_TO_IMAGE,
        ),
        VisualType.IMAGE_MOTION: LaneTarget(
            "krea2_comfyui", Capability.TEXT_TO_IMAGE,
        ),
        # FLUX_STILL is intentionally absent: no shot-scope request builder
        # exists for it, so it must not be advertised as wired.
    },
    ShotLane.H3: {
        VisualType.H3_AUDIOVISUAL: LaneTarget("comfyui", Capability.TEXT_TO_VIDEO),
        VisualType.H3_REFERENCE: LaneTarget("comfyui", Capability.IMAGE_TO_VIDEO),
        # WAN_VIDEO is intentionally absent: no shot-scope request builder
        # exists for it, so it must not be advertised as wired.
    },
    ShotLane.HTML: {
        VisualType.GRAPHIC_SCREEN: LaneTarget(None, None, "graphic_screen"),
        VisualType.TITLE_CARD: LaneTarget(None, None, "graphic_screen"),
        VisualType.DIAGRAM: LaneTarget(None, None, "graphic_screen"),
    },
}

MOCK_BACKEND_NAME = "mock"

_VIDEO_OUTPUT_TYPES = frozenset({
    VisualType.H3_AUDIOVISUAL,
    VisualType.H3_REFERENCE,
    VisualType.WAN_VIDEO,
})
_AUTOMATIC_BACKENDS = frozenset({"", "automatic"})


def describe_lane_targets() -> dict[str, Any]:
    """Machine-readable summary of wired targets for API/UI discovery.

    Deterministic handlers report ``available`` so pending targets (such as
    REAL imported media) are visible without being advertised as usable.
    """
    return {
        lane.value: {
            visual_type.value: (
                {
                    "kind": "deterministic",
                    "handler": target.handler,
                    "available": target.handler in DETERMINISTIC_HANDLERS,
                }
                if target.backend_name is None
                else {
                    "kind": "backend",
                    "backend": target.backend_name,
                    "capability": target.capability.value if target.capability else None,
                    "available": True,
                }
            )
            for visual_type, target in targets.items()
        }
        for lane, targets in LANE_TARGETS.items()
    }


def _mock_capability(visual_type: VisualType) -> Capability:
    if visual_type in _VIDEO_OUTPUT_TYPES:
        return Capability.TEXT_TO_VIDEO
    if visual_type is VisualType.REUSED_MEDIA:
        return Capability.IMAGE_TO_IMAGE
    return Capability.TEXT_TO_IMAGE


def _require_lane_visual_match(
    lane: ShotLane, visual_type: VisualType,
) -> dict[VisualType, LaneTarget]:
    targets = LANE_TARGETS.get(lane, {})
    if visual_type not in targets:
        supported = sorted(item.value for item in targets)
        raise LaneResolutionError(
            f"Visual type {visual_type.value!r} does not belong to the {lane.value!r} lane; "
            f"supported types for this lane are: {', '.join(supported)}.",
            LaneErrorCode.LANE_VISUAL_TYPE_MISMATCH,
            details={
                "lane": lane.value,
                "visual_type": visual_type.value,
                "supported": supported,
            },
        )
    return targets


def _fetch_backend(
    registry: Mapping[str, GeneratorBackend] | Any, name: str,
) -> GeneratorBackend:
    """Fetch by name, treating both KeyError and None as 'not registered'."""
    getter = getattr(registry, "get", None)
    try:
        backend = getter(name) if callable(getter) else registry[name]
    except KeyError:
        backend = None
    if backend is None:
        raise LaneResolutionError(
            f"Backend {name!r} is not registered; install/configure it before "
            "queueing this shot.",
            LaneErrorCode.BACKEND_NOT_FOUND,
            details={"backend": name},
        )
    return backend


def _resolve_selected_backend(
    shot: Shot, spec: LaneTarget, registry: Mapping[str, GeneratorBackend] | Any,
) -> GeneratorBackend:
    selected = (shot.selected_backend or "").strip().lower()
    designated = spec.backend_name or ""
    if selected in _AUTOMATIC_BACKENDS:
        return _fetch_backend(registry, designated)
    if selected != designated:
        raise LaneResolutionError(
            f"Selected backend {selected!r} cannot implement visual type "
            f"{shot.visual_type.value!r}, which requires {designated!r}.",
            LaneErrorCode.CAPABILITY_MISMATCH,
            details={
                "selected_backend": selected,
                "required_backend": designated,
                "required_capability": spec.capability.value if spec.capability else None,
                "shot_id": shot.id,
            },
        )
    return _fetch_backend(registry, selected)


def _validate_declared_capabilities(
    backend: GeneratorBackend, spec: LaneTarget, shot: Shot,
) -> None:
    capability = spec.capability
    if capability is None or capability in backend.capabilities():
        return
    declared = sorted(item.value for item in backend.capabilities())
    raise LaneResolutionError(
        f"Backend {backend.descriptor().backend_name!r} does not declare the "
        f"{capability.value!r} capability required by visual type "
        f"{shot.visual_type.value!r} (declared: {', '.join(declared) or 'none'}).",
        LaneErrorCode.CAPABILITY_MISMATCH,
        details={
            "backend": backend.descriptor().backend_name,
            "required_capability": capability.value,
            "declared_capabilities": declared,
            "shot_id": shot.id,
        },
    )


def _check_readiness(backend: GeneratorBackend) -> None:
    health: Mapping[str, Any]
    try:
        health = backend.health()
    except Exception as exc:
        raise LaneResolutionError(
            f"Backend {backend.descriptor().backend_name!r} failed its readiness check: {exc}",
            LaneErrorCode.BACKEND_NOT_READY,
            retryable=True,
            details={"backend": backend.descriptor().backend_name},
        ) from exc
    status = str(health.get("status", ""))
    if status == "healthy":
        return
    guidance = health.get("install_guidance")
    message = (
        f"Backend {backend.descriptor().backend_name!r} is not ready "
        f"(status: {status or 'unknown'})."
    )
    if guidance:
        message = f"{message} {guidance}"
    raise LaneResolutionError(
        message,
        LaneErrorCode.BACKEND_NOT_READY,
        retryable=status == "unhealthy",
        details={"backend": backend.descriptor().backend_name, "health": dict(health)},
    )


@dataclass(frozen=True, slots=True)
class ResolvedLaneTarget:
    """The validated execution target for one shot's visual."""

    shot_id: str
    lane: ShotLane
    visual_type: VisualType
    kind: str  # "backend" | "deterministic"
    backend: GeneratorBackend | None = None
    backend_name: str | None = None
    capability: Capability | None = None
    handler: str | None = None

    @property
    def uses_gpu(self) -> bool:
        return bool(self.backend and self.backend.descriptor().heavyweight)


def resolve_lane_target(
    shot: Shot,
    registry: Mapping[str, GeneratorBackend] | Any,
    *,
    mock_mode: bool = False,
    check_readiness: bool = True,
) -> ResolvedLaneTarget:
    """Resolve a shot's lane/visual type to a validated local execution target.

    Raises :class:`LaneResolutionError` before any job would be queued when the
    lane/visual combination is unwired, the selected backend mismatches the
    declared capabilities, or the backend fails readiness.
    """
    lane = shot.lane if isinstance(shot.lane, ShotLane) else ShotLane(shot.lane)
    visual_type = (
        shot.visual_type
        if isinstance(shot.visual_type, VisualType)
        else VisualType(shot.visual_type)
    )

    if mock_mode:
        backend = _fetch_backend(registry, MOCK_BACKEND_NAME)
        return ResolvedLaneTarget(
            shot_id=shot.id,
            lane=lane,
            visual_type=visual_type,
            kind="backend",
            backend=backend,
            backend_name=MOCK_BACKEND_NAME,
            capability=_mock_capability(visual_type),
        )

    targets = _require_lane_visual_match(lane, visual_type)
    spec = targets[visual_type]

    if spec.handler is not None:
        guidance = DETERMINISTIC_HANDLERS.get(spec.handler)
        if guidance is None:
            pending_note = PENDING_HANDLERS.get(
                spec.handler, "No executable handler is wired for it yet."
            )
            raise LaneResolutionError(
                f"The {spec.handler!r} handler for visual type "
                f"{visual_type.value!r} is not available on this installation. "
                f"{pending_note}",
                LaneErrorCode.HANDLER_UNAVAILABLE,
                details={
                    "handler": spec.handler,
                    "shot_id": shot.id,
                    "lane": lane.value,
                    "visual_type": visual_type.value,
                },
            )
        return ResolvedLaneTarget(
            shot_id=shot.id,
            lane=lane,
            visual_type=visual_type,
            kind="deterministic",
            handler=spec.handler,
        )

    backend = _resolve_selected_backend(shot, spec, registry)
    _validate_declared_capabilities(backend, spec, shot)
    if check_readiness:
        _check_readiness(backend)
    return ResolvedLaneTarget(
        shot_id=shot.id,
        lane=lane,
        visual_type=visual_type,
        kind="backend",
        backend=backend,
        backend_name=backend.descriptor().backend_name,
        capability=spec.capability,
    )
