import math
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.schemas import (
    AudioCue,
    AudioCueKind,
    AudioMixPolicy,
    OverlayCue,
    OverlayKind,
    Scene,
    Shot,
    ShotLane,
    ShotStartMode,
    ShotStatus,
    ShotTimingError,
    ShotTransition,
    ShotTransitionKind,
    VisualType,
    compile_shot_timings,
    effective_shots,
    implicit_shot_from_scene,
    implicit_shot_id,
    scene_rendered_duration,
    validate_shot_sequence,
)


def make_scene() -> Scene:
    return Scene(project_id="p1", index=0, duration=10, title="Opening",
                 visual_type=VisualType.GRAPHIC_SCREEN)


def make_shot(**overrides) -> Shot:
    fields = dict(project_id="p1", scene_id="s1", index=0, duration_seconds=4)
    fields.update(overrides)
    return Shot(**fields)


def overlay(**overrides) -> OverlayCue:
    fields = dict(kind=OverlayKind.EXACT_TEXT, exact_text="AUGUST 7, 1976",
                  start_seconds=0.5, duration_seconds=1.0)
    fields.update(overrides)
    return OverlayCue(**fields)


def test_shot_round_trips_through_json() -> None:
    shot = make_shot(
        lane=ShotLane.HTML,
        overlays=[overlay()],
        audio_cues=[AudioCue(kind=AudioCueKind.AMBIENCE, asset_id="a1")],
        transition_in=ShotTransition(kind=ShotTransitionKind.CROSSFADE,
                                     duration_seconds=0.5),
        reference_assets=[],
    )
    restored = Shot.model_validate_json(shot.model_dump_json())
    assert restored == shot
    assert restored.transition_in.kind is ShotTransitionKind.CROSSFADE


def test_shot_defaults_to_weighted_timing() -> None:
    assert make_shot().start_mode is ShotStartMode.WEIGHTED


@pytest.mark.parametrize("bad_duration", [0, -1, float("inf"), float("nan")])
def test_shot_duration_must_be_finite_and_positive(bad_duration: float) -> None:
    with pytest.raises(ValidationError):
        make_shot(duration_seconds=bad_duration)


def test_source_trim_requires_both_bounds_ordered() -> None:
    with pytest.raises(ValidationError, match="set together"):
        make_shot(source_in_seconds=1.0)
    with pytest.raises(ValidationError, match="set together"):
        make_shot(source_out_seconds=3.0)
    with pytest.raises(ValidationError, match="greater"):
        make_shot(source_in_seconds=3.0, source_out_seconds=3.0)
    trimmed = make_shot(source_in_seconds=1.0, source_out_seconds=3.5)
    assert trimmed.source_out_seconds == 3.5


def test_cut_transition_zeroes_overlap_and_dissolve_aliases_crossfade() -> None:
    cut = ShotTransition(kind=ShotTransitionKind.CUT, duration_seconds=9)
    assert cut.duration_seconds == 0.0
    dissolve = ShotTransition(kind=ShotTransitionKind.DISSOLVE, duration_seconds=0.4)
    assert dissolve.kind.renders_as == "crossfade"
    with pytest.raises(ValidationError, match="positive duration_seconds"):
        ShotTransition(kind=ShotTransitionKind.FADE_THROUGH_BLACK, duration_seconds=0)
    with pytest.raises(ValidationError, match="positive duration_seconds"):
        ShotTransition(kind=ShotTransitionKind.DIP_TO_WHITE)


def test_overlay_payload_rules() -> None:
    with pytest.raises(ValidationError, match="exact_text"):
        overlay(exact_text=None)
    with pytest.raises(ValidationError, match="asset_id"):
        overlay(kind=OverlayKind.GRAPHIC)
    with pytest.raises(ValidationError, match="require asset_id"):
        overlay(kind=OverlayKind.IMAGE)
    with pytest.raises(ValidationError, match="cannot reference an asset"):
        overlay(asset_id="a9")
    with pytest.raises(ValidationError, match="cannot exceed"):
        overlay(fade_in_seconds=0.8, fade_out_seconds=0.8)
    with pytest.raises(ValidationError, match="together"):
        overlay(width=100)
    ok = overlay(
        kind=OverlayKind.IMAGE, exact_text=None, asset_id="doc1",
        width=200, height=80, anchor="bottom_left", x=64, y=64,
    )
    assert ok.end_seconds == 1.5


def test_overlays_must_fit_inside_the_shot_and_have_unique_ids() -> None:
    with pytest.raises(ValidationError, match="after the shot ends"):
        make_shot(duration_seconds=2, overlays=[
            overlay(start_seconds=1.5, duration_seconds=1.0),
        ])
    duplicate = overlay()
    with pytest.raises(ValidationError, match="unique"):
        make_shot(overlays=[duplicate, duplicate])


def test_audio_cues_validate_gain_fades_and_fit() -> None:
    with pytest.raises(ValidationError):
        AudioCue(kind=AudioCueKind.EFFECT, asset_id="a1", gain_db=40)
    with pytest.raises(ValidationError, match="cannot exceed"):
        AudioCue(kind=AudioCueKind.EFFECT, asset_id="a1", duration_seconds=1,
                 fade_in_seconds=0.7, fade_out_seconds=0.7)
    with pytest.raises(ValidationError, match="after the shot ends"):
        make_shot(duration_seconds=2, audio_cues=[
            AudioCue(kind=AudioCueKind.AMBIENCE, asset_id="a1",
                     start_seconds=1, duration_seconds=2),
        ])
    cue = AudioCue(kind=AudioCueKind.NATIVE_CLIP, asset_id="v1")
    assert isinstance(cue.mix_policy, AudioMixPolicy)


