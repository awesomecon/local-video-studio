"""Build render-ready timelines from generated scene assets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, cast

from .models import AudioTrack, MediaKind, SubtitleCue, Timeline, TimelineClip


@dataclass(frozen=True, slots=True)
class SceneTiming:
    scene_id: str
    asset_path: Path
    duration_seconds: float
    media_kind: str = "video"
    transition: str = "cut"
    transition_duration_seconds: float = 0.0
    camera_motion: str | None = None


def adjust_scene_durations(
    durations: Sequence[float],
    narration_duration_seconds: float,
    *,
    minimum_scene_seconds: float = 0.5,
) -> list[float]:
    """Scale planned scene durations to match narration while preserving proportions."""

    if not durations:
        return []
    if not math.isfinite(narration_duration_seconds) or narration_duration_seconds <= 0:
        raise ValueError("Narration duration must be positive")
    if not math.isfinite(minimum_scene_seconds) or minimum_scene_seconds <= 0:
        raise ValueError("Minimum scene duration must be positive")
    if any(not math.isfinite(duration) or duration <= 0 for duration in durations):
        raise ValueError("All source scene durations must be positive")
    source_total = sum(durations)
    if narration_duration_seconds >= source_total:
        scale = narration_duration_seconds / source_total
        adjusted = [duration * scale for duration in durations]
        adjusted[-1] += narration_duration_seconds - sum(adjusted)
        return adjusted
    minimum_total = minimum_scene_seconds * len(durations)
    if narration_duration_seconds < minimum_total:
        raise ValueError(
            f"Narration is too short for {len(durations)} scenes at the configured minimum"
        )
    distributable = narration_duration_seconds - minimum_total
    adjusted = [
        minimum_scene_seconds + distributable * duration / source_total for duration in durations
    ]
    adjusted[-1] += narration_duration_seconds - sum(adjusted)
    return adjusted


def build_timeline(
    scenes: Iterable[SceneTiming],
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 24,
    narration_path: Path | None = None,
    narration_gain_db: float = 0.0,
    music_path: Path | None = None,
    subtitles: Iterable[SubtitleCue] = (),
) -> Timeline:
    """Lay scenes sequentially, accounting for explicit transition overlaps."""

    clips: list[TimelineClip] = []
    cursor = 0.0
    for index, scene in enumerate(scenes):
        if scene.duration_seconds <= 0:
            raise ValueError(f"Scene {scene.scene_id} has non-positive duration")
        if scene.media_kind not in {"image", "video", "title", "diagram"}:
            raise ValueError(
                f"Scene {scene.scene_id} has unsupported media kind {scene.media_kind}"
            )
        overlap = 0.0
        if index and scene.transition != "cut":
            overlap = scene.transition_duration_seconds
            if overlap <= 0 or overlap >= min(scene.duration_seconds, clips[-1].duration_seconds):
                raise ValueError(f"Scene {scene.scene_id} has an invalid transition duration")
            cursor -= overlap
        clips.append(
            TimelineClip(
                scene_id=scene.scene_id,
                path=scene.asset_path,
                start_seconds=round(cursor, 6),
                duration_seconds=scene.duration_seconds,
                media_kind=cast(MediaKind, scene.media_kind),
                transition=scene.transition,
                transition_duration_seconds=overlap,
                camera_motion=scene.camera_motion,
            )
        )
        cursor += scene.duration_seconds

    tracks: list[AudioTrack] = []
    if narration_path:
        tracks.append(AudioTrack(
            path=narration_path, kind="narration", gain_db=narration_gain_db,
        ))
    if music_path:
        tracks.append(AudioTrack(path=music_path, kind="music", loop=True))
    timeline = Timeline(
        clips=clips,
        width=width,
        height=height,
        fps=fps,
        audio_tracks=tracks,
        subtitles=list(subtitles),
    )
    timeline.validate()
    return timeline
