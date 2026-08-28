"""Tiny deterministic media generators for mock-mode integration tests."""

from __future__ import annotations

from pathlib import Path

from .binaries import FFmpegBinaries, discover_binaries, require_ffmpeg
from .process import run_media_process


def _prepare(destination: str | Path) -> Path:
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _seed_color(seed: int) -> str:
    value = (seed * 2_654_435_761) & 0xFFFFFF
    return f"0x{value:06x}"


def create_placeholder_image(
    destination: str | Path,
    *,
    width: int = 640,
    height: int = 360,
    seed: int = 0,
    binaries: FFmpegBinaries | None = None,
) -> Path:
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")
    output = _prepare(destination)
    ffmpeg = require_ffmpeg(binaries or discover_binaries())
    run_media_process(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={_seed_color(seed)}:s={width}x{height}:d=0.04",
            "-frames:v",
            "1",
            str(output),
        ],
        timeout=30,
    )
    return output


def create_placeholder_audio(
    destination: str | Path,
    *,
    duration_seconds: float = 1.0,
    frequency_hz: int = 440,
    volume: float = 0.08,
    binaries: FFmpegBinaries | None = None,
) -> Path:
    if duration_seconds <= 0 or frequency_hz <= 0 or not 0 <= volume <= 1:
        raise ValueError("Invalid placeholder audio settings")
    output = _prepare(destination)
    ffmpeg = require_ffmpeg(binaries or discover_binaries())
    run_media_process(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency_hz}:sample_rate=48000:duration={duration_seconds}",
            "-af",
            f"volume={volume}",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        timeout=30,
    )
    return output


def create_placeholder_video(
    destination: str | Path,
    *,
    duration_seconds: float = 1.0,
    width: int = 640,
    height: int = 360,
    fps: int = 24,
    seed: int = 0,
    with_audio: bool = False,
    binaries: FFmpegBinaries | None = None,
) -> Path:
    if duration_seconds <= 0 or width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("Invalid placeholder video settings")
    output = _prepare(destination)
    ffmpeg = require_ffmpeg(binaries or discover_binaries())
    argv = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=s={width}x{height}:r={fps}:d={duration_seconds},hue=h={seed % 360}",
    ]
    if with_audio:
        argv.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={330 + seed % 220}:sample_rate=48000:duration={duration_seconds}",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
            ]
        )
    argv.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-shortest" if with_audio else "-an",
            str(output),
        ]
    )
    run_media_process(argv, timeout=60)
    return output
