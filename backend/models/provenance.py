"""Provenance and stale-state graph helpers over assets carrying ``shot_id``.

Regeneration of one shot must invalidate only that shot's current visual and
the dependents that consumed it: continuity links plus ANY typed reference
asset they point at (every role is dependency-bearing, since each referenced
hash participates in the consumer's cache key and provenance). Locked shots
and scenes are skipped. Media files are never deleted here: stale dependents
keep their current asset for review while being flagged for regeneration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from backend.models.h3_shot_continuity import (
    effective_predecessor_shot_id,
    timeline_key,
)
from backend.schemas.models import Asset, Scene
from backend.schemas.shots import ReferenceRole, Shot, ShotStatus

#: Every typed reference role is dependency-bearing: each reference asset's
#: sha256 participates in the consumer's generation cache key and provenance,
#: so a change to ANY referenced source (including style, composition,
#: character, and source_evidence) invalidates the consuming shot.
_DEPENDENCY_BEARING_ROLES = frozenset(ReferenceRole)


def current_visual_asset(shot: Shot, assets: Iterable[Asset]) -> Asset | None:
    """The newest visual asset recorded for a shot, or None.

    Mirrors scene-scope semantics: only ``role == "visual"`` assets count, the
    recorded visual type must match the shot, and an older revision is never
    returned over the revision the shot currently points at.
    """
    candidates: list[Asset] = []
    expected_type = (
        shot.visual_type.value
        if hasattr(shot.visual_type, "value") else str(shot.visual_type)
    )
    for asset in assets:
        if asset.shot_id != shot.id or asset.settings.get("role") != "visual":
            continue
        recorded_type = asset.settings.get("visual_type")
        if recorded_type is not None and recorded_type != expected_type:
            continue
        try:
            revision = int(asset.settings.get("visual_revision", 0))
        except (TypeError, ValueError):
            revision = 0
        try:
            wanted = int(shot.settings.get("visual_revision", 0))
        except (TypeError, ValueError):
            wanted = 0
        if revision != wanted:
            continue
        candidates.append(asset)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.created_at, item.id))
    return candidates[-1]


@dataclass(frozen=True, slots=True)
class RegenerationImpact:
    """What regenerating one shot touches — computed BEFORE any work runs."""

    regenerated_shot_id: str
    #: Direct + transitive dependent shots to mark stale.
    stale_shot_ids: tuple[str, ...]
    #: Locked dependents skipped by staleness marking, in no particular order.
    locked_skipped_shot_ids: tuple[str, ...]
    #: Shots in locked scenes: staleness is deferred until the scene unlocks.
    locked_scene_skipped_shot_ids: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "regenerated_shot_id": self.regenerated_shot_id,
            "stale_shot_ids": list(self.stale_shot_ids),
            "locked_skipped_shot_ids": list(self.locked_skipped_shot_ids),
            "locked_scene_skipped_shot_ids": list(self.locked_scene_skipped_shot_ids),
        }


def dependency_edges(shots: Sequence[Shot], assets: Iterable[Asset]) -> dict[str, set[str]]:
    """Consumer shot id -> set of producer shot ids it depends on.

    Edges come from continuity links (predecessor shot ids) and from typed
    reference assets owned by another shot. Every reference role is
    dependency-bearing — ``first_frame``/``continuity`` feed generation
    inputs directly, while ``style``/``composition``/``character``/
    ``source_evidence`` hashes participate in the consumer's cache key, so a
    change to any of them stales the consuming shot as well.
    """
    producer_by_asset: dict[str, str] = {
        asset.id: asset.shot_id
        for asset in assets
        if asset.shot_id
    }
    edges: dict[str, set[str]] = {}
    all_shots = list(shots)
    for shot in all_shots:
        consumers: set[str] = set()
        predecessor = effective_predecessor_shot_id(shot, all_shots)
        if predecessor is not None:
            consumers.add(predecessor)
        for reference in shot.reference_assets:
            if reference.role not in _DEPENDENCY_BEARING_ROLES:
                continue
            producer = producer_by_asset.get(reference.asset_id)
            if producer and producer != shot.id:
                consumers.add(producer)
        if consumers:
            edges[shot.id] = consumers
    return edges


def plan_regeneration(
    source_shot_id: str,
    shots: Sequence[Shot],
    assets: Sequence[Asset],
    *,
    scenes: Sequence[Scene] = (),
) -> RegenerationImpact:
    """Compute which shots a regeneration marks stale, before queueing work.

    Locked shots are never marked. Shots inside locked scenes are reported
    separately so the caller can surface them without mutating them.
    """
    all_shots = list(shots)
    by_id = {item.id: item for item in all_shots}
    if source_shot_id not in by_id:
        raise KeyError(f"shot not found: {source_shot_id}")
    edges = dependency_edges(all_shots, assets)
    # BFS over consumer edges, chain order preserved per depth level.
    ordered: list[str] = []
    seen: set[str] = set()
    frontier: list[str] = sorted(
        (
            shot.id for shot in all_shots
            if source_shot_id in edges.get(shot.id, set())
        ),
        key=lambda shot_id: timeline_key(by_id[shot_id]),
    )
    while frontier:
        nxt: list[str] = []
        for shot_id in frontier:
            if shot_id in seen:
                continue
            seen.add(shot_id)
            ordered.append(shot_id)
            nxt.extend(
                candidate.id for candidate in all_shots
                if shot_id in edges.get(candidate.id, set())
                and candidate.id not in seen
            )
        frontier = sorted(set(nxt), key=lambda shot_id: timeline_key(by_id[shot_id]))

    locked_shots = {item.id for item in all_shots if item.locked}
    locked_scenes = {scene.id for scene in scenes if scene.locked}
    scene_of = {item.id: item.scene_id for item in all_shots}
    deferred_by_scene = [
        item for item in ordered
        if item not in locked_shots and scene_of.get(item) in locked_scenes
    ]
    return RegenerationImpact(
        regenerated_shot_id=source_shot_id,
        stale_shot_ids=tuple(
            item for item in ordered
            if item not in locked_shots and item not in set(deferred_by_scene)
        ),
        locked_skipped_shot_ids=tuple(
            item for item in ordered if item in locked_shots
        ),
        locked_scene_skipped_shot_ids=tuple(sorted(set(deferred_by_scene))),
    )


def apply_regeneration_staleness(
    shots: Sequence[Shot],
    impact: RegenerationImpact,
    *,
    marked_at: datetime | None = None,
) -> list[Shot]:
    """Return updated copies of shots with staleness markers applied.

    Only the impact's targeted dependents change status/settings; every other
    shot is returned untouched, and media files are never touched at all.
    """
    stamp_dt = marked_at or datetime.now(timezone.utc)
    stamp = stamp_dt.isoformat()
    stale = set(impact.stale_shot_ids)
    updated: list[Shot] = []
    for shot in shots:
        if shot.id not in stale:
            updated.append(shot)
            continue
        settings = dict(shot.settings)
        staleness = dict(settings.get("staleness") or {})
        staleness.update({
            "source_shot_id": impact.regenerated_shot_id,
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
    return updated


def provenance_summary(
    shot: Shot,
    assets: Sequence[Asset],
) -> dict[str, object]:
    """Human-auditable provenance for one shot's current visual.

    Includes the generating backend/model identity, seed, prompt hash inputs,
    and the content hash recorded on the asset row — the cue-sheet building
    block for REAL/generated classification later.
    """
    asset = current_visual_asset(shot, assets)
    lane = shot.lane.value if hasattr(shot.lane, "value") else str(shot.lane)
    summary: dict[str, object] = {
        "shot_id": shot.id,
        "scene_id": shot.scene_id,
        "lane": lane,
        "visual_type": (
            shot.visual_type.value
            if hasattr(shot.visual_type, "value") else str(shot.visual_type)
        ),
        "seed": shot.seed,
        "locked": shot.locked,
        "status": (
            shot.status.value if hasattr(shot.status, "value") else str(shot.status)
        ),
        "asset_id": asset.id if asset else None,
        "filepath": str(asset.filepath) if asset else None,
        "sha256": asset.hash if asset else None,
        "backend": asset.backend if asset else None,
        "model": asset.model if asset else None,
        "model_version": asset.model_version if asset else None,
        "workflow_version": asset.workflow_version if asset else None,
        "references": [
            {"role": ref.role.value, "asset_id": ref.asset_id}
            for ref in shot.reference_assets
        ],
    }
    return summary


__all__ = [
    "RegenerationImpact",
    "apply_regeneration_staleness",
    "current_visual_asset",
    "dependency_edges",
    "plan_regeneration",
    "provenance_summary",
]
