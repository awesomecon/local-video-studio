"""Real FFmpeg checks on this runner's filesystem, including punctuation."""

from pathlib import Path

from backend.rendering.binaries import discover_binaries
from backend.rendering.commands import RenderOptions
from backend.rendering.mock_media import create_placeholder_video
from backend.rendering.renderer import FFmpegRenderer
from backend.timeline.models import SubtitleCue, Timeline, TimelineClip


def test_burn_subtitles_with_unicode_spaces_and_apostrophes(tmp_path: Path) -> None:
    workspace = tmp_path / "Studio's café [draft], files"
    workspace.mkdir()
    binaries = discover_binaries()
    source = create_placeholder_video(workspace / "source.mp4", duration_seconds=0.5,
                                      width=160, height=90, binaries=binaries)
    timeline = Timeline(
        clips=[TimelineClip("one", source, 0, 0.5)], width=160, height=90,
        subtitles=[SubtitleCue(0, 0.4, "Portable captions")],
    )
    renderer = FFmpegRenderer(binaries, temp_root=workspace)
    output = workspace / "final.mp4"
    info = renderer.render(timeline, output, RenderOptions(burn_subtitles=True))
    assert output.is_file() and info.duration_seconds > 0
