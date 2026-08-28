"""Overlay geometry resolution and structured overlay composition tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.rendering.overlays import (
    OverlayAssetError,
    append_overlay_filters,
    resolve_overlays,
    resolve_placement,
    sort_overlays,
)
from backend.schemas.shots import OverlayAnchor, OverlayCue
from tests.multishot_fixtures import (
    CANVAS,
    FPS,
    assert_pixel_close,
    available_binaries,
    create_alpha_png,
    create_color_png,
    make_shot,
    sample_pixel,
)

W, H = CANVAS


def cue(**overrides) -> OverlayCue:
    payload = {
        "kind": "image",
        "asset_id": "asset-1",
        "start_seconds": 0.0,
        "duration_seconds": 1.0,
        "anchor": "center",
        "width": 40.0,
        "height": 30.0,
        "fit": "stretch",
    }
    payload.update(overrides)
    return OverlayCue.model_validate(payload)


def test_nine_anchors_place_the_anchor_point_on_canvas() -> None:
    cases = {
        OverlayAnchor.TOP_LEFT: (0.0, 0.0),
        OverlayAnchor.TOP_CENTER: (80.0, 0.0),
        OverlayAnchor.TOP_RIGHT: (160.0, 0.0),
        OverlayAnchor.CENTER_LEFT: (0.0, 45.0),
        OverlayAnchor.CENTER: (80.0, 45.0),
        OverlayAnchor.CENTER_RIGHT: (160.0, 45.0),
        OverlayAnchor.BOTTOM_LEFT: (0.0, 90.0),
        OverlayAnchor.BOTTOM_CENTER: (80.0, 90.0),
        OverlayAnchor.BOTTOM_RIGHT: (160.0, 90.0),
    }
    for anchor, (reference_x, reference_y) in cases.items():
        width, height, x, y = resolve_placement(
            cue(anchor=anchor.value),
            canvas_width=W,
            canvas_height=H,
            natural_width=40,
            natural_height=30,
        )
        assert (width, height) == (40, 30)
        # The anchor point of the box lands exactly on the reference point.
        horizontal = (
            0.0 if anchor.value.endswith("_left")
            else 1.0 if anchor.value.endswith("_right")
            else 0.5
        )
        vertical = (
            0.0 if anchor.value.startswith("top")
            else 1.0 if anchor.value.startswith("bottom")
            else 0.5
        )
        assert x + horizontal * width == pytest.approx(reference_x)
        assert y + vertical * height == pytest.approx(reference_y)


def test_safe_area_clamps_and_defaults_the_reference_point() -> None:
    width, height, x, y = resolve_placement(
        cue(anchor="bottom_right", safe_area=0.1, width=None, height=None),
        canvas_width=W,
        canvas_height=H,
        natural_width=40,
        natural_height=30,
    )
    # Default reference is the bottom-right corner of the safe area
    # (144, 81); the box is then clamped fully inside it.
    assert (x, y) == (104, 51)


def test_explicit_xy_override_and_contain_cover_stretch() -> None:
    base = dict(canvas_width=W, canvas_height=H)

    contain = resolve_placement(
        cue(x=10, y=10, fit="contain"),
        natural_width=100, natural_height=50, **base,
    )
    assert contain[:2] == (40, 20)  # contained inside the 40x30 box

    cover = resolve_placement(
        cue(x=10, y=10, fit="cover", anchor="top_left"),
        natural_width=100, natural_height=50, **base,
    )
    # Positioning uses the post-crop box (40x30); the pre-crop scale is 60x30.
    assert cover == (40, 30, 10, 10)

    stretch = resolve_placement(
        cue(x=10, y=10, fit="stretch", anchor="top_left"),
        natural_width=100, natural_height=50, **base,
    )
    assert stretch[:2] == (40, 30)
    # The explicit (x, y) is the box's top-left anchor point.
    assert stretch[2:] == (10, 10)

    centered_default = resolve_placement(
        cue(fit="stretch"), natural_width=40, natural_height=30, **base,
    )
    assert centered_default[2:] == (60, 30)  # canvas center without explicit x/y


def test_missing_overlay_asset_raises_structured_error(tmp_path: Path) -> None:
    shot = make_shot("p", "s", 0, overlays=[cue(id="missing-cue")])
    with pytest.raises(OverlayAssetError, match="missing-cue"):
        resolve_overlays(shot.overlays, {}, canvas_width=W, canvas_height=H)


def test_paint_order_is_z_then_start_then_id() -> None:
    first = cue(id="a", z_index=5, start_seconds=0.5)
    second = cue(id="b", z_index=5, start_seconds=0.25)
    third = cue(id="c", z_index=1)

    ordered = sort_overlays([first, second, third])

    assert [item.id for item in ordered] == ["c", "b", "a"]


def test_filter_chain_is_ordered_with_enable_windows_and_opacity(tmp_path: Path) -> None:
    binaries = available_binaries()
    lower = cue(id="lower", z_index=0, opacity=0.5, fit="stretch")
    upper = cue(
        id="upper", z_index=9, fade_in_seconds=0.25, fade_out_seconds=0.25,
        start_seconds=0.25, duration_seconds=0.5,
    )
    assets = {
        item.id: create_color_png(
            tmp_path / f"{item.id}.png", color="green", width=8, height=6,
            binaries=binaries,
        )
        for item in (lower, upper)
    }

    resolved = resolve_overlays([upper, lower], assets, canvas_width=W, canvas_height=H)
    filters: list[str] = []
    label = append_overlay_filters(filters, resolved, base_label="base0")
    graph = ";".join(filters)

    # Paint order follows z_index regardless of input order.
    assert resolved[0].cue.id == "lower" and resolved[1].cue.id == "upper"
    assert "colorchannelmixer=aa=0.500000" in graph
    assert "fade=t=in:st=0.250000:d=0.250000:alpha=1" in graph
    assert "fade=t=out:st=0.500000:d=0.250000:alpha=1" in graph
    # Half-open windows: a frame landing exactly on the end boundary is
    # already base layer again.
    assert "enable='gte(t,0.000000)*lt(t,1.000000)'" in graph
    assert "enable='gte(t,0.250000)*lt(t,0.750000)'" in graph
    assert "between(t," not in graph
    assert label == "olbl1"


def test_overlay_layer_order_and_windows_composite_correctly(tmp_path: Path) -> None:
    binaries = available_binaries()
    background = create_color_png(tmp_path / "bg.png", color="red", binaries=binaries)
    blue_box = create_color_png(
        tmp_path / "blue.png", color="blue", width=60, height=40, binaries=binaries
    )
    white_box = create_color_png(
        tmp_path / "white.png", color="white", width=20, height=10, binaries=binaries
    )

    from backend.rendering.shots import NormalizationInputs, ShotNormalizer

    normalizer = ShotNormalizer(binaries, tmp_path / "cache")

    def normalize(white_z: int, blue_z: int):
        shot = make_shot("p", "s", 0, overlays=[
            cue(
                id="blue", asset_id="b", z_index=blue_z, fit="stretch",
                start_seconds=0.25, duration_seconds=0.5,
                width=60.0, height=40.0,
            ),
            cue(
                id="white", asset_id="w", z_index=white_z, fit="stretch",
                start_seconds=0.25, duration_seconds=0.5,
                width=20.0, height=10.0,
            ),
        ])
        return normalizer.normalize(
            NormalizationInputs(
                shot=shot,
                source_path=background,
                overlay_paths={"blue": blue_box, "white": white_box},
                canvas_width=W,
                canvas_height=H,
                fps=FPS,
            ),
            duration_seconds=1.0,
        )

    top_white = normalize(white_z=2, blue_z=1)
    top_blue = normalize(white_z=1, blue_z=2)

    # White stacked above blue: center shows white; a point inside blue but
    # outside the smaller white box shows blue.
    assert_pixel_close(sample_pixel(top_white.path, 6, binaries=binaries), (255, 255, 255))
    inside_blue_only = sample_pixel(
        top_white.path, 6, x=W // 2 - 25, y=H // 2, binaries=binaries
    )
    assert_pixel_close(inside_blue_only, (0, 0, 255))

    # Enable window: before and after the window the background passes through.
    assert_pixel_close(sample_pixel(top_white.path, 0, binaries=binaries), (255, 0, 0))
    assert_pixel_close(sample_pixel(top_white.path, 11, binaries=binaries), (255, 0, 0))

    # Blue stacked above white flips the center pixel back to blue.
    assert_pixel_close(sample_pixel(top_blue.path, 6, binaries=binaries), (0, 0, 255))


def test_opacity_blends_instead_of_replacing(tmp_path: Path) -> None:
    binaries = available_binaries()
    background = create_color_png(tmp_path / "bg.png", color="red", binaries=binaries)
    magenta_box = create_color_png(
        tmp_path / "magenta.png", color="magenta", width=60, height=40, binaries=binaries
    )
    shot = make_shot("p", "s", 0, overlays=[
        cue(id="mag", asset_id="m", opacity=0.5, fit="stretch"),
    ])

    from backend.rendering.shots import NormalizationInputs, ShotNormalizer

    result = ShotNormalizer(binaries, tmp_path / "cache").normalize(
        NormalizationInputs(
            shot=shot,
            source_path=background,
            overlay_paths={"mag": magenta_box},
            canvas_width=W,
            canvas_height=H,
            fps=FPS,
        ),
        duration_seconds=0.5,
    )

    red, green, blue = sample_pixel(result.path, 3, binaries=binaries)
    # magenta is (255, 0, 255); half blended into red gives ~(255, 0, 128).
    assert abs(red - 255) < 25 and green < 30 and abs(blue - 128) < 30

def test_partial_alpha_preserved_through_rgba_pipeline(tmp_path: Path) -> None:
    binaries = available_binaries()
    background = create_color_png(tmp_path / "bg.png", color="black", binaries=binaries)
    half_alpha = create_alpha_png(
        tmp_path / "half.png", color="white", alpha=128, width=60, height=40,
        binaries=binaries,
    )
    shot = make_shot("p", "s", 0, overlays=[
        cue(id="half", asset_id="h", fit="stretch"),
    ])

    from backend.rendering.shots import NormalizationInputs, ShotNormalizer

    result = ShotNormalizer(binaries, tmp_path / "cache").normalize(
        NormalizationInputs(
            shot=shot,
            source_path=background,
            overlay_paths={"half": half_alpha},
            canvas_width=W,
            canvas_height=H,
            fps=FPS,
        ),
        duration_seconds=0.5,
    )

    value = sample_pixel(result.path, 2, binaries=binaries)[0]
    expected = round(255 * 128 / 255)
    assert abs(value - expected) < 25  # ~127, proving alpha was not flattened


def test_overlay_window_is_half_open_at_the_end_boundary(tmp_path: Path) -> None:
    binaries = available_binaries()
    background = create_color_png(tmp_path / "bg.png", color="red", binaries=binaries)
    full_canvas = create_color_png(
        tmp_path / "blue.png", color="blue", width=W, height=H, binaries=binaries
    )
    # Window [0.25, 0.5): at 12 fps frames 3..5 sit inside, frame 6 sits
    # exactly on t=0.5 and must already show the base layer.
    shot = make_shot("p", "s", 0, overlays=[
        cue(id="full", asset_id="f", fit="stretch",
            start_seconds=0.25, duration_seconds=0.25),
    ])

    from backend.rendering.shots import NormalizationInputs, ShotNormalizer

    result = ShotNormalizer(binaries, tmp_path / "cache").normalize(
        NormalizationInputs(
            shot=shot,
            source_path=background,
            overlay_paths={"full": full_canvas},
            canvas_width=W,
            canvas_height=H,
            fps=FPS,
        ),
        duration_seconds=0.75,
    )

    assert_pixel_close(sample_pixel(result.path, 3, binaries=binaries), (0, 0, 255))
    assert_pixel_close(sample_pixel(result.path, 5, binaries=binaries), (0, 0, 255))
    assert_pixel_close(sample_pixel(result.path, 6, binaries=binaries), (255, 0, 0))


def test_cover_placement_anchors_with_post_crop_box() -> None:
    width, height, x, y = resolve_placement(
        cue(anchor="top_right", fit="cover"),
        canvas_width=W,
        canvas_height=H,
        natural_width=100,
        natural_height=50,
    )
    # The 100x50 source cover-fills to 80x40 and crops down to the 40x30
    # box; anchoring happens on that final box, flush into the top-right.
    assert (width, height) == (40, 30)
    assert (x, y) == (W - 40, 0)


def test_oversized_overlay_is_scaled_inside_safe_area() -> None:
    width, height, x, y = resolve_placement(
        cue(width=None, height=None, safe_area=0.1),
        canvas_width=W,
        canvas_height=H,
        natural_width=300,
        natural_height=200,
    )
    # Usable area is 128x72 at a 10% inset; the 300x200 native overlay is
    # scaled proportionally down into it.
    assert width <= W - 2 * round(0.1 * W)
    assert height <= H - 2 * round(0.1 * H)
    assert (width, height) == (108, 72)
    assert x >= round(0.1 * W) and y >= round(0.1 * H)

    stretched = resolve_placement(
        cue(fit="stretch", width=400.0, height=100.0, safe_area=0.1),
        canvas_width=W,
        canvas_height=H,
        natural_width=400,
        natural_height=100,
    )
    assert stretched[:2] == (128, 72)
