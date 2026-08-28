from __future__ import annotations

import json
from pathlib import Path

from backend.captions import (
    CaptionWord,
    build_caption_cues,
    restore_authored_punctuation,
)
from backend.core import load_config
from backend.models import BackendDescriptor, Capability, GenerationResult
from backend.pipeline import PipelineService
from backend.schemas import AssetType, ProjectCreate


def test_caption_cues_follow_word_timestamps_and_break_on_pause() -> None:
    words = [
        CaptionWord(0.10, 0.30, "Hello"),
        CaptionWord(0.32, 0.50, "there."),
        CaptionWord(1.20, 1.42, "A"),
        CaptionWord(1.45, 1.70, "new"),
        CaptionWord(1.72, 2.00, "thought."),
    ]

    cues = build_caption_cues(words, pause_break_seconds=0.5)

    assert [(cue.start_seconds, cue.end_seconds, cue.text) for cue in cues] == [
        (0.10, 0.50, "Hello there."),
        (1.20, 2.00, "A new thought."),
    ]
    assert [[word.text for word in cue.words] for cue in cues] == [
        ["Hello", "there."],
        ["A", "new", "thought."],
    ]


def test_caption_cues_wrap_at_two_readable_lines() -> None:
    words = [
        CaptionWord(index * 0.2, index * 0.2 + 0.1, text)
        for index, text in enumerate(("one", "two", "three", "four", "five"))
    ]

    cues = build_caption_cues(words, max_line_characters=8, max_lines=2, max_cue_seconds=10)

    assert [cue.text for cue in cues] == ["one two\nthree", "four\nfive"]


def test_caption_cues_break_at_sentence_boundaries_and_word_limit() -> None:
    words = [
        CaptionWord(index * 0.2, index * 0.2 + 0.1, text)
        for index, text in enumerate(("First", "thought.", "One", "two", "three", "four"))
    ]

    cues = build_caption_cues(
        words,
        max_line_characters=80,
        max_lines=2,
        max_cue_seconds=10,
        max_words=3,
    )

    assert [cue.text for cue in cues] == ["First thought.", "One two three", "four"]


def test_authored_punctuation_replaces_unpunctuated_alignment_text() -> None:
    words = [
        CaptionWord(index * 0.2, index * 0.2 + 0.1, text)
        for index, text in enumerate(("hello", "world", "how", "are", "you"))
    ]

    restored = restore_authored_punctuation(words, "Hello, world! \u201cHow are you?\u201d")

    assert [word.text for word in restored] == [
        "Hello,", "world!", "\u201cHow", "are", "you?\u201d",
    ]
    assert [(word.start_seconds, word.end_seconds) for word in restored] == [
        (word.start_seconds, word.end_seconds) for word in words
    ]


def test_authored_punctuation_survives_standalone_marks_and_word_mismatch() -> None:
    words = [
        CaptionWord(index * 0.2, index * 0.2 + 0.1, text)
        for index, text in enumerate(("wait", "well", "what", "really"))
    ]

    restored = restore_authored_punctuation(words, "Wait ... well, extra what \u2014 really?")

    assert [word.text for word in restored] == ["Wait ...", "well,", "what \u2014", "really?"]


def test_over_long_word_becomes_overflowed_line_instead_of_crashing() -> None:
    url = "https://example.com/" + "a" * 60
    words = [
        CaptionWord(0.00, 0.20, "Visit"),
        CaptionWord(0.22, 0.42, url),
        CaptionWord(0.44, 0.64, "today."),
    ]

    cues = build_caption_cues(words, max_line_characters=42, max_lines=2, max_cue_seconds=10)

    # The URL is kept (never dropped) on its own overflowed line; QC reports
    # subtitle overflow separately.
    assert cues[0].text.splitlines() == ["Visit", url]
    assert cues[1].text == "today."
    assert url in "\n".join(cue.text for cue in cues)


def test_single_over_long_word_is_emitted_as_its_own_cue() -> None:
    cjk_run = "长" * 50

    cues = build_caption_cues([CaptionWord(0.0, 0.5, cjk_run)], max_line_characters=42)

    assert [cue.text for cue in cues] == [cjk_run]


