"""Shot normalization: exact intermediates, trim/pad policy, cache manifests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.rendering.mock_media import create_placeholder_video
from backend.rendering.shots import (
    NormalizationInputs,
    ShotNormalizationError,
    ShotNormalizer,
    build_normalization_command,
)
from tests.multishot_fixtures import (
    CANVAS,
    FPS,
    available_binaries,
    create_color_png,
    make_shot,
)

W, H = CANVAS


@pytest.fixture(scope="module")
def binaries():
    return available_binaries()


def image_inputs(
    tmp_path: Path,
    binaries,
    *,
    camera_instruction: str = "",
    canvas: tuple[int, int] = CANVAS,
    fps: int = FPS,
):
    source = create_color_png(tmp_path / "assets" / "still.png", color="red", binaries=binaries)
    shot = make_shot(
        "p", "s", 0, camera_instruction=camera_instruction, visual_type="flux_still",
    )
    return NormalizationInputs(
        shot=shot, source_path=source,
        canvas_width=canvas[0], canvas_height=canvas[1], fps=fps,
    )


def test_image_loops_to_exact_frame_count_and_silent_canvas(tmp_path: Path, binaries) -> None:
    normalizer = ShotNormalizer(binaries, tmp_path / "cache")

    first = normalizer.normalize(image_inputs(tmp_path, binaries), duration_seconds=1.0)
    second = normalizer.normalize(image_inputs(tmp_path, binaries), duration_seconds=1.0)

    assert not first.cache_hit and second.cache_hit
    assert second.path == first.path
    # A cache hit never rewrites the cached media.
    assert second.path.stat().st_mtime_ns == first.path.stat().st_mtime_ns
    from backend.rendering.probe import count_video_frames, probe_media
    info = probe_media(first.path, binaries)
    assert (info.width, info.height) == (W, H)
    assert not info.has_audio
    assert count_video_frames(first.path, binaries) == 12
    manifest = json.loads(first.path.with_name("manifest.json").read_text("utf-8"))
    assert manifest["cache_key"] == first.cache_key
    assert manifest["outcomes"]["actual_frames"] == 12


def test_camera_motion_uses_shared_eased_filter_path(tmp_path: Path, binaries) -> None:
    inputs = image_inputs(tmp_path, binaries, camera_instruction="pan right")
    destination = tmp_path / "out.mp4"

    argv, _ = build_normalization_command(binaries, inputs, destination=destination, frames=12)

    filters = argv[argv.index("-filter_complex") + 1]
    assert "zoompan=z='1.08'" in filters  # eased pan from the shared motion helper
    assert "x='(iw-iw/zoom)*" in filters
    # The looped still is trimmed to the shot duration and re-clocked to the
    # project fps after setpts so overlay windows evaluate on a known rate.
    assert "trim=duration=0.083333" not in filters
    assert "trim=duration=1.000000" in filters
    assert "settb=AVTB,setpts=PTS-STARTPTS,fps=12[base0]" in filters
    assert "-frames:v" in argv and argv[argv.index("-frames:v") + 1] == "12"
    assert "-an" in argv
    assert "-stream_loop" not in argv

    normalizer = ShotNormalizer(binaries, tmp_path / "cache")
    result = normalizer.normalize(inputs, duration_seconds=1.0)
    from backend.rendering.probe import count_video_frames
    assert count_video_frames(result.path, binaries) == 12


def test_video_trim_honors_source_in_out_seconds(tmp_path: Path, binaries) -> None:
    source = create_placeholder_video(
        tmp_path / "assets" / "clip.mp4", duration_seconds=1.0,
        width=W, height=H, fps=FPS, seed=5, binaries=binaries,
    )
    shot = make_shot(
        "p", "s", 0,
        visual_type="reused_media",
        source_in_seconds=0.25,
        source_out_seconds=0.75,
    )
    normalizer = ShotNormalizer(binaries, tmp_path / "cache")

    result = normalizer.normalize(
        NormalizationInputs(shot=shot, source_path=source, canvas_width=W, canvas_height=H, fps=FPS),
        duration_seconds=0.5,
    )

    from backend.rendering.probe import count_video_frames, probe_media
    assert count_video_frames(result.path, binaries) == 6
    info = probe_media(result.path, binaries)
    assert info.duration_seconds == pytest.approx(0.5, abs=2 / FPS)


def test_short_video_never_silently_loops_without_pad_policy(
    tmp_path: Path, binaries,
) -> None:
    source = create_placeholder_video(
        tmp_path / "assets" / "short.mp4", duration_seconds=0.5,
        width=W, height=H, fps=FPS, seed=6, binaries=binaries,
    )
    normalizer = ShotNormalizer(binaries, tmp_path / "cache")

    unpadded = normalizer.normalize(
        NormalizationInputs(
            shot=make_shot("p", "s", 0, visual_type="reused_media"),
            source_path=source, canvas_width=W, canvas_height=H, fps=FPS,
        ),
        duration_seconds=1.0,
    )

    # Ends at min(duration, available): 6 frames instead of 12, no looping.
    assert unpadded.actual_frames == 6
    assert unpadded.shortfall_frames == 6
    manifest = json.loads(unpadded.path.with_name("manifest.json").read_text("utf-8"))
    assert manifest["outcomes"]["shortfall_frames"] == 6

    padded = normalizer.normalize(
        NormalizationInputs(
            shot=make_shot(
                "p", "s", 0,
                visual_type="reused_media",
                settings={"pad_final_frame": True},
            ),
            source_path=source, canvas_width=W, canvas_height=H, fps=FPS,
        ),
        duration_seconds=1.0,
    )

    assert padded.actual_frames == 12
    assert padded.shortfall_frames == 0
    assert padded.cache_key != unpadded.cache_key


def test_pad_policy_never_applies_to_loopable_image_sources(
    tmp_path: Path, binaries,
) -> None:
    inputs = image_inputs(tmp_path, binaries)
    inputs = NormalizationInputs(
        shot=inputs.shot.model_copy(update={"settings": {"pad_final_frame": True}}),
        source_path=inputs.source_path,
        canvas_width=W, canvas_height=H, fps=FPS,
    )

    argv, _ = build_normalization_command(binaries, inputs, destination=tmp_path / "x.mp4", frames=12)

    filters = argv[argv.index("-filter_complex") + 1]
    assert "tpad" not in filters  # looping covers the duration; padding is video-only
    assert "-stream_loop" not in argv


def test_cache_key_covers_every_relevant_input_hash(tmp_path: Path, binaries) -> None:
    baseline = image_inputs(tmp_path, binaries)
    normalizer = ShotNormalizer(binaries, tmp_path / "cache")
    reference_key = normalizer.cache_key(baseline, frames=12)

    def rebuild(inputs: NormalizationInputs, **overrides) -> NormalizationInputs:
        fields = {
            "shot": inputs.shot,
            "source_path": inputs.source_path,
            "overlay_paths": dict(inputs.overlay_paths),
            "canvas_width": inputs.canvas_width,
            "canvas_height": inputs.canvas_height,
            "fps": inputs.fps,
        }
        fields.update(overrides)
        return NormalizationInputs(**fields)

    variants: dict[str, str] = {}

    other_source = create_color_png(tmp_path / "v-source.png", color="blue", binaries=binaries)
    variants["source_bytes"] = normalizer.cache_key(
        rebuild(baseline, source_path=other_source), frames=12,
    )
    variants["shot_settings"] = normalizer.cache_key(
        rebuild(baseline, shot=baseline.shot.model_copy(
            update={"settings": {"pad_final_frame": True}},
        )),
        frames=12,
    )
    variants["camera_motion"] = normalizer.cache_key(
        rebuild(baseline, shot=baseline.shot.model_copy(
            update={"camera_instruction": "pan left"},
        )),
        frames=12,
    )
    variants["fps"] = normalizer.cache_key(rebuild(baseline, fps=FPS * 2), frames=24)
    variants["canvas"] = normalizer.cache_key(
        rebuild(baseline, canvas_width=W * 2), frames=12,
    )
    variants["renderer_version"] = ShotNormalizer(
        binaries, tmp_path / "cache2", renderer_version="other-1",
    ).cache_key(baseline, frames=12)
    from backend.rendering.binaries import FFmpegBinaries

    variants["ffmpeg_identity"] = ShotNormalizer(
        FFmpegBinaries(ffmpeg=Path("/nonexistent/ffmpeg"), ffprobe=None, source="override"),
        tmp_path / "cache3",
    ).cache_key(baseline, frames=12)
    from backend.schemas.shots import OverlayCue

    overlay = OverlayCue.model_validate({
        "kind": "exact_text", "exact_text": "HELLO", "start_seconds": 0,
        "duration_seconds": 1,
    })
    variants["overlay_payload"] = normalizer.cache_key(
        rebuild(baseline, shot=baseline.shot.model_copy(update={"overlays": [overlay]})),
        frames=12,
    )

    assert len(set(variants.values())) == len(variants), variants
    for name, key in variants.items():
        assert key != reference_key, name


def test_changed_overlay_asset_hash_invalidates_only_that_shot(
    tmp_path: Path, binaries,
) -> None:
    from backend.schemas.shots import OverlayCue

    normalizer = ShotNormalizer(binaries, tmp_path / "cache")
    source = create_color_png(tmp_path / "assets" / "bg.png", color="red", binaries=binaries)
    cue = OverlayCue.model_validate({
        "id": "ov-cue",
        "kind": "image", "asset_id": "ov", "start_seconds": 0,
        "duration_seconds": 1, "width": 40, "height": 30, "fit": "stretch",
    })

    def normalized(overlay_file: Path):
        return normalizer.normalize(
            NormalizationInputs(
                shot=make_shot("p", "s", 0, overlays=[cue]),
                source_path=source,
                overlay_paths={cue.id: overlay_file},
                canvas_width=W, canvas_height=H, fps=FPS,
            ),
            duration_seconds=1.0,
        )

    overlay_png_a = create_color_png(
        tmp_path / "assets" / "overlay-a.png", color="green",
        width=40, height=30, binaries=binaries,
    )
    first = normalized(overlay_png_a)
    again = normalized(overlay_png_a)
    assert first.cache_key == again.cache_key and again.cache_hit

    overlay_png_b = create_color_png(
        tmp_path / "assets" / "overlay-b.png", color="yellow",
        width=40, height=30, binaries=binaries,
    )
    changed = normalized(overlay_png_b)
    assert changed.cache_key != first.cache_key
    assert not changed.cache_hit
    # The old composite remains on disk untouched.
    assert first.path.is_file()
    assert first.path != changed.path


def test_missing_source_file_fails_fast(tmp_path: Path, binaries) -> None:
    inputs = image_inputs(tmp_path, binaries)
    broken = NormalizationInputs(
        shot=inputs.shot,
        source_path=tmp_path / "does-not-exist.png",
        canvas_width=W, canvas_height=H, fps=FPS,
    )
    normalizer = ShotNormalizer(binaries, tmp_path / "cache")
    with pytest.raises(FileNotFoundError):
        normalizer.normalize(broken, duration_seconds=1.0)


def test_normalizer_requires_persistent_cache_root(tmp_path: Path, binaries) -> None:
    # Without a persistent cache the returned path would live inside a
    # TemporaryDirectory deleted before the caller can use it.
    with pytest.raises(ValueError, match="persistent cache_root"):
        ShotNormalizer(binaries)
    with pytest.raises(ValueError, match="persistent cache_root"):
        ShotNormalizer(binaries, None)


def test_wrong_resolution_output_is_rejected_and_never_cached(
    tmp_path: Path, binaries, monkeypatch,
) -> None:
    from backend.rendering.mock_media import create_placeholder_video
    import backend.rendering.shots as shots_module

    def wrong_resolution_run(argv, timeout=30.0, job_id=None):
        # Simulate a broken encoder run: a real but wrong-canvas MP4 lands
        # exactly where the command would have written.
        create_placeholder_video(
            Path(argv[-1]),
            duration_seconds=1.0,
            width=320,
            height=180,
            fps=FPS,
            seed=99,
            binaries=binaries,
        )

    monkeypatch.setattr(shots_module, "run_media_process", wrong_resolution_run)
    normalizer = ShotNormalizer(binaries, tmp_path / "cache")

    with pytest.raises(ShotNormalizationError, match=f"320x180.*expected {W}x{H}|expected {W}x{H}"):
        normalizer.normalize(image_inputs(tmp_path, binaries), duration_seconds=1.0)

    # Nothing may be published into the cache scope for a rejected output.
    scope_root = tmp_path / "cache" / "shots" / "shot-0"
    assert not scope_root.exists() or not any(scope_root.rglob("manifest.json"))


def test_fps_mismatch_output_is_rejected(tmp_path: Path, binaries, monkeypatch) -> None:
    from backend.rendering.mock_media import create_placeholder_video
    import backend.rendering.shots as shots_module

    def wrong_fps_run(argv, timeout=30.0, job_id=None):
        create_placeholder_video(
            Path(argv[-1]),
            duration_seconds=1.0,
            width=W,
            height=H,
            fps=24,  # project fps is 12
            seed=98,
            binaries=binaries,
        )

    monkeypatch.setattr(shots_module, "run_media_process", wrong_fps_run)
    normalizer = ShotNormalizer(binaries, tmp_path / "cache")

    with pytest.raises(ShotNormalizationError, match="runs at 24.0 fps; expected 12"):
        normalizer.normalize(image_inputs(tmp_path, binaries), duration_seconds=1.0)
