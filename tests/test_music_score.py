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


# ---------------------------------------------------------------------------
# Deterministic score renderer (Phase 2)
# ---------------------------------------------------------------------------

import math
import wave as _wave
from array import array as _array

from backend.music.score import (
    SCORED_SAMPLE_RATE,
    ScoreCue as _ScoreCue,
    ScorePlan as _ScorePlan,
    ScoreRenderError,
    compile_music_envelope,
    load_score_mix_manifest,
    render_scored_background,
    score_mix_manifest_path,
)


def _write_wav(path: Path, frames: list[int], *, channels: int, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(_array("h", frames).tobytes())


def _tone_frames(seconds: float, hz: float, rate: int, amp: int, channels: int = 2) -> list[int]:
    frames: list[int] = []
    for index in range(int(seconds * rate)):
        value = int(amp * math.sin(2 * math.pi * hz * index / rate))
        frames.extend([value] * channels)
    return frames


def _read_pcm(path: Path) -> tuple[_array, int]:
    with _wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        return _array("h", handle.readframes(handle.getnframes())), rate


def _rms(samples: _array, start_seconds: float, end_seconds: float) -> float:
    start = int(start_seconds * SCORED_SAMPLE_RATE)
    end = min(int(end_seconds * SCORED_SAMPLE_RATE), len(samples) // 2)
    segment = samples[start * 2:end * 2]
    if not segment:
        return 0.0
    return math.sqrt(sum(value * value for value in segment) / len(segment))


def _stereo_background(path: Path, *, seconds: float, hz: float = 440.0) -> None:
    """48 kHz stereo tone; exercises the renderer's direct-read path."""
    _write_wav(path, _tone_frames(seconds, hz, SCORED_SAMPLE_RATE, 12000), channels=2, rate=SCORED_SAMPLE_RATE)


def _mono_background(path: Path, *, seconds: float, hz: float = 440.0) -> None:
    """24 kHz mono tone; forces the FFmpeg normalization path."""
    _write_wav(path, _tone_frames(seconds, hz, 24000, 12000, channels=1), channels=1, rate=24000)


@pytest.fixture()
def music_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "music").mkdir(parents=True)
    return root


def test_scored_output_is_48k_stereo_pcm_at_exact_length(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=3.2)
    plan = _ScorePlan(
        duration_seconds=3.2,
        cues=[_ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-10)],
    )
    manifest = render_scored_background(music_root, plan)
    samples, rate = _read_pcm(music_root / "music" / "scored-background.wav")
    assert rate == 48000
    assert len(samples) % 2 == 0
    assert len(samples) // 2 == round(3.2 * SCORED_SAMPLE_RATE)
    assert manifest["sample_rate"] == 48000
    assert manifest["channels"] == 2
    assert manifest["loudness_normalization"] is False


def test_silence_step_is_sample_accurate(music_root: Path) -> None:
    _mono_background(music_root / "music" / "background.wav", seconds=3.0, hz=600.0)
    plan = _ScorePlan(
        duration_seconds=3.0,
        cues=[_ScoreCue(time_seconds=1.5, action="silence", transition_seconds=0.0)],
    )
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    cut = int(1.5 * SCORED_SAMPLE_RATE)
    before = max(abs(v) for v in samples[(cut - 96) * 2: cut * 2])
    after = max(abs(v) for v in samples[cut * 2:(cut + 960) * 2])
    assert before > 500, "bed must be audible before the silence cue"
    assert after == 0, "silence must hold from the exact cue sample"


def test_silence_ramp_reaches_floor_at_transition_end(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=3.0, hz=600.0)
    plan = _ScorePlan(
        duration_seconds=3.0,
        cues=[_ScoreCue(time_seconds=1.0, action="silence", transition_seconds=0.3)],
    )
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    assert _rms(samples, 0.2, 0.8) > 500
    # Just inside the ramp it is still (partially) audible; well past it, silent.
    assert max(abs(v) for v in samples[int(1.2 * SCORED_SAMPLE_RATE) * 2:int(1.3 * SCORED_SAMPLE_RATE) * 2]) > 0
    assert max(abs(v) for v in samples[int(1.35 * SCORED_SAMPLE_RATE) * 2:]) == 0


