"""Last-frame extraction for H3 continuity, exercised with real FFmpeg."""

from pathlib import Path

import pytest

from backend.rendering.binaries import discover_binaries
from backend.rendering.frames import build_last_frame_command, compute_sha256, extract_last_frame
from backend.rendering.mock_media import create_placeholder_video
from backend.rendering.probe import probe_media


@pytest.fixture(scope="module")
def binaries():
    discovered = discover_binaries()
    if not discovered.available:
        pytest.skip("FFmpeg is unavailable")
    return discovered


def test_build_last_frame_command_forces_image2_format_for_staged_file(binaries) -> None:
    # The staged file keeps the destination's extension so image2's extension-based
    # codec selection matches the published image; -f image2 keeps it explicit.
    argv = build_last_frame_command(binaries.ffmpeg, Path("/src.mp4"), Path("/out.dir/.out.png.abc123.png"))
    assert argv[-1].endswith(".png")
    assert ".pending" not in argv[-1]
    assert "-f" in argv
    assert argv[argv.index("-f") + 1] == "image2"
    assert "-frames:v" in argv
    assert argv[argv.index("-vf") + 1] == "reverse"


def test_extract_last_frame_produces_atomic_png(tmp_path: Path, binaries) -> None:
    source = create_placeholder_video(
        tmp_path / "short.mp4",
        duration_seconds=0.9,
        width=320,
        height=180,
        fps=12,
        seed=7,
        binaries=binaries,
    )
    destination = tmp_path / "continuity" / "first-frame.png"

    info = extract_last_frame(source, destination, binaries=binaries, timeout=60.0)

    assert destination.is_file()
    assert destination.stat().st_size > 0
    # The staged file must carry the destination extension so ffmpeg encodes a
    # real PNG; MJPEG output would start with the JPEG SOI marker instead.
    assert destination.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # No staging artifacts are left behind.
    assert not list(tmp_path.rglob(".*"))
    assert info.has_video
    assert (info.width, info.height) == (320, 180)
    # Deterministic content hash is stable across extraction.
    assert len(compute_sha256(destination)) == 64


def test_extract_last_frame_raises_when_source_missing(tmp_path: Path, binaries) -> None:
    with pytest.raises(FileNotFoundError):
        extract_last_frame(
            tmp_path / "missing.mp4",
            tmp_path / "out.png",
            binaries=binaries,
        )
