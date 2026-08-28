"""ffprobe-backed media metadata with an FFmpeg-only compatibility fallback."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .binaries import FFmpegBinaries, discover_binaries, require_ffmpeg


@dataclass(frozen=True, slots=True)
class MediaInfo:
    path: Path
    duration_seconds: float | None
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool
    format_name: str | None = None
    fps: float | None = None


def _rate(value: Any) -> float | None:
    """Parse an FFmpeg rational rate such as ``12/1`` into a float."""
    if not value or value == "0/0":
        return None
    try:
        numerator, _, denominator = str(value).partition("/")
        rate = float(numerator) / float(denominator or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probe_json(path: Path, binary: Path) -> MediaInfo:
    result = subprocess.run(
        [
            str(binary),
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,format_name:"
                "stream=codec_type,width,height,duration,"
                "avg_frame_rate,r_frame_rate"
            ),
            "-of",
            "json",
            str(path),
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "ffprobe could not inspect media")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    fmt = payload.get("format", {})
    duration = _number(fmt.get("duration"))
    fps = _rate(video.get("avg_frame_rate")) if video else None
    if fps is None and video:
        fps = _rate(video.get("r_frame_rate"))
    if duration is None:
        # Fall back to stream durations, but only when at least one stream
        # actually reports one: an explicit 0.0 must survive while a fully
        # absent duration stays unknown (None).
        reported = [_number(stream.get("duration")) for stream in streams]
        if any(value is not None for value in reported):
            duration = max(value or 0.0 for value in reported)
    return MediaInfo(
        path=path,
        duration_seconds=duration,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        has_video=video is not None,
        has_audio=audio is not None,
        format_name=fmt.get("format_name"),
        fps=fps,
    )


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream .* Video:.*?\b(\d{2,5})x(\d{2,5})\b")


_FPS_RE = re.compile(r"([\d.]+)\sfps")


def _probe_with_ffmpeg(path: Path, binary: Path) -> MediaInfo:
    result = subprocess.run(
        [str(binary), "-hide_banner", "-i", str(path)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )
    text = result.stderr
    duration_match = _DURATION_RE.search(text)
    video_match = _VIDEO_RE.search(text)
    fps_match = _FPS_RE.search(text)
    has_video = " Video:" in text
    has_audio = " Audio:" in text
    if not (duration_match or has_video or has_audio):
        raise ValueError(text.strip()[-1000:] or "FFmpeg could not inspect media")
    duration = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return MediaInfo(
        path=path,
        duration_seconds=duration,
        width=int(video_match.group(1)) if video_match else None,
        height=int(video_match.group(2)) if video_match else None,
        has_video=has_video,
        has_audio=has_audio,
        fps=float(fps_match.group(1)) if fps_match else None,
    )


def probe_media(path: str | Path, binaries: FFmpegBinaries | None = None) -> MediaInfo:
    media_path = Path(path)
    if not media_path.is_file():
        raise FileNotFoundError(media_path)
    discovered = binaries or discover_binaries()
    if discovered.ffprobe:
        return _probe_json(media_path, discovered.ffprobe)
    return _probe_with_ffmpeg(media_path, require_ffmpeg(discovered))


_FRAME_PACKETS_RE = re.compile(r"(\d+)\s*$")
_FFMPEG_PROGRESS_RE = re.compile(r"frame=\s*(\d+)")


def count_video_frames(path: str | Path, binaries: FFmpegBinaries | None = None) -> int:
    """Count decoded video frames exactly (packet count; decode-count fallback)."""
    media_path = Path(path)
    if not media_path.is_file():
        raise FileNotFoundError(media_path)
    discovered = binaries or discover_binaries()
    ffprobe = discovered.ffprobe
    if ffprobe:
        result = subprocess.run(
            [
                str(ffprobe),
                "-v", "error",
                "-select_streams", "v:0",
                "-count_packets",
                "-show_entries", "stream=nb_read_packets",
                "-of", "csv=p=0",
                str(media_path),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=60,
        )
        match = _FRAME_PACKETS_RE.search(result.stdout.strip())
        if result.returncode == 0 and match:
            return int(match.group(1))
    ffmpeg = require_ffmpeg(discovered)
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(media_path),
         "-map", "0:v:0", "-f", "null", "-"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    matches = _FFMPEG_PROGRESS_RE.findall(result.stderr)
    if not matches:
        raise ValueError(f"could not count video frames in {media_path}")
    return int(matches[-1])
