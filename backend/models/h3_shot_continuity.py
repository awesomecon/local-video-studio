"""H3 continuity generalized to predecessor SHOT ids.

Scene-level continuity (``settings.h3_continuity.predecessor_scene_id``) keeps
working: a scene-level link is interpreted as linking to the deterministic
implicit continuation source of that scene. Shot-scope links declare
``predecessor_shot_id`` instead and are the preferred form going forward.

This module validates continuity chains, orders mixed-lane batches so chains
execute head-to-tail under the existing single-GPU serialization, propagates
staleness transitively by shot id when a source regenerates (dependent shots
are marked stale WITHOUT deleting their media), and pins the default H3 native
audio policy to mute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from backend.schemas.models import Scene, VisualType
from backend.schemas.shots import (
    AudioMixPolicy,
    Shot,
    ShotLane,
    ShotStatus,
    implicit_shot_from_scene,
    implicit_shot_id,
)

#: Native H3 audio is discarded from final exports for this documentary; the
#: narration stays authoritative. Enabling it is an explicit per-shot edit.
H3_NATIVE_AUDIO_MIX_POLICY = AudioMixPolicy.MUTE

_H3_VISUAL_TYPES = frozenset({
    VisualType.H3_AUDIOVISUAL,
    VisualType.H3_REFERENCE,
})


class ShotContinuityErrorCode(StrEnum):
    INVALID_CONTINUITY = "invalid_continuity"
    SELF_LINK = "continuity_self_link"
    MISSING_PREDECESSOR = "continuity_missing_predecessor"
    FORWARD_LINK = "continuity_forward_link"
    PREDECESSOR_NOT_H3 = "continuity_predecessor_not_h3"
    CYCLE = "continuity_cycle"


class ShotContinuityError(ValueError):
    """Structured validation failure of a shot continuity chain."""

    def __init__(
        self,
        message: str,
        code: ShotContinuityErrorCode | str,
        *,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = (
            code.value if isinstance(code, ShotContinuityErrorCode) else str(code)
        )
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True, slots=True)
class ShotContinuityLink:
    """Parsed ``settings.h3_continuity`` payload of one shot."""

    enabled: bool = False
    group: str = ""
    predecessor_shot_id: str | None = None
    predecessor_scene_id: str | None = None
    #: True when only the legacy scene-level predecessor was declared.
    legacy_scene_level: bool = False


def parse_shot_continuity(settings: Mapping[str, Any]) -> ShotContinuityLink:
    """Read one shot's continuity block without raising on disabled/absent."""
    raw = settings.get("h3_continuity")
    if raw is None or raw is False:
        return ShotContinuityLink()
    if not isinstance(raw, dict):
        raise ShotContinuityError(
            "h3_continuity must be an object or false.",
            ShotContinuityErrorCode.INVALID_CONTINUITY,
        )
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ShotContinuityError(
            "h3_continuity.enabled must be true or false.",
            ShotContinuityErrorCode.INVALID_CONTINUITY,
        )
    if not enabled:
        return ShotContinuityLink()

    def _text(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ShotContinuityError(
                f"h3_continuity.{key} must be text or null.",
                ShotContinuityErrorCode.INVALID_CONTINUITY,
            )
        return value.strip() or None

    group = _text("group") or ""
    predecessor_shot_id = _text("predecessor_shot_id")
    predecessor_scene_id = _text("predecessor_scene_id")
    legacy = predecessor_shot_id is None and predecessor_scene_id is not None
    return ShotContinuityLink(
        enabled=True,
        group=group,
        predecessor_shot_id=predecessor_shot_id,
        predecessor_scene_id=predecessor_scene_id,
        legacy_scene_level=legacy,
    )


def _is_eligible_h3_predecessor(shot: Shot) -> bool:
    """Whether a shot can serve as an H3 continuity source."""
    if shot.lane is not ShotLane.H3:
        return False
    visual_type = shot.visual_type
    if not isinstance(visual_type, VisualType):
        try:
            visual_type = VisualType(visual_type)
        except ValueError:
            return False
    return visual_type in _H3_VISUAL_TYPES


def last_eligible_h3_shot_id(scene_id: str, shots: Iterable[Shot]) -> str | None:
    """The highest-indexed eligible H3 shot stored for a scene, or None."""
    eligible = [
        item for item in shots
        if item.scene_id == scene_id and _is_eligible_h3_predecessor(item)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: item.index).id


def effective_predecessor_shot_id(shot: Shot, shots: Iterable[Shot] | None = None) -> str | None:
    """The concrete predecessor shot id of one shot's continuity link.

    Shot-level links name ``predecessor_shot_id`` directly. Legacy scene-level
    links (``predecessor_scene_id``) resolve against the provided shots: when
    that scene stores explicit shots, the link targets its LAST ELIGIBLE H3
    shot; otherwise it falls back to the deterministic implicit projection id.
    Passing no shots keeps the pure implicit fallback for callers without a
    project view.
    """
    link = parse_shot_continuity(shot.settings)
    if not link.enabled:
        return None
    if link.predecessor_shot_id is not None:
        return link.predecessor_shot_id
    if link.predecessor_scene_id is None:
        return None
    if shots is not None:
        resolved = last_eligible_h3_shot_id(link.predecessor_scene_id, shots)
        if resolved is not None:
            return resolved
    # No explicit eligible source exists: keep the implicit projection id so
    # validation fails with missing_predecessor instead of silently treating
    # the chain as rooted.
    return f"{link.predecessor_scene_id}-implicit"


def timeline_key(shot: Shot, scene_order: Mapping[str, int] | None = None) -> tuple[int, int]:
    """Sort key placing shots in playback order across scenes."""
    scene_position = (scene_order or {}).get(shot.scene_id, 0)
    return (scene_position, shot.index)


def validate_continuity_chain(
    shot: Shot,
    shots_by_id: Mapping[str, Shot],
    *,
    scene_order: Mapping[str, int] | None = None,
) -> ShotContinuityLink:
    """Validate one shot's continuity link against the project's shots.

    Raises :class:`ShotContinuityError` with a structured code for self-links,
    missing predecessors, forward links, non-H3 predecessors, and cycles.
    Scene-level links remain valid while migrating.
    """
    link = parse_shot_continuity(shot.settings)
    if not link.enabled:
        return link
    order = timeline_key(shot, scene_order)
    project_shots = list(shots_by_id.values())
    predecessor_id = effective_predecessor_shot_id(shot, project_shots)
    if predecessor_id == shot.id:
        raise ShotContinuityError(
            "A shot cannot link to itself as its continuity predecessor.",
            ShotContinuityErrorCode.SELF_LINK,
            details={"shot_id": shot.id},
        )
    if predecessor_id is None:
        return link
    predecessor = shots_by_id.get(predecessor_id)
    if predecessor is None:
        raise ShotContinuityError(
            f"Continuity predecessor {predecessor_id!r} is not a shot in this project.",
            ShotContinuityErrorCode.MISSING_PREDECESSOR,
            details={"shot_id": shot.id, "predecessor_shot_id": predecessor_id},
        )
    predecessor_order = timeline_key(predecessor, scene_order)
    if predecessor_order >= order:
        raise ShotContinuityError(
            "The continuity predecessor must come earlier in the timeline than this shot.",
            ShotContinuityErrorCode.FORWARD_LINK,
            details={
                "shot_id": shot.id,
                "predecessor_shot_id": predecessor_id,
                "predecessor_order": list(predecessor_order),
                "order": list(order),
            },
        )
    if (
        predecessor.lane is not ShotLane.H3
        or (
            isinstance(predecessor.visual_type, VisualType)
            and predecessor.visual_type not in _H3_VISUAL_TYPES
        )
    ):
        raise ShotContinuityError(
            "The continuity predecessor must be an H3 shot.",
            ShotContinuityErrorCode.PREDECESSOR_NOT_H3,
            details={
                "shot_id": shot.id,
                "predecessor_shot_id": predecessor_id,
                "predecessor_lane": (
                    predecessor.lane.value
                    if hasattr(predecessor.lane, "value") else str(predecessor.lane)
                ),
            },
        )
    visited: set[str] = set()
    current_id: str | None = predecessor_id
    while current_id is not None:
        if current_id == shot.id:
            raise ShotContinuityError(
                "The continuity chain contains a cycle back into this shot.",
                ShotContinuityErrorCode.CYCLE,
                details={"shot_id": shot.id},
            )
        if current_id in visited:
            break  # a cycle among predecessors does not involve this shot
        visited.add(current_id)
        current = shots_by_id.get(current_id)
        current_id = (
            effective_predecessor_shot_id(current, project_shots) if current else None
        )
    return link


def direct_dependents(source_shot_id: str, shots: Iterable[Shot]) -> list[Shot]:
    """Shots whose continuity predecessor is the given shot, in timeline order."""
    all_shots = list(shots)
    dependents = [
        shot for shot in all_shots
        if effective_predecessor_shot_id(shot, all_shots) == source_shot_id
    ]
    dependents.sort(key=lambda item: (item.scene_id, item.index))
    return dependents


def stale_closure(source_shot_id: str, shots: Iterable[Shot]) -> list[Shot]:
    """Transitive closure of dependents of a regenerated shot, chain order.

    The returned order walks each continuity chain head-to-tail so callers can
    regenerate serially: every stale shot's own predecessor appears before it.
    """
    all_shots = list(shots)
    ordered: list[Shot] = []
    seen: set[str] = set()
    frontier = direct_dependents(source_shot_id, all_shots)
    while frontier:
        nxt: list[Shot] = []
        for dependent in sorted(frontier, key=lambda item: (item.scene_id, item.index)):
            if dependent.id in seen:
                continue
            seen.add(dependent.id)
            ordered.append(dependent)
            nxt.extend(direct_dependents(dependent.id, all_shots))
        frontier = nxt
    return ordered


def mark_dependents_stale(
    source_shot_id: str,
    shots: Sequence[Shot],
    *,
    marked_at: datetime | None = None,
) -> tuple[list[Shot], list[str]]:
    """Mark transitive dependents stale after their source regenerated.

    Dependent shots keep their media: only status and a staleness provenance
    marker change. Locked dependents are skipped entirely (their approval wins
    over staleness); unlocked dependents drop from ready/approved back to draft
    so the editor re-reviews them. Returns the updated shot list plus the ids
    of skipped locked dependents.
    """
    stamp_dt = marked_at or datetime.now(timezone.utc)
    stamp = stamp_dt.isoformat()
    stale_ids = {
        item.id for item in stale_closure(source_shot_id, shots)
        if item.id != source_shot_id
    }
    skipped_locked: list[str] = []
    updated: list[Shot] = []
    for shot in shots:
        if shot.id not in stale_ids:
            updated.append(shot)
            continue
        if shot.locked:
            skipped_locked.append(shot.id)
            updated.append(shot)
            continue
        settings = dict(shot.settings)
        staleness = dict(settings.get("staleness") or {})
        staleness.update({
            "source_shot_id": source_shot_id,
            "reason": "predecessor_regenerated",
            "marked_at": stamp,
        })
        settings["staleness"] = staleness
        next_status = (
            ShotStatus.DRAFT
            if shot.status in {ShotStatus.READY, ShotStatus.APPROVED, ShotStatus.FAILED}
            else shot.status
        )
        updated.append(shot.model_copy(update={
            "status": next_status,
            "settings": settings,
            "updated_at": stamp_dt,
        }))
    return updated, skipped_locked


def order_for_serial_execution(
    shots: Sequence[Shot],
    *,
    scene_order: Mapping[str, int] | None = None,
) -> list[Shot]:
    """Serialize a mixed-lane batch for the single-GPU runner.

    Playback order is preserved except where a continuity chain would be
    violated; chain members always run strictly after their predecessors. The
    caller still executes through the service's GPU lock — one heavyweight job
    at a time, exactly as today.
    """
    by_id = {item.id: item for item in shots}
    for shot in shots:
        validate_continuity_chain(shot, by_id, scene_order=scene_order)
    remaining = sorted(shots, key=lambda item: timeline_key(item, scene_order))
    placed: set[str] = set()
    ordered: list[Shot] = []
    while remaining:
        progressed = False
        deferred: list[Shot] = []
        for shot in remaining:
            predecessor_id = effective_predecessor_shot_id(shot, shots)
            if predecessor_id is not None and predecessor_id not in placed:
                if predecessor_id in by_id:
                    deferred.append(shot)
                    continue
            ordered.append(shot)
            placed.add(shot.id)
            progressed = True
        if not progressed:
            unresolved = [item.id for item in deferred]
            raise ShotContinuityError(
                "Continuity chains could not be serialized (cycle involving "
                f"{', '.join(unresolved)}).",
                ShotContinuityErrorCode.CYCLE,
                details={"shot_ids": unresolved},
            )
        remaining = deferred
    return ordered


def native_audio_policy(shot: Shot) -> AudioMixPolicy:
    """Effective native-audio mix policy for an H3 shot; defaults to mute.

    Only an explicit ``settings.h3_native_audio_mix_policy`` override changes
    it; anything unrecognized falls back to mute rather than leaking audio.
    """
    raw = shot.settings.get("h3_native_audio_mix_policy")
    if isinstance(raw, str):
        try:
            return AudioMixPolicy(raw.strip().lower())
        except ValueError:
            return H3_NATIVE_AUDIO_MIX_POLICY
    return H3_NATIVE_AUDIO_MIX_POLICY


def build_shots_index(
    shots: Iterable[Shot],
    scenes: Iterable[Scene] = (),
) -> dict[str, Shot]:
    """Shot id -> Shot index including deterministic implicit projections.

    Scene-level continuity links name a scene; resolving them against this
    index maps them onto that scene's implicit shot (``<scene_id>-implicit``)
    when no explicit shots exist yet, so legacy links validate unchanged.
    """
    index = {item.id: item for item in shots}
    materialized_scenes = {item.scene_id for item in shots}
    for scene in scenes:
        if scene.id not in materialized_scenes:
            index[implicit_shot_id(scene.id)] = implicit_shot_from_scene(scene)
    return index
