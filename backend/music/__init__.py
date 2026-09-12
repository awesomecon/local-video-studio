"""Music support shared by the mock and real generation paths.

The package provides three cooperating pieces:

- :mod:`planner` groups scenes into long musical movements (tens of seconds,
  never per-scene stings) with a stable energy curve derived from scene moods.
- :mod:`synth` is a deterministic, dependency-free procedural composer used by
  the mock backend so development renders contain actual music (harmony,
  bass, melody, percussion) instead of a sine tone.
- :mod:`stitch` joins per-movement audio files into the single
  ``music/background.wav`` consumed by the timeline and renderer.
- :mod:`score` defines the Score Studio cue plan (timed pullbacks, silences,
  impacts) over the generated soundtrack, its validation, and its atomic
  persistence as ``music/score-plan.json``.
"""

from .planner import MovementPlan, energy_for_mood, plan_hash, plan_movements
from .score import (
    DEFAULT_BUILD_DB,
    DEFAULT_PULL_BACK_DB,
    EFFECTS_DIRECTORY,
    SCORE_MIX_MANIFEST_FILENAME,
    SCORE_MIX_WORKFLOW_VERSION,
    SCORE_PLAN_FILENAME,
    SCORE_PLAN_VERSION,
    SCORED_OUTPUT_FILENAME,
    SCORED_SAMPLE_RATE,
    ScoreAction,
    ScoreCue,
    ScoreCueSource,
    ScorePlan,
    ScorePlanConflict,
    ScoreRenderError,
    compile_music_envelope,
    empty_score_plan,
    hash_audio_file,
    load_score_mix_manifest,
    load_score_plan,
    render_scored_background,
    resolve_cue_effect_path,
    save_score_plan,
    score_mix_manifest_path,
    score_plan_hash,
    score_plan_path,
    scored_background_path,
    validate_score_plan_effects,
)
from .synth import (
    SAMPLE_RATE,
    apply_edge_fades,
    compose_movement,
    compose_movement_frames,
    read_wav_frames,
    stitch_dips,
    write_wav,
)

__all__ = [
    "SAMPLE_RATE",
    "DEFAULT_BUILD_DB",
    "DEFAULT_PULL_BACK_DB",
    "EFFECTS_DIRECTORY",
    "MovementPlan",
    "SCORE_MIX_MANIFEST_FILENAME",
    "SCORE_MIX_WORKFLOW_VERSION",
    "SCORE_PLAN_FILENAME",
    "SCORE_PLAN_VERSION",
    "SCORED_OUTPUT_FILENAME",
    "SCORED_SAMPLE_RATE",
    "ScoreAction",
    "ScoreCue",
    "ScoreCueSource",
    "ScorePlan",
    "ScorePlanConflict",
    "ScoreRenderError",
    "apply_edge_fades",
    "compile_music_envelope",
    "compose_movement",
    "compose_movement_frames",
    "empty_score_plan",
    "energy_for_mood",
    "hash_audio_file",
    "load_score_mix_manifest",
    "load_score_plan",
    "plan_hash",
    "plan_movements",
    "read_wav_frames",
    "render_scored_background",
    "resolve_cue_effect_path",
    "save_score_plan",
    "score_mix_manifest_path",
    "score_plan_hash",
    "score_plan_path",
    "scored_background_path",
    "stitch_dips",
    "validate_score_plan_effects",
    "write_wav",
]
