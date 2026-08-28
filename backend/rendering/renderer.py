"""High-level preview and final rendering with atomic output publication."""

from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from pathlib import Path

from backend.timeline.models import Timeline

from .binaries import FFmpegBinaries, discover_binaries, require_ffmpeg
from .commands import RenderOptions, build_finalize_command, build_video_command
from .process import run_media_process
from .probe import MediaInfo, probe_media
from .subtitles import write_ass, write_srt


def _create_staged_file(destination: Path) -> Path:
    """Stage next to the destination so publication stays on one filesystem.

    The staged name keeps the destination's suffix so FFmpeg infers the same
    muxer/codec it would for the final file; os.replace can then never fail
    with an invalid cross-device link.
    """

    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=destination.suffix,
        dir=str(destination.parent),
    )
    os.close(descriptor)
    return Path(staged_name)


class FFmpegRenderer:
    def __init__(
        self,
        binaries: FFmpegBinaries | None = None,
        *,
        temp_root: str | Path | None = None,
    ) -> None:
        self.binaries = binaries or discover_binaries()
        require_ffmpeg(self.binaries)
        self.temp_root = Path(temp_root) if temp_root else None

    def render(
        self,
        timeline: Timeline,
        destination: str | Path,
        options: RenderOptions | None = None,
    ) -> MediaInfo:
        timeline.validate()
        selected = options or RenderOptions()
        selected.validate()
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.temp_root:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="lvs-render-",
            dir=str(self.temp_root) if self.temp_root else None,
        ) as temporary:
            workspace = Path(temporary)
            silent_video = workspace / "video.mp4"
            run_media_process(
                build_video_command(timeline, silent_video, selected, self.binaries),
                timeout=max(120.0, timeline.duration_seconds * 10),
            )
            subtitle_path = None
            if timeline.subtitles:
                if selected.burn_subtitles:
                    width = selected.width or timeline.width
                    height = selected.height or timeline.height
                    subtitle_path = write_ass(
                        timeline.subtitles,
                        workspace / "captions.ass",
                        width=width,
                        height=height,
                    )
                else:
                    subtitle_path = write_srt(timeline.subtitles, workspace / "captions.srt")
            staged_output = _create_staged_file(output)
            try:
                run_media_process(
                    build_finalize_command(
                        silent_video,
                        timeline,
                        staged_output,
                        selected,
                        self.binaries,
                        subtitle_path=subtitle_path,
                    ),
                    timeout=max(120.0, timeline.duration_seconds * 10),
                )
                os.replace(staged_output, output)
            finally:
                staged_output.unlink(missing_ok=True)
        return probe_media(output, self.binaries)

    def render_preview(
        self,
        timeline: Timeline,
        destination: str | Path,
        options: RenderOptions | None = None,
    ) -> MediaInfo:
        base = options or RenderOptions()
        preview = replace(
            base,
            width=base.width or 640,
            height=base.height or 360,
            video_preset="ultrafast",
            crf=max(base.crf, 26),
        )
        return self.render(timeline, destination, preview)

    def render_final(
        self,
        timeline: Timeline,
        destination: str | Path,
        options: RenderOptions | None = None,
    ) -> MediaInfo:
        return self.render(timeline, destination, options or RenderOptions())

    def extract_frame(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        timestamp_seconds: float,
        width: int = 640,
        height: int = 360,
    ) -> Path:
        """Extract a deterministic, letterboxed thumbnail image atomically."""
        input_path = Path(source)
        if not input_path.is_file():
            raise FileNotFoundError(input_path)
        if timestamp_seconds < 0:
            raise ValueError("Thumbnail timestamp cannot be negative")
        if width <= 0 or height <= 0:
            raise ValueError("Thumbnail dimensions must be positive")

        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        staged_output = _create_staged_file(output)
        try:
            run_media_process(
                [
                    str(self.binaries.ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{timestamp_seconds:.6f}",
                    "-i",
                    str(input_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    (
                        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                    ),
                    str(staged_output),
                ],
                timeout=60,
            )
            if not staged_output.is_file() or staged_output.stat().st_size == 0:
                raise ValueError(
                    f"Frame extraction produced no output at {timestamp_seconds:.6f}s; "
                    "the timestamp may lie beyond the end of the source media"
                )
            os.replace(staged_output, output)
        finally:
            staged_output.unlink(missing_ok=True)
        return output


__all__ = ["FFmpegRenderer", "RenderOptions"]
