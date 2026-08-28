"""Structured overlay composition for shot normalization.

Overlays enter the renderer as validated ``OverlayCue`` payloads plus
project-local rasterized assets (transparent RGBA PNGs for exact text,
graphics, or images). Everything FFmpeg sees here is derived from those
structured fields: nine-anchor geometry with safe-area scaling and
clamping, contain/cover/stretch sizing, ordered ``overlay`` filters with
half-open ``[start, end)`` enable windows, alpha fades, and a constant
opacity via ``colorchannelmixer``. No client-authored filter expressions
exist.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from backend.schemas.shots import OverlayCue, OverlayFit

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_ANCHOR_FRACTIONS: dict[str, tuple[float, float]] = {
    "top_left": (0.0, 0.0),
    "top_center": (0.5, 0.0),
    "top_right": (1.0, 0.0),
    "center_left": (0.0, 0.5),
    "center": (0.5, 0.5),
    "center_right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0),
    "bottom_center": (0.5, 1.0),
    "bottom_right": (1.0, 1.0),
}


class OverlayAssetError(ValueError):
    """An overlay cue references an asset the caller did not resolve."""


@dataclass(frozen=True, slots=True)
class ResolvedOverlay:
    """Placement of one overlay box on the canvas, fully resolved.

    ``width``/``height`` are the post-crop visible box that anchoring and
    clamping operate on; for COVER they are the crop target while
    ``scale_width``/``scale_height`` are the larger pre-crop scaled size.
    """

    cue: OverlayCue
    input_index: int
    width: int
    height: int
    x: int
    y: int
    scale_width: int = 0
    scale_height: int = 0

    @property
    def effective_scale(self) -> tuple[int, int]:
        return (
            self.scale_width or self.width,
            self.scale_height or self.height,
        )


def sort_overlays(cues: Sequence[OverlayCue]) -> list[OverlayCue]:
    """Deterministic paint order: z_index first, then start time, then id."""
    return sorted(cues, key=lambda cue: (cue.z_index, cue.start_seconds, cue.id))


def image_pixel_size(path: str | Path) -> tuple[int, int]:
    """Read natural pixel dimensions without spawning a subprocess for PNGs."""
    overlay_path = Path(path)
    with overlay_path.open("rb") as stream:
        header = stream.read(33)
    if header[:8] == _PNG_SIGNATURE:
        width, height = struct.unpack(">II", header[16:24])
        return int(width), int(height)
    from .probe import probe_media  # local import keeps module import cost low

    info = probe_media(overlay_path)
    if info.width is None or info.height is None:
        raise ValueError(f"cannot determine overlay dimensions for {overlay_path}")
    return info.width, info.height


def _fit_size(
    natural_width: int,
    natural_height: int,
    box_width: int,
    box_height: int,
    fit: OverlayFit,
) -> tuple[int, int]:
    """Scaled source size for the box before any cover crop is applied."""
    if fit is OverlayFit.STRETCH:
        return box_width, box_height
    if fit is OverlayFit.CONTAIN:
        factor = min(box_width / natural_width, box_height / natural_height)
    else:  # cover overfills before the centered crop down to the box
        factor = max(box_width / natural_width, box_height / natural_height)
    return (
        max(1, round(natural_width * factor)),
        max(1, round(natural_height * factor)),
    )


def _resolve_box(
    cue: OverlayCue,
    *,
    canvas_width: int,
    canvas_height: int,
    natural_width: int,
    natural_height: int,
) -> tuple[int, int, int, int, int, int]:
    """Resolve ``(width, height, x, y, scale_width, scale_height)``.

    ``(width, height)`` is the visible post-crop box that anchoring and
    clamping operate on; ``(scale_width, scale_height)`` is the scaled
    source size before a COVER crop. The requested box (or the natural size
    when no box is set) is first shrunk into the configured safe area, so an
    oversized overlay can never extend past it. For COVER the crop target is
    the shrunk box, which is why positioning uses post-crop dimensions.
    """
    anchor_x, anchor_y = _ANCHOR_FRACTIONS[cue.anchor.value]
    safe_x = round(cue.safe_area * canvas_width)
    safe_y = round(cue.safe_area * canvas_height)
    usable_w = max(1, canvas_width - 2 * safe_x)
    usable_h = max(1, canvas_height - 2 * safe_y)

    if cue.width is not None and cue.height is not None:
        box_w = max(1, min(round(cue.width), usable_w))
        box_h = max(1, min(round(cue.height), usable_h))
        scale_w, scale_h = _fit_size(
            natural_width, natural_height, box_w, box_h, cue.fit
        )
    else:
        # No explicit box: natural size, shrunk proportionally to fit.
        limit = min(1.0, usable_w / natural_width, usable_h / natural_height)
        box_w = max(1, round(natural_width * limit))
        box_h = max(1, round(natural_height * limit))
        scale_w, scale_h = box_w, box_h

    # Contain shows the whole source, so its visible box is the scaled size;
    # cover and stretch fill the (shrunk) box exactly.
    if cue.fit is OverlayFit.CONTAIN:
        visible_w, visible_h = scale_w, scale_h
    else:
        visible_w, visible_h = box_w, box_h

    reference_x = cue.x if cue.x is not None else safe_x + anchor_x * usable_w
    reference_y = cue.y if cue.y is not None else safe_y + anchor_y * usable_h
    left = reference_x - anchor_x * visible_w
    top = reference_y - anchor_y * visible_h

    max_left = max(safe_x, canvas_width - safe_x - visible_w)
    max_top = max(safe_y, canvas_height - safe_y - visible_h)
    left = min(max(left, safe_x), max_left)
    top = min(max(top, safe_y), max_top)
    return visible_w, visible_h, round(left), round(top), scale_w, scale_h


def resolve_placement(
    cue: OverlayCue,
    *,
    canvas_width: int,
    canvas_height: int,
    natural_width: int,
    natural_height: int,
) -> tuple[int, int, int, int]:
    """Resolve ``(width, height, x, y)`` for the overlay's visible box."""
    width, height, x, y, _, _ = _resolve_box(
        cue,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        natural_width=natural_width,
        natural_height=natural_height,
    )
    return width, height, x, y