def test_sequence_validation_indexes_and_transitions() -> None:
    shots = [
        make_shot(index=0, duration_seconds=6),
        make_shot(index=1, duration_seconds=4,
                  transition_in=ShotTransition(kind=ShotTransitionKind.CROSSFADE,
                                               duration_seconds=1)),
    ]
    validate_shot_sequence(shots)
    assert scene_rendered_duration(shots) == 9.0
    with pytest.raises(ShotTimingError, match="unique"):
        validate_shot_sequence([shots[0], shots[1],
                                make_shot(index=1, duration_seconds=2)])
    with pytest.raises(ShotTimingError, match="contiguous"):
        validate_shot_sequence([shots[0], make_shot(index=2, duration_seconds=2)])
    with pytest.raises(ShotTimingError, match="shorter than both adjacent shots"):
        validate_shot_sequence([
            make_shot(index=0, duration_seconds=1),
            make_shot(index=1, duration_seconds=4,
                      transition_in=ShotTransition(kind=ShotTransitionKind.CROSSFADE,
                                                   duration_seconds=1)),
        ])


def test_implicit_projection_from_legacy_scene() -> None:
    scene = make_scene()
    scene.transition = "dissolve"
    implicit = implicit_shot_from_scene(scene)
    assert implicit.id == implicit_shot_id(scene.id) == f"{scene.id}-implicit"
    assert implicit.lane is ShotLane.HTML
    assert implicit.visual_type is VisualType.GRAPHIC_SCREEN
    assert implicit.duration_seconds == scene.duration
    # dissolve projects onto crossfade with the legacy default overlap policy.
    assert implicit.transition_in.kind is ShotTransitionKind.CROSSFADE
    assert implicit.transition_in.duration_seconds == pytest.approx(0.35)
    cut_scene = make_scene()
    assert implicit_shot_from_scene(cut_scene).transition_in.kind \
        is ShotTransitionKind.CUT
    # Deterministic across calls so repeated reads are stable.
    assert implicit_shot_from_scene(scene).id == implicit.id


def test_effective_shots_falls_back_to_implicit() -> None:
    scene = make_scene()
    assert [shot.id for shot in effective_shots(scene, [])] == [implicit_shot_id(scene.id)]
    stored = [make_shot(index=1), make_shot(index=0)]
    ordered = effective_shots(scene, stored)
    assert [shot.index for shot in ordered] == [0, 1]


def test_compile_matches_target_within_one_frame() -> None:
    shots = [
        make_shot(index=0, duration_seconds=4),
        make_shot(index=1, duration_seconds=6),
    ]
    compiled = compile_shot_timings(shots, 10.0, fps=24)
    assert [item.start_seconds for item in compiled] == [0.0, 4.0]
    assert sum(item.duration_seconds for item in compiled) == 10.0
    # One frame of slack is accepted without retiming.
    compiled = compile_shot_timings(shots, 10.0 + 1 / 24 - 1e-9, fps=24)
    assert all(not item.adjusted for item in compiled)


def test_compile_redistributes_only_weighted_shots() -> None:
    shots = [
        make_shot(index=0, duration_seconds=4, start_mode=ShotStartMode.FIXED),
        make_shot(index=1, duration_seconds=6,
                  start_mode=ShotStartMode.WEIGHTED),
    ]
    compiled = compile_shot_timings(shots, 12.0, fps=24)
    assert compiled[0].duration_seconds == 4.0
    assert compiled[1].duration_seconds == 8.0
    assert compiled[1].adjusted
    assert compiled[1].start_seconds == 4.0
    assert abs(sum(item.end_seconds for item in compiled[-1:]) - 12.0) < 1e-6


def test_compile_with_only_fixed_shots_beyond_tolerance_raises() -> None:
    shots = [
        make_shot(index=0, duration_seconds=4, start_mode=ShotStartMode.FIXED),
        make_shot(index=1, duration_seconds=6, start_mode=ShotStartMode.FIXED),
    ]
    with pytest.raises(ShotTimingError, match="weighted"):
        compile_shot_timings(shots, 12.0, fps=24)


def test_compile_never_stretches_locked_weighted_shots() -> None:
    shots = [
        make_shot(index=0, duration_seconds=4, start_mode=ShotStartMode.WEIGHTED,
                  locked=True, status=ShotStatus.APPROVED),
        make_shot(index=1, duration_seconds=6, start_mode=ShotStartMode.FIXED),
    ]
    with pytest.raises(ShotTimingError):
        compile_shot_timings(shots, 14.0, fps=24)


def test_compile_respects_incoming_overlaps() -> None:
    shots = [
        make_shot(index=0, duration_seconds=6),
        make_shot(index=1, duration_seconds=4,
                  transition_in=ShotTransition(kind=ShotTransitionKind.CROSSFADE,
                                               duration_seconds=1.5)),
    ]
    compiled = compile_shot_timings(shots, 8.5, fps=24)
    assert compiled[1].start_seconds == pytest.approx(4.5)
    assert compiled[1].overlap_seconds == 1.5
    assert compiled[1].end_seconds == pytest.approx(8.5)


def test_compile_rejects_invalid_fps_and_empty_scenes() -> None:
    with pytest.raises(ValueError, match="fps"):
        compile_shot_timings([make_shot()], 4.0, fps=0)
    with pytest.raises(ShotTimingError, match="at least one shot"):
        compile_shot_timings([], 4.0)


def test_timestamps_default_to_utc() -> None:
    shot = make_shot()
    assert shot.created_at.tzinfo is timezone.utc
    assert shot.updated_at.tzinfo is timezone.utc


def test_scene_rendered_duration_handles_empty_list() -> None:
    assert scene_rendered_duration([]) == 0.0
    assert math.isfinite(scene_rendered_duration([]))