def test_pullback_applies_exact_gain_change(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=4.0)
    plan = _ScorePlan(
        duration_seconds=4.0,
        cues=[_ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-20, transition_seconds=0.25)],
    )
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    before = _rms(samples, 0.1, 0.8)
    after = _rms(samples, 1.5, 3.9)
    change_db = 20 * math.log10(after / before)
    assert abs(change_db - (-20.0)) < 1.0


def test_restore_recovers_full_level(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=4.0)
    plan = _ScorePlan(
        duration_seconds=4.0,
        cues=[
            _ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-20, transition_seconds=0.2),
            _ScoreCue(time_seconds=2.5, action="restore", transition_seconds=0.2),
        ],
    )
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    base = _rms(samples, 0.1, 0.8)
    recovered = _rms(samples, 3.0, 3.9)
    assert abs(recovered - base) / base < 0.05


def test_lowpass_region_attenuates_high_frequencies(music_root: Path) -> None:
    # A 4 kHz tone sits well above the 1000 Hz pullback filter.
    _stereo_background(music_root / "music" / "background.wav", seconds=4.0, hz=4000.0)
    plan = _ScorePlan(
        duration_seconds=4.0,
        cues=[
            _ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-6, transition_seconds=0.2, lowpass_hz=1000),
            _ScoreCue(time_seconds=2.5, action="restore", transition_seconds=0.2),
        ],
    )
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    before = _rms(samples, 0.1, 0.8)
    during = _rms(samples, 1.5, 2.3)
    after = _rms(samples, 2.9, 3.9)
    assert before > 500
    assert during < before * 0.2, "low-passed pullback must kill the 4 kHz tone"
    assert after > before * 0.6, "restore returns the unfiltered tone"


def test_effect_is_audible_at_its_cue_time(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=4.0, hz=200.0)
    effects = music_root / "music" / "effects"
    effects.mkdir(parents=True, exist_ok=True)
    _write_wav(
        effects / "hit.wav",
        _tone_frames(0.15, 3000.0, SCORED_SAMPLE_RATE, 20000, channels=2),
        channels=2,
        rate=SCORED_SAMPLE_RATE,
    )
    plan = _ScorePlan(
        duration_seconds=4.0,
        cues=[
            _ScoreCue(time_seconds=2.0, action="impact", effect_path="music/effects/hit.wav"),
        ],
    )
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    quiet = _rms(samples, 1.2, 1.8)
    hit = _rms(samples, 2.0, 2.15)
    assert hit > quiet * 1.5, "impact must be clearly audible at its cue time"
    # The impact starts exactly at the cue: no energy before it.
    before_cut = _rms(samples, 1.95, 1.999)
    assert before_cut < quiet * 1.5


def test_effect_survives_a_silenced_bed(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=4.0)
    effects = music_root / "music" / "effects"
    effects.mkdir(parents=True, exist_ok=True)
    _write_wav(
        effects / "sting.wav",
        _tone_frames(0.2, 1500.0, SCORED_SAMPLE_RATE, 20000, channels=2),
        channels=2,
        rate=SCORED_SAMPLE_RATE,
    )
    plan = _ScorePlan(
        duration_seconds=4.0,
        cues=[
            _ScoreCue(time_seconds=1.0, action="silence", transition_seconds=0.0),
            _ScoreCue(time_seconds=1.2, action="impact", effect_path="music/effects/sting.wav"),
        ],
    )
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    bed = max(abs(v) for v in samples[int(1.05 * SCORED_SAMPLE_RATE) * 2:int(1.18 * SCORED_SAMPLE_RATE) * 2])
    sting = max(abs(v) for v in samples[int(1.25 * SCORED_SAMPLE_RATE) * 2:int(1.45 * SCORED_SAMPLE_RATE) * 2])
    assert bed == 0, "bed must be silent after the silence cue"
    assert sting > 5000, "the sting plays through the silence"


def test_output_is_padded_to_plan_duration(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=2.0)
    plan = _ScorePlan(duration_seconds=3.0, cues=[_ScoreCue(time_seconds=0.5, action="build")])
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    assert len(samples) // 2 == round(3.0 * SCORED_SAMPLE_RATE)
    tail = samples[int(2.5 * SCORED_SAMPLE_RATE) * 2:]
    assert max(abs(v) for v in tail) == 0, "padding is silence"


def test_output_is_trimmed_to_plan_duration(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=3.0)
    plan = _ScorePlan(duration_seconds=2.0, cues=[_ScoreCue(time_seconds=0.5, action="build")])
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    assert len(samples) // 2 == round(2.0 * SCORED_SAMPLE_RATE)


