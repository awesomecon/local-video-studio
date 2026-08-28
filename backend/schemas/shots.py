"""Shot-level domain models for multi-shot scenes.

A shot is one sequential visual beat inside a narration scene. The lane
(`real | image | h3 | html`) describes the editorial truth/source policy while
``visual_type`` keeps describing the production implementation, so the
REAL/IMAGE/H3/HTML distinction never overloads ``VisualType``.

Overlay and audio cues are embedded in the shot payload in this first
implementation; provenance travels with the shot through ``MediaSource``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Sequence

from pydantic import Field, model_validator

from .models import DomainModel, Scene, VisualType, new_id, utc_now

IMPLICIT_SHOT_SUFFIX = "-implicit"
MAX_SHOT_SECONDS = 3600.0


class ShotTimingError(ValueError):
    """Shot timings cannot be fitted to the scene clock."""


class ShotLane(StrEnum):
    REAL = "real"
    IMAGE = "image"
    H3 = "h3"
    HTML = "html"


class ShotStartMode(StrEnum):
    FIXED = "fixed"
    WEIGHTED = "weighted"


class ShotTransitionKind(StrEnum):
    CUT = "cut"
    CROSSFADE = "crossfade"
    DISSOLVE = "dissolve"  # stored alias of crossfade; compiled identically
    FADE_THROUGH_BLACK = "fade_through_black"
    DIP_TO_WHITE = "dip_to_white"

    @property
    def renders_as(self) -> str:
        """The renderer-level transition name (aliases collapsed)."""
        return "crossfade" if self is ShotTransitionKind.DISSOLVE else self.value


class ShotStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    GENERATING = "generating"
    READY = "ready"
    APPROVED = "approved"
    FAILED = "failed"


class OverlayKind(StrEnum):
    EXACT_TEXT = "exact_text"
    GRAPHIC = "graphic"
    IMAGE = "image"


class OverlayAnchor(StrEnum):
    TOP_LEFT = "top_left"
    TOP_CENTER = "top_center"
    TOP_RIGHT = "top_right"
    CENTER_LEFT = "center_left"
    CENTER = "center"
    CENTER_RIGHT = "center_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_CENTER = "bottom_center"
    BOTTOM_RIGHT = "bottom_right"


class OverlayFit(StrEnum):
    CONTAIN = "contain"
    COVER = "cover"
    STRETCH = "stretch"


class AudioCueKind(StrEnum):
    AMBIENCE = "ambience"
    EFFECT = "effect"
    NATIVE_CLIP = "native_clip"


class AudioMixPolicy(StrEnum):
    MUTE = "mute"
    UNDER_NARRATION = "under_narration"
    FOREGROUND = "foreground"


class ReferenceRole(StrEnum):
    SOURCE_EVIDENCE = "source_evidence"
    COMPOSITION = "composition"
    STYLE = "style"
    CHARACTER = "character"
    FIRST_FRAME = "first_frame"
    CONTINUITY = "continuity"


class SourceClassification(StrEnum):
    DOCUMENTARY_EVIDENCE = "documentary_evidence"
    EDITORIAL_CONTEXT = "editorial_context"
    ILLUSTRATION = "illustration"


#: Exact-text overlays are rasterized into sanitized static HTML/CSS, so their
#: style vocabulary is a strict allowlist: one key, strictly validated values.
#: Placement for exact text is template-owned in this phase; the positional
#: fields stay reserved for asset-backed overlay kinds.
_EXACT_TEXT_STYLE_KEYS = frozenset({"color"})
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_NAMED_COLORS = frozenset({
    "white", "black", "yellow", "red", "cyan", "magenta", "orange", "lime",
})


class ShotTransition(DomainModel):
    kind: ShotTransitionKind = ShotTransitionKind.CUT
    duration_seconds: float = Field(default=0.0, ge=0)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def overlap_requires_duration(self) -> ShotTransition:
        if not math.isfinite(self.duration_seconds):
            raise ValueError("transition duration must be finite")
        if self.kind is not ShotTransitionKind.CUT and self.duration_seconds <= 0:
            raise ValueError(f"{self.kind.value} requires a positive duration_seconds")
        if self.kind is ShotTransitionKind.CUT:
            # Bypass assignment validation so the after-validator cannot recurse.
            object.__setattr__(self, "duration_seconds", 0.0)
        return self


class MediaSource(DomainModel):
    """Provenance record for imported REAL-lane material.

    The source URL is stored as plain text only; nothing in this module fetches,
    scrapes, or downloads. Acquisition always remains an explicit user action.
    """

    title: str = Field(default="", max_length=2000)
    creator: str = Field(default="", max_length=1000)
    publisher: str = Field(default="", max_length=1000)
    source_url: str = Field(default="", max_length=4000)
    access_date: str = Field(default="", max_length=10)
    license_note: str = Field(default="", max_length=4000)
    classification: SourceClassification = SourceClassification.ILLUSTRATION
    notes: str = Field(default="", max_length=8000)
    sha256: str | None = Field(default=None, max_length=64)


class OverlayCue(DomainModel):
    id: str = Field(default_factory=new_id)
    kind: OverlayKind
    asset_id: str | None = None
    exact_text: str | None = Field(default=None, max_length=2000)
    template: str = Field(default="", max_length=200)
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0, le=MAX_SHOT_SECONDS)
    z_index: int = Field(default=0, ge=0)
    anchor: OverlayAnchor = OverlayAnchor.CENTER
    x: float | None = None
    y: float | None = None
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    safe_area: float = Field(default=0.0, ge=0, le=0.25)
    fit: OverlayFit = OverlayFit.CONTAIN
    opacity: float = Field(default=1.0, gt=0, le=1)
    fade_in_seconds: float = Field(default=0.0, ge=0)
    fade_out_seconds: float = Field(default=0.0, ge=0)
    style: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds

    @model_validator(mode="after")
    def validate_payload(self) -> OverlayCue:
        for value in (self.start_seconds, self.duration_seconds):
            if not math.isfinite(value):
                raise ValueError("overlay timing must be finite")
        if self.fade_in_seconds + self.fade_out_seconds > self.duration_seconds:
            raise ValueError("overlay fades cannot exceed the overlay duration")
        if self.width is not None and self.height is None:
            raise ValueError("overlay width and height must be set together")
        if self.height is not None and self.width is None:
            raise ValueError("overlay width and height must be set together")
        if self.kind is OverlayKind.EXACT_TEXT:
            if not (self.exact_text or "").strip():
                raise ValueError("exact_text overlays require non-empty exact_text")
            if self.asset_id is not None:
                raise ValueError("exact_text overlays cannot reference an asset")
            reserved = [
                name for name in ("x", "y", "width", "height")
                if getattr(self, name) is not None
            ]
            if reserved:
                raise ValueError(
                    f"exact_text overlays are template-placed; {', '.join(reserved)} "
                    "must be omitted"
                )
            if self.anchor is not OverlayAnchor.CENTER \
                    or self.fit is not OverlayFit.CONTAIN or self.safe_area != 0:
                raise ValueError(
                    "exact_text overlays use template-owned placement; omit anchor, "
                    "fit, and safe_area"
                )
            unsupported = sorted(set(self.style) - _EXACT_TEXT_STYLE_KEYS)
            if unsupported:
                raise ValueError(
                    f"exact_text overlay style supports only {sorted(_EXACT_TEXT_STYLE_KEYS)}"
                )
            color = self.style.get("color")
            if color is not None and not (
                isinstance(color, str)
                and (_HEX_COLOR.match(color) or color.lower() in _NAMED_COLORS)
            ):
                raise ValueError(
                    "exact_text overlay colors must be #rgb/#rrggbb hex or one of "
                    f"{sorted(_NAMED_COLORS)}"
                )
        else:
            if not self.asset_id:
                raise ValueError(f"{self.kind.value} overlays require asset_id")
            if self.exact_text is not None:
                raise ValueError(
                    f"{self.kind.value} overlays carry assets, not exact_text"
                )
        return self


class AudioCue(DomainModel):
    id: str = Field(default_factory=new_id)
    kind: AudioCueKind
    asset_id: str
    start_seconds: float = Field(default=0.0, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0, le=MAX_SHOT_SECONDS)
    gain_db: float = Field(default=0.0, ge=-60, le=12)
    fade_in_seconds: float = Field(default=0.0, ge=0)
    fade_out_seconds: float = Field(default=0.0, ge=0)
    loop: bool = False
    mix_policy: AudioMixPolicy = AudioMixPolicy.UNDER_NARRATION

    @model_validator(mode="after")
    def validate_timing(self) -> AudioCue:
        for name, value in (
            ("start", self.start_seconds),
            ("duration", self.duration_seconds),
            ("fade_in", self.fade_in_seconds),
            ("fade_out", self.fade_out_seconds),
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"audio cue {name} must be finite")
        if (
            self.duration_seconds is not None
            and self.fade_in_seconds + self.fade_out_seconds > self.duration_seconds
        ):
            raise ValueError("audio cue fades cannot exceed the cue duration")
        return self


class ShotReference(DomainModel):
    role: ReferenceRole
    asset_id: str


class Shot(DomainModel):
    id: str = Field(default_factory=new_id)
    project_id: str
    scene_id: str
    index: int = Field(ge=0)
    title: str = Field(default="", max_length=1000)
    duration_seconds: float = Field(gt=0, le=MAX_SHOT_SECONDS)
    start_mode: ShotStartMode = ShotStartMode.WEIGHTED
    lane: ShotLane = ShotLane.IMAGE
    visual_type: VisualType = VisualType.FLUX_STILL
    selected_backend: str = "automatic"
    visual_prompt: str = ""
    negative_prompt: str = ""
    camera_instruction: str = ""
    source_asset_id: str | None = None
    source_in_seconds: float | None = Field(default=None, ge=0)
    source_out_seconds: float | None = Field(default=None, ge=0)
    transition_in: ShotTransition = Field(default_factory=ShotTransition)
    references: list[str] = Field(default_factory=list)
    reference_assets: list[ShotReference] = Field(default_factory=list)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
    status: ShotStatus = ShotStatus.DRAFT
    locked: bool = False
    overlays: list[OverlayCue] = Field(default_factory=list)
    audio_cues: list[AudioCue] = Field(default_factory=list)
    source: MediaSource | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def transition_overlap(self) -> float:
        return 0.0 if self.transition_in.kind is ShotTransitionKind.CUT \
            else self.transition_in.duration_seconds

    @model_validator(mode="after")
    def validate_shot(self) -> Shot:
        if not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("shot duration must be finite and positive")
        trim_in, trim_out = self.source_in_seconds, self.source_out_seconds
        if (trim_in is None) != (trim_out is None):
            raise ValueError("source_in_seconds and source_out_seconds are set together")
        if trim_in is not None and trim_out is not None and trim_out <= trim_in:
            raise ValueError("source_out_seconds must be greater than source_in_seconds")
        if self.overlays:
            ids = [overlay.id for overlay in self.overlays]
            if len(ids) != len(set(ids)):
                raise ValueError("overlay ids must be unique within a shot")
            for overlay in self.overlays:
                if overlay.end_seconds > self.duration_seconds:
                    raise ValueError(
                        f"overlay {overlay.id!r} ends after the shot ends"
                    )
        if self.audio_cues:
            cue_ids = [cue.id for cue in self.audio_cues]
            if len(cue_ids) != len(set(cue_ids)):
                raise ValueError("audio cue ids must be unique within a shot")
            for cue in self.audio_cues:
                end = cue.start_seconds + (cue.duration_seconds or 0.0)
                if cue.duration_seconds is not None and end > self.duration_seconds:
                    raise ValueError(f"audio cue {cue.id!r} ends after the shot ends")
        seen_references: set[tuple[str, str]] = set()
        for reference in self.reference_assets:
            key = (reference.role.value, reference.asset_id)
            if key in seen_references:
                raise ValueError(f"duplicate {key[0]} reference to {key[1]}")
            seen_references.add(key)
        return self


def implicit_shot_id(scene_id: str) -> str:
    return f"{scene_id}{IMPLICIT_SHOT_SUFFIX}"


def implicit_shot_from_scene(scene: Scene) -> Shot:
    """Project a legacy single-visual scene as one implicit shot.

    The projection never mutates the scene; the deterministic id keeps repeated
    compilations stable so a later materialization cannot collide with it.
    """
    return Shot(
        id=implicit_shot_id(scene.id),
        project_id=scene.project_id,
        scene_id=scene.id,
        index=0,
        title=scene.title,
        duration_seconds=scene.duration,
        lane=default_lane_for_visual_type(scene.visual_type),
        visual_type=scene.visual_type,
        selected_backend=scene.selected_backend,
        visual_prompt=scene.visual_prompt,
        negative_prompt=scene.negative_prompt,
        camera_instruction=scene.camera_instruction,
        transition_in=shot_transition_from_scene_string(
            scene.transition, scene.duration,
        ),
        references=list(scene.references),
        seed=scene.seed,
        status=implicit_status(scene.status.value, scene.locked),
        locked=scene.locked,
        settings=dict(scene.settings),
        created_at=scene.created_at,
        updated_at=scene.updated_at,
    )


def implicit_status(scene_status_value: str, locked: bool) -> ShotStatus:
    mapping = {
        "draft": ShotStatus.DRAFT,
        "queued": ShotStatus.QUEUED,
        "generating": ShotStatus.GENERATING,
        "generated": ShotStatus.READY,
        "approved": ShotStatus.APPROVED,
        "locked": ShotStatus.APPROVED,
        "failed": ShotStatus.FAILED,
    }
    status = mapping.get(scene_status_value, ShotStatus.DRAFT)
    if locked and status is not ShotStatus.APPROVED:
        return ShotStatus.APPROVED
    return status


def default_lane_for_visual_type(visual_type: VisualType) -> ShotLane:
    if visual_type in {VisualType.GRAPHIC_SCREEN, VisualType.TITLE_CARD, VisualType.DIAGRAM}:
        return ShotLane.HTML
    if visual_type in {
        VisualType.H3_AUDIOVISUAL, VisualType.H3_REFERENCE, VisualType.WAN_VIDEO,
    }:
        return ShotLane.H3
    if visual_type is VisualType.REUSED_MEDIA:
        return ShotLane.REAL
    return ShotLane.IMAGE


def shot_transition_from_scene_string(
    transition: str, duration_hint_seconds: float | None = None,
) -> ShotTransition:
    """Project a legacy scene transition string onto a validated shot transition.

    Legacy scenes carry no transition length; the existing timeline builder uses
    ``min(0.35, duration / 4)`` for overlapping transitions, so the projection
    mirrors that convention.
    """
    normalized = (transition or "cut").strip().lower()
    aliases = {"dissolve": "crossfade", "fade": "crossfade"}
    try:
        kind = ShotTransitionKind(aliases.get(normalized, normalized))
    except ValueError:
        kind = ShotTransitionKind.CROSSFADE if normalized else ShotTransitionKind.CUT
    if kind is ShotTransitionKind.CUT:
        return ShotTransition(kind=kind)
    hint = duration_hint_seconds or 0.0
    return ShotTransition(kind=kind, duration_seconds=min(0.35, hint / 4))


def effective_shots(scene: Scene, shots: Sequence[Shot]) -> list[Shot]:
    """Shots to compile for a scene: stored shots, or the implicit legacy shot."""
    ordered = sorted(shots, key=lambda shot: shot.index)
    return list(ordered) if ordered else [implicit_shot_from_scene(scene)]


def scene_rendered_duration(shots: Sequence[Shot]) -> float:
    """sum(durations) - sum(incoming transition overlaps)."""
    total = 0.0
    for position, shot in enumerate(shots):
        overlap = shot.transition_overlap if position else 0.0
        total += shot.duration_seconds - overlap
    return round(total, 6)


def validate_shot_sequence(shots: Sequence[Shot]) -> None:
    """Structural rules that hold regardless of the narration clock."""
    indexes = [shot.index for shot in shots]
    if len(set(indexes)) != len(indexes):
        raise ShotTimingError("shot indexes must be unique within a scene")
    for position, expected in enumerate(sorted(indexes)):
        if expected != position:
            raise ShotTimingError(
                f"shot indexes must be contiguous from zero (found {expected} at "
                f"position {position})"
            )
    ordered = sorted(shots, key=lambda shot: shot.index)
    for shot in ordered[1:]:
        overlap = shot.transition_overlap
        if overlap >= min(ordered[shot.index - 1].duration_seconds, shot.duration_seconds):
            raise ShotTimingError(
                f"shot {shot.index} transition overlap {overlap}s must be shorter than "
                "both adjacent shots"
            )


@dataclass(frozen=True, slots=True)
class CompiledShot:
    shot_id: str
    index: int
    start_seconds: float
    duration_seconds: float
    overlap_seconds: float
    adjusted: bool = False

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


def compile_shot_timings(
    shots: Sequence[Shot],
    target_duration_seconds: float,
    *,
    fps: int = 24,
) -> list[CompiledShot]:
    """Fit shot durations onto the scene clock, retiming only weighted shots.

    The compiled scene length must match the target within one video frame.
    Deltas beyond that frame are distributed across ``start_mode == weighted``
    shots proportionally to their durations; fixed shots keep their exact
    duration, and nothing here ever stretches a locked shot.
    """
    if fps <= 0:
        raise ValueError("fps must be positive")
    if not shots:
        raise ShotTimingError("a scene needs at least one shot to compile")
    ordered = sorted(shots, key=lambda shot: shot.index)
    validate_shot_sequence(ordered)
    if not math.isfinite(target_duration_seconds) or target_duration_seconds < 0:
        raise ValueError("target duration must be finite and non-negative")
    tolerance = 1.0 / fps + 1e-6

    overlaps = [0.0] + [shot.transition_overlap for shot in ordered[1:]]
    minimums = [
        overlap + min_frame(fps) if shot.start_mode is ShotStartMode.WEIGHTED else None
        for shot, overlap in zip(ordered, overlaps, strict=True)
    ]
    durations = [shot.duration_seconds for shot in ordered]
    delta = target_duration_seconds - scene_rendered_duration(ordered)
    adjusted_flags = [False] * len(ordered)
    weighted_positions = [
        position for position, shot in enumerate(ordered)
        if shot.start_mode is ShotStartMode.WEIGHTED and shot.locked is False
    ]
    if abs(delta) > tolerance:
        if not weighted_positions:
            raise ShotTimingError(
                f"shot timings total {scene_rendered_duration(ordered):.6f}s but the scene "
                f"clock needs {target_duration_seconds:.6f}s; mark at least one shot "
                "start_mode='weighted' to allow retiming"
            )
        weight_total = sum(durations[position] for position in weighted_positions)
        share = delta / weight_total
        for position in weighted_positions:
            floor = minimums[position]
            candidate = durations[position] * (1 + share)
            if floor is not None and candidate <= floor:
                candidate = floor
            if candidate <= 0:
                raise ShotTimingError("weighted retiming would collapse a shot to zero")
            adjusted_flags[position] = True
            durations[position] = candidate

    compiled: list[CompiledShot] = []
    cursor = 0.0
    for position, shot in enumerate(ordered):
        overlap = overlaps[position] if position else 0.0
        cursor -= overlap
        compiled.append(CompiledShot(
            shot_id=shot.id,
            index=shot.index,
            start_seconds=round(cursor, 9),
            duration_seconds=round(durations[position], 9),
            overlap_seconds=overlap,
            adjusted=adjusted_flags[position],
        ))
        cursor += durations[position]
    residual = abs(cursor - target_duration_seconds)
    if residual > tolerance:
        raise ShotTimingError(
            f"compiled shot timings miss the scene clock by {residual:.6f}s "
            f"(tolerance {tolerance:.6f}s)"
        )
    return compiled


def min_frame(fps: int) -> float:
    return 1.0 / fps
