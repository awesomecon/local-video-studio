"""Non-destructive media quality-control checks."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from backend.timeline.models import SubtitleCue, Timeline

from .binaries import FFmpegBinaries, discover_binaries, require_ffmpeg
from .probe import MediaInfo, probe_media


class QCSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class QCIssue:
    code: str
    message: str
    severity: QCSeverity
    path: Path | None = None


@dataclass(slots=True)
class QCReport:
    issues: list[QCIssue] = field(default_factory=list)
    inspected_files: int = 0

    @property
    def passed(self) -> bool:
        return not any(issue.severity == QCSeverity.ERROR for issue in self.issues)

    def extend(self, other: "QCReport") -> None:
        self.issues.extend(other.issues)
        self.inspected_files += other.inspected_files


@dataclass(frozen=True, slots=True)
class AudioLevels:
    mean_db: float | None
    max_db: float | None


_MEAN_RE = re.compile(r"mean_volume:\s*(-?(?:inf|\d+(?:\.\d+)?)) dB")
_MAX_RE = re.compile(r"max_volume:\s*(-?(?:inf|\d+(?:\.\d+)?)) dB")


def _db(match: re.Match[str] | None) -> float | None:
    if not match or match.group(1) == "-inf":
        return None
    return float(match.group(1))


def audio_levels(path: str | Path, binaries: FFmpegBinaries | None = None) -> AudioLevels:
    selected = binaries or discover_binaries()
    ffmpeg = require_ffmpeg(selected)
    result = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-vn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        return AudioLevels(None, None)
    return AudioLevels(_db(_MEAN_RE.search(result.stderr)), _db(_MAX_RE.search(result.stderr)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MediaQC:
    def __init__(self, binaries: FFmpegBinaries | None = None) -> None:
        self.binaries = binaries or discover_binaries()

    def check_file(
        self,
        path: str | Path,
        *,
        expected_duration_seconds: float | None = None,
        expected_resolution: tuple[int, int] | None = None,
        require_audio: bool = False,
        duration_tolerance_seconds: float = 0.25,
    ) -> QCReport:
        media_path = Path(path)
        report = QCReport(inspected_files=1)
        if not media_path.exists():
            report.issues.append(
                QCIssue(
                    "missing_file",
                    "Expected media file is missing",
                    QCSeverity.ERROR,
                    media_path,
                )
            )
            return report
        if not media_path.is_file() or media_path.stat().st_size == 0:
            report.issues.append(
                QCIssue(
                    "empty_file",
                    "Media file is empty or invalid",
                    QCSeverity.ERROR,
                    media_path,
                )
            )
            return report
        try:
            info = probe_media(media_path, self.binaries)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            report.issues.append(
                QCIssue(
                    "corrupt_media",
                    f"Media could not be decoded: {exc}",
                    QCSeverity.ERROR,
                    media_path,
                )
            )
            return report
        self._check_metadata(
            report,
            info,
            expected_duration_seconds,
            expected_resolution,
            require_audio,
            duration_tolerance_seconds,
        )
        if info.has_audio:
            levels = audio_levels(media_path, self.binaries)
            if levels.max_db is None:
                report.issues.append(
                    QCIssue("audio_silence", "Audio appears silent", QCSeverity.WARNING, media_path)
                )
            elif levels.max_db >= -0.1:
                report.issues.append(
                    QCIssue(
                        "audio_clipping",
                        f"Audio peak ({levels.max_db:.1f} dBFS) is near clipping",
                        QCSeverity.WARNING,
                        media_path,
                    )
                )
            elif levels.mean_db is not None and levels.mean_db <= -55:
                report.issues.append(
                    QCIssue(
                        "audio_silence",
                        "Audio is effectively silent",
                        QCSeverity.WARNING,
                        media_path,
                    )
                )
        return report

    @staticmethod
    def _check_metadata(
        report: QCReport,
        info: MediaInfo,
        expected_duration: float | None,
        expected_resolution: tuple[int, int] | None,
        require_audio: bool,
        tolerance: float,
    ) -> None:
        if expected_duration is not None and (
            info.duration_seconds is None
            or abs(info.duration_seconds - expected_duration) > tolerance
        ):
            report.issues.append(
                QCIssue(
                    "duration_mismatch",
                    f"Expected {expected_duration:.3f}s, found {info.duration_seconds!r}s",
                    QCSeverity.ERROR,
                    info.path,
                )
            )
        if expected_resolution and (info.width, info.height) != expected_resolution:
            report.issues.append(
                QCIssue(
                    "resolution_mismatch",
                    f"Expected {expected_resolution[0]}x{expected_resolution[1]}, "
                    f"found {info.width}x{info.height}",
                    QCSeverity.ERROR,
                    info.path,
                )
            )
        if require_audio and not info.has_audio:
            report.issues.append(
                QCIssue("missing_audio", "Media has no audio stream", QCSeverity.ERROR, info.path)
            )

    def check_timeline(self, timeline: Timeline) -> QCReport:
        report = QCReport()
        try:
            timeline.validate()
        except ValueError as exc:
            report.issues.append(QCIssue("invalid_timeline", str(exc), QCSeverity.ERROR))
            return report
        expected_start = 0.0
        hashes: dict[str, Path] = {}
        for clip in timeline.clips:
            overlap = clip.transition_duration_seconds if clip.transition != "cut" else 0.0
            expected_start -= overlap
            if abs(clip.start_seconds - expected_start) > 0.01:
                report.issues.append(
                    QCIssue(
                        "timeline_gap",
                        f"Scene {clip.scene_id} starts at {clip.start_seconds:.3f}s; "
                        f"expected {expected_start:.3f}s",
                        QCSeverity.ERROR,
                        clip.path,
                    )
                )
            expected_start = clip.end_seconds
            # Source videos are trimmed or last-frame padded by the renderer to
            # their assigned timeline duration. QC the source itself here, not
            # the render-time duration transformation.
            file_report = self.check_file(clip.path)
            report.extend(file_report)
            if clip.path.is_file() and clip.path.stat().st_size:
                digest = _sha256(clip.path)
                if digest in hashes:
                    report.issues.append(
                        QCIssue(
                            "duplicate_output",
                            f"Scene {clip.scene_id} duplicates {hashes[digest]}",
                            QCSeverity.WARNING,
                            clip.path,
                        )
                    )
                else:
                    hashes[digest] = clip.path
        report.issues.extend(check_subtitle_overflow(timeline.subtitles))
        return report


def check_subtitle_overflow(
    cues: Iterable[SubtitleCue],
    *,
    maximum_characters_per_line: int = 42,
    maximum_lines: int = 2,
) -> list[QCIssue]:
    issues: list[QCIssue] = []
    for index, cue in enumerate(cues, start=1):
        lines = cue.text.splitlines() or [cue.text]
        if len(lines) > maximum_lines or any(
            len(line) > maximum_characters_per_line for line in lines
        ):
            issues.append(
                QCIssue(
                    "subtitle_overflow",
                    f"Subtitle cue {index} may exceed the safe display area",
                    QCSeverity.WARNING,
                )
            )
    return issues


def archive_variant(path: str | Path, archive_directory: str | Path) -> Path:
    """Move a rejected generation into an archive without deleting or overwriting it."""

    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    archive = Path(archive_directory)
    archive.mkdir(parents=True, exist_ok=True)
    candidate = archive / source.name
    suffix = 1
    while candidate.exists():
        candidate = archive / f"{source.stem}-{suffix}{source.suffix}"
        suffix += 1
    return Path(shutil.move(str(source), candidate))
