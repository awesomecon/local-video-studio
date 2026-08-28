"""Locate FFmpeg tools without installing or modifying the host environment."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegNotFoundError(RuntimeError):
    """Raised when no usable FFmpeg executable can be found."""


@dataclass(frozen=True, slots=True)
class FFmpegBinaries:
    ffmpeg: Path | None
    ffprobe: Path | None
    source: str

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None


def _usable_executable(value: str | os.PathLike[str] | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    try:
        result = subprocess.run(
            [str(path), "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path.resolve() if result.returncode == 0 else None


def _bundled_ffmpeg() -> Path | None:
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        return _usable_executable(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, OSError, RuntimeError):
        return None


def _sibling_ffprobe(ffmpeg: Path | None) -> Path | None:
    if ffmpeg is None:
        return None
    candidates = (ffmpeg.with_name("ffprobe"), ffmpeg.with_name("ffprobe.exe"))
    return next((path for path in candidates if _usable_executable(path)), None)


def discover_binaries(
    *,
    ffmpeg_override: str | os.PathLike[str] | None = None,
    ffprobe_override: str | os.PathLike[str] | None = None,
) -> FFmpegBinaries:
    """Find explicit overrides, system tools, then imageio-ffmpeg's bundled binary."""

    explicit_ffmpeg = _usable_executable(ffmpeg_override)
    explicit_ffprobe = _usable_executable(ffprobe_override)
    if explicit_ffmpeg:
        return FFmpegBinaries(
            explicit_ffmpeg,
            explicit_ffprobe or _sibling_ffprobe(explicit_ffmpeg),
            "override",
        )

    system_ffmpeg = _usable_executable(shutil.which("ffmpeg"))
    system_ffprobe = explicit_ffprobe or _usable_executable(shutil.which("ffprobe"))
    if system_ffmpeg:
        return FFmpegBinaries(system_ffmpeg, system_ffprobe, "system")

    bundled = _bundled_ffmpeg()
    if bundled:
        return FFmpegBinaries(
            bundled,
            system_ffprobe or _sibling_ffprobe(bundled),
            "imageio_ffmpeg",
        )
    return FFmpegBinaries(None, system_ffprobe, "unavailable")


def require_ffmpeg(binaries: FFmpegBinaries | None = None) -> Path:
    discovered = binaries or discover_binaries()
    if discovered.ffmpeg is None:
        raise FFmpegNotFoundError(
            "FFmpeg was not found on PATH and imageio-ffmpeg did not provide a bundled binary."
        )
    return discovered.ffmpeg
