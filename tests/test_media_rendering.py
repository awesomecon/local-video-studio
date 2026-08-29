import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.rendering.binaries import FFmpegBinaries, discover_binaries
from backend.rendering.commands import RenderOptions, build_finalize_command, build_video_command
from backend.rendering.mock_media import (
    create_placeholder_audio,
    create_placeholder_image,
    create_placeholder_video,
)
from backend.rendering.process import (
    MediaProcessError,
    get_active_media_pids,
    run_media_process,
)
from backend.rendering.probe import probe_media
from backend.rendering.qc import MediaQC, QCSeverity, check_subtitle_overflow
from backend.rendering.renderer import FFmpegRenderer
from backend.rendering.subtitles import write_ass, write_srt
from backend.timeline.builder import SceneTiming, build_timeline
from backend.timeline.models import AudioTrack, SubtitleCue, SubtitleWord, Timeline, TimelineClip


@pytest.fixture(scope="module")
def binaries():
    discovered = discover_binaries()
    if not discovered.available:
        pytest.skip("FFmpeg is unavailable")
    return discovered


def test_discovery_finds_system_or_bundled_ffmpeg(binaries) -> None:
    assert binaries.ffmpeg is not None
    assert binaries.source in {"override", "system", "imageio_ffmpeg"}


def test_subtitle_writers(tmp_path: Path) -> None:
    cues = [SubtitleCue(
        1.25,
        2.5,
        "A line\nwith braces {safe}",
        words=(
            SubtitleWord(1.25, 1.5, "A"),
            SubtitleWord(1.55, 1.85, "line"),
            SubtitleWord(1.9, 2.1, "with"),
            SubtitleWord(2.15, 2.35, "braces"),
            SubtitleWord(2.36, 2.5, "{safe}"),
        ),
    )]
    srt = write_srt(cues, tmp_path / "captions.srt")
    ass = write_ass(cues, tmp_path / "captions.ass", width=640, height=360)

    assert "00:00:01,250 --> 00:00:02,500" in srt.read_text(encoding="utf-8")
    ass_text = ass.read_text(encoding="utf-8")
    assert "PlayResX: 640" in ass_text
    assert "Style: Default,DejaVu Sans,19," in ass_text
    assert ass_text.count("Dialogue: 0,") == 9  # five spoken words plus four neutral gaps
    assert ass_text.count(r"{\c&H0000D7FF&\b1}") == 5
    assert r"\Nwith braces \{safe\}" in ass_text
    assert r"{\c&H0000D7FF&\b1}\{safe\}{\r}" in ass_text
    assert r"{\c&H0000D7FF&\b1}" not in srt.read_text(encoding="utf-8")


def test_burned_subtitles_are_also_embedded_as_a_track(tmp_path: Path, binaries) -> None:
    silent = tmp_path / "silent.mp4"
    silent.touch()
    captions = write_ass(
        [SubtitleCue(0.0, 1.0, "Visible captions")],
        tmp_path / "captions.ass",
        width=320,
        height=180,
    )
    timeline = Timeline(
        clips=[TimelineClip("scene", tmp_path / "clip.mp4", 0.0, 1.0)],
        width=320,
        height=180,
    )

    argv = build_finalize_command(
        silent,
        timeline,
        tmp_path / "out.mp4",
        RenderOptions(width=320, height=180, burn_subtitles=True, embed_subtitle_track=True),
        binaries,
        subtitle_path=captions,
    )

    assert str(captions) in argv
    assert "mov_text" in argv
    assert "subtitles=filename=" in argv[argv.index("-filter_complex") + 1]
    assert "copy" not in argv[argv.index("-c:v") + 1:]


def test_finalize_uses_balanced_voice_cleanup_music_ducking_and_limiter(
    tmp_path: Path, binaries,
) -> None:
    silent = tmp_path / "silent.mp4"
    narration = tmp_path / "narration.wav"
    music = tmp_path / "music.wav"
    for path in (silent, narration, music):
        path.touch()
    timeline = Timeline(
        clips=[TimelineClip("scene", tmp_path / "clip.mp4", 0.0, 1.0)],
        width=160,
        height=90,
        audio_tracks=[
            AudioTrack(path=narration, kind="narration"),
            AudioTrack(path=music, kind="music"),
        ],
    )

    argv = build_finalize_command(
        silent, timeline, tmp_path / "out.mp4", RenderOptions(), binaries,
    )
    filters = argv[argv.index("-filter_complex") + 1]

    assert "highpass=f=60,lowpass=f=15000,afftdn=nr=8:nf=-50:tn=1" in filters
    assert "loudnorm=I=-18:TP=-2:LRA=7,volume=0.00dB" in filters
    assert "volume=-12.00dB" in filters
    assert "sidechaincompress=threshold=0.08:ratio=2.5:attack=30:release=650:knee=4" in filters
    assert "alimiter=limit=0.891:attack=5:release=100:level=disabled:latency=true" in filters
    first_map = argv.index("-map", argv.index("-filter_complex"))
    assert argv[argv.index("-map", first_map + 1) + 1] == "[limitedaudio]"


