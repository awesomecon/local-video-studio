"""Scene plan compilation from shot contracts: pure timing/frame tests."""

from __future__ import annotations

import pytest

from backend.schemas.shots import ShotTimingError
from backend.timeline.shots import compile_scene_plan, frame_count
from tests.multishot_fixtures import FPS, make_scene, make_shot

PROJECT = "proj"
SCENE = "scene"


def test_legacy_scene_without_shots_compiles_one_implicit_shot() -> None:
    scene = make_scene(PROJECT, duration=2.0, scene_id=SCENE)

    plan = compile_scene_plan(scene, [], fps=FPS)

    assert plan.ordered_shot_ids() == (f"{SCENE}-implicit",)
    assert plan.frame_counts == (24,)
    assert plan.boundaries == ()
    assert plan.total_frames == 24
    assert plan.duration_seconds == pytest.approx(2.0)


def test_cuts_concatenate_frame_counts() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=1.0),
        make_shot(PROJECT, SCENE, 1, duration_seconds=1.0),
        make_shot(PROJECT, SCENE, 2, duration_seconds=0.5),
    ]

    plan = compile_scene_plan(scene, shots, fps=FPS)

    assert plan.frame_counts == (12, 12, 6)
    assert plan.total_frames == 30
    assert all(boundary.kind == "cut" for boundary in plan.boundaries)
    # Second cut begins exactly where the first shot ends.
    assert [b.offset_frames for b in plan.boundaries] == [12, 24]


def test_overlap_subtraction_and_exact_offsets_from_compiled_timings() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=2.0),
        make_shot(
            PROJECT,
            SCENE,
            1,
            duration_seconds=1.0,
            transition_in={"kind": "crossfade", "duration_seconds": 0.5},
        ),
    ]

    plan = compile_scene_plan(scene, shots, fps=FPS)

    assert plan.frame_counts == (24, 12)
    boundary = plan.boundaries[0]
    assert boundary.kind == "crossfade"
    assert boundary.overlap_frames == 6
    assert boundary.offset_frames == 18
    assert boundary.offset_seconds == pytest.approx(1.5)
    assert plan.total_frames == 30


def test_dissolve_is_an_alias_compiled_as_crossfade() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=1.0),
        make_shot(
            PROJECT,
            SCENE,
            1,
            duration_seconds=1.0,
            transition_in={"kind": "dissolve", "duration_seconds": 0.25},
        ),
    ]

    plan = compile_scene_plan(scene, shots, fps=FPS)

    assert plan.boundaries[0].kind == "crossfade"
    assert plan.total_frames == 12 + 12 - 3


def test_weighted_shots_absorb_delta_proportionally_fixed_stay_untouched() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=2.0),
        make_shot(PROJECT, SCENE, 1, duration_seconds=1.0, start_mode="weighted"),
    ]

    plan = compile_scene_plan(scene, shots, fps=FPS, target_duration_seconds=4.0)

    assert plan.frame_counts[0] == 24  # fixed shot untouched
    assert plan.frame_counts[1] == 48 - 24  # weighted absorbs the whole delta
    assert plan.total_frames == 48
    assert plan.shots[1].adjusted is True
    assert plan.shots[0].adjusted is False


def test_locked_weighted_shot_is_never_retimed() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=2.0),
        make_shot(
            PROJECT, SCENE, 1, duration_seconds=1.0,
            start_mode="weighted", locked=True,
        ),
    ]

    with pytest.raises(ShotTimingError, match="start_mode='weighted'"):
        compile_scene_plan(scene, shots, fps=FPS, target_duration_seconds=4.0)


def test_mismatch_beyond_one_frame_without_weighted_raises_totals() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=2.0),
        make_shot(PROJECT, SCENE, 1, duration_seconds=2.0),
    ]

    with pytest.raises(ShotTimingError, match=r"4\.000000s.*5\.000000s|needs 5"):
        compile_scene_plan(scene, shots, fps=FPS, target_duration_seconds=5.0)


