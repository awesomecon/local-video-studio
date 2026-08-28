"""Dependency-light, JSON-serializable timeline types."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

MediaKind = Literal["image", "video", "title", "diagram"]
AudioKind = Literal["narration", "music", "ambience", "effect"]


@dataclass(frozen=True, slots=True)
class TimelineClip:
    scene_id: str
    path: Path
    start_seconds: float
    duration_seconds: float
    media_kind: MediaKind = "video"
    transition: str = "cut"
    transition_duration_seconds: float = 0.0
    camera_motion: str | None = None

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


@dataclass(frozen=True, slots=True)
class AudioTrack:
    path: Path
    kind: AudioKind
    start_seconds: float = 0.0
    gain_db: float = 0.0
    loop: bool = False


@dataclass(frozen=True, slots=True)
class SubtitleWord:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    start_seconds: float
    end_seconds: float
    text: str
    words: tuple[SubtitleWord, ...] = ()


@dataclass(slots=True)
class Timeline:
    clips: list[TimelineClip]
    width: int = 1920
    height: int = 1080
    fps: int = 24
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    subtitles: list[SubtitleCue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return max((clip.end_seconds for clip in self.clips), default=0.0)

    def validate(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.fps <= 0:
            raise ValueError("Timeline width, height, and fps must be positive")
        if not self.clips:
            raise ValueError("Timeline must contain at least one clip")
        previous_start = -1.0
        for clip in self.clips:
            if not math.isfinite(clip.start_seconds) or not math.isfinite(clip.duration_seconds):
                raise ValueError(f"Scene {clip.scene_id} timing must be finite")
            if clip.start_seconds < 0 or clip.duration_seconds <= 0:
                raise ValueError(f"Invalid timing for scene {clip.scene_id}")
            if (
                not math.isfinite(clip.transition_duration_seconds)
                or clip.transition_duration_seconds < 0
            ):
                raise ValueError("Transition duration cannot be negative")
            if clip.transition != "cut" and clip.transition_duration_seconds <= 0:
                raise ValueError(
                    f"Scene {clip.scene_id} uses transition '{clip.transition}' which "
                    "requires a positive transition_duration_seconds"
                )
            if clip.start_seconds < previous_start:
                raise ValueError("Timeline clips must be ordered by start time")
            previous_start = clip.start_seconds
        for cue in self.subtitles:
            if (
                not math.isfinite(cue.start_seconds)
                or not math.isfinite(cue.end_seconds)
                or cue.start_seconds < 0
                or cue.end_seconds <= cue.start_seconds
            ):
                raise ValueError("Subtitle cues require positive, ordered timestamps")
            previous_word_start = cue.start_seconds
            for word in cue.words:
                if (
                    not math.isfinite(word.start_seconds)
                    or not math.isfinite(word.end_seconds)
                    or word.start_seconds < cue.start_seconds
                    or word.end_seconds > cue.end_seconds
                    or word.end_seconds <= word.start_seconds
                    or word.start_seconds < previous_word_start
                    or not word.text.strip()
                ):
                    raise ValueError("Subtitle words require ordered timestamps inside their cue")
                previous_word_start = word.start_seconds

    def to_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, list):
                return [convert(item) for item in value]
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            return value

        return convert(asdict(self))

    def write_json(self, destination: str | Path) -> Path:
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return output