def test_finalize_can_disable_narration_processing_and_output_limiter(
    tmp_path: Path, binaries,
) -> None:
    silent = tmp_path / "silent.mp4"
    narration = tmp_path / "narration.wav"
    silent.touch()
    narration.touch()
    timeline = Timeline(
        clips=[TimelineClip("scene", tmp_path / "clip.mp4", 0.0, 1.0)],
        width=160,
        height=90,
        audio_tracks=[AudioTrack(path=narration, kind="narration")],
    )

    argv = build_finalize_command(
        silent,
        timeline,
        tmp_path / "out.mp4",
        RenderOptions(clean_narration=False, normalize_narration=False, limit_audio=False),
        binaries,
    )
    filters = argv[argv.index("-filter_complex") + 1]

    assert "afftdn" not in filters
    assert "loudnorm" not in filters
    assert "alimiter" not in filters


def test_finalize_preserves_requested_fps_when_burning_subtitles(
    tmp_path: Path, binaries,
) -> None:
    silent = tmp_path / "silent.mp4"
    silent.touch()
    timeline = Timeline(
        clips=[TimelineClip("scene", tmp_path / "clip.mp4", 0.0, 1.0)],
        width=320,
        height=568,
        fps=12,
        subtitles=[SubtitleCue(0.0, 1.0, "Exact caption")],
    )

    argv = build_finalize_command(
        silent,
        timeline,
        tmp_path / "out.mp4",
        RenderOptions(burn_subtitles=True, fps=12),
        binaries,
        subtitle_path=tmp_path / "captions.ass",
    )

    assert argv[argv.index("-r") + 1] == "12"


def test_command_keeps_asset_path_as_single_argv_item(tmp_path: Path, binaries) -> None:
    asset = tmp_path / "scene; not-a-command.mp4"
    asset.touch()
    timeline = build_timeline([SceneTiming("scene", asset, 1.0)])

    argv = build_video_command(
        timeline,
        tmp_path / "out.mp4",
        RenderOptions(width=320, height=180),
        binaries,
    )

    assert str(asset) in argv
    assert argv.count(str(asset)) == 1
    assert ";" not in argv[: argv.index(str(asset))]


def test_video_command_clone_pads_short_sources(tmp_path: Path, binaries) -> None:
    asset = tmp_path / "short.mp4"
    asset.touch()
    timeline = build_timeline([SceneTiming("scene", asset, 1.25)])

    argv = build_video_command(
        timeline,
        tmp_path / "out.mp4",
        RenderOptions(width=320, height=180),
        binaries,
    )
    filters = argv[argv.index("-filter_complex") + 1]

    assert "tpad=stop_mode=clone:stop_duration=1.250000" in filters
    assert "trim=duration=1.250000" in filters
    assert "-stream_loop" not in argv


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("slow push in", "z='1+0.08*(0.5-0.5*cos(PI*on/119))'"),
        ("slow pull out", "z='1.08-0.08*(0.5-0.5*cos(PI*on/119))'"),
        ("pan left", "x='(iw-iw/zoom)*(1-(0.5-0.5*cos(PI*on/119)))'"),
        ("pan right", "x='(iw-iw/zoom)*(0.5-0.5*cos(PI*on/119))'"),
        ("drift up", "y='(ih-ih/zoom)*(1-(0.5-0.5*cos(PI*on/119)))'"),
        ("drift down", "y='(ih-ih/zoom)*(0.5-0.5*cos(PI*on/119))'"),
    ],
)
def test_image_motion_is_eased_and_directional(
    tmp_path: Path, binaries, instruction: str, expected: str,
) -> None:
    asset = tmp_path / "still.png"
    asset.touch()
    timeline = build_timeline([
        SceneTiming("scene", asset, 5.0, "image", camera_motion=instruction),
    ], fps=24)

    argv = build_video_command(timeline, tmp_path / "out.mp4", RenderOptions(), binaries)
    filters = argv[argv.index("-filter_complex") + 1]

    assert expected in filters
    assert ":d=1:" in filters
    assert "min(zoom+0.001" not in filters