def test_completed_mock_subtitles_keep_deterministic_fallback(tmp_path: Path) -> None:
    config = load_config(environ={})
    service = PipelineService(
        config,
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )
    project = service.create_project(ProjectCreate(title="Captions", topic="test", target_duration=2))
    service.ensure_plan(project.id)
    cues = service._ensure_subtitles(project, force=False)

    root = service.store.project_path(project)
    assert cues
    assert not (root / "subtitles" / "word-timings.json").exists()
    outputs = json.loads((root / "stage-state.json").read_text(encoding="utf-8"))["stages"]["subtitles"][
        "outputs"
    ]
    assert outputs == ["subtitles/captions.srt", "subtitles/captions.ass"]


def test_real_alignment_uses_word_timings_and_records_audio_hash(tmp_path: Path) -> None:
    class FakeWhisper:
        def descriptor(self) -> BackendDescriptor:
            return BackendDescriptor(
                backend_name="whisper",
                model_name="Fake Whisper",
                model_version="test",
                device="cpu",
                capabilities=frozenset({Capability.SPEECH_TO_TEXT}),
            )

        def load(self) -> None:
            return None

        def unload(self) -> None:
            return None

        def generate(self, request) -> GenerationResult:
            assert request.references[0].name == "master.wav"
            output = request.output_dir / "word-timings.json"
            output.write_text(json.dumps({"words": [
                {"start_seconds": 0.1, "end_seconds": 0.3, "text": "hello"},
                {"start_seconds": 0.31, "end_seconds": 0.5, "text": "world"},
                {"start_seconds": 0.6, "end_seconds": 0.8, "text": "how"},
                {"start_seconds": 0.81, "end_seconds": 1.0, "text": "are"},
                {"start_seconds": 1.01, "end_seconds": 1.2, "text": "you"},
                {"start_seconds": 1.3, "end_seconds": 1.5, "text": "fine"},
                {"start_seconds": 1.51, "end_seconds": 1.7, "text": "thanks"},
            ]}), encoding="utf-8")
            return GenerationResult(
                outputs=(output,),
                metadata={
                    "backend": "whisper",
                    "model": "Fake Whisper",
                    "model_version": "test",
                    "seed": 0,
                    "settings": {"audio_derived": True},
                },
            )

    config = load_config(environ={})
    config.backends.whisper.enabled = True
    config.hardware.preferred_device = "cpu"
    service = PipelineService(
        config,
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )
    project = service.create_project(ProjectCreate(title="Real captions", topic="test", target_duration=2))
    service.ensure_plan(project.id)
    scenes = service.database.list_scenes(project.id)
    authored_lines = ("Hello, world.", "How are you?", "Fine, thanks!")
    assert len(scenes) == len(authored_lines)
    for scene, narration in zip(scenes, authored_lines, strict=True):
        service.update_scene(scene.id, {"narration": narration})
    service._ensure_narration(project, force=False)
    service.mock_mode = False
    service.registry.register(FakeWhisper(), name="whisper", replace=True)

    cues = service._ensure_subtitles(project, force=False)

    root = service.store.project_path(project)
    timing = json.loads((root / "subtitles" / "word-timings.json").read_text(encoding="utf-8"))
    assets = service.database.list_assets(project.id)
    caption_assets = [asset for asset in assets if asset.settings.get("role") == "captions"]
    assert [(cue.start_seconds, cue.end_seconds, cue.text) for cue in cues] == [
        (0.1, 0.5, "Hello, world."),
        (0.6, 1.2, "How are you?"),
        (1.3, 1.7, "Fine, thanks!"),
    ]
    assert [word["text"] for word in timing["words"]] == [
        "Hello,", "world.", "How", "are", "you?", "Fine,", "thanks!",
    ]
    assert timing["input_audio"] == "narration/master.wav"
    assert len(timing["input_audio_sha256"]) == 64
    assert timing["punctuation_source"] == "authored_scene_narration"
    assert all(asset.settings["audio_derived"] for asset in caption_assets)
    assert all(
        asset.settings["punctuation_workflow_version"] == "authored-punctuation-v1"
        for asset in caption_assets
    )
    assert any(asset.type is AssetType.METADATA and asset.settings.get("role") == "caption_timing" for asset in assets)
