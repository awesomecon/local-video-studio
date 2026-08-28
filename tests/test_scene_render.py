"""Multi-shot scene assembly: exact transitions, QC gating, atomic publish."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.rendering.mock_media import create_placeholder_video
from backend.rendering.scenes import (
    SceneAssembler,
    SceneInputError,
    SceneQCError,
    build_scene_command,
)
from backend.rendering.shots import NormalizationInputs, ShotNormalizer
from backend.timeline.shots import compile_scene_plan
from tests.multishot_fixtures import (
    CANVAS,
    FPS,
    assert_pixel_close,
    available_binaries,
    create_color_png,
    make_scene,
    make_shot,
    sample_pixel,
)

W, H = CANVAS
PROJECT = "proj"
SCENE = "scene-1"


@pytest.fixture(scope="module")
def binaries():
    return available_binaries()


def normalize_all(tmp_path: Path, binaries, shots, sources, durations):
    """Normalize every shot; return (plan, intermediates dict, shot_keys dict)."""
    normalizer = ShotNormalizer(binaries, tmp_path / "shot-cache")
    scene = make_scene(PROJECT, duration=sum(durations), scene_id=SCENE)
    plan = compile_scene_plan(scene, shots, fps=FPS)
    intermediates: dict[str, Path] = {}
    keys: dict[str, str] = {}
    for shot, duration, source in zip(shots, durations, sources, strict=True):
        result = normalizer.normalize(
            NormalizationInputs(
                shot=shot,
                source_path=source,
                canvas_width=W,
                canvas_height=H,
                fps=FPS,
            ),
            duration_seconds=duration,
        )
        intermediates[shot.id] = result.path
        keys[shot.id] = result.cache_key
    return plan, intermediates, keys


def image_shot(index: int, **overrides):
    return make_shot(PROJECT, SCENE, index, visual_type="flux_still", **overrides)


def test_cut_joins_exact_frame_counts(tmp_path: Path, binaries) -> None:
    shots = [image_shot(0), image_shot(1)]
    sources = [
        create_color_png(tmp_path / "a.png", color="red", binaries=binaries),
        create_color_png(tmp_path / "b.png", color="blue", binaries=binaries),
    ]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, sources, [1.0, 1.0],
    )

    output = tmp_path / "scenes" / "001" / "rendered.mp4"
    result = SceneAssembler(binaries).render(
        plan, intermediates, output, shot_keys=keys,
    )

    from backend.rendering.probe import count_video_frames, probe_media
    assert count_video_frames(output, binaries) == 24 == plan.total_frames
    info = probe_media(output, binaries)
    assert not info.has_audio and (info.width, info.height) == (W, H)
    assert_pixel_close(sample_pixel(output, 3, binaries=binaries), (255, 0, 0))
    assert_pixel_close(sample_pixel(output, 15, binaries=binaries), (0, 0, 255))
    assert result.manifest_path is not None and result.manifest_path.is_file()
    manifest = json.loads(result.manifest_path.read_text("utf-8"))
    assert manifest["total_frames"] == 24
    assert manifest["shots"][1]["cache_key"] == keys[shots[1].id]


def test_crossfade_overlap_produces_exact_frames_and_blend_midpoint(
    tmp_path: Path, binaries,
) -> None:
    shots = [
        image_shot(0),
        make_shot(PROJECT, SCENE, 1, transition_in={
            "kind": "crossfade", "duration_seconds": 4 / FPS,
        }),
    ]
    sources = [
        create_color_png(tmp_path / "red.png", color="red", binaries=binaries),
        create_color_png(tmp_path / "blue.png", color="blue", binaries=binaries),
    ]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, sources, [1.0, 1.0],
    )

    output = tmp_path / "crossfade.mp4"
    SceneAssembler(binaries).render(plan, intermediates, output, shot_keys=keys)

    from backend.rendering.probe import count_video_frames
    assert plan.total_frames == 12 + 12 - 4
    assert count_video_frames(output, binaries) == 20

    # Blend segment covers output frames 8..11 with a linear A→B ramp
    # (T/O ratios of 0, 0.25, 0.5, 0.75 across those four frames).
    early = sample_pixel(output, 9, binaries=binaries)
    late = sample_pixel(output, 11, binaries=binaries)
    assert early[0] > 150 and early[2] < 120  # mostly red, moving toward blue
    assert late[2] > 150 and late[0] < 120  # mostly blue, coming from red


def test_fade_through_black_hits_black_between_shots(
    tmp_path: Path, binaries,
) -> None:
    overlap = 6 / FPS  # fade out over 3 frames, fade in over 3 frames
    shots = [
        image_shot(0),
        make_shot(PROJECT, SCENE, 1, transition_in={
            "kind": "fade_through_black", "duration_seconds": overlap,
        }),
    ]
    sources = [
        create_color_png(tmp_path / "red.png", color="red", binaries=binaries),
        create_color_png(tmp_path / "green.png", color="green", binaries=binaries),
    ]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, sources, [1.0, 1.0],
    )

    assert plan.total_frames == 12 + 12 - 6
    output = tmp_path / "ftb.mp4"
    SceneAssembler(binaries).render(plan, intermediates, output, shot_keys=keys)

    from backend.rendering.probe import count_video_frames
    assert count_video_frames(output, binaries) == plan.total_frames

    def brightness(frame: int) -> int:
        pixel = sample_pixel(output, frame, binaries=binaries)
        return max(pixel)

    # Segment layout for counts 12/12 with a 6-frame overlap:
    # [0..5] shot A, [6..8] fade-out (1, .67, .33), [9..11] fade-in, [12..17] shot B.
    assert brightness(2) > 180  # shot A at full strength
    assert max(brightness(8), brightness(9)) < 110  # the black trough
    assert brightness(17) > 120  # shot B at full strength


def test_dip_to_white_peaks_white_between_shots(tmp_path: Path, binaries) -> None:
    shots = [
        image_shot(0),
        make_shot(PROJECT, SCENE, 1, transition_in={
            "kind": "dip_to_white", "duration_seconds": 6 / FPS,
        }),
    ]
    sources = [
        create_color_png(tmp_path / "black.png", color="black", binaries=binaries),
        create_color_png(tmp_path / "gray.png", color="0x404040", binaries=binaries),
    ]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, sources, [1.0, 1.0],
    )

    output = tmp_path / "dip.mp4"
    SceneAssembler(binaries).render(plan, intermediates, output, shot_keys=keys)

    # Segment layout for counts 12/12 with a 6-frame overlap: the white peak
    # sits at the boundary between the fade-out and fade-in halves (frames
    # 8/9); mid-shot frames stay dark.
    assert max(sample_pixel(output, 3, binaries=binaries)) < 80
    assert min(sample_pixel(output, 9, binaries=binaries)) > 190


def test_vertical_slice_image_video_title_card_with_two_transitions(
    tmp_path: Path, binaries,
) -> None:
    still = create_color_png(tmp_path / "open.png", color="0x203040", binaries=binaries)
    footage = create_placeholder_video(
        tmp_path / "archival.mp4", duration_seconds=1.5,
        width=W, height=H, fps=FPS, seed=42, binaries=binaries,
    )
    card = create_color_png(tmp_path / "card.png", color="white", binaries=binaries)

    shots = [
        make_shot(PROJECT, SCENE, 0, duration_seconds=1.0, camera_instruction="slow push in"),
        make_shot(
            PROJECT, SCENE, 1,
            duration_seconds=1.0,
            visual_type="reused_media",
            source_in_seconds=0.25,
            source_out_seconds=1.25,  # 1.0s of source
            transition_in={"kind": "crossfade", "duration_seconds": 0.25},
        ),
        make_shot(
            PROJECT, SCENE, 2,
            duration_seconds=0.75,
            visual_type="graphic_screen",
            transition_in={"kind": "dip_to_white", "duration_seconds": 0.25},
        ),
    ]
    durations = [1.0, 1.0, 0.75]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, [still, footage, card], durations,
    )

    output = tmp_path / "scene-slice.mp4"
    assembler = SceneAssembler(binaries, temp_root=tmp_path / "tmp")
    result = assembler.render(plan, intermediates, output, shot_keys=keys)

    from backend.rendering.probe import count_video_frames, probe_media
    expected_frames = sum(plan.frame_counts) - sum(b.overlap_frames for b in plan.boundaries)
    assert plan.total_frames == expected_frames == 27
    assert count_video_frames(output, binaries) == 27
    info = probe_media(output, binaries)
    assert info.duration_seconds == pytest.approx(27 / FPS, abs=2 / FPS)
    assert not info.has_audio
    assert result.duration_seconds == pytest.approx(27 / FPS)


def test_mismatched_intermediate_frame_count_is_rejected_before_rendering(
    tmp_path: Path, binaries,
) -> None:
    shots = [image_shot(0)]
    sources = [create_color_png(tmp_path / "solo.png", color="red", binaries=binaries)]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, sources, [1.0],
    )

    assembler = SceneAssembler(binaries)
    sentinel = tmp_path / "published.mp4"
    sentinel.write_bytes(b"previous-render")

    with pytest.raises(SceneInputError):
        assembler.render(
            plan, {"missing-shot": next(iter(intermediates.values()))}, sentinel,
            shot_keys=keys,
        )
    with pytest.raises(SceneInputError):
        assembler.render(plan, intermediates, sentinel, shot_keys={})

    assert sentinel.read_bytes() == b"previous-render"


def test_failed_qc_never_touches_the_published_render(
    tmp_path: Path, binaries, monkeypatch,
) -> None:
    shots = [image_shot(0)]
    sources = [create_color_png(tmp_path / "qc.png", color="red", binaries=binaries)]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, sources, [1.0],
    )

    output = tmp_path / "published" / "rendered.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"previous-good-render")
    manifest = output.parent / "rendered.manifest.json"
    manifest.write_text('{"previous": true}', encoding="utf-8")

    def exploding_quality_check(self, staged, plan, expected_size):
        raise SceneQCError("forced failure")

    monkeypatch.setattr(SceneAssembler, "_quality_check", exploding_quality_check)
    with pytest.raises(SceneQCError, match="forced failure"):
        SceneAssembler(binaries).render(
            plan, intermediates, output, shot_keys=keys,
            manifest_path=manifest,
        )

    assert output.read_bytes() == b"previous-good-render"
    assert json.loads(manifest.read_text("utf-8")) == {"previous": True}
    # No staging leftovers beside the published file.
    assert sorted(item.name for item in output.parent.iterdir()) == [
        "rendered.manifest.json", "rendered.mp4",
    ]

    monkeypatch.undo()
    result = SceneAssembler(binaries).render(
        plan, intermediates, output, shot_keys=keys, manifest_path=manifest,
    )
    assert result.total_frames == 12
    assert output.read_bytes() != b"previous-good-render"
    payload = json.loads(manifest.read_text("utf-8"))
    assert payload["total_frames"] == 12


def test_scene_command_is_bounded_per_scene(tmp_path: Path, binaries) -> None:
    shots = [image_shot(i) for i in range(6)]
    sources = [
        create_color_png(tmp_path / f"s{i}.png", color=f"0x{i:02x}0000", binaries=binaries)
        for i in range(6)
    ]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, sources, [0.5] * 6,
    )

    argv = build_scene_command(
        binaries,
        plan,
        [intermediates[shot.shot_id] for shot in plan.shots],
        tmp_path / "six.mp4",
    )

    filter_index = argv.index("-filter_complex")
    graph = argv[filter_index + 1]
    assert len(argv) < 200
    assert len(graph) < 10_000
    # One command per scene: only this scene's six inputs are referenced.
    assert argv.count("-i") == 6


def test_scene_cache_key_tracks_only_its_own_shot_keys(
    tmp_path: Path, binaries,
) -> None:
    shots = [image_shot(0), image_shot(1)]
    sources = [
        create_color_png(tmp_path / "k1.png", color="red", binaries=binaries),
        create_color_png(tmp_path / "k2.png", color="blue", binaries=binaries),
    ]
    normalizer = ShotNormalizer(binaries, tmp_path / "cache")
    scene = make_scene(PROJECT, duration=2.0, scene_id=SCENE)
    plan = compile_scene_plan(scene, shots, fps=FPS)

    def key_for(second_source: Path) -> tuple[str, str]:
        results = []
        for shot, source in zip(shots, [sources[0], second_source], strict=True):
            normalized = normalizer.normalize(
                NormalizationInputs(
                    shot=shot, source_path=source,
                    canvas_width=W, canvas_height=H, fps=FPS,
                ),
                duration_seconds=1.0,
            )
            results.append(normalized.cache_key)
        assembler = SceneAssembler(binaries)
        return (
            assembler.scene_cache_key(plan, {shots[0].id: results[0], shots[1].id: results[1]}),
            results[1],
        )

    original_scene_key, original_second_key = key_for(sources[1])
    changed_source = create_color_png(tmp_path / "k3.png", color="green", binaries=binaries)
    changed_scene_key, changed_second_key = key_for(changed_source)

    assert changed_second_key != original_second_key  # only this shot rebuilt
    assert changed_scene_key != original_scene_key  # so its scene must rebuild


# ---------------------------------------------------------------------------
# Second-level scene cache


@pytest.fixture()
def cached_scene(tmp_path: Path, binaries):
    """Two red/blue cut shots with a cache-backed assembler."""
    shots = [image_shot(0), image_shot(1)]
    sources = [
        create_color_png(tmp_path / "c1.png", color="red", binaries=binaries),
        create_color_png(tmp_path / "c2.png", color="blue", binaries=binaries),
    ]
    plan, intermediates, keys = normalize_all(
        tmp_path, binaries, shots, sources, [1.0, 1.0],
    )
    assembler = SceneAssembler(binaries, cache_root=tmp_path / "scene-cache")
    return assembler, plan, intermediates, keys, tmp_path / "scene-cache"


def _encode_calls(monkeypatch) -> list:
    import backend.rendering.scenes as scenes_module
    calls: list[list] = []
    real = scenes_module.run_media_process

    def counting(argv, timeout=30.0, job_id=None):
        calls.append(list(argv))
        return real(argv, timeout=timeout, job_id=job_id)

    monkeypatch.setattr(scenes_module, "run_media_process", counting)
    return calls


def test_unchanged_scene_reuses_validated_cache_artifact(
    tmp_path: Path, binaries, cached_scene, monkeypatch,
) -> None:
    assembler, plan, intermediates, keys, cache_root = cached_scene
    destination = tmp_path / "published" / "rendered.mp4"
    calls = _encode_calls(monkeypatch)

    first = assembler.render(plan, intermediates, destination, shot_keys=keys)
    second = assembler.render(plan, intermediates, destination, shot_keys=keys)

    assert not first.cache_hit and second.cache_hit
    assert len(calls) == 1  # the second render encoded nothing
    from backend.rendering.probe import count_video_frames
    assert count_video_frames(destination, binaries) == plan.total_frames
    scope_manifest = next((cache_root / "scenes" / SCENE).rglob("manifest.json"))
    import json
    payload = json.loads(scope_manifest.read_text("utf-8"))
    import hashlib
    media = next((cache_root / "scenes" / SCENE).rglob("rendered.mp4"))
    assert hashlib.sha256(media.read_bytes()).hexdigest() == payload["media_sha256"]


def test_corrupted_cached_render_is_rejected_and_rebuilt(
    tmp_path: Path, binaries, cached_scene,
) -> None:
    assembler, plan, intermediates, keys, cache_root = cached_scene
    destination = tmp_path / "published" / "rendered.mp4"
    assembler.render(plan, intermediates, destination, shot_keys=keys)

    cached_media = next((cache_root / "scenes" / SCENE).rglob("rendered.mp4"))
    with cached_media.open("ab") as stream:
        stream.write(b"corruption-bytes")

    result = assembler.render(plan, intermediates, destination, shot_keys=keys)

    assert not result.cache_hit  # SHA mismatch forced a fresh render
    from backend.rendering.probe import count_video_frames
    assert count_video_frames(destination, binaries) == plan.total_frames
    # The vouched artifact is whole again after the rebuild.
    import json
    payload = json.loads(cached_media.with_name("manifest.json").read_text("utf-8"))
    import hashlib
    assert hashlib.sha256(cached_media.read_bytes()).hexdigest() == payload["media_sha256"]


def test_stale_or_tampered_manifest_invalidates_the_cache(
    tmp_path: Path, binaries, cached_scene,
) -> None:
    assembler, plan, intermediates, keys, cache_root = cached_scene
    destination = tmp_path / "published" / "rendered.mp4"
    assembler.render(plan, intermediates, destination, shot_keys=keys)
    cached_manifest = next((cache_root / "scenes" / SCENE).rglob("manifest.json"))

    import json
    payload = json.loads(cached_manifest.read_text("utf-8"))
    payload["total_frames"] = 999  # manifest no longer describes the media
    cached_manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = assembler.render(plan, intermediates, destination, shot_keys=keys)

    assert not result.cache_hit
    rewritten = json.loads(cached_manifest.read_text("utf-8"))
    assert rewritten["total_frames"] == plan.total_frames


def test_crf_change_invalidates_the_scene_cache(
    tmp_path: Path, binaries, cached_scene,
) -> None:
    from backend.rendering.scenes import SceneEncodeOptions

    assembler, plan, intermediates, keys, cache_root = cached_scene
    destination = tmp_path / "published" / "rendered.mp4"

    first = assembler.render(plan, intermediates, destination, shot_keys=keys)
    again_same = assembler.render(plan, intermediates, destination, shot_keys=keys)
    different = assembler.render(
        plan, intermediates, destination, shot_keys=keys,
        options=SceneEncodeOptions(video_preset="ultrafast", crf=18),
    )

    assert not first.cache_hit and again_same.cache_hit and not different.cache_hit
    scopes = list((cache_root / "scenes" / SCENE).iterdir())
    assert len(scopes) == 2  # one cache entry per distinct encode setting


def test_wrong_resolution_scene_publication_is_rejected_and_retained(
    tmp_path: Path, binaries, cached_scene, monkeypatch,
) -> None:
    from backend.rendering.mock_media import create_placeholder_video
    from backend.rendering.scenes import SceneQCError
    import backend.rendering.scenes as scenes_module

    assembler, plan, intermediates, keys, cache_root = cached_scene
    destination = tmp_path / "published" / "rendered.mp4"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"previous-good-render")

    def wrong_resolution_run(argv, timeout=30.0, job_id=None):
        create_placeholder_video(
            Path(argv[-1]),
            duration_seconds=2.0,
            width=320,
            height=180,
            fps=FPS,
            seed=7,
            binaries=binaries,
        )

    monkeypatch.setattr(scenes_module, "run_media_process", wrong_resolution_run)

    with pytest.raises(SceneQCError, match=f"320x180.*expected {W}x{H}|expected {W}x{H}"):
        assembler.render(plan, intermediates, destination, shot_keys=keys)

    assert destination.read_bytes() == b"previous-good-render"
    # Nothing may be vouched into the cache for a rejected render.
    scene_scopes = cache_root / "scenes" / SCENE
    assert not scene_scopes.exists() or not any(scene_scopes.rglob("manifest.json"))