def test_zero_cue_plan_reproduces_the_master_bytes(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=2.5)
    plan = _ScorePlan(duration_seconds=2.5)
    manifest = render_scored_background(music_root, plan)
    master = (music_root / "music" / "background.wav").read_bytes()
    scored = (music_root / "music" / "scored-background.wav").read_bytes()
    assert master == scored
    assert manifest["plan"]["cue_count"] == 0


def test_manifest_records_inputs_plan_and_output_hashes(music_root: Path) -> None:
    background = music_root / "music" / "background.wav"
    _stereo_background(background, seconds=2.0)
    from backend.music import hash_audio_file

    plan = _ScorePlan(
        duration_seconds=2.0,
        source_music_hash=hash_audio_file(background),
        cues=[_ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-12)],
    )
    from backend.music import save_score_plan, score_plan_hash

    save_score_plan(music_root, plan, expected_revision=0)
    manifest = render_scored_background(music_root, plan)
    assert manifest["source"]["sha256"] == hash_audio_file(background)
    assert manifest["plan"]["plan_hash"] == score_plan_hash(plan)
    output = music_root / "music" / "scored-background.wav"
    assert manifest["output"]["sha256"] == hash_audio_file(output)
    assert manifest["output"]["duration_seconds"] == 2.0
    assert manifest["loudness_normalization"] is False
    on_disk = load_score_mix_manifest(music_root)
    assert on_disk is not None and on_disk["plan"]["plan_hash"] == score_plan_hash(plan)
    assert score_mix_manifest_path(music_root) == music_root / "music" / "score-mix-manifest.json"


def test_source_master_is_never_modified(music_root: Path) -> None:
    background = music_root / "music" / "background.wav"
    _stereo_background(background, seconds=2.0)
    before = background.read_bytes()
    plan = _ScorePlan(
        duration_seconds=2.0,
        cues=[_ScoreCue(time_seconds=0.8, action="silence", transition_seconds=0.1)],
    )
    render_scored_background(music_root, plan)
    assert background.read_bytes() == before


def test_render_requires_background(music_root: Path) -> None:
    plan = _ScorePlan(duration_seconds=1.0)
    with pytest.raises(ScoreRenderError, match="background.wav"):
        render_scored_background(music_root, plan)


def test_render_rejects_missing_effect_file(music_root: Path) -> None:
    _stereo_background(music_root / "music" / "background.wav", seconds=2.0)
    plan = _ScorePlan(
        duration_seconds=2.0,
        cues=[_ScoreCue(time_seconds=1.0, action="impact", effect_path="music/effects/ghost.wav")],
    )
    with pytest.raises(ValueError, match="effect file not found"):
        render_scored_background(music_root, plan)


def test_compile_envelope_tracks_level_changes(music_root: Path) -> None:
    plan = _ScorePlan(
        duration_seconds=10.0,
        cues=[
            _ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-10, transition_seconds=0.5),
            _ScoreCue(time_seconds=3.0, action="restore", transition_seconds=0.5),
            _ScoreCue(time_seconds=5.0, action="silence", transition_seconds=0.0),
        ],
    )
    segments, regions = compile_music_envelope(plan)
    assert [(seg.from_db, seg.to_db) for seg in segments] == [
        (0.0, -10.0),
        (-10.0, 0.0),
        (0.0, -120.0),
    ]
    assert segments[0].start_sample == int(1.0 * SCORED_SAMPLE_RATE)
    assert regions == []

    plan_filtered = _ScorePlan(
        duration_seconds=10.0,
        cues=[
            _ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-10, transition_seconds=0.2, lowpass_hz=2000),
            _ScoreCue(time_seconds=4.0, action="restore", transition_seconds=0.2),
        ],
    )
    _, filtered_regions = compile_music_envelope(plan_filtered)
    assert len(filtered_regions) == 1
    region = filtered_regions[0]
    assert region.cutoff_hz == 2000.0
    assert region.start_sample == int(1.0 * SCORED_SAMPLE_RATE)
    assert region.end_sample == int(4.0 * SCORED_SAMPLE_RATE)


