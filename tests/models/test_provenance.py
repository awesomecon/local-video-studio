"""Provenance/stale graph coverage over assets carrying shot_id.

Regeneration impact must touch only the targeted shot and its transitive
dependents (continuity links plus consumed first_frame/continuity
references); locked shots and locked scenes are skipped; media files stay
untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.models.h3_shot_continuity import (
    ShotContinuityError,
    ShotContinuityErrorCode,
)
from backend.models.provenance import (
    RegenerationImpact,
    apply_regeneration_staleness,
    current_visual_asset,
    dependency_edges,
    plan_regeneration,
    provenance_summary,
)
from backend.schemas.models import Asset, AssetType, Scene
from backend.schemas.shots import ReferenceRole, Shot, ShotLane, ShotReference, ShotStatus


def make_shot(
    shot_id: str,
    *,
    scene_id: str = "scene-1",
    index: int = 0,
    lane: ShotLane = ShotLane.H3,
    visual_type: str = "h3_audiovisual",
    settings: dict | None = None,
    status: ShotStatus = ShotStatus.READY,
    locked: bool = False,
    reference_assets: list[ShotReference] | None = None,
) -> Shot:
    fields: dict = {
        "id": shot_id,
        "project_id": "proj-1",
        "scene_id": scene_id,
        "index": index,
        "duration_seconds": 5.0,
        "lane": lane,
        "visual_type": visual_type,
        "status": status,
        "locked": locked,
        "settings": settings or {},
    }
    if reference_assets is not None:
        fields["reference_assets"] = reference_assets
    return Shot(**fields)


def continuity(shot_id: str, group: str = "hero") -> dict:
    return {"h3_continuity": {
        "enabled": True, "group": group, "predecessor_shot_id": shot_id,
    }}


def make_asset(
    asset_id: str,
    shot_id: str | None,
    *,
    revision: int = 0,
    role: str = "visual",
    created_order: int = 0,
    content_hash: str | None = None,
) -> Asset:
    stamp = datetime(2026, 8, 25, 12, 0, created_order, tzinfo=timezone.utc)
    scene_for_shot = {"shot-a": "scene-1", "shot-b": "scene-1"}.get(shot_id, "scene-2")
    return Asset(
        id=asset_id,
        project_id="proj-1",
        scene_id=scene_for_shot,
        shot_id=shot_id,
        type=AssetType.VIDEO,
        filepath=Path(f"shots/{asset_id}.mp4"),
        backend="mock",
        model="deterministic-placeholder-v1",
        model_version="1",
        workflow_version="minimax-h3-av-v1",
        seed=1 + created_order,
        settings={
            "role": role,
            "visual_type": "h3_audiovisual",
            "visual_revision": revision,
        },
        hash=content_hash,
        created_at=stamp,
    )


# ---------------------------------------------------------------------------
# Current visual selection


def test_current_visual_asset_prefers_latest_matching_revision() -> None:
    shot = make_shot("shot-a", settings={"visual_revision": 2})
    stale = make_asset("old", "shot-a", revision=1, created_order=0)
    current = make_asset("new", "shot-a", revision=2, created_order=1)
    assert current_visual_asset(shot, [stale, current]) is current


def test_current_visual_asset_ignores_other_shots_roles_and_types() -> None:
    shot = make_shot("shot-a")
    other_shot = make_asset("x", "shot-b", created_order=0)
    narration_role = make_asset("y", "shot-a", role="narration", created_order=1)
    wrong_type = make_asset("z", "shot-a", created_order=2)
    wrong_type = wrong_type.model_copy(
        update={"settings": {**wrong_type.settings, "visual_type": "krea2_still"}},
    )
    assert current_visual_asset(shot, [other_shot, narration_role, wrong_type]) is None


def test_current_visual_asset_without_revision_settings_returns_newest() -> None:
    shot = make_shot("shot-a")
    first = make_asset("a1", "shot-a", created_order=0)
    second = make_asset("a2", "shot-a", created_order=1)
    assert current_visual_asset(shot, [first, second]) is second


# ---------------------------------------------------------------------------
# Dependency edges


def test_dependency_edges_combine_continuity_and_all_reference_roles() -> None:
    source_media = make_asset("src-asset", "shot-a", created_order=0)
    consumer = make_shot(
        "shot-c",
        scene_id="scene-2",
        settings=continuity("shot-a"),
        reference_assets=[
            ShotReference(role=ReferenceRole.FIRST_FRAME, asset_id="src-asset"),
            ShotReference(role=ReferenceRole.CONTINUITY, asset_id="src-asset"),
            # Style/composition hashes participate in the cache key too, so
            # they are dependency-bearing exactly like frame references.
            ShotReference(role=ReferenceRole.STYLE, asset_id="src-asset"),
            ShotReference(role=ReferenceRole.COMPOSITION, asset_id="src-asset"),
        ],
    )
    producer = make_shot("shot-a")
    edges = dependency_edges([producer, consumer], [source_media])
    assert edges["shot-c"] == {"shot-a"}
    assert "shot-a" not in edges


@pytest.mark.parametrize(
    "role",
    [
        ReferenceRole.STYLE,
        ReferenceRole.COMPOSITION,
        ReferenceRole.CHARACTER,
        ReferenceRole.SOURCE_EVIDENCE,
        ReferenceRole.FIRST_FRAME,
        ReferenceRole.CONTINUITY,
    ],
)
def test_every_reference_role_is_dependency_bearing(role: ReferenceRole) -> None:
    source_media = make_asset("m-src", "shot-a", created_order=0)
    producer = make_shot("shot-a")
    consumer = make_shot(
        "shot-consumer",
        scene_id="scene-3",
        reference_assets=[ShotReference(role=role, asset_id="m-src")],
    )

    impact = plan_regeneration(
        "shot-a", [producer, consumer], [source_media],
    )

    assert set(impact.stale_shot_ids) == {"shot-consumer"}


def test_reference_edge_to_own_asset_is_not_a_cycle() -> None:
    own_media = make_asset("own", "shot-c", created_order=0)
    shot = make_shot(
        "shot-c",
        reference_assets=[
            ShotReference(role=ReferenceRole.CONTINUITY, asset_id="own"),
        ],
    )
    assert dependency_edges([shot], [own_media]) == {}


# ---------------------------------------------------------------------------
# Regeneration planning and application


def chain_with_media() -> tuple[list[Shot], list[Asset]]:
    first = make_shot("shot-a", index=0)
    second = make_shot("shot-b", index=1, settings=continuity("shot-a"))
    third = make_shot(
        "shot-c", scene_id="scene-2", index=0, settings=continuity("shot-b"),
    )
    independent = make_shot(
        "shot-d", scene_id="scene-2", index=1, settings=continuity(None, group="other"),
    )
    media = [
        make_asset("m-a", "shot-a", created_order=0),
        make_asset("m-b", "shot-b", created_order=1),
        make_asset("m-c", "shot-c", created_order=2),
        make_asset("m-d", "shot-d", created_order=3),
    ]
    return [first, second, third, independent], media


SCENE_ORDER = {"scene-1": 0, "scene-2": 1}


def _timeline_keys(shots: list[Shot]) -> dict[str, tuple[int, int]]:
    return {
        item.id: (SCENE_ORDER.get(item.scene_id, 0), item.index)
        for item in shots
    }


def test_plan_regeneration_targets_only_chain_dependents() -> None:
    shots, media = chain_with_media()

    impact = plan_regeneration("shot-a", shots, media)

    assert isinstance(impact, RegenerationImpact)
    assert impact.regenerated_shot_id == "shot-a"
    assert set(impact.stale_shot_ids) == {"shot-b", "shot-c"}
    assert impact.locked_skipped_shot_ids == ()
    keys = _timeline_keys(shots)
    order = [keys[item] for item in impact.stale_shot_ids]
    assert order == sorted(order)


def test_plan_regeneration_via_typed_first_frame_reference() -> None:
    source_media = make_asset("m-a", "shot-a", created_order=0)
    producer = make_shot("shot-a")
    consumer = make_shot(
        "shot-ref",
        scene_id="scene-3",
        index=0,
        reference_assets=[
            ShotReference(role=ReferenceRole.FIRST_FRAME, asset_id="m-a"),
        ],
    )
    unrelated = make_shot("shot-z", scene_id="scene-3", index=1)

    impact = plan_regeneration(
        "shot-a", [producer, consumer, unrelated], [source_media],
    )

    assert set(impact.stale_shot_ids) == {"shot-ref"}


def test_locked_dependent_is_reported_but_not_marked() -> None:
    shots, media = chain_with_media()
    shots[1] = shots[1].model_copy(update={"locked": True})

    impact = plan_regeneration("shot-a", shots, media)

    assert "shot-b" in impact.locked_skipped_shot_ids
    assert "shot-b" not in impact.stale_shot_ids
    # Transitive dependents beyond the locked shot still need re-review.
    assert "shot-c" in impact.stale_shot_ids


def test_locked_scene_defers_staleness_for_its_shots() -> None:
    shots, media = chain_with_media()
    scenes = [
        Scene(id="scene-1", project_id="proj-1", index=0, duration=10.0),
        Scene(
            id="scene-2", project_id="proj-1", index=1, duration=10.0,
            status="locked", locked=True,
        ),
    ]

    impact = plan_regeneration("shot-a", shots, media, scenes=scenes)

    assert "shot-b" in impact.stale_shot_ids  # unlocked scene keeps marking
    assert "shot-c" not in impact.stale_shot_ids  # deferred: scene-2 is locked
    assert "shot-c" in impact.locked_scene_skipped_shot_ids


def test_unknown_source_shot_raises_key_error() -> None:
    shots, media = chain_with_media()
    with pytest.raises(KeyError):
        plan_regeneration("ghost", shots, media)


def test_apply_regeneration_staleness_mutates_only_impacted_copies() -> None:
    shots, media = chain_with_media()
    stamp = datetime(2026, 8, 25, 13, 30, tzinfo=timezone.utc)

    impact = plan_regeneration("shot-a", shots, media)
    updated = apply_regeneration_staleness(shots, impact, marked_at=stamp)
    by_id = {item.id: item for item in updated}

    assert by_id["shot-b"].status is ShotStatus.DRAFT
    assert by_id["shot-b"].settings["staleness"]["marked_at"] == stamp.isoformat()
    assert by_id["shot-c"].status is ShotStatus.DRAFT
    assert by_id["shot-a"] is shots[0]
    assert by_id["shot-d"] is shots[3]
    # Originals untouched.
    assert shots[1].status is ShotStatus.READY
    # Media records were never rewritten: hashes stay exactly as recorded.
    assert all(asset.hash is None for asset in media)


def test_apply_regeneration_keeps_queued_status_instead_of_drafting_it() -> None:
    shots, media = chain_with_media()
    shots[1] = shots[1].model_copy(update={"status": ShotStatus.QUEUED})
    impact = plan_regeneration("shot-a", shots, media)

    updated = apply_regeneration_staleness(shots, impact)
    by_id = {item.id: item for item in updated}

    assert by_id["shot-b"].status is ShotStatus.QUEUED
    assert by_id["shot-b"].settings["staleness"]["reason"] == "predecessor_regenerated"


# ---------------------------------------------------------------------------
# Provenance summaries


def test_provenance_summary_reports_identity_and_hash() -> None:
    shots, media = chain_with_media()
    media[0] = make_asset(
        "m-a", "shot-a", created_order=0, content_hash="f" * 64,
    )
    shot = next(item for item in shots if item.id == "shot-a")

    summary = provenance_summary(shot, media)

    assert summary["shot_id"] == "shot-a"
    assert summary["lane"] == "h3"
    assert summary["sha256"] == "f" * 64
    assert summary["backend"] == "mock"
    assert summary["workflow_version"] == "minimax-h3-av-v1"


def test_provenance_summary_without_media_is_none_safe() -> None:
    shot = make_shot("shot-lonely", scene_id="scene-9")

    summary = provenance_summary(shot, [])

    assert summary["asset_id"] is None
    assert summary["sha256"] is None


# The continuity error contract stays importable from the models package so
# dispatch code can map both failure families onto structured API errors.
def test_continuity_errors_remain_structured() -> None:
    error = ShotContinuityError("bad chain", ShotContinuityErrorCode.CYCLE)
    assert error.as_dict()["code"] == "continuity_cycle"
