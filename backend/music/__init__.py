"""Music support shared by the mock and real generation paths.

The package provides three cooperating pieces:

- :mod:`planner` groups scenes into long musical movements (tens of seconds,
  never per-scene stings) with a stable energy curve derived from scene moods.
- :mod:`synth` is a deterministic, dependency-free procedural composer used by
  the mock backend so development renders contain actual music (harmony,
  bass, melody, percussion) instead of a sine tone.
- :mod:`stitch` joins per-movement audio files into the single
  ``music/background.wav`` consumed by the timeline and renderer.
"""

from .planner import MovementPlan, energy_for_mood, plan_hash, plan_movements
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
    "MovementPlan",
    "apply_edge_fades",
    "compose_movement",
    "compose_movement_frames",
    "energy_for_mood",
    "plan_hash",
    "plan_movements",
    "read_wav_frames",
    "stitch_dips",
    "write_wav",
]
