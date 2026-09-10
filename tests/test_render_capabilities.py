from pathlib import Path

import pytest

from backend.rendering import capabilities


def test_subtitle_dependency_only_blocks_selected_feature(monkeypatch) -> None:
    monkeypatch.setattr(capabilities, "ffmpeg_features", lambda _: ({"libx264", "aac"}, set()))
    capabilities.require_render_features(Path("ffmpeg"), video_codec="libx264", audio_codec="aac")
    with pytest.raises(ValueError, match="subtitles filter"):
        capabilities.require_render_features(Path("ffmpeg"), video_codec="libx264",
                                              audio_codec="aac", burn_subtitles=True)


def test_missing_selected_encoder_has_actionable_error(monkeypatch) -> None:
    monkeypatch.setattr(capabilities, "ffmpeg_features", lambda _: ({"aac"}, set()))
    with pytest.raises(ValueError, match="libx264"):
        capabilities.require_render_features(Path("ffmpeg"), video_codec="libx264", audio_codec="aac")


def test_entry_parsing_survives_filter_flag_width_changes() -> None:
    old_style = (
        "Filters:\n"
        "  T.. = Timeline support\n"
        "  ------\n"
        " TSC subtitles         V->V       Render text subtitles onto input video using the libass library.\n"
    )
    # FFmpeg 9 prints two flag columns instead of three; the structure, not
    # the width, must identify entries (this silently dropped every filter).
    new_style = (
        "Filters:\n"
        "  T.. = Timeline support\n"
        "  ------\n"
        " .. subtitles         V->V       Render text subtitles onto input video using the libass library.\n"
    )
    assert capabilities._entry_names(old_style) == {"subtitles"}
    assert capabilities._entry_names(new_style) == {"subtitles"}
    encoders = "Encoders:\n  V..... = Video\n ------\n V..... libx264  libx264 H.264\n"
    assert capabilities._entry_names(encoders) == {"libx264"}