def test_directional_image_motion_filter_renders(tmp_path: Path, binaries) -> None:
    image = create_placeholder_image(
        tmp_path / "still.png", width=160, height=90, seed=19, binaries=binaries,
    )
    timeline = build_timeline(
        [SceneTiming("scene", image, 0.4, "image", camera_motion="pan right")],
        width=160,
        height=90,
        fps=12,
    )
    output = tmp_path / "motion.mp4"

    info = FFmpegRenderer(binaries).render_preview(timeline, output)

    assert output.stat().st_size > 0
    assert info.has_video


def test_mock_media_and_complete_render(tmp_path: Path, binaries) -> None:
    image = create_placeholder_image(
        tmp_path / "reference.png", width=320, height=180, seed=7, binaries=binaries
    )
    video = create_placeholder_video(
        tmp_path / "clip.mp4",
        duration_seconds=0.8,
        width=320,
        height=180,
        fps=12,
        seed=8,
        binaries=binaries,
    )
    narration = create_placeholder_audio(
        tmp_path / "narration.wav", duration_seconds=1.5, binaries=binaries
    )
    music = create_placeholder_audio(
        tmp_path / "music.wav",
        duration_seconds=0.5,
        frequency_hz=220,
        volume=0.03,
        binaries=binaries,
    )
    timeline = build_timeline(
        [
            SceneTiming("one", image, 0.8, "image", camera_motion="push_in"),
            SceneTiming(
                "two",
                video,
                0.8,
                transition="crossfade",
                transition_duration_seconds=0.1,
            ),
        ],
        width=320,
        height=180,
        fps=12,
        narration_path=narration,
        music_path=music,
        subtitles=[
            SubtitleCue(0.0, 0.7, "Scene one"),
            SubtitleCue(0.7, 1.5, "Scene two"),
        ],
    )
    output = tmp_path / "preview.mp4"

    info = FFmpegRenderer(binaries).render_preview(timeline, output)

    assert output.stat().st_size > 1000
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (640, 360)
    assert info.duration_seconds == pytest.approx(1.5, abs=0.1)


def test_short_video_holds_last_frame_through_narration(tmp_path: Path, binaries) -> None:
    video = create_placeholder_video(
        tmp_path / "short.mp4",
        duration_seconds=0.5,
        width=320,
        height=180,
        fps=12,
        seed=9,
        binaries=binaries,
    )
    narration = create_placeholder_audio(
        tmp_path / "narration.wav", duration_seconds=1.25, binaries=binaries
    )
    timeline = build_timeline(
        [SceneTiming("video", video, 1.25)],
        width=320,
        height=180,
        fps=12,
        narration_path=narration,
    )
    output = tmp_path / "extended.mp4"
    renderer = FFmpegRenderer(binaries)

    info = renderer.render_preview(
        timeline,
        output,
        RenderOptions(width=320, height=180, fps=12),
    )
    final_frame = renderer.extract_frame(
        output,
        tmp_path / "near-end.png",
        timestamp_seconds=1.15,
        width=320,
        height=180,
    )

    assert info.has_video and info.has_audio
    assert info.duration_seconds == pytest.approx(1.25, abs=0.1)
    assert final_frame.stat().st_size > 0


def test_timeline_qc_accepts_source_video_duration_difference(tmp_path: Path, binaries) -> None:
    video = create_placeholder_video(
        tmp_path / "source.mp4",
        duration_seconds=0.5,
        width=160,
        height=90,
        binaries=binaries,
    )
    timeline = build_timeline([SceneTiming("video", video, 1.25)])

    report = MediaQC(binaries).check_timeline(timeline)

    assert "duration_mismatch" not in {issue.code for issue in report.issues}
    assert report.passed


def test_qc_reports_mismatch_and_subtitle_overflow(tmp_path: Path, binaries) -> None:
    video = create_placeholder_video(
        tmp_path / "tiny.mp4",
        duration_seconds=0.5,
        width=160,
        height=90,
        binaries=binaries,
    )
    report = MediaQC(binaries).check_file(
        video,
        expected_duration_seconds=2.0,
        expected_resolution=(320, 180),
        require_audio=True,
    )
    codes = {issue.code for issue in report.issues}

    assert not report.passed
    assert {"duration_mismatch", "resolution_mismatch", "missing_audio"} <= codes
    assert all(issue.severity == QCSeverity.ERROR for issue in report.issues)
    overflow = check_subtitle_overflow(
        [SubtitleCue(0.0, 1.0, "A subtitle line that is intentionally far too long to fit")]
    )
    assert overflow[0].code == "subtitle_overflow"


