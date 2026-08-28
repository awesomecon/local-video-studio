"""Join per-movement audio files into the final background track.

Movements are normalized to their exact planned durations before stitching,
so a plain concat keeps the total length identical to the video. Interior
boundaries get short fade-out/fade-in dips (instead of lossy crossfades) so
movement changes breathe apart without altering total duration.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from backend.rendering.binaries import FFmpegBinaries, require_ffmpeg
from backend.rendering.process import run_media_process
from backend.tts.audio import wav_duration


# Normalize every generated movement before joining it. ACE-Step can return
# noticeably different levels for otherwise compatible prompts, and a single
# gain applied to the completed soundtrack preserves those jumps. Normalizing
# each movement to the same broadcast-style target keeps transitions even; the
# renderer still applies its separate background-music gain beneath narration.
MUSIC_TARGET_LUFS = -16.0
MUSIC_TRUE_PEAK_DB = -1.5
MUSIC_LRA = 11.0


def build_stitch_command(
    clips: Sequence[str | Path],
    destination: str | Path,
    *,
    dip_seconds: float = 1.5,
    binaries: FFmpegBinaries | None = None,
) -> list[str]:
    if not clips:
        raise ValueError("Stitching requires at least one movement clip")
    dip = min(max(0.0, dip_seconds), 3.0)
    last_index = len(clips) - 1

    argv = [
        str(require_ffmpeg(binaries)),
        "-hide_banner", "-loglevel", "error", "-y",
    ]
    for clip in clips:
        argv.extend(["-i", str(clip)])

    filters: list[str] = []
    labels: list[str] = []
    for index in range(len(clips)):
        pieces = [
            f"[{index}:a]aresample=48000",
            (
                f"loudnorm=I={MUSIC_TARGET_LUFS:g}:"
                f"TP={MUSIC_TRUE_PEAK_DB:g}:LRA={MUSIC_LRA:g}"
            ),
        ]
        if dip > 0 and index < last_index:
            duration = max(0.1, wav_duration(Path(clips[index])))
            start = max(0.0, duration - dip)
            pieces.append(f"afade=t=out:st={start:.3f}:d={dip:.3f}")
        if dip > 0 and index > 0:
            pieces.append(f"afade=t=in:st=0:d={dip:.3f}")
        label = f"mv{index}"
        filters.append(f"{','.join(pieces)}[{label}]")
        labels.append(label)

    filters.append(
        f"{''.join(f'[{label}]' for label in labels)}concat=n={len(labels)}:v=0:a=1[out]"
    )
    argv.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[out]",
        "-ar", "48000", "-ac", "2",
        "-c:a", "pcm_s16le",
        str(destination),
    ])
    return argv


def stitch_movements(
    clips: Sequence[str | Path],
    destination: str | Path,
    *,
    dip_seconds: float = 1.5,
    binaries: FFmpegBinaries | None = None,
) -> Path:
    command = build_stitch_command(clips, destination, dip_seconds=dip_seconds, binaries=binaries)
    run_media_process(command, timeout=max(120.0, len(clips) * 60.0))
    return Path(destination)