def test_build_and_pullback_gains_are_relative_to_running_level() -> None:
    """Build/pull_back shift the bed; restore/silence set an absolute state."""
    plan = _ScorePlan(
        duration_seconds=10.0,
        cues=[
            _ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-10, transition_seconds=0.5),
            # A +4 dB build from -10 dB lands at -6 dB, not +4 dB.
            _ScoreCue(time_seconds=3.0, action="build", gain_db=4, transition_seconds=0.5),
        ],
    )
    segments, _ = compile_music_envelope(plan)
    assert [(seg.from_db, seg.to_db) for seg in segments] == [
        (0.0, -10.0),
        (-10.0, -6.0),
    ]

    stacked = _ScorePlan(
        duration_seconds=10.0,
        cues=[
            _ScoreCue(time_seconds=1.0, action="build", transition_seconds=0.2),
            # Opening hook leaves +3 dB; a +2 dB build stacks to +5 dB.
            _ScoreCue(time_seconds=3.0, action="build", gain_db=2, transition_seconds=0.2),
        ],
    )
    stacked_segments, _ = compile_music_envelope(stacked)
    assert [(seg.from_db, seg.to_db) for seg in stacked_segments] == [
        (0.0, 3.0),
        (3.0, 5.0),
    ]


def test_restore_accepts_an_explicit_absolute_target() -> None:
    plan = _ScorePlan(
        duration_seconds=10.0,
        cues=[
            _ScoreCue(time_seconds=1.0, action="pull_back", gain_db=-10, transition_seconds=0.2),
            _ScoreCue(time_seconds=3.0, action="restore", gain_db=-2, transition_seconds=0.2),
        ],
    )
    segments, _ = compile_music_envelope(plan)
    assert [(seg.from_db, seg.to_db) for seg in segments] == [
        (0.0, -10.0),
        (-10.0, -2.0),
    ]


def test_overlapping_transition_is_interrupted_not_double_gained() -> None:
    """A cue landing mid-ramp settles the running ramp at the cue time."""
    plan = _ScorePlan(
        duration_seconds=4.0,
        cues=[
            _ScoreCue(time_seconds=0.5, action="silence", transition_seconds=0.5),
            _ScoreCue(time_seconds=0.7, action="restore", transition_seconds=0.2),
        ],
    )
    segments, _ = compile_music_envelope(plan)
    assert len(segments) == 2
    first, second = segments
    # The first ramp is truncated at the second cue's time; no sample is
    # ramped twice and the envelope stays continuous.
    assert first.start_sample == int(0.5 * SCORED_SAMPLE_RATE)
    assert first.end_sample == int(0.7 * SCORED_SAMPLE_RATE)
    assert second.start_sample == first.end_sample
    assert second.end_sample == int(0.9 * SCORED_SAMPLE_RATE)
    assert second.from_db == first.to_db
    # Interrupted 0.2s into a 0.5s ramp toward -120 dB: 0 + (-120) * 0.4.
    assert first.from_db == 0.0
    assert first.to_db == pytest.approx(-48.0)
    assert second.to_db == 0.0


def test_overlapping_restore_is_audible_before_first_ramp_would_end(
    music_root: Path,
) -> None:
    """Audio proof: the bed restores from 0.7 s, not from 1.0 s."""
    _stereo_background(music_root / "music" / "background.wav", seconds=4.0)
    plan = _ScorePlan(
        duration_seconds=4.0,
        cues=[
            _ScoreCue(time_seconds=0.5, action="silence", transition_seconds=0.5),
            _ScoreCue(time_seconds=0.7, action="restore", transition_seconds=0.2),
        ],
    )
    render_scored_background(music_root, plan)
    samples, _ = _read_pcm(music_root / "music" / "scored-background.wav")
    restoring = max(
        abs(v)
        for v in samples[int(0.78 * SCORED_SAMPLE_RATE) * 2:int(0.82 * SCORED_SAMPLE_RATE) * 2]
    )
    assert restoring > 200, "the restore ramp must already be audible at 0.8 s"
    recovered = _rms(samples, 1.2, 1.8)
    assert recovered > 500


