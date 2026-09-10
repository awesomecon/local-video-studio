"""Real FFmpeg checks on this runner's filesystem, including punctuation."""

from pathlib import Path

import pytest

from backend.rendering.binaries import discover_binaries
from backend.rendering.commands import RenderOptions
from backend.rendering.mock_media import create_placeholder_video
from backend.rendering.renderer import FFmpegRenderer
from backend.timeline.models import SubtitleCue, Timeline, TimelineClip


def _timeline(source: Path, caption: str = "Portable captions") -> Timeline:
    return Timeline(
        clips=[TimelineClip("one", source, 0, 0.5)], width=160, height=90,
        subtitles=[SubtitleCue(0, 0.4, caption)],
    )


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


@pytest.mark.parametrize("dirname", [
    "café au lait (final) [v2]",
    "日本語フォルダ 名前",
    "sp ace [brackets] (parens) 'quotes'",
])
def test_preview_render_and_frame_extraction_in_hostile_directories(
    tmp_path: Path, dirname: str,
) -> None:
    """Preview renders and frame extraction must survive hostile native paths.

    The CI matrix executes this on each OS with true separators and Unicode
    handling, so a passing Linux run alone is not a portability claim.
    """
    workspace = tmp_path / dirname
    workspace.mkdir()
    binaries = discover_binaries()
    source = create_placeholder_video(workspace / "s ource (1).mp4", duration_seconds=0.5,
                                      width=160, height=90, binaries=binaries)
    renderer = FFmpegRenderer(binaries, temp_root=workspace)
    preview = workspace / "prev iew [cut].mp4"
    info = renderer.render_preview(_timeline(source), preview)
    assert preview.is_file() and info.duration_seconds > 0
    frame = renderer.extract_frame(
        preview, workspace / "thumb (1).png",
        timestamp_seconds=0.1, width=64, height=36,
    )
    assert frame.is_file() and frame.stat().st_size > 0
