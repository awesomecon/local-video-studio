"""Bounded FFmpeg feature inspection; discovery alone does not prove rendering."""

from functools import lru_cache
from pathlib import Path

from .process import CanceledError, MediaProcessError, run_media_process


@lru_cache(maxsize=8)
def _probe(executable: str, size: int, modified: int) -> tuple[frozenset[str], frozenset[str]]:
    del size, modified  # File identity invalidates cached results after upgrades.
    outputs = []
    for option in ("-encoders", "-filters"):
        result = run_media_process([executable, "-hide_banner", option], timeout=10, capture_stdout=True)
        names = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] != "=" and len(parts[0]) in (3, 6):
                names.add(parts[1])
        outputs.append(frozenset(names))
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
