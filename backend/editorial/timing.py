"""Caption-clock timing policy for Editorial compositions."""

from __future__ import annotations

import re
from collections.abc import Sequence

from backend.captions import CaptionWord

from .models import EditorialComposition


EDITORIAL_SENTENCE_TAIL_SECONDS = 0.5
_SENTENCE_END_RE = re.compile(r"(?:[.!?]+|…+)[\"'”’)}\]]*$")


def caption_sentence_boundaries(
    words: Sequence[CaptionWord],
    *,
    timeline_duration: float,
    fps: int,
    tail_seconds: float = EDITORIAL_SENTENCE_TAIL_SECONDS,
) -> list[float]:
    """Return frame-aligned visual cut points after spoken sentence endings."""
    if not words or timeline_duration <= 0 or fps <= 0:
        return []

    def frame_time(seconds: float) -> float:
        return round(round(seconds * fps) / fps, 6)

    points = {
        frame_time(min(word.end_seconds + tail_seconds, timeline_duration))
        for word in words
        if _SENTENCE_END_RE.search(word.text.strip())
    }
    final_frame = frame_time(timeline_duration)
    return sorted(point for point in points if 0 < point < final_frame)


def retime_compositions_to_caption_sentences(
    compositions: Sequence[EditorialComposition],
    words: Sequence[CaptionWord],
    *,
    timeline_duration: float,
    fps: int,
    tail_seconds: float = EDITORIAL_SENTENCE_TAIL_SECONDS,
) -> list[EditorialComposition] | None:
    """Snap composition cuts to caption sentences while preserving authored order.

    The old boundaries select the nearest semantic caption boundary, after being
    scaled onto the active narration clock. A short tail keeps the current visual
    on screen after the sentence finishes. Motion event timing is scaled with its
    composition so the authored animation sequence remains intact.
    """
    if not compositions or timeline_duration <= 0 or fps <= 0:
        return None
    internal_count = len(compositions) - 1
    candidates = caption_sentence_boundaries(
        words,
        timeline_duration=timeline_duration,
        fps=fps,
        tail_seconds=tail_seconds,
    )
    if len(candidates) < internal_count:
        return None

    source_duration = compositions[-1].start + compositions[-1].duration
    if source_duration <= 0:
        return None
    target_boundaries = [
        (composition.start + composition.duration) * timeline_duration / source_duration
        for composition in compositions[:-1]
    ]

    chosen: list[float] = []
    previous_index = -1
    for position, target in enumerate(target_boundaries):
        remaining = internal_count - position - 1
        first = previous_index + 1
        last = len(candidates) - remaining
        index = min(
            range(first, last),
            key=lambda candidate_index: (
                abs(candidates[candidate_index] - target), candidate_index,
            ),
        )
        chosen.append(candidates[index])
        previous_index = index

    frame_duration = round(round(timeline_duration * fps) / fps, 6)
    boundaries = [0.0, *chosen, frame_duration]
    retimed: list[EditorialComposition] = []
    for index, composition in enumerate(compositions):
        start = boundaries[index]
        duration = round(boundaries[index + 1] - start, 6)
        if duration < 1.0 / fps:
            return None
        scale = duration / composition.duration
        events = [
            event.model_copy(update={
                "time": min(duration, round(round(event.time * scale * fps) / fps, 6)),
                "duration": min(30.0, duration, event.duration * scale),
            })
            for event in composition.events
        ]
        retimed.append(EditorialComposition.model_validate({
            **composition.model_dump(mode="python"),
            "start": start,
            "duration": duration,
            "events": [event.model_dump(mode="python") for event in events],
        }))
    return retimed