def test_lowpass_region_is_time_aligned(music_root: Path) -> None:
    """The wet filter reads each region sample's own position (no 20 ms shift)."""
    from backend.music.score import (
        _apply_lowpass_regions,
        _biquad_lowpass_coeffs,
        _filter_mono,
        _LowpassRegion,
        _seconds_to_samples,
    )

    length = int(2.0 * SCORED_SAMPLE_RATE)
    # A per-frame ramp makes a 960-sample (20 ms) misalignment obvious.
    samples = _array("h", [0]) * (length * 2)
    for index in range(length):
        value = (index % 2000) - 1000
        samples[index * 2] = value
        samples[index * 2 + 1] = value
    original = _array("h", samples)

    start = _seconds_to_samples(0.5)
    end = _seconds_to_samples(1.5)
    engage = _seconds_to_samples(0.2)
    region = _LowpassRegion(start, end, 3200.0, engage, 0)
    _apply_lowpass_regions(samples, [region], length)

    # At the region head the crossfade weight is 0, so the output must equal
    # the dry sample at the region start (a shifted read would return the
    # content from 20 ms earlier).
    assert samples[start * 2] == original[start * 2]
    assert samples[start * 2 + 1] == original[start * 2 + 1]

    # Mid-region (fully wet) must match the correctly-aligned filtered
    # reference to rounding precision, not the warm-shifted one.
    warm = _seconds_to_samples(0.02)
    coeffs = _biquad_lowpass_coeffs(3200.0, SCORED_SAMPLE_RATE)
    for channel in range(2):
        dry = original[channel::2]
        wet = _filter_mono(_array("d", dry[start - warm:end]), coeffs)
        mid = start + _seconds_to_samples(0.5)
        expected = int(round(wet[(mid - start) + warm]))
        actual = samples[mid * 2 + channel]
        assert abs(actual - expected) <= 1
        shifted = int(round(wet[mid - start]))
        assert abs(shifted - expected) > 10, "test signal must expose a shift"


# ---------------------------------------------------------------------------
# Score preview (Phase 3) and deterministic auto-score
# ---------------------------------------------------------------------------

from array import array as _arr2  # noqa: E402

from pydantic import ValidationError as _ValidationError  # noqa: E402

from backend.music import (  # noqa: E402
    AutoScoreSuggestion,
    ScoreRenderError as _ScoreRenderError,
    apply_auto_score_suggestions,
    deterministic_auto_score,
    load_score_preview_manifest,
    mix_stereo,
    render_score_preview,
    score_preview_path,
)


def test_mix_stereo_adds_and_clamps() -> None:
    dest = _arr2("h", [1000, 20000, -30000, -500])
    addend = _arr2("h", [2000, 20000, -20000, 0])
    mix_stereo(dest, addend)
    assert dest[0] == 3000
    assert dest[1] == 32767  # 20000 + 20000 clamps to full scale
    assert dest[2] == -32768  # -30000 - 20000 clamps to the low rail
    assert dest[3] == -500


def test_mix_stereo_respects_gain_and_max_frames() -> None:
    dest = _arr2("h", [0, 0, 0, 0])
    addend = _arr2("h", [1000, 1000, 1000, 1000])
    mix_stereo(dest, addend, gain_db=6.0, max_frames=1)
    assert 1900 < dest[0] < 2100  # 1000 * 10**(6/20) ~ 1995
    assert dest[2] == 0  # second frame not touched (max_frames=1)


def test_render_score_preview_with_narration(music_root: Path) -> None:
    background = music_root / "music" / "background.wav"
    _stereo_background(background, seconds=2.0, hz=300.0)
    # Scored bed and narration share a file here: the preview must be louder.
    render_score_preview(music_root, scored_path=background, narration_path=background)
    preview = score_preview_path(music_root)
    assert preview.is_file()
    samples, rate = _read_pcm(preview)
    assert rate == 48000
    master = _read_pcm(background)[0]
    assert _rms(samples, 0.2, 1.5) > _rms(master, 0.2, 1.5)
    manifest = load_score_preview_manifest(music_root)
    assert manifest is not None
    assert manifest["narration"] is not None
    assert manifest["loudness_normalization"] is False
    assert manifest["output"]["duration_seconds"] == pytest.approx(2.0, abs=0.01)


def test_render_score_preview_without_narration_is_a_copy(music_root: Path) -> None:
    background = music_root / "music" / "background.wav"
    _stereo_background(background, seconds=2.0)
    master_bytes = background.read_bytes()
    manifest = render_score_preview(music_root, scored_path=background)
    preview = score_preview_path(music_root)
    assert manifest["narration"] is None
    # 48 kHz stereo bed with no narration: preview is byte-identical to the bed.
    assert preview.read_bytes() == master_bytes


