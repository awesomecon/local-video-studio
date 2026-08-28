"""Shared tiny-media helpers for multi-shot renderer tests.

Everything here is deterministic and FFmpeg-only: solid-color PNG fixtures,
RGBA overlays with controlled alpha, exact-frame pixel sampling, and small
schema builders. No model downloads, no GPU work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PIL import Image

from backend.rendering.binaries import FFmpegBinaries, discover_binaries, require_ffmpeg
from backend.rendering.process import run_media_process
from backend.schemas.models import Scene
from backend.schemas.shots import OverlayCue, Shot

CANVAS = (160, 90)
FPS = 12


def available_binaries() -> FFmpegBinaries:
    discovered = discover_binaries()
    if not discovered.available:
        raise RuntimeError("FFmpeg is unavailable")
    return discovered


def run_ffmpeg(binaries: FFmpegBinaries, argv: Sequence[str], timeout: float = 60.0) -> None:
    require_ffmpeg(binaries)
    run_media_process([str(binaries.ffmpeg), *argv], timeout=timeout)


def create_color_png(
    destination: Path,
    *,
    color: str,
    width: int = CANVAS[0],
    height: int = CANVAS[1],
    binaries: FFmpegBinaries | None = None,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected = binaries or available_binaries()
    run_ffmpeg(
        selected,
        [
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height},format=rgba",
            "-frames:v", "1",
            str(destination),
        ],
    )
    return destination


def create_alpha_png(
    destination: Path,
    *,
    color: str,
    alpha: int,
    width: int,
    height: int,
    binaries: FFmpegBinaries | None = None,
) -> Path:
    """Solid color PNG with a uniform alpha level (0-255)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected = binaries or available_binaries()
    run_ffmpeg(
        selected,
        [
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            (
                f"color=c={color}:s={width}x{height},format=rgba,"
                f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a={alpha}"
            ),
            "-frames:v", "1",
            str(destination),
        ],
    )
    return destination


def sample_pixel(
    media: Path,
    frame_index: int,
    *,
    x: int | None = None,
    y: int | None = None,
    binaries: FFmpegBinaries | None = None,
) -> tuple[int, int, int]:
    """Decode one frame to PNG and return its ``(r, g, b)`` at a pixel."""
    import tempfile

    selected = binaries or available_binaries()
    with tempfile.TemporaryDirectory(prefix="lvs-pixel-") as temporary:
        workspace = Path(temporary)
        frame_png = workspace / "frame.png"
        run_ffmpeg(
            selected,
            [
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(media),
                "-vf", f"select=eq(n\\,{frame_index})",
                "-frames:v", "1",
                str(frame_png),
            ],
        )
        with Image.open(frame_png) as image:
            rgb = image.convert("RGB")
            px = rgb.getpixel((
                rgb.width // 2 if x is None else x,
                rgb.height // 2 if y is None else y,
            ))
    return tuple(px)  # type: ignore[return-value]


def assert_pixel_close(
    actual: tuple[int, int, int],
    expected: tuple[int, int, int],
    *,
    tolerance: int = 12,
) -> None:
    """Codec-tolerant RGB comparison (yuv420p round-trips shift a few units)."""
    for got, want in zip(actual, expected, strict=True):
        assert abs(got - want) <= tolerance, (actual, expected)


def make_scene(
    project_id: str,
    *,
    duration: float = 3.0,
    scene_id: str | None = None,
    index: int = 0,
) -> Scene:
    from backend.schemas.models import VisualType

    return Scene(
        id=scene_id or "scene-plan-1",
        project_id=project_id,
        index=index,
        title="plan fixture",
        duration=duration,
        visual_type=VisualType.FLUX_STILL,
    )


def make_shot(
    project_id: str,
    scene_id: str,
    index: int,
    *,
    shot_id: str | None = None,
    duration_seconds: float = 1.0,
    visual_type: str = "flux_still",
    camera_instruction: str = "",
    transition_in: dict | None = None,
    overlays: Sequence[OverlayCue] = (),
    settings: dict | None = None,
    source_in_seconds: float | None = None,
    source_out_seconds: float | None = None,
    start_mode: str = "fixed",
    locked: bool = False,
    status: str = "ready",
) -> Shot:
    payload: dict = {
        "id": shot_id or f"shot-{index}",
        "project_id": project_id,
        "scene_id": scene_id,
        "index": index,
        "duration_seconds": duration_seconds,
        "visual_type": visual_type,
        "camera_instruction": camera_instruction,
        "overlays": list(overlays),
        "start_mode": start_mode,
        "locked": locked,
        "status": status,
    }
    if transition_in is not None:
        payload["transition_in"] = transition_in
    if settings is not None:
        payload["settings"] = settings
    if source_in_seconds is not None:
        payload["source_in_seconds"] = source_in_seconds
    if source_out_seconds is not None:
        payload["source_out_seconds"] = source_out_seconds
    return Shot.model_validate(payload)
