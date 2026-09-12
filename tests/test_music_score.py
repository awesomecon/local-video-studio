"""Score-plan schema validation, ordering, persistence, and provenance.

Phase 1 of Score Studio: cues describe exact musical moves that FFmpeg
applies to the generated soundtrack later. These tests pin the portable
``music/score-plan.json`` contract only - no audio rendering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.music.score import (
    SCORE_PLAN_FILENAME,
    SCORED_OUTPUT_FILENAME,
    ScoreAction,
    ScoreCue,
    ScorePlan,
    ScorePlanConflict,
    empty_score_plan,
    hash_audio_file,
    load_score_plan,
    resolve_cue_effect_path,
    save_score_plan,
    score_plan_hash,
    score_plan_path,
    scored_background_path,
    validate_score_plan_effects,
)


def _cue(time_seconds: float, action: str = "pull_back", **fields: object) -> ScoreCue:
    return ScoreCue(time_seconds=time_seconds, action=action, **fields)


def _plan(cues: list[ScoreCue] | None = None, **fields: object) -> ScorePlan:
    return ScorePlan(duration_seconds=43.446, cues=cues or [], **fields)


# ---------------------------------------------------------------------------
# Cue schema validation


def test_cue_defaults_are_conservative() -> None:
    cue = _cue(12.5)
    assert cue.id
    assert cue.action is ScoreAction.PULL_BACK
    assert cue.label == ""
    assert 0.0 < cue.transition_seconds <= 5.0
    assert cue.gain_db is None
    assert cue.lowpass_hz is None
    assert cue.effect_asset_id is None
    assert cue.effect_path is None
    assert cue.effect_gain_db is None
    assert cue.source == "manual"
    assert cue.locked is False


def test_cue_accepts_every_mvp_action() -> None:
    actions = [
        "build", "pull_back", "silence", "restore",
        "impact", "riser", "end_sting",
    ]
    for action in actions:
        assert _cue(1.0, action).action == action


def test_cue_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        _cue(1.0, "repaint_region")


def test_cue_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        _cue(1.0, source="telepathy")


def test_cue_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _cue(1.0, bogus_field=1)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", [
    "time_seconds", "transition_seconds", "gain_db", "lowpass_hz", "effect_gain_db",
])
def test_cue_rejects_non_finite_values(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        ScoreCue(**{**{"time_seconds": 1.0, "action": "build"}, field: bad})


def test_cue_gain_bounds_are_minus_sixty_to_twelve() -> None:
    assert _cue(1.0, gain_db=-60.0).gain_db == -60.0
    assert _cue(1.0, gain_db=12.0).gain_db == 12.0
    with pytest.raises(ValidationError):
        _cue(1.0, gain_db=-60.5)
    with pytest.raises(ValidationError):
        _cue(1.0, gain_db=12.5)
    with pytest.raises(ValidationError):
        _cue(1.0, effect_gain_db=40.0)


def test_cue_lowpass_bounds_are_two_hundred_to_twenty_thousand() -> None:
    assert _cue(1.0, lowpass_hz=200).lowpass_hz == 200.0
    assert _cue(1.0, lowpass_hz=20_000).lowpass_hz == 20_000.0
    with pytest.raises(ValidationError):
        _cue(1.0, lowpass_hz=199)
    with pytest.raises(ValidationError):
        _cue(1.0, lowpass_hz=20_001)


def test_cue_transition_bounds_are_zero_to_five_seconds() -> None:
    assert _cue(1.0, transition_seconds=0).transition_seconds == 0.0
    assert _cue(1.0, transition_seconds=5.0).transition_seconds == 5.0
    with pytest.raises(ValidationError):
        _cue(1.0, transition_seconds=-0.1)
    with pytest.raises(ValidationError):
        _cue(1.0, transition_seconds=5.5)


def test_cue_rejects_negative_time() -> None:
    with pytest.raises(ValidationError):
        _cue(-0.001)


@pytest.mark.parametrize("bad_path", [
    "/etc/passwd",
    "../outside.wav",
    "music/../../outside.wav",
    "..\\outside.wav",
    "C:\\Sounds\\hit.wav",
    "\\\\server\\share\\hit.wav",
    "music/effects/../effects/hit.wav",
])
def test_cue_effect_path_rejects_traversal_and_absolute_paths(bad_path: str) -> None:
    with pytest.raises(ValidationError):
        _cue(1.0, "impact", effect_path=bad_path)


def test_cue_effect_path_keeps_valid_project_relative_form() -> None:
    cue = _cue(1.0, "impact", effect_path="music/effects/hit-abc.wav")
    assert cue.effect_path == "music/effects/hit-abc.wav"


# ---------------------------------------------------------------------------
# Plan validation, ordering, and stable ids


def test_plan_sorts_cues_by_time_and_preserves_ids() -> None:
    late = _cue(31.7, "silence")
    early = _cue(24.1, "pull_back")
    tie_a = _cue(31.7, "impact")
    plan = _plan([late, early, tie_a])
    assert [cue.id for cue in plan.cues] == [early.id, late.id, tie_a.id]
    assert [cue.time_seconds for cue in plan.cues] == [24.1, 31.7, 31.7]


def test_plan_ids_survive_round_trip_unchanged() -> None:
    cues = [_cue(2.0, label="build"), _cue(8.5, "impact")]
    plan = _plan(cues)
    restored = ScorePlan.model_validate_json(plan.model_dump_json())
    assert [cue.id for cue in restored.cues] == [cue.id for cue in plan.cues]


def test_plan_rejects_duplicate_cue_ids() -> None:
    first = _cue(1.0)
    clone = first.model_copy(update={"time_seconds": 2.0})
    with pytest.raises(ValidationError, match="duplicate cue ids"):
        _plan([first, clone])


def test_plan_rejects_cue_past_soundtrack_end() -> None:
    with pytest.raises(ValidationError, match="past the end"):
        _plan([_cue(43.5)])


def test_plan_allows_cue_at_exact_soundtrack_end() -> None:
    plan = _plan([_cue(43.446)])
    assert plan.cues[0].time_seconds == 43.446


@pytest.mark.parametrize("duration", [0.0, -1.0, float("nan"), float("inf")])
def test_plan_rejects_invalid_duration(duration: float) -> None:
    with pytest.raises(ValidationError):
        ScorePlan(duration_seconds=duration)


def test_plan_rejects_unsupported_version() -> None:
    with pytest.raises(ValidationError, match="unsupported score plan version"):
        ScorePlan(duration_seconds=10.0, version=2)


def test_plan_rejects_malformed_source_music_hash() -> None:
    with pytest.raises(ValidationError, match="hex sha256"):
        ScorePlan(duration_seconds=10.0, source_music_hash="not-a-hash!")


def test_plan_normalizes_source_music_hash() -> None:
    digest = "AB" * 32
    plan = ScorePlan(duration_seconds=10.0, source_music_hash=f" {digest} ")
    assert plan.source_music_hash == digest.lower()


def test_empty_score_plan_has_no_cues() -> None:
    plan = empty_score_plan(30.0, source_music_hash="ab" * 16, source_backend="mock")
    assert plan.cues == []
    assert plan.duration_seconds == 30.0
    assert plan.source_backend == "mock"


# ---------------------------------------------------------------------------
# Persistence


def test_artifact_paths_live_under_music_directory(tmp_path: Path) -> None:
    assert score_plan_path(tmp_path) == tmp_path / "music" / SCORE_PLAN_FILENAME
    assert scored_background_path(tmp_path) == tmp_path / "music" / SCORED_OUTPUT_FILENAME


def test_load_missing_plan_returns_none(tmp_path: Path) -> None:
    assert load_score_plan(tmp_path) is None


def test_save_creates_directory_and_is_human_readable(tmp_path: Path) -> None:
    plan = _plan([_cue(24.1, "pull_back", gain_db=-7, lowpass_hz=3200)], revision=1)
    saved = save_score_plan(tmp_path, plan)
    text = score_plan_path(tmp_path).read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["cues"][0]["action"] == "pull_back"
    assert payload["cues"][0]["gain_db"] == -7
    assert payload["cues"][0]["lowpass_hz"] == 3200
    assert saved.model_dump() == load_score_plan(tmp_path).model_dump()
    # Pretty-printed, project-portable JSON (not one dense line).
    assert text.count("\n") > 10


def test_save_with_expected_revision_increments(tmp_path: Path) -> None:
    first = save_score_plan(tmp_path, _plan(revision=1), expected_revision=0)
    assert first.revision == 1
    second = save_score_plan(tmp_path, first.model_copy(update={"revision": 1}), expected_revision=1)
    assert second.revision == 2
    assert load_score_plan(tmp_path).revision == 2


def test_save_rejects_stale_revision_and_preserves_disk(tmp_path: Path) -> None:
    save_score_plan(tmp_path, _plan([_cue(5.0)]), expected_revision=0)
    save_score_plan(tmp_path, _plan([_cue(6.0)]), expected_revision=1)
    stale = _plan([_cue(9.0)])
    with pytest.raises(ScorePlanConflict, match="expected revision 1"):
        save_score_plan(tmp_path, stale, expected_revision=1)
    on_disk = load_score_plan(tmp_path)
    assert on_disk.revision == 2
    assert [cue.time_seconds for cue in on_disk.cues] == [6.0]


def test_save_without_expected_revision_keeps_caller_revision(tmp_path: Path) -> None:
    saved = save_score_plan(tmp_path, _plan(revision=7))
    assert saved.revision == 7


def test_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    save_score_plan(tmp_path, _plan([_cue(1.0)]))
    save_score_plan(tmp_path, _plan([_cue(2.0)]), expected_revision=1)
    leftovers = [
        entry for entry in (tmp_path / "music").iterdir()
        if entry.name != SCORE_PLAN_FILENAME
    ]
    assert leftovers == []


def test_load_malformed_plan_raises(tmp_path: Path) -> None:
    path = score_plan_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_score_plan(tmp_path)


def test_load_rejects_unknown_fields(tmp_path: Path) -> None:
    path = score_plan_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _plan().model_dump(mode="json")
    payload["surprise"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_score_plan(tmp_path)


# ---------------------------------------------------------------------------
# Content hash (cache/invalidation key)


def test_plan_hash_ignores_revision_and_timestamp() -> None:
    plan = _plan([_cue(24.1, "pull_back", gain_db=-7)])
    moved = plan.model_copy(update={"revision": 99})
    assert score_plan_hash(plan) == score_plan_hash(moved)


def test_plan_hash_tracks_cue_audio_content() -> None:
    base = _plan([_cue(24.1, "pull_back", gain_db=-7)])
    louder = _plan([_cue(24.1, "pull_back", gain_db=-6)])
    later = _plan([_cue(24.2, "pull_back", gain_db=-7)])
    other_action = _plan([_cue(24.1, "silence", gain_db=-7)])
    assert score_plan_hash(base) != score_plan_hash(louder)
    assert score_plan_hash(base) != score_plan_hash(later)
    assert score_plan_hash(base) != score_plan_hash(other_action)


def test_plan_hash_is_independent_of_construction_order() -> None:
    a = _cue(24.1, "pull_back")
    b = _cue(31.7, "silence")
    assert score_plan_hash(_plan([a, b])) == score_plan_hash(_plan([b, a]))


def test_hash_audio_file_matches_sha256(tmp_path: Path) -> None:
    import hashlib

    payload = b"RIFF....WAVEfmt data"
    source = tmp_path / "background.wav"
    source.write_bytes(payload)
    assert hash_audio_file(source) == hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Effect path resolution inside the project directory


def test_resolve_effect_path_none_without_path() -> None:
    cue = _cue(1.0, "impact", effect_asset_id="asset-123")
    assert resolve_cue_effect_path(Path("/project"), cue) is None


def test_resolve_effect_path_returns_inside_project(tmp_path: Path) -> None:
    effects = tmp_path / "music" / "effects"
    effects.mkdir(parents=True)
    (effects / "hit.wav").write_bytes(b"RIFF")
    cue = _cue(1.0, "impact", effect_path="music/effects/hit.wav")
    assert resolve_cue_effect_path(tmp_path, cue) == (effects / "hit.wav").resolve()


def test_validate_plan_effects_requires_existing_files(tmp_path: Path) -> None:
    cue = _cue(1.0, "impact", effect_path="music/effects/missing.wav")
    with pytest.raises(ValueError, match="effect file not found"):
        validate_score_plan_effects(tmp_path, _plan([cue]))
    (tmp_path / "music" / "effects").mkdir(parents=True)
    (tmp_path / "music" / "effects" / "missing.wav").write_bytes(b"RIFF")
    validate_score_plan_effects(tmp_path, _plan([cue]))  # no raise


def test_validate_plan_effects_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.wav"
    outside.write_bytes(b"RIFF")
    effects = tmp_path / "music" / "effects"
    effects.mkdir(parents=True)
    (effects / "escape.wav").symlink_to(outside)
    cue = _cue(1.0, "impact", effect_path="music/effects/escape.wav")
    with pytest.raises(ValueError, match="inside the project"):
        validate_score_plan_effects(tmp_path, _plan([cue]))


def test_locked_flag_round_trips() -> None:
    plan = _plan([_cue(4.0, "build", locked=True, source="auto")])
    restored = ScorePlan.model_validate_json(plan.model_dump_json())
    assert restored.cues[0].locked is True
    assert restored.cues[0].source == "auto"
