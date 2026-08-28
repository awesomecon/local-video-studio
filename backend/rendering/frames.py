"""Atomic native-size last-frame extraction from MP4 videos for continuity."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from .binaries import FFmpegBinaries, discover_binaries, require_ffmpeg
from .process import run_media_process
from .probe import MediaInfo, probe_media


LAST_FRAME_EXTRACTOR_VERSION = "last-frame-v2"
_DEFAULT_BACKOFF_SECONDS = 0.5


def build_last_frame_command(
    ffmpeg_path: Path,
    source_path: Path,
    staged_path: Path,
    *,
    backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
) -> list[str]:
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-sseof",
        f"-{backoff_seconds:.6f}",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-vf",
        "reverse",
        "-frames:v",
        "1",
        "-update",
        "1",
        "-q:v",
        "2",
        "-f",
        "image2",
        str(staged_path),
    ]


def extract_last_frame(
    source_path: str | Path,
    destination_path: str | Path,
    *,
    binaries: FFmpegBinaries | None = None,
    temp_root: str | Path | None = None,
    timeout: float = 60.0,
) -> MediaInfo:
    input_path = Path(source_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"Last-frame input video does not exist: {input_path}")

    output_path = Path(destination_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    discovered = binaries or discover_binaries()
    require_ffmpeg(discovered)

    # Probe source dimensions for the caller (pipeline verifies match).
    source_info = probe_media(input_path, discovered)
    if not source_info.has_video:
        raise ValueError(f"Input file {input_path} has no video stream.")

    # Stage atomically within the output directory to guarantee same filesystem.
    # The staged name keeps the destination's extension so image2 codec selection
    # (extension-based) matches the published file instead of falling back to MJPEG.
    descriptor, temp_path_str = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=output_path.suffix,
        dir=str(output_path.parent),
    )
    temp_path = Path(temp_path_str)
    # mkstemp creates the file open; close descriptor and rely on run_media_process.
    try:
        os.close(descriptor)
    except OSError:
        pass

    argv = build_last_frame_command(
        discovered.ffmpeg, input_path, temp_path, backoff_seconds=_DEFAULT_BACKOFF_SECONDS
    )
    try:
        run_media_process(argv, timeout=timeout)
        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise ValueError("Last-frame extraction produced no output.")

        # FFmpeg auto-rotation normalizes decoded pixels; verify the staged PNG before publish.
        output_info = probe_media(temp_path, discovered)
        if not output_info.has_video:
            raise ValueError("Last-frame extraction did not produce a readable image.")
        os.replace(str(temp_path), str(output_path))
    finally:
        temp_path.unlink(missing_ok=True)

    return output_info


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
