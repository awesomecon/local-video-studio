"""Bounded FFmpeg feature inspection; discovery alone does not prove rendering."""

import re
from functools import lru_cache
from pathlib import Path

from .process import CanceledError, MediaProcessError, run_media_process

#: Entry lines in `-encoders`/`-filters` output start with exactly one space;
#: legend lines use two and titles use none. Flag-column widths changed across
#: releases (three filter flags through FFmpeg 8, two from FFmpeg 9), so the
#: structure, not the width, identifies an entry.
_NAME = re.compile(r"[A-Za-z0-9_+.@-]+")


def _entry_names(output: str) -> frozenset[str]:
    names = set()
    for line in output.splitlines():
        if not line.startswith(" ") or line.startswith("  "):
            continue
        parts = line.split()
        if len(parts) >= 2 and _NAME.fullmatch(parts[1]):
            names.add(parts[1])
    return frozenset(names)


@lru_cache(maxsize=8)
def _probe(executable: str, size: int, modified: int) -> tuple[frozenset[str], frozenset[str]]:
    del size, modified  # File identity invalidates cached results after upgrades.
    outputs = []
    for option in ("-encoders", "-filters"):
        result = run_media_process([executable, "-hide_banner", option], timeout=10, capture_stdout=True)
        outputs.append(_entry_names(result.stdout))
    return outputs[0], outputs[1]


def ffmpeg_features(executable: Path) -> tuple[frozenset[str], frozenset[str]]:
    stat = executable.stat()
    return _probe(str(executable), stat.st_size, stat.st_mtime_ns)


def require_render_features(executable: Path, *, video_codec: str, audio_codec: str,
                            burn_subtitles: bool = False) -> None:
    """Reject missing selected features before starting a render."""
    try:
        encoders, filters = ffmpeg_features(executable)
    except CanceledError:
        raise
    except (MediaProcessError, OSError) as exc:
        raise ValueError("Could not inspect the configured FFmpeg build; check its installation.") from exc
    missing = sorted({video_codec, audio_codec} - encoders)
    if burn_subtitles and "subtitles" not in filters:
        missing.append("subtitles filter (libass)")
    if missing:
        raise ValueError("The configured FFmpeg build lacks: " + ", ".join(missing)
                         + ". Select a build with these features.")
