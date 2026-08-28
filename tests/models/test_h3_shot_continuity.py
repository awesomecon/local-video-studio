"""H3 continuity generalized to predecessor SHOT ids (mock-only coverage).

Chain validation codes, transitive staleness propagation by shot id without
media deletion, serial ordering of mixed-lane batches, scene-level legacy
link compatibility, and the muted native-audio default.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.models.h3_shot_continuity import (
    H3_NATIVE_AUDIO_MIX_POLICY,
    ShotContinuityError,
    ShotContinuityErrorCode,
    build_shots_index,
    direct_dependents,
    effective_predecessor_shot_id,
    last_eligible_h3_shot_id,
    mark_dependents_stale,
    native_audio_policy,
    order_for_serial_execution,
    parse_shot_continuity,
    stale_closure,
    validate_continuity_chain,
)
from backend.schemas.models import Scene
from backend.schemas.shots import AudioMixPolicy, Shot, ShotLane, ShotStatus


def make_shot(
    shot_id: str,
    *,
    scene_id: str = "scene-1",
    index: int = 0,
    lane: ShotLane = ShotLane.H3,
    settings: dict | None = None,
    status: ShotStatus = ShotStatus.READY,
    locked: bool = False,
    visual_type: str = "h3_audiovisual",
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
    }
    if settings is not None:
        fields["settings"] = settings
    return Shot(**fields)


def continuity_settings(predecessor_shot_id: str | None, group: str = "hero") -> dict:
    payload: dict = {"enabled": True, "group": group}
    if predecessor_shot_id is not None:
        payload["predecessor_shot_id"] = predecessor_shot_id
    return {"h3_continuity": payload}


def chain_fixture() -> tuple[Shot, Shot, Shot, Shot]:
    """A -> B -> C inside one continuity group; D independent."""
    first = make_shot("shot-a", index=0)
    second = make_shot(
        "shot-b", index=1, settings=continuity_settings("shot-a"),
    )
    third = make_shot(
        "shot-c", scene_id="scene-2", index=0,
        settings=continuity_settings("shot-b"),
    )
    independent = make_shot(
        "shot-d", scene_id="scene-2", index=1,
        settings=continuity_settings(None, group="other"),
    )
    return first, second, third, independent


# ---------------------------------------------------------------------------
# Parsing and validation


def test_parse_shot_continuity_reads_shot_and_legacy_links() -> None:
    link = parse_shot_continuity(continuity_settings("shot-a"))
    assert link.enabled and link.predecessor_shot_id == "shot-a"
    assert not link.legacy_scene_level

    legacy = parse_shot_continuity({
        "h3_continuity": {
            "enabled": True,
            "predecessor_scene_id": "scene-0",
        },
    })
    assert legacy.predecessor_scene_id == "scene-0"
    assert legacy.predecessor_shot_id is None
    assert legacy.legacy_scene_level

    disabled = parse_shot_continuity({"h3_continuity": False})
    assert not disabled.enabled


def test_effective_predecessor_resolves_legacy_scene_link_to_implicit_shot() -> None:
    shot = make_shot("shot-x", settings={
        "h3_continuity": {"enabled": True, "predecessor_scene_id": "scene-0"},
    })
    # Without project context the deterministic implicit projection is used.
    assert effective_predecessor_shot_id(shot) == "scene-0-implicit"


def test_legacy_scene_link_targets_last_eligible_h3_shot_when_explicit() -> None:
    follower = make_shot("shot-x", scene_id="scene-1", settings={
        "h3_continuity": {"enabled": True, "predecessor_scene_id": "scene-0"},
    })
    stored = [
        make_shot("legacy-a", scene_id="scene-0", index=0),
        make_shot("legacy-mid", scene_id="scene-0", index=1),
        # A later non-H3 beat must not steal the link target...
        make_shot(
            "legacy-image", scene_id="scene-0", index=2,
            lane=ShotLane.IMAGE, visual_type="krea2_still",
        ),
        # ...and neither may an H3-lane shot with a non-H3 visual type.
        make_shot(
            "legacy-wrong-type", scene_id="scene-0", index=3,
            lane=ShotLane.H3, visual_type="qwen_image_still",
        ),
    ]
    resolved = effective_predecessor_shot_id(follower, stored)
    assert resolved == "legacy-mid"
    assert last_eligible_h3_shot_id("scene-0", stored) == "legacy-mid"


def test_legacy_scene_link_without_eligible_shots_fails_validation() -> None:
    follower = make_shot("shot-x", scene_id="scene-1", index=0, settings={
        "h3_continuity": {"enabled": True, "predecessor_scene_id": "scene-0"},
    })
    stored_images = [
        make_shot(
            "still-a", scene_id="scene-0", index=0,
            lane=ShotLane.IMAGE, visual_type="krea2_still",
        ),
        make_shot(
            "still-b", scene_id="scene-0", index=1,
            lane=ShotLane.IMAGE, visual_type="qwen_image_still",
        ),
    ]
    # The predecessor scene stores explicit shots, so build_shots_index adds
    # no implicit projection and the ineligible link must surface as a
    # missing predecessor instead of silently rooting the chain.
    by_id = build_shots_index(stored_images + [follower])
    with pytest.raises(ShotContinuityError) as excinfo:
        validate_continuity_chain(
            follower, by_id, scene_order={"scene-0": 0, "scene-1": 1},
        )
    assert excinfo.value.code == ShotContinuityErrorCode.MISSING_PREDECESSOR.value


def test_regenerating_an_earlier_scene_beat_does_not_stale_via_last_rule() -> None:
    head = make_shot("legacy-head", scene_id="scene-0", index=0)
    tail = make_shot("legacy-tail", scene_id="scene-0", index=1)
    follower = make_shot("shot-x", scene_id="scene-1", index=0, settings={
        "h3_continuity": {"enabled": True, "predecessor_scene_id": "scene-0"},
    })
    updated, skipped = mark_dependents_stale("legacy-head", [head, tail, follower])
    assert skipped == []
    follower_after = next(item for item in updated if item.id == "shot-x")
    # The scene-level link binds to the last eligible H3 beat only.
    assert follower_after.status is ShotStatus.READY
    assert "staleness" not in follower_after.settings


def test_legacy_scene_link_to_own_scene_is_a_self_link() -> None:
    selfish = make_shot("shot-s", scene_id="scene-0", index=2, settings={
        "h3_continuity": {"enabled": True, "predecessor_scene_id": "scene-0"},
    })
    earlier = make_shot("shot-p", scene_id="scene-0", index=1)
    by_id = build_shots_index([earlier, selfish])
    with pytest.raises(ShotContinuityError) as excinfo:
        validate_continuity_chain(
            selfish, by_id, scene_order={"scene-0": 0},
        )
    assert excinfo.value.code == ShotContinuityErrorCode.SELF_LINK.value


def test_legacy_scene_link_validates_against_last_explicit_shot() -> None:
    legacy_shots = [
        make_shot("legacy-first", scene_id="scene-0", index=0),
        make_shot("legacy-last", scene_id="scene-0", index=1),
    ]
    follower = make_shot("shot-x", scene_id="scene-1", index=0, settings={
        "h3_continuity": {"enabled": True, "predecessor_scene_id": "scene-0"},
    })
    by_id = build_shots_index(legacy_shots + [follower])
    validate_continuity_chain(
        follower, by_id, scene_order={"scene-0": 0, "scene-1": 1},
    )


def test_valid_chain_passes_validation() -> None:
    first, second, _, independent = chain_fixture()
    by_id = build_shots_index([first, second, independent])
    validate_continuity_chain(first, by_id)
    validate_continuity_chain(second, by_id)
    validate_continuity_chain(independent, by_id)


SCENE_ORDER = {"scene-1": 0, "scene-2": 1}


@pytest.mark.parametrize(
    ("mutation", "target_id", "expected_code"),
    [
        (
            lambda shots: [
                item.model_copy(update={"settings": continuity_settings("shot-b")})
                if item.id == "shot-b" else item
                for item in shots
            ],
            "shot-b",
            ShotContinuityErrorCode.SELF_LINK.value,
        ),
        (
            lambda shots: [
                item.model_copy(update={"settings": continuity_settings("ghost")})
                if item.id == "shot-c" else item
                for item in shots
            ],
            "shot-c",
            ShotContinuityErrorCode.MISSING_PREDECESSOR.value,
        ),
        (
            lambda shots: [
                item.model_copy(update={"settings": continuity_settings("shot-c")})
                if item.id == "shot-a" else item
                for item in shots
            ],
            "shot-a",
            ShotContinuityErrorCode.FORWARD_LINK.value,
        ),
        (
            lambda shots: [
                item.model_copy(update={
                    "lane": ShotLane.IMAGE, "visual_type": "krea2_still",
                })
                if item.id == "shot-a" else item
                for item in shots
            ],
            "shot-b",
            ShotContinuityErrorCode.PREDECESSOR_NOT_H3.value,
        ),
    ],
)
def test_invalid_chains_raise_structured_codes(
    mutation, target_id: str, expected_code: str,
) -> None:
    shots = mutation(list(chain_fixture()))
    by_id = build_shots_index(shots)
    with pytest.raises(ShotContinuityError) as excinfo:
        validate_continuity_chain(by_id[target_id], by_id, scene_order=SCENE_ORDER)
    assert excinfo.value.code == expected_code


def test_cross_scene_chains_need_the_scene_order_map() -> None:
    # Scene indexes alone cannot order shots across scenes: a scene-2/index-0
    # shot would compare equal to its scene-1/index-1 predecessor. Passing the
    # scene-order map is required, and its absence must fail loudly rather
    # than silently accept or reject the chain.
    shots = list(chain_fixture())
    third = shots[2]
    by_id = build_shots_index(shots)
    validate_continuity_chain(third, by_id, scene_order=SCENE_ORDER)


def test_cycle_through_predecessors_is_detected() -> None:
    first = make_shot("shot-a", index=0, settings=continuity_settings("shot-c"))
    second = make_shot("shot-b", index=1, settings=continuity_settings("shot-a"))
    third = make_shot(
        "shot-c", scene_id="scene-2", index=0,
        settings=continuity_settings("shot-b"),
    )
    by_id = build_shots_index([first, second, third])
    with pytest.raises(ShotContinuityError) as excinfo:
        validate_continuity_chain(
            third, by_id, scene_order={"scene-1": 0, "scene-2": 1},
        )
    assert excinfo.value.code == ShotContinuityErrorCode.CYCLE.value


def test_legacy_scene_level_link_validates_against_implicit_projection() -> None:
    legacy_scene = Scene(
        id="scene-0", project_id="proj-1", index=0, duration=5.0,
        visual_type="h3_audiovisual",
    )
    follower = make_shot(
        "shot-x", scene_id="scene-1", index=0,
        settings={"h3_continuity": {
            "enabled": True,
            "predecessor_scene_id": "scene-0",
        }},
    )
    by_id = build_shots_index([follower], scenes=[legacy_scene])
    assert "scene-0-implicit" in by_id
    validate_continuity_chain(
        follower,
        by_id,
        scene_order={"scene-0": 0, "scene-1": 1},
    )


def test_staleness_follows_legacy_scene_links_through_explicit_shots() -> None:
    head = make_shot("legacy-head", scene_id="scene-0", index=0)
    tail = make_shot("legacy-tail", scene_id="scene-0", index=1)
    follower = make_shot(
        "shot-x", scene_id="scene-1", index=0,
        status=ShotStatus.APPROVED,
        settings={
            "h3_continuity": {
                "enabled": True, "predecessor_scene_id": "scene-0",
            },
        },
    )
    shots = [head, tail, follower]

    updated, skipped = mark_dependents_stale("legacy-tail", shots)

    by_id = {item.id: item for item in updated}
    assert skipped == []
    # The scene-level link resolves to the LAST eligible H3 shot, so
    # regenerating it stales the follower while regenerating an earlier beat
    # does not.
    assert by_id["shot-x"].status is ShotStatus.DRAFT
    assert by_id["shot-x"].settings["staleness"]["source_shot_id"] == "legacy-tail"


# ---------------------------------------------------------------------------
# Staleness propagation by shot id


def test_staleness_propagates_transitively_by_shot_id() -> None:
    first, second, third, independent = chain_fixture()
    closure = {item.id for item in stale_closure("shot-a", [first, second, third, independent])}
    assert closure == {"shot-b", "shot-c"}


def test_mark_dependents_stale_keeps_media_records_and_flags_reason() -> None:
    first, second, third, independent = chain_fixture()
    second = second.model_copy(update={"status": ShotStatus.APPROVED})
    stamp = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    updated, skipped = mark_dependents_stale(
        "shot-a", [first, second, third, independent], marked_at=stamp,
    )
    by_id = {item.id: item for item in updated}

    assert skipped == []
    assert by_id["shot-b"].status is ShotStatus.DRAFT
    marker = by_id["shot-b"].settings["staleness"]
    assert marker["source_shot_id"] == "shot-a"
    assert marker["reason"] == "predecessor_regenerated"
    assert by_id["shot-c"].status is ShotStatus.DRAFT
    # Independent shots are untouched.
    assert by_id["shot-d"].status is ShotStatus.READY
    assert "staleness" not in by_id["shot-d"].settings
    assert by_id["shot-a"].status is ShotStatus.READY
    # Inputs are never mutated.
    original = next(item for item in [first, second, third, independent] if item.id == "shot-b")
    assert original.status is ShotStatus.APPROVED
    assert "staleness" not in original.settings


def test_locked_dependents_are_skipped_by_staleness_marking() -> None:
    first, second, third, _ = chain_fixture()
    second = second.model_copy(update={"status": ShotStatus.APPROVED, "locked": True})

    updated, skipped = mark_dependents_stale("shot-a", [first, second, third])

    assert skipped == ["shot-b"]
    by_id = {item.id: item for item in updated}
    assert by_id["shot-b"].locked is True
    assert by_id["shot-b"].status is ShotStatus.APPROVED
    # Transitive marking stops at the locked shot's media but continues past it.
    assert by_id["shot-c"].status is ShotStatus.DRAFT


def test_direct_dependents_lists_immediate_children_only() -> None:
    first, second, third, independent = chain_fixture()
    dependents = direct_dependents("shot-a", [first, second, third, independent])
    assert [item.id for item in dependents] == ["shot-b"]
    deep = direct_dependents("shot-b", [first, second, third, independent])
    assert [item.id for item in deep] == ["shot-c"]


# ---------------------------------------------------------------------------
# Serial execution of mixed-lane batches


def test_order_for_serial_execution_runs_chains_head_to_tail() -> None:
    first, second, third, independent = chain_fixture()
    image_shot = make_shot(
        "shot-img", scene_id="scene-3", index=0,
        lane=ShotLane.IMAGE, visual_type="qwen_image_still",
        settings={},
    )
    batch = [third, image_shot, independent, second, first]

    ordered = order_for_serial_execution(
        batch, scene_order={"scene-1": 0, "scene-2": 1, "scene-3": 2},
    )

    positions = {item.id: position for position, item in enumerate(ordered)}
    assert set(positions) == {
        "shot-a", "shot-b", "shot-c", "shot-d", "shot-img",
    }
    assert positions["shot-a"] < positions["shot-b"] < positions["shot-c"]
    # Every shot appears exactly once: a flat, GPU-serializable sequence.
    assert len(ordered) == len(batch)


def test_order_for_serial_execution_rejects_unserializable_cycle() -> None:
    first = make_shot("shot-a", index=0, settings=continuity_settings("shot-c"))
    second = make_shot("shot-b", index=1, settings=continuity_settings("shot-a"))
    third = make_shot(
        "shot-c", scene_id="scene-2", index=0,
        settings=continuity_settings("shot-b"),
    )
    with pytest.raises(ShotContinuityError):
        order_for_serial_execution(
            [first, second, third], scene_order={"scene-1": 0, "scene-2": 1},
        )


# ---------------------------------------------------------------------------
# Native audio policy


def test_h3_native_audio_defaults_to_mute() -> None:
    assert H3_NATIVE_AUDIO_MIX_POLICY is AudioMixPolicy.MUTE
    shot = make_shot("shot-a")
    assert native_audio_policy(shot) is AudioMixPolicy.MUTE


def test_native_audio_policy_requires_explicit_opt_in() -> None:
    enabled = make_shot("shot-a", settings={"h3_native_audio_mix_policy": "foreground"})
    assert native_audio_policy(enabled) is AudioMixPolicy.FOREGROUND
    nonsense = make_shot("shot-a", settings={"h3_native_audio_mix_policy": "loud"})
    assert native_audio_policy(nonsense) is AudioMixPolicy.MUTE