def test_render_score_preview_requires_scored_bed(music_root: Path) -> None:
    with pytest.raises(_ScoreRenderError):
        render_score_preview(music_root, scored_path=music_root / "music" / "nope.wav")


# --- deterministic auto-score ---


def _suggestion_actions(suggestions) -> list[str]:
    return [suggestion.action for suggestion in suggestions]


def _first(suggestions, action: str):
    return next(s for s in suggestions if s.action == action)


def test_auto_score_recipe_shape() -> None:
    suggestions = deterministic_auto_score(40.0)
    actions = _suggestion_actions(suggestions)
    assert actions.count("build") == 2
    assert "pull_back" in actions
    assert "silence" in actions
    assert "restore" in actions
    assert "end_sting" in actions
    for suggestion in suggestions:
        assert 0.0 <= suggestion.time_seconds <= 40.0
        assert suggestion.reason
        assert suggestion.transition_seconds > 0
    times = [suggestion.time_seconds for suggestion in suggestions]
    assert times == sorted(times)
    restore = _first(suggestions, "restore").time_seconds
    silence = _first(suggestions, "silence").time_seconds
    assert restore >= 40.0 * 0.5
    assert 0.0 <= restore - silence <= 0.4


def test_auto_score_respects_locked_cue_times() -> None:
    locked = [40.0 * 0.25]  # where the gentle build would land
    suggestions = deterministic_auto_score(40.0, locked_cue_times=locked)
    assert not any(
        abs(s.time_seconds - 40.0 * 0.25) <= 0.5 and s.action == "build"
        for s in suggestions
    )


def test_auto_score_empty_duration_returns_nothing() -> None:
    assert deterministic_auto_score(0.0) == []


def test_auto_score_suggestion_to_cue_is_auto_and_unlocked() -> None:
    cue = deterministic_auto_score(40.0)[0].to_cue()
    assert cue.source == "auto"
    assert cue.locked is False
    assert cue.id


def test_auto_score_suggestion_rejects_invalid_fields() -> None:
    with pytest.raises(_ValidationError):
        AutoScoreSuggestion(time_seconds=1.0, action="not_an_action")
    with pytest.raises(_ValidationError):
        AutoScoreSuggestion(time_seconds=-1.0, action="build")
    with pytest.raises(_ValidationError):
        AutoScoreSuggestion(time_seconds=1.0, action="build", bogus=1)


def test_apply_auto_score_preserves_locked_cues() -> None:
    plan = _plan([_cue(10.0, "silence", locked=True, source="manual")])
    suggestions = [
        AutoScoreSuggestion(time_seconds=10.1, action="silence", reason="x"),
        AutoScoreSuggestion(time_seconds=30.0, action="build", reason="y"),
    ]
    merged, added = apply_auto_score_suggestions(plan, suggestions)
    auto_silences = [c for c in merged.cues if c.action == "silence" and c.source == "auto"]
    assert auto_silences == [], "a locked silence protects that region from auto silence"
    assert any(c.action == "build" and c.source == "auto" for c in merged.cues)
    locked = [c for c in merged.cues if c.locked]
    assert len(locked) == 1 and locked[0].time_seconds == 10.0
    assert len(added) == 1
    assert [c.time_seconds for c in merged.cues] == sorted(
        c.time_seconds for c in merged.cues
    )


# --- local-LLM auto score (Phase 4) ---

from backend.music import (  # noqa: E402
    AUTO_SCORE_JSON_SCHEMA,
    AutoScoreProposal,
    build_auto_score_prompt,
    llm_auto_score,
    validate_auto_score_payload,
)


class _FakeLlmBackend:
    """Stands in for LocalLLMBackend.complete in auto-score tests."""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def _auto_score_context(**overrides) -> dict:
    context = {
        "duration_seconds": 40.0,
        "music_direction": "tense documentary tension",
        "intensity": "balanced",
        "narration_segments": [
            {"start_seconds": 1.0, "end_seconds": 5.0, "text": "In 2016 the idea was born"},
            {"start_seconds": 30.0, "end_seconds": 34.0, "text": "Today, the neural link is real"},
        ],
        "scene_spans": [
            {"scene_id": "a", "index": 0, "title": "Origins", "start_seconds": 0.0, "end_seconds": 20.0},
            {"scene_id": "b", "index": 1, "title": "The Reveal", "start_seconds": 20.0, "end_seconds": 40.0},
        ],
        "emphasis_phrases": [{"text": "the neural link is real", "start_seconds": 30.0}],
        "locked_cues": [{"time_seconds": 12.0, "action": "silence", "label": "manual hold"}],
        "effect_assets": ["impact.wav"],
    }
    context.update(overrides)
    return context


