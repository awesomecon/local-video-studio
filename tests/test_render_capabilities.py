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