def test_ffmpeg_only_probe_detects_streams(tmp_path: Path, binaries) -> None:
    video = create_placeholder_video(
        tmp_path / "with-audio.mp4",
        duration_seconds=0.5,
        width=160,
        height=90,
        with_audio=True,
        binaries=binaries,
    )

    info = probe_media(video, binaries)

    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (160, 90)


def test_render_publishes_via_same_directory_staging(
    tmp_path: Path, binaries, monkeypatch
) -> None:
    real_replace = os.replace

    def reject_cross_device(src, dst) -> None:
        if Path(src).resolve().parent != Path(dst).resolve().parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(src, dst)

    monkeypatch.setattr("backend.rendering.renderer.os.replace", reject_cross_device)
    image = create_placeholder_image(
        tmp_path / "reference.png", width=160, height=90, seed=11, binaries=binaries
    )
    timeline = build_timeline(
        [SceneTiming("one", image, 0.4, "image")], width=160, height=90, fps=12
    )
    output = tmp_path / "elsewhere" / "final.mp4"

    info = FFmpegRenderer(binaries, temp_root=tmp_path / "workspace").render_preview(
        timeline, output
    )

    assert output.is_file() and output.stat().st_size > 0
    assert info.has_video


def test_extract_frame_past_end_raises_clear_error(tmp_path: Path, binaries) -> None:
    video = create_placeholder_video(
        tmp_path / "short.mp4",
        duration_seconds=0.5,
        width=160,
        height=90,
        fps=12,
        seed=3,
        binaries=binaries,
    )
    output = tmp_path / "thumbs" / "late.png"

    with pytest.raises(ValueError, match="beyond the end|produced no output"):
        FFmpegRenderer(binaries).extract_frame(
            video, output, timestamp_seconds=9999.0, width=160, height=90
        )

    assert not output.exists()
    assert list((tmp_path / "thumbs").iterdir()) == []


def test_qc_survives_probe_timeout(tmp_path: Path, binaries, monkeypatch) -> None:
    media = tmp_path / "stuck.mp4"
    media.write_bytes(b"\x00" * 32)

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=15)

    monkeypatch.setattr("backend.rendering.qc.probe_media", timed_out)

    report = MediaQC(binaries).check_file(media)

    assert {issue.code for issue in report.issues} == {"corrupt_media"}
    assert not report.passed


def test_finalize_command_delays_every_audio_channel(tmp_path: Path, binaries) -> None:
    silent = tmp_path / "silent.mp4"
    silent.touch()
    music = tmp_path / "music.wav"
    music.touch()
    timeline = Timeline(
        clips=[TimelineClip("scene", tmp_path / "clip.mp4", 0.0, 1.0)],
        width=160,
        height=90,
        audio_tracks=[AudioTrack(path=music, kind="music", start_seconds=0.5)],
    )

    argv = build_finalize_command(
        silent,
        timeline,
        tmp_path / "out.mp4",
        RenderOptions(width=160, height=90),
        binaries,
    )
    filters = argv[argv.index("-filter_complex") + 1]

    # all=1 delays every channel regardless of channel count.
    assert "adelay=delays=500:all=1" in filters


def test_run_media_process_unregisters_reaped_processes() -> None:
    run_media_process([sys.executable, "-c", "raise SystemExit(0)"], timeout=30)
    assert get_active_media_pids() == []

    with pytest.raises(MediaProcessError, match="timed out"):
        run_media_process(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5
        )
    assert get_active_media_pids() == []


@pytest.mark.parametrize(
    ("stream_duration", "expected"),
    [("0.0", 0.0), (None, None)],
)
def test_probe_fallback_stream_duration_is_preserved(
    tmp_path: Path, monkeypatch, stream_duration, expected
) -> None:
    stream = {"codec_type": "video", "width": 16, "height": 16}
    if stream_duration is not None:
        stream["duration"] = stream_duration
    payload = json.dumps({"streams": [stream], "format": {}})

    class _Result:
        returncode = 0
        stderr = ""
        stdout = payload

    media = tmp_path / "still.png"
    media.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr("backend.rendering.probe.subprocess.run", lambda *a, **k: _Result())
    fake_binaries = FFmpegBinaries(ffmpeg=None, ffprobe=Path("ffprobe"), source="override")

    info = probe_media(media, fake_binaries)

    assert info.duration_seconds == expected
