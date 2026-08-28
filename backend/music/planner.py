"""Group scenes into long musical movements.

A movement is a multi-scene stretch of video scored as one piece of music
(default ~60 seconds). Scene-level ``music_mood`` hints influence where
movement boundaries fall and how much energy each movement carries, but a new
movement is never created for a scene alone: boundaries require the current
movement to be at least ``min_movement_seconds`` long, and mood changes that
arrive early are absorbed into the running movement.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_MOVEMENT_SECONDS = 60.0
MIN_MOVEMENT_SECONDS = 30.0

_ENERGY_LOW = (
    "calm", "gentle", "soft", "subtle", "restrained", "quiet", "mellow",
    "minimal", "ambient", "peaceful", "serene", "somber", "melancholy", "sad",
)
_ENERGY_HIGH = (
    "uplifting", "bright", "hopeful", "playful", "energetic", "driving",
    "epic", "intense", "triumphant", "urgent", "exciting", "action",
    "climax", "heroic", "dramatic",
)
_ENERGY_MID = (
    "tense", "dark", "mysterious", "moody", "curious", "thoughtful",
    "contemplative", "warm",
)


def energy_for_mood(mood: str) -> float:
    """Map free-text mood wording onto a 0..1 musical energy level."""
    text = (mood or "").lower()
    if not text:
        return 0.5
    if any(keyword in text for keyword in _ENERGY_LOW):
        return 0.3
    if any(keyword in text for keyword in _ENERGY_HIGH):
        return 0.85
    if any(keyword in text for keyword in _ENERGY_MID):
        return 0.55
    return 0.5


@dataclass(frozen=True, slots=True)
class MovementPlan:
    """One scored stretch of the video."""

    index: int
    start_seconds: float
    duration_seconds: float
    scene_indices: tuple[int, ...]
    mood: str
    energy: float

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


def plan_hash(plans: Sequence[MovementPlan]) -> str:
    """Stable digest of movement boundaries and energies for cache keys."""
    payload = [
        {
            "index": plan.index,
            "start": round(plan.start_seconds, 3),
            "duration": round(plan.duration_seconds, 3),
            "mood": plan.mood,
            "energy": round(plan.energy, 3),
        }
        for plan in plans
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def plan_movements(
    scene_durations: Sequence[float],
    scene_moods: Sequence[str],
    total_duration: float,
    *,
    movement_seconds: float = DEFAULT_MOVEMENT_SECONDS,
    min_movement_seconds: float = MIN_MOVEMENT_SECONDS,
    max_movement_seconds: float | None = None,
) -> list[MovementPlan]:
    """Plan movements covering exactly ``total_duration`` seconds.

    Scene durations are scaled proportionally so their sum equals
    ``total_duration`` (the narration-driven video length), then grouped into
    contiguous movements. The returned durations always sum to
    ``total_duration`` within microsecond rounding.
    """
    if not math.isfinite(total_duration) or total_duration <= 0:
        raise ValueError("Total duration must be positive")
    movement_seconds = max(MIN_MOVEMENT_SECONDS, float(movement_seconds))
    min_movement_seconds = min(max(1.0, float(min_movement_seconds)), movement_seconds)
    hard_cap = float(max_movement_seconds) if max_movement_seconds else None

    if not scene_durations:
        energy = energy_for_mood(scene_moods[0] if scene_moods else "")
        return [
            MovementPlan(
                index=0,
                start_seconds=0.0,
                duration_seconds=total_duration,
                scene_indices=(),
                mood=scene_moods[0] if scene_moods else "",
                energy=energy,
            )
        ]

    source_total = sum(duration for duration in scene_durations if duration > 0)
    if source_total <= 0 or not math.isfinite(source_total):
        scale = 1.0 / len(scene_durations)
        scaled = [total_duration * scale] * len(scene_durations)
    else:
        scale = total_duration / source_total
        scaled = [max(0.0, duration) * scale for duration in scene_durations]
    drift = total_duration - sum(scaled)
    scaled[-1] += drift

    groups: list[list[int]] = []
    current: list[int] = []
    current_length = 0.0
    current_energy = energy_for_mood(scene_moods[0]) if scene_moods else 0.5
    for index, duration in enumerate(scaled):
        if current:
            candidate_energy = (
                energy_for_mood(scene_moods[index])
                if index < len(scene_moods) else current_energy
            )
            boundary = False
            mood_shift = abs(candidate_energy - current_energy) >= 0.2
            if mood_shift and current_length >= min_movement_seconds:
                boundary = True
            over_cap = (
                hard_cap is not None
                and current_length + duration > hard_cap
                and current_length >= min_movement_seconds
            )
            if over_cap:
                boundary = True
            elif current_length >= movement_seconds:
                boundary = True
            if boundary:
                groups.append(current)
                current = []
                current_length = 0.0
                current_energy = candidate_energy
        current.append(index)
        current_length += duration
    if current:
        groups.append(current)

    # Do not leave a brief musical fragment at the end. A final remainder is
    # the most common source of 15–20 second movements because it never gets a
    # chance to satisfy the boundary check above.
    while len(groups) > 1 and sum(
        scaled[index] for index in groups[-1]
    ) < min_movement_seconds:
        tail = groups.pop()
        groups[-1].extend(tail)

    plans: list[MovementPlan] = []
    cursor = 0.0
    for group_index, group in enumerate(groups):
        duration = sum(scaled[index] for index in group)
        if group_index == len(groups) - 1:
            duration = total_duration - cursor
        first_scene = group[0]
        mood = scene_moods[first_scene] if first_scene < len(scene_moods) else ""
        plans.append(
            MovementPlan(
                index=group_index,
                start_seconds=round(cursor, 6),
                duration_seconds=round(max(0.1, duration), 6),
                scene_indices=tuple(group),
                mood=mood,
                energy=energy_for_mood(mood),
            )
        )
        cursor += duration
    if plans:
        plans[-1] = MovementPlan(
            index=plans[-1].index,
            start_seconds=plans[-1].start_seconds,
            duration_seconds=round(total_duration - plans[-1].start_seconds, 6),
            scene_indices=plans[-1].scene_indices,
            mood=plans[-1].mood,
            energy=plans[-1].energy,
        )
    return plans
