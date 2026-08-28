"""Compile a scene's shots into an ordered, frame-accurate render plan.

This module is the bridge between the validated shot contracts
(``backend.schemas.shots``) and the FFmpeg normalization/assembly layers in
``backend.rendering``. It never mutates shots: retiming happens only inside
``compile_shot_timings`` and only for unlocked ``start_mode='weighted'``
shots; everything else raises ``ShotTimingError``.

The compiled plan is expressed twice, on purpose:

- float seconds for narration-clock comparisons and manifests;
- integer frame counts (at the project fps) so the renderer can produce
  exact frame counts and exact transition offsets instead of trusting
  UI pixels or accumulated floating-point cursors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from backend.schemas.models import Scene
from backend.schemas.shots import (
    CompiledShot,
    Shot,
    ShotTimingError,
    compile_shot_timings,
    effective_shots,
    scene_rendered_duration,
)

RENDERER_TRANSITION_KINDS = frozenset(
    {"cut", "crossfade", "fade_through_black", "dip_to_white"}
)


def frame_count(duration_seconds: float, fps: int) -> int:
    """Nearest whole frame count for a duration at ``fps``."""
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValueError("duration must be finite and non-negative")
    if fps <= 0:
        raise ValueError("fps must be positive")
    return max(1, round(duration_seconds * fps))


@dataclass(frozen=True, slots=True)
class ShotBoundary:
    """One incoming transition between shot ``position - 1`` and ``position``.

    ``offset_seconds``/``offset_frames`` locate where the transition begins in
    scene-local output time, taken directly from the compiled shot start.
    """

    position: int
    kind: str
    overlap_seconds: float
    overlap_frames: int
    offset_seconds: float
    offset_frames: int


@dataclass(frozen=True, slots=True)
class SceneRenderPlan:
    """Ordered render plan for one scene.

    ``frame_counts[i]`` is the exact number of frames the normalized
    intermediate for ``shots[i].shot_id`` must contain. Rendered scene length
    is ``sum(frame_counts) - sum(boundary overlaps)`` frames.
    """

    scene_id: str
    fps: int
    shots: tuple[CompiledShot, ...]
    boundaries: tuple[ShotBoundary, ...]
    duration_seconds: float
    total_frames: int
    frame_counts: tuple[int, ...]

    def frame_count_for(self, shot_id: str) -> int:
        for shot, frames in zip(self.shots, self.frame_counts, strict=True):
            if shot.shot_id == shot_id:
                return frames
        raise KeyError(shot_id)

    def ordered_shot_ids(self) -> tuple[str, ...]:
        return tuple(shot.shot_id for shot in self.shots)


def _boundary(
    position: int,
    compiled: CompiledShot,
    source: Shot,
    fps: int,
) -> ShotBoundary:
    kind = source.transition_in.kind.renders_as
    overlap = 0.0 if kind == "cut" else source.transition_overlap
    return ShotBoundary(
        position=position,
        kind=kind,
        overlap_seconds=overlap,
        overlap_frames=0 if kind == "cut" else max(1, round(overlap * fps)),
        offset_seconds=compiled.start_seconds,
        offset_frames=max(0, round(compiled.start_seconds * fps)),
    )


def _assign_frame_counts(
    compiled: Sequence[CompiledShot],
    *,
    target_duration_seconds: float,
    overlaps: Sequence[int],
    fps: int,
) -> tuple[int, ...]:
    """Snap compiled durations onto the project frame grid without drift.

    Largest-remainder rounding distributes the whole-scene residual one frame
    at a time to the shots whose ideal lengths carry the largest fractional
    parts, so no shot — fixed, locked, or weighted — ever absorbs more than a
    single frame of accumulated drift. Every individual adjustment stays
    within half a frame of its shot's ideal length on the project grid.
    """
    ideals = [item.duration_seconds * fps for item in compiled]
    base = [max(1, math.floor(ideal + 1e-9)) for ideal in ideals]
    target_total = round(target_duration_seconds * fps)
    extra = target_total + sum(overlaps) - sum(base)
    if not 0 <= extra <= len(base):
        raise ShotTimingError(
            f"compiled shot timings miss the scene clock by {abs(extra)} frames "
            "on the frame grid; retiming failed to fit within tolerance"
        )
    order = sorted(
        range(len(base)),
        key=lambda position: (-(ideals[position] - base[position]), position),
    )
    incoming = {0: 0}
    incoming.update({position: overlaps[position - 1] for position in range(1, len(base))})
    for position in order[:extra]:
        adjusted = base[position] + 1
        if adjusted <= max(1, incoming[position]):
            raise ShotTimingError(
                f"shot {position} cannot absorb its frame-grid rounding share "
                "without collapsing below its transition overlap"
            )
        base[position] = adjusted
    return tuple(base)


def compile_scene_plan(
    scene: Scene,
    shots: Sequence[Shot],
    *,
    fps: int,
    target_duration_seconds: float | None = None,
) -> SceneRenderPlan:
    """Compile one scene into an ordered render plan.

    ``target_duration_seconds`` is the measured narration clock when known;
    when omitted the structural shot durations are accepted as-is. Either way
    the result must be reachable within one frame, only weighted unlocked
    shots may be retimed, and any violation raises ``ShotTimingError``.
    """
    if fps <= 0:
        raise ValueError("fps must be positive")
    ordered_sources = effective_shots(scene, shots)
    target = (
        scene_rendered_duration(ordered_sources)
        if target_duration_seconds is None
        else float(target_duration_seconds)
    )
    compiled = compile_shot_timings(ordered_sources, target, fps=fps)

    overlaps: list[int] = [0]
    for position, source in enumerate(ordered_sources[1:], start=1):
        kind = source.transition_in.kind.renders_as
        if kind not in RENDERER_TRANSITION_KINDS:
            raise ShotTimingError(f"unsupported shot transition kind {kind!r}")
        if kind == "cut":
            overlaps.append(0)
            continue
        overlap_frames = max(1, round(source.transition_overlap * fps))
        previous_frames = max(1, frame_count(compiled[position - 1].duration_seconds, fps))
        current_frames = max(1, frame_count(compiled[position].duration_seconds, fps))
        if overlap_frames >= min(previous_frames, current_frames):
            raise ShotTimingError(
                f"shot {position} transition overlap must be shorter than both "
                f"adjacent shots on the frame grid ({overlap_frames} frames)"
            )
        if kind in {"fade_through_black", "dip_to_white"} and overlap_frames < 2:
            raise ShotTimingError(
                f"shot {position} {kind} transition needs at least two frames of "
                "overlap to hold both fade halves"
            )
        overlaps.append(overlap_frames)

    counts = _assign_frame_counts(
        compiled,
        target_duration_seconds=target,
        overlaps=overlaps,
        fps=fps,
    )
    for position, source in enumerate(ordered_sources[1:], start=1):
        kind = source.transition_in.kind.renders_as
        if kind == "cut":
            continue
        if not 0 < overlaps[position] < min(counts[position - 1], counts[position]):
            raise ShotTimingError(
                f"shot {position} transition overlap does not fit the snapped "
                "frame grid of both adjacent shots"
            )

    boundaries = []
    for position, source in enumerate(ordered_sources[1:], start=1):
        boundaries.append(_boundary(position, compiled[position], source, fps))
    total_frames = sum(counts) - sum(boundary.overlap_frames for boundary in boundaries)
    return SceneRenderPlan(
        scene_id=scene.id,
        fps=fps,
        shots=tuple(compiled),
        boundaries=tuple(boundaries),
        duration_seconds=total_frames / fps,
        total_frames=total_frames,
        frame_counts=tuple(counts),
    )


__all__ = [
    "RENDERER_TRANSITION_KINDS",
    "SceneRenderPlan",
    "ShotBoundary",
    "compile_scene_plan",
    "frame_count",
]