def resolve_overlays(
    cues: Sequence[OverlayCue],
    asset_paths: dict[str, Path],
    *,
    canvas_width: int,
    canvas_height: int,
) -> list[ResolvedOverlay]:
    """Resolve every cue in paint order against its resolved asset path."""
    resolved: list[ResolvedOverlay] = []
    for position, cue in enumerate(sort_overlays(cues)):
        asset = asset_paths.get(cue.id)
        if asset is None or not Path(asset).is_file():
            raise OverlayAssetError(
                f"overlay {cue.id!r} ({cue.kind.value}) has no resolved asset file"
            )
        natural_w, natural_h = image_pixel_size(asset)
        width, height, x, y, scale_w, scale_h = _resolve_box(
            cue,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            natural_width=natural_w,
            natural_height=natural_h,
        )
        resolved.append(
            ResolvedOverlay(
                cue=cue,
                input_index=position,
                width=width,
                height=height,
                x=x,
                y=y,
                scale_width=scale_w,
                scale_height=scale_h,
            )
        )
    return resolved


def _overlay_source_chain(resolved: ResolvedOverlay) -> list[str]:
    cue = resolved.cue
    chain = [f"[{resolved.input_index}:v]format=rgba"]
    scale_w, scale_h = resolved.effective_scale
    chain.append(f"scale={scale_w}:{scale_h}")
    if cue.fit is OverlayFit.COVER and (scale_w, scale_h) != (resolved.width, resolved.height):
        chain.append(
            f"crop={resolved.width}:{resolved.height}:(iw-ow)/2:(ih-oh)/2"
        )
    if cue.fade_in_seconds > 0:
        chain.append(
            f"fade=t=in:st={cue.start_seconds:.6f}:d={cue.fade_in_seconds:.6f}:alpha=1"
        )
    if cue.fade_out_seconds > 0:
        fade_out_start = cue.end_seconds - cue.fade_out_seconds
        chain.append(
            f"fade=t=out:st={fade_out_start:.6f}:d={cue.fade_out_seconds:.6f}:alpha=1"
        )
    if cue.opacity < 1:
        chain.append(f"colorchannelmixer=aa={cue.opacity:.6f}")
    return chain


def append_overlay_filters(
    filters: list[str],
    resolved: Sequence[ResolvedOverlay],
    *,
    base_label: str,
) -> str:
    """Append ordered ``overlay`` filters to ``filters``; return the new label."""
    label = base_label
    for order, item in enumerate(resolved):
        cue = item.cue
        source_chain = _overlay_source_chain(item)
        filters.append(",".join(source_chain) + f"[ovl{order}]")
        # Half-open window [start, end): a frame sitting exactly on the end
        # boundary must already show the base layer again.
        start = cue.start_seconds
        end = cue.end_seconds
        enable = f"gte(t,{start:.6f})*lt(t,{end:.6f})"
        filters.append(
            f"[{label}][ovl{order}]overlay=x={item.x}:y={item.y}"
            f":eof_action=repeat:enable='{enable}'[olbl{order}]"
        )
        label = f"olbl{order}"
    return label


__all__ = [
    "OverlayAssetError",
    "ResolvedOverlay",
    "append_overlay_filters",
    "image_pixel_size",
    "resolve_overlays",
    "resolve_placement",
    "sort_overlays",
]