def test_auto_score_json_schema_covers_the_cue_vocabulary() -> None:
    items = AUTO_SCORE_JSON_SCHEMA["properties"]["cues"]["items"]
    assert set(items["properties"]) == {
        "time_seconds", "action", "label", "transition_seconds",
        "reason", "gain_db", "lowpass_hz",
    }
    assert set(items["properties"]["action"]["enum"]) == {
        "build", "pull_back", "silence", "restore", "impact", "riser", "end_sting",
    }
    # Grammar-safe: no string length bounds for the llama.cpp compiler.
    assert "maxLength" not in str(AUTO_SCORE_JSON_SCHEMA)


def test_auto_score_prompt_carries_only_local_project_context() -> None:
    messages = build_auto_score_prompt(_auto_score_context())
    joined = " ".join(message["content"] for message in messages)
    assert "tense documentary tension" in joined
    assert "In 2016 the idea was born" in joined
    assert "Today, the neural link is real" in joined
    assert "Origins" in joined and "The Reveal" in joined
    assert "the neural link is real" in joined
    assert "manual hold" in joined and "impact.wav" in joined
    assert "40.000s" in joined  # the soundtrack length is stated
    # The prompt must stay local: no URLs, no remote hosts, no keys.
    assert "http" not in joined
    assert "api_key" not in joined.lower()


def test_llm_auto_score_validates_and_sanitizes() -> None:
    backend = _FakeLlmBackend({
        "cues": [
            # Out-of-range time: clamped to the soundtrack end.
            {"time_seconds": 99.0, "action": "end_sting", "label": "Late sting", "reason": "lands the ending"},
            # Crowds the locked silence at 12s: dropped.
            {"time_seconds": 12.3, "action": "silence", "label": "Auto silence", "reason": "quiet beat"},
            # Near the locked cue but a different action: kept.
            {"time_seconds": 11.4, "action": "pull_back", "label": "Sit back", "reason": "room for voice", "gain_db": -6},
            # In range, sorted before the clamped sting.
            {"time_seconds": 2.0, "action": "build", "label": "Opening lift", "reason": "establish bed"},
        ],
    })
    suggestions = llm_auto_score(backend, context=_auto_score_context())
    assert [s.action for s in suggestions] == ["build", "pull_back", "end_sting"]
    assert suggestions[2].time_seconds == 40.0  # clamped, not dropped
    call = backend.calls[0]
    assert call["structured"] is True
    assert call["json_schema"] is AUTO_SCORE_JSON_SCHEMA
    assert call["thinking_budget_tokens"] is not None  # reasoning stays enabled
    assert call["validator"] is validate_auto_score_payload
    # The prompt carries the full creative context: narration, scenes, emphasis.
    context_blob = str(call["messages"][1]["content"])
    assert "Timed narration" in context_blob
    assert "Scene / composition boundaries" in context_blob
    assert "Locked manual cues" in context_blob


def test_llm_auto_score_rejects_malformed_structured_payload() -> None:
    backend = _FakeLlmBackend({
        "cues": [
            {"time_seconds": 5.0, "action": "not_an_action", "label": "x", "reason": "y"},
        ],
    })
    with pytest.raises(_ValidationError):
        llm_auto_score(backend, context=_auto_score_context())


def test_llm_auto_score_rejects_payload_with_unknown_keys() -> None:
    backend = _FakeLlmBackend({"cues": [], "extra": 1})
    with pytest.raises(_ValidationError):
        llm_auto_score(backend, context=_auto_score_context())


def test_llm_auto_score_requires_a_positive_duration() -> None:
    backend = _FakeLlmBackend({"cues": []})
    with pytest.raises(ValueError):
        llm_auto_score(backend, context=_auto_score_context(duration_seconds=0.0))


def test_validate_auto_score_payload_accepts_minimal_cues() -> None:
    proposal = validate_auto_score_payload({
        "cues": [{"time_seconds": 3.5, "action": "silence"}],
    })
    assert proposal.cues[0].label == ""
    assert proposal.cues[0].reason == ""
    assert proposal.cues[0].transition_seconds == 0.25
