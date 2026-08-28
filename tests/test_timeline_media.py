from pathlib import Path

import pytest

from backend.timeline.builder import SceneTiming, adjust_scene_durations, build_timeline
from backend.timeline.models import SubtitleCue, Timeline, TimelineClip


def test_adjust_scene_durations_matches_narration() -> None:
    adjusted = adjust_scene_durations([2.0, 3.0, 5.0], 20.0)

    assert sum(adjusted) == pytest.approx(20.0)
    assert all(duration >= 0.5 for duration in adjusted)
    assert adjusted[2] > adjusted[1] > adjusted[0]


def test_adjust_scene_durations_expansion_preserves_exact_proportions() -> None:
    source = [0.7, 1.1, 2.3]
    narration_duration = 7.9

    adjusted = adjust_scene_durations(source, narration_duration)
    scale = narration_duration / sum(source)

    assert adjusted[:-1] == [duration * scale for duration in source[:-1]]
    assert adjusted[-1] == narration_duration - sum(adjusted[:-1])
    assert sum(adjusted) == narration_duration
    assert all(result >= planned for result, planned in zip(adjusted, source, strict=True))


def test_expansion_is_not_blocked_by_contraction_minimum() -> None:
    adjusted = adjust_scene_durations([0.1, 0.1], 0.3, minimum_scene_seconds=0.5)

    assert adjusted == pytest.approx([0.15, 0.15])


def test_adjust_scene_durations_rejects_impossible_minimum() -> None:
    with pytest.raises(ValueError, match="too short"):
        adjust_scene_durations([1.0, 1.0], 0.5, minimum_scene_seconds=0.5)


def test_build_timeline_accounts_for_transition_overlap(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    timeline = build_timeline(
        [
            SceneTiming("one", first, 2.0),
            SceneTiming(
                "two",
                second,
                3.0,
                transition="crossfade",
                transition_duration_seconds=0.5,
            ),
        ],
        subtitles=[SubtitleCue(0.0, 1.0, "Opening")],
    )

    assert timeline.clips[1].start_seconds == pytest.approx(1.5)
    assert timeline.duration_seconds == pytest.approx(4.5)
    assert timeline.to_dict()["clips"][0]["path"] == str(first)


def test_timeline_json_is_human_readable(tmp_path: Path) -> None:
    timeline = build_timeline([SceneTiming("one", tmp_path / "image.png", 1.0, "image")])
    destination = timeline.write_json(tmp_path / "timeline.json")

    text = destination.read_text(encoding="utf-8")
    assert '"scene_id": "one"' in text
    assert str(tmp_path / "image.png") in text


def test_build_timeline_applies_narration_gain(tmp_path: Path) -> None:
    narration = tmp_path / "narration.wav"
    timeline = build_timeline(
        [SceneTiming("one", tmp_path / "image.png", 1.0, "image")],
        narration_path=narration,
        narration_gain_db=12,
    )

    assert timeline.audio_tracks[0].kind == "narration"
    assert timeline.audio_tracks[0].gain_db == 12


def test_zero_duration_crossfade_is_rejected(tmp_path: Path) -> None:
    clips = [
        TimelineClip("one", tmp_path / "a.mp4", 0.0, 2.0),
        TimelineClip(
            "two",
            tmp_path / "b.mp4",
            2.0,
            2.0,
            transition="crossfade",
            transition_duration_seconds=0.0,
        ),
    ]

    with pytest.raises(ValueError, match="positive transition"):
        Timeline(clips=clips).validate()


def test_negative_transition_duration_still_rejected(tmp_path: Path) -> None:
    clips = [
        TimelineClip(
            "one",
            tmp_path / "a.mp4",
            0.0,
            2.0,
            transition="crossfade",
            transition_duration_seconds=-1.0,
        ),
    ]

    with pytest.raises(ValueError, match="Transition duration"):
        Timeline(clips=clips).validate()


@pytest.mark.parametrize("field", ["start_seconds", "duration_seconds"])
def test_non_finite_clip_timing_is_rejected(tmp_path: Path, field: str) -> None:
    timing = {"start_seconds": 0.0, "duration_seconds": 1.0}
    timing[field] = float("nan")

    with pytest.raises(ValueError, match="finite"):
        Timeline(clips=[TimelineClip("one", tmp_path / "a.mp4", **timing)]).validate()


def test_non_finite_subtitle_cue_timing_is_rejected(tmp_path: Path) -> None:
    nan = float("nan")
    clips = [TimelineClip("one", tmp_path / "a.mp4", 0.0, 2.0)]

    for cue in (SubtitleCue(nan, 1.0, "x"), SubtitleCue(0.0, nan, "x")):
        with pytest.raises(ValueError, match="ordered timestamps"):
            Timeline(clips=clips, subtitles=[cue]).validate()


def test_build_timeline_rejects_nan_scene_duration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="finite"):
        build_timeline([SceneTiming("one", tmp_path / "a.mp4", float("nan"))])