def test_one_frame_tolerance_accepts_without_retiming() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [make_shot(PROJECT, SCENE, 0, duration_seconds=2.0)]

    plan = compile_scene_plan(
        scene, shots, fps=FPS, target_duration_seconds=2.0 + 1.0 / FPS,
    )

    assert plan.shots[0].adjusted is False
    assert plan.total_frames == 25  # snapped onto the narration frame grid


def test_invalid_sequences_raise_shot_timing_error() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    gap = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=1.0),
        make_shot(PROJECT, SCENE, 2, duration_seconds=1.0),
    ]
    with pytest.raises(ShotTimingError, match="contiguous"):
        compile_scene_plan(scene, gap, fps=FPS)

    swallow = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=1.0),
        make_shot(
            PROJECT, SCENE, 1, duration_seconds=1.0,
            transition_in={"kind": "crossfade", "duration_seconds": 1.0},
        ),
    ]
    with pytest.raises(ShotTimingError, match="shorter than both"):
        compile_scene_plan(scene, swallow, fps=FPS)


def test_dip_transition_needs_two_frames_of_overlap() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=1.0),
        make_shot(
            PROJECT, SCENE, 1, duration_seconds=1.0,
            transition_in={"kind": "dip_to_white", "duration_seconds": 1.0 / FPS},
        ),
    ]

    with pytest.raises(ShotTimingError, match="two frames"):
        compile_scene_plan(scene, shots, fps=FPS)


def test_overlap_filling_a_whole_neighbor_on_the_frame_grid_raises() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    # Structurally valid (0.13 < 0.15) but rounds to an overlap that fills
    # every frame of the short neighbor at 12 fps.
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=0.15),
        make_shot(
            PROJECT, SCENE, 1, duration_seconds=1.0,
            transition_in={"kind": "crossfade", "duration_seconds": 0.13},
        ),
    ]

    with pytest.raises(ShotTimingError, match="shorter than both"):
        compile_scene_plan(scene, shots, fps=FPS)


def test_frame_count_helper_rounds_to_nearest_frame() -> None:
    assert frame_count(1.0, FPS) == 12
    assert frame_count(0.5, FPS) == 6
    assert frame_count(0.04, FPS) == 1
    with pytest.raises(ValueError, match="finite"):
        frame_count(float("nan"), FPS)


def test_twenty_fixed_shots_distribute_grid_residual_one_frame_each() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, index, duration_seconds=1.02) for index in range(20)
    ]

    plan = compile_scene_plan(scene, shots, fps=24)

    # Ideal length 24.48 frames each; the 10-frame scene residual spreads as
    # +1 to the ten largest fractional parts instead of piling onto one shot.
    assert plan.frame_counts.count(25) == 10
    assert plan.frame_counts.count(24) == 10
    assert plan.total_frames == 490 == sum(plan.frame_counts)
    for count in plan.frame_counts:
        assert abs(count - (1.02 * 24)) < 1.0  # every shot stays within a frame
    assert all(not shot.adjusted for shot in plan.shots)  # fixed shots unretimed


def test_distributed_rounding_respects_transition_overlaps() -> None:
    scene = make_scene(PROJECT, scene_id=SCENE)
    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=1.02),
        make_shot(
            PROJECT, SCENE, 1,
            duration_seconds=1.02,
            transition_in={"kind": "crossfade", "duration_seconds": 0.125},
        ),
    ]

    plan = compile_scene_plan(scene, shots, fps=24)

    # needed = round(1.915*24)=46 + 3 overlap = 49; ideals 24.48 -> floors
    # 24+24=48, so exactly one +1 lands on the largest fraction.
    assert sorted(plan.frame_counts) == [24, 25]
    assert plan.frame_counts[0] == 25  # identical fractions: lowest index wins
    assert plan.total_frames == 46
