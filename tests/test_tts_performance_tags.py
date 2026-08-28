"""Fish S2 Pro delivery tags: validation, cue-aware chunking, LLM tagging,
manager integration, and the API surface.

No test touches the network: the local LLM is a fake object and the TTS
backends are in-memory recorders.
"""

from __future__ import annotations

import json
import re
import wave
import io
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core import load_config
from backend.models import (
    BackendDescriptor,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
)
from backend.models.errors import BackendError, BackendErrorCode
from backend.schemas import ProjectCreate, Scene, VisualType
from backend.tts.chunking import chunk_narration, chunk_narration_tagged
from backend.tts.models import NarrationRequest
from backend.tts.performance import (
    NoNarrationTextError,
    PerformanceScript,
    PerformanceSegment,
    count_spoken_words,
    count_tags,
    cue_ceiling,
    normalize_tagged_layout,
    strip_performance_tags,
    validate_tagged,
)
from backend.tts.performance_llm import PerformanceTagger


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def wav_bytes(*, frames: int = 800, sample_rate: int = 8000, sample: int = 0) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(sample.to_bytes(2, byteorder="little", signed=True) * frames)
    return target.getvalue()


class PromptRecordingBackend(GeneratorBackend):
    """In-memory TTS backend that records the exact prompt of every chunk."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name="fish_s2_pro", model_name="Fake S2 Pro", model_version="test",
        )

    def health(self) -> dict:
        return {"status": "healthy"}

    def load(self) -> None:
        pass

    def unload(self) -> None:
        pass

    def cancel(self, job_id: str) -> bool:
        return False

    def estimate_resources(self, request: GenerationRequest) -> dict:
        return {}

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.prompts.append(request.prompt)
        output = request.output_dir / str(request.settings["filename"])
        output.write_bytes(wav_bytes(sample=1000))
        return GenerationResult(outputs=(output,), metadata={"backend": "fish_s2_pro"})


class FakeLLM:
    """Stands in for LocalLLMBackend; the responder decides each completion."""

    def __init__(self, responder: Any) -> None:
        self.model = "fake-local-model"
        self.complete_calls: list[dict[str, Any]] = []
        self._responder = responder

    def selected_model(self, *, model: str | None = None) -> str:
        return model or self.model

    def complete(
        self,
        *,
        messages: Any,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        structured: bool = False,
        json_schema: Any = None,
        validator: Any = None,
        model: str | None = None,
        thinking_budget_tokens: int | None = None,
    ) -> Any:
        self.complete_calls.append({
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "structured": structured,
            "json_schema": json_schema,
            "model": model,
            "thinking_budget_tokens": thinking_budget_tokens,
        })
        return self._responder(messages, self.complete_calls)


def narration_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    service = app.state.service
    monkeypatch.setattr(service.tts_workers, "ensure_running", lambda provider: False)
    monkeypatch.setattr(service.tts_workers, "stop", lambda provider: False)
    return service


def _add_scenes(service: Any, project: Any, narrations: list[str]) -> list[Scene]:
    scenes = [
        Scene(
            project_id=project.id, index=index, title=f"Scene {index}",
            duration=1, narration=text, visual_type=VisualType.FLUX_STILL,
        )
        for index, text in enumerate(narrations)
    ]
    for scene in scenes:
        service.database.save_scene(scene)
        service.store.save_scene(project.slug, scene)
    return scenes


def _enable_fake_llm(service: Any, monkeypatch: pytest.MonkeyPatch, responder: Any) -> FakeLLM:
    """Point the director at a fake LLM and lift the mock-mode guard."""
    fake = FakeLLM(responder)
    monkeypatch.setattr(service.director, "llm", fake)
    monkeypatch.setattr(service, "mock_mode", False)
    return fake


def _tagged_responder(tagged_by_source: dict[str, str]) -> Any:
    """Responder that tags each segment from the prompt's segment sources."""
    header = re.compile(r"^Segment (\d+) \(")

    def responder(messages: Any, calls: list[dict[str, Any]]) -> dict[str, Any]:
        user = messages[-1]["content"]
        segments: list[dict[str, str]] = []
        index = 0
        for line in user.splitlines():
            match = header.match(line.strip())
            if match:
                index = int(match.group(1))
            elif line.strip() and not line.startswith(("Narration", "Tag every")):
                source = line.strip()
                tagged = tagged_by_source.get(source)
                if tagged is not None:
                    segments.append({"index": index, "tagged": tagged})
        return {"segments": segments}
    return responder


# ---------------------------------------------------------------------------
# Pure logic: strip / normalize / counts
# ---------------------------------------------------------------------------

def test_strip_performance_tags_removes_cues_and_collapses_whitespace() -> None:
    text = "[calm narration] I thought\neverything was normal. [pause] It really was."
    assert strip_performance_tags(text) == (
        "I thought everything was normal. It really was."
    )
    assert strip_performance_tags("no cues here") == "no cues here"
    assert strip_performance_tags("   ") == ""


def test_normalize_tagged_layout_glues_cue_only_lines() -> None:
    text = "[calm narration]\nI thought everything was normal.\n\n[pause]\nIt really was."
    # The real paragraph break (blank line) is preserved; only the cue-only
    # lines are glued to the sentence they direct.
    assert normalize_tagged_layout(text) == (
        "[calm narration] I thought everything was normal.\n\n"
        "[pause] It really was."
    )
    # A trailing cue-only line has nothing to attach to and is dropped.
    assert normalize_tagged_layout("Hello there.\n[pause]") == "Hello there."
    # Untagged text is unchanged, including its paragraph breaks.
    assert normalize_tagged_layout("One line.\n\nTwo lines.") == "One line.\n\nTwo lines."


def test_count_spoken_words_excludes_cues() -> None:
    assert count_spoken_words("[emphasis] This is really important.") == 4
    assert count_spoken_words("plain words here") == 3
    assert count_spoken_words("[only a cue]") == 0


# ---------------------------------------------------------------------------
# validate_tagged: reject, don't trust
# ---------------------------------------------------------------------------

def test_validate_tagged_accepts_valid_cues() -> None:
    source = "I thought everything was normal. It really was."
    tagged = "[calm, conversational narration] I thought everything was normal. [pause] It really was."
    assert validate_tagged(source, tagged) == []


def test_validate_tagged_allows_light_punctuation_drift() -> None:
    source = "I thought everything was normal."
    tagged = "[soft tone] I thought everything was normal!"
    assert validate_tagged(source, tagged) == []


def test_validate_tagged_rejects_added_deleted_or_reordered_words() -> None:
    source = "The cat sat on the mat."
    assert any("added or deleted" in e for e in validate_tagged(source, "[calm] The cat sat on the mat.")) is False
    assert validate_tagged(source, "[calm] The cat sat on the mat.") == []
    errors = validate_tagged(source, "[calm] The cat sat on the mat. Extra.")
    assert any("added or deleted" in e for e in errors)
    errors = validate_tagged(source, "[calm] The cat sat on mat.")
    assert any("added or deleted" in e for e in errors)
    errors = validate_tagged(source, "[calm] The mat sat on the cat.")
    assert any("changed or reordered" in e for e in errors)


def test_validate_tagged_rejects_bad_bracket_structure() -> None:
    source = "The cat sat on the mat."
    assert any("nested" in e for e in validate_tagged(source, "[a [b]] The cat sat on the mat."))
    assert any("unbalanced" in e for e in validate_tagged(source, "[a The cat sat on the mat."))
    assert any("unbalanced" in e for e in validate_tagged(source, "a] The cat sat on the mat."))
    assert any("empty" in e for e in validate_tagged(source, "[ ] The cat sat on the mat."))
    assert any("multi-line" in e for e in validate_tagged(
        source, "[calm\nand quiet] The cat sat on the mat.",
    ))


def test_validate_tagged_allows_combined_open_domain_tags() -> None:
    # A short line earns the floor of 3 cues; open-domain combined cues are
    # fine while they stay under the ceiling.
    source = "One. Two. Three. Four. Five."
    tagged = "[a] One. [b] Two. [c] Three. Four. Five."
    assert validate_tagged(source, tagged) == []
    # A longer line earns a higher ceiling, so a run of combined cues is fine.
    long_source = " ".join(f"word{i}" for i in range(90))  # 90 words -> ceiling 6
    long_tagged = (
        "[panting] [tired] [trying to stay calm] [low voice] " + long_source
    )
    assert validate_tagged(long_source, long_tagged) == []


def test_cue_ceiling_scales_with_length() -> None:
    # The floor of 3 applies to short lines...
    assert cue_ceiling("One two three") == 3
    assert cue_ceiling(" ".join("word" for _ in range(30))) == 3
    # ...and longer narration earns more cues.
    assert cue_ceiling(" ".join("word" for _ in range(60))) == 4
    assert cue_ceiling(" ".join("word" for _ in range(90))) == 6


def test_validate_tagged_rejects_over_tagging_beyond_the_ceiling() -> None:
    # A short line's ceiling is the floor of 3; a 4th cue is over-tagging.
    source = "One. Two. Three. Four. Five."
    tagged = "[a] One. [b] Two. [c] Three. [d] Four. Five."
    errors = validate_tagged(source, tagged)
    assert any("ceiling of 3" in e for e in errors)
    # The same cue count is fine on a longer line that earns a higher ceiling.
    long_source = " ".join(f"word{i}" for i in range(90))  # ceiling 6
    long_tagged = "[a] " + long_source + " [b] [c] [d] [e] [f]"
    assert validate_tagged(long_source, long_tagged) == []


def test_validate_tagged_allows_official_free_form_cues_but_rejects_spoken_caps() -> None:
    source = "The cat sat on the mat."
    assert validate_tagged(
        source,
        "[the calm, measured tone of someone who has done this a thousand times] "
        + source,
    ) == []
    assert validate_tagged(source, "[NARRATOR, low and slow] " + source) == []
    # A new ALL-CAPS run (>= 4 letters) not present in the source is rejected;
    # the spoken word is unchanged so the word-sequence check still passes.
    errors = validate_tagged(
        "The satellite sat on the mat.",
        "[calm] The SATELLITE sat on the mat.",
    )
    assert any("ALL-CAPS" in e for e in errors)
    # Caps already present in the source are fine.
    assert validate_tagged("NASA launched the probe.", "[calm] NASA launched the probe.") == []


def test_validate_tagged_rejects_empty_and_over_tagged_text() -> None:
    source = "The cat sat on the mat."
    assert validate_tagged(source, "   ") == ["tagged text is empty"]


def test_validate_tagged_preserves_bracketed_text_from_clean_source() -> None:
    source = "Read [section two] aloud."
    tagged = "[calm] Read [section two] aloud."
    assert validate_tagged(source, source) == []
    assert validate_tagged(source, tagged) == []
    script = PerformanceScript(segments=[
        PerformanceSegment(key="override", source=source, tagged=tagged),
    ])
    assert script.tag_count == 1


# ---------------------------------------------------------------------------
# Cue-aware chunking
# ---------------------------------------------------------------------------

def test_tagged_chunking_glues_cue_lines_and_keeps_untagged_unchanged() -> None:
    tagged = "[calm narration]\nI thought everything was normal. It really was."
    chunks = chunk_narration_tagged(tagged, 30)
    assert chunks == ["[calm narration] I thought everything was normal. It really was."]

    # Untagged input behaves exactly as before.
    text = "First short paragraph.\n\nSecond short paragraph.\n\n" + "word " * 40
    assert chunk_narration_tagged(text, 5, words_per_second=2) == chunk_narration(
        text, 5, words_per_second=2,
    )


def test_tagged_chunking_never_emits_a_cue_only_chunk() -> None:
    text = "First line here.\n\n[pause]\n\nSecond line here."
    chunks = chunk_narration_tagged(text, 30)
    assert all(count_spoken_words(chunk) > 0 for chunk in chunks)
    assert "[pause]" in chunks[0]
    assert " ".join(chunks).count("word") == 0  # no words lost or invented


def test_tagged_chunking_keeps_cue_glued_across_hard_splits() -> None:
    long = "word " * 30 + "[emphasis] " + "word " * 30
    chunks = chunk_narration_tagged(long, 5, words_per_second=2)
    assert all(count_spoken_words(chunk) <= 10 for chunk in chunks)
    # The cue travels with the word it annotates; no chunk is cue-only.
    assert all(count_spoken_words(chunk) > 0 for chunk in chunks)
    assert sum(count_tags(chunk) for chunk in chunks) == 1
    joined = " ".join(chunks)
    assert joined.count("word") == 60


def test_tagged_hard_split_attaches_boundary_cue_to_following_word() -> None:
    text = " ".join(["word"] * 10 + ["[emphasis]", "NEXT", "tail"])
    chunks = chunk_narration_tagged(text, 5, words_per_second=2)
    assert chunks == [" ".join(["word"] * 10), "[emphasis] NEXT tail"]


def test_clean_chunking_preserves_original_bracket_word_boundaries() -> None:
    text = "Alpha beta gamma delta epsilon zeta eta theta iota [stage direction] kappa lambda."
    assert chunk_narration(text, 5, words_per_second=2) == [
        "Alpha beta gamma delta epsilon zeta eta theta iota [stage",
        "direction] kappa lambda.",
    ]


def test_tagged_chunking_sizing_ignores_cue_words() -> None:
    # 9 spoken words + 3 cues: sized by the 9 spoken words only, so it fits
    # the 10-word target in a single chunk even though the raw token count is
    # much higher.
    text = "[a] " + " ".join(f"w{i}" for i in range(8)) + " [b] end [c]"
    chunks = chunk_narration_tagged(text, 5, words_per_second=2)
    assert len(chunks) == 1
    assert count_spoken_words(chunks[0]) == 9
    assert count_tags(chunks[0]) == 3


# ---------------------------------------------------------------------------
# PerformanceTagger with a fake LLM
# ---------------------------------------------------------------------------

def _segments(sources: list[str]) -> list[PerformanceSegment]:
    return [
        PerformanceSegment(
            key=f"scene:s{i}", source=source, tagged=source,
            scene_id=f"s{i}", scene_index=i, scene_title=f"Scene {i}",
        )
        for i, source in enumerate(sources)
    ]


def test_tagger_tags_segments_via_fake_llm() -> None:
    sources = [
        "I thought everything was normal.",
        "Then the lights went out.",
    ]
    fake = FakeLLM(_tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
        "Then the lights went out.": "[shouting] Then the lights went out.",
    }))
    script, warnings = PerformanceTagger(fake).tag(
        _segments(sources), intensity="balanced", model="fake-local-model",
    )
    assert warnings == []
    assert script.model == "fake-local-model"
    assert script.intensity == "balanced"
    assert [s.tagged for s in script.segments] == [
        "[calm] I thought everything was normal.",
        "[shouting] Then the lights went out.",
    ]
    assert [s.source for s in script.segments] == sources
    assert script.tag_count == 2
    call = fake.complete_calls[0]
    assert call["structured"] is True
    assert call["json_schema"] is not None
    assert call["thinking_budget_tokens"] == 3000
    assert call["temperature"] == 0.4
    assert call["model"] == "fake-local-model"
    system = call["messages"][0]["content"]
    assert "Fish Audio S2 Pro" in system
    assert "balanced" in system
    for documented_tag in (
        "[sigh]", "[inhale]", "[exhale]", "[gasp]", "[panting]",
        "[clears throat]", "[laughing]", "[chuckling]", "[giggle]",
        "[sobbing]", "[crying]", "[groan]", "[pause]", "[short pause]",
        "[long pause]", "[whispering]", "[soft voice]", "[loud voice]",
        "[shouting]", "[low voice]", "[excited]", "[angry]", "[sad]",
        "[surprised]", "[emphasis]", "[rustling sound]",
    ):
        assert documented_tag in system
    assert "[break]" not in system
    assert "[long-break]" not in system
    assert "open-domain" in system
    assert "narration language" in system


def test_tagger_includes_video_context_in_system_prompt() -> None:
    fake = FakeLLM(_tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    PerformanceTagger(fake).tag(
        _segments(["I thought everything was normal."]),
        context="Title: The Haunted House\nAbout: a true-crime story",
    )
    system = fake.complete_calls[0]["messages"][0]["content"]
    assert "About this video:" in system
    assert "The Haunted House" in system
    assert "true-crime story" in system


def test_tagger_omits_context_block_when_empty() -> None:
    fake = FakeLLM(_tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    PerformanceTagger(fake).tag(_segments(["I thought everything was normal."]))
    system = fake.complete_calls[0]["messages"][0]["content"]
    assert "About this video:" not in system


def test_tagger_degrades_to_source_when_output_is_invalid() -> None:
    sources = ["I thought everything was normal."]
    # The model rewrites the script: the validator must reject it and the
    # repair attempt (same bad responder) must degrade to the clean source.
    fake = FakeLLM(lambda messages, calls: {
        "segments": [{"index": 0, "tagged": "[calm] I thought everything was fine."}],
    })
    script, warnings = PerformanceTagger(fake).tag(_segments(sources))
    assert script.segments[0].tagged == sources[0]
    assert len(warnings) == 1
    assert "rejected" in warnings[0]
    # One initial completion plus one repair completion.
    assert len(fake.complete_calls) == 2


def test_tagger_repairs_a_failed_segment_individually() -> None:
    sources = [
        "I thought everything was normal.",
        "Then the lights went out.",
    ]

    def responder(messages, calls):
        user = messages[-1]["content"]
        if "Re-tag the single segment" in user:
            # Repair call for the one failed segment: return the corrected text.
            return {"segments": [
                {"index": 0, "tagged": "[shouting] Then the lights went out."},
            ]}
        # Initial batch call: segment 0 is fine, segment 1 rewrites a word.
        return {"segments": [
            {"index": 0, "tagged": "[calm] I thought everything was normal."},
            {"index": 1, "tagged": "[shouting] Then the lights went fine."},
        ]}

    fake = FakeLLM(responder)
    script, warnings = PerformanceTagger(fake).tag(_segments(sources))
    assert warnings == []
    assert script.segments[0].tagged == "[calm] I thought everything was normal."
    assert script.segments[1].tagged == "[shouting] Then the lights went out."
    # One initial batch call plus one per-segment repair call.
    assert len(fake.complete_calls) == 2
    repair_user = fake.complete_calls[1]["messages"][-1]["content"]
    # The repair tells the LLM exactly what failed last time...
    assert "failed validation" in repair_user
    assert "changed or reordered" in repair_user
    # ...including the previous (bad) attempt...
    assert "Then the lights went fine." in repair_user
    # ...and the clean source it must preserve.
    assert "Then the lights went out." in repair_user
    # Only the failed segment is re-sent, not the whole batch.
    assert "I thought everything was normal." not in repair_user


def test_tagger_repairs_a_segment_with_empty_output() -> None:
    sources = [
        "I thought everything was normal.",
        "Then the lights went out.",
    ]

    def responder(messages, calls):
        user = messages[-1]["content"]
        if "Re-tag the single segment" in user:
            return {"segments": [
                {"index": 0, "tagged": "[shouting] Then the lights went out."},
            ]}
        # Initial call returns segment 1 with an empty tagged field.
        return {"segments": [
            {"index": 0, "tagged": "[calm] I thought everything was normal."},
            {"index": 1, "tagged": ""},
        ]}

    fake = FakeLLM(responder)
    script, warnings = PerformanceTagger(fake).tag(_segments(sources))
    assert warnings == []
    assert script.segments[0].tagged == "[calm] I thought everything was normal."
    assert script.segments[1].tagged == "[shouting] Then the lights went out."
    repair_user = fake.complete_calls[1]["messages"][-1]["content"]
    assert "tagged text is empty" in repair_user


def test_tagger_repairs_over_tagged_segment_down_to_the_ceiling() -> None:
    # A short line earns a ceiling of 3; the model over-tags it with 4 cues.
    source = "One. Two. Three. Four. Five."
    sources = [source]

    def responder(messages, calls):
        user = messages[-1]["content"]
        if "Re-tag the single segment" in user:
            # Repair: drop one cue so it fits the ceiling of 3.
            return {"segments": [
                {"index": 0, "tagged": "[a] One. [b] Two. [c] Three. Four. Five."},
            ]}
        return {"segments": [
            {"index": 0, "tagged": "[a] One. [b] Two. [c] Three. [d] Four. Five."},
        ]}

    fake = FakeLLM(responder)
    script, warnings = PerformanceTagger(fake).tag(_segments(sources))
    assert warnings == []
    assert script.segments[0].tagged == "[a] One. [b] Two. [c] Three. Four. Five."
    repair_user = fake.complete_calls[1]["messages"][-1]["content"]
    assert "ceiling of 3" in repair_user


def test_tagger_batches_by_words_and_segment_count() -> None:
    sources = [f"Segment number {i} has some words in it." for i in range(30)]
    fake = FakeLLM(_tagged_responder({
        source: f"[calm] {source}" for source in sources
    }))
    script, warnings = PerformanceTagger(fake).tag(_segments(sources))
    assert warnings == []
    assert len(script.segments) == 30
    assert all(s.tagged.startswith("[calm]") for s in script.segments)
    # 30 segments must be split into multiple batches (<= 12 per batch).
    assert len(fake.complete_calls) >= 3
    header = re.compile(r"^Segment \d+ \(", re.MULTILINE)
    for call in fake.complete_calls:
        user = call["messages"][-1]["content"]
        assert len(header.findall(user)) <= 12


def test_tagger_skips_a_segment_larger_than_the_batch_ceiling() -> None:
    source = " ".join(["word"] * 801)
    fake = FakeLLM(lambda messages, calls: pytest.fail("oversized batch reached LLM"))
    script, warnings = PerformanceTagger(fake).tag(_segments([source]))
    assert fake.complete_calls == []
    assert script.segments[0].tagged == source
    assert warnings and "800-spoken-word" in warnings[0]


def test_tagger_raises_without_an_llm() -> None:
    with pytest.raises(RuntimeError, match="no local LLM"):
        PerformanceTagger(None).tag(_segments(["Hello."]))


def test_tagger_missing_index_falls_back_positionally() -> None:
    sources = ["First sentence here.", "Second sentence here."]
    # The model echoes the tagged text but forgets the index field.
    fake = FakeLLM(lambda messages, calls: {
        "segments": [
            {"tagged": "[calm] First sentence here."},
            {"tagged": "[shouting] Second sentence here."},
        ],
    })
    script, warnings = PerformanceTagger(fake).tag(_segments(sources))
    assert warnings == []
    assert script.segments[0].tagged == "[calm] First sentence here."
    assert script.segments[1].tagged == "[shouting] Second sentence here."


# ---------------------------------------------------------------------------
# Manager integration
# ---------------------------------------------------------------------------

def test_generate_performance_script_requires_an_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="No LLM", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    with pytest.raises(BackendError) as excinfo:
        service.tts.generate_performance_script(project.id)
    assert excinfo.value.code is BackendErrorCode.MODEL_SELECTION_REQUIRED


def test_generate_performance_script_requires_a_model_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="No Model", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    _enable_fake_llm(service, monkeypatch, lambda messages, calls: {"segments": []})
    with pytest.raises(BackendError) as excinfo:
        service.tts.generate_performance_script(project.id)
    assert excinfo.value.code is BackendErrorCode.MODEL_SELECTION_REQUIRED


def test_generate_performance_script_saves_portable_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Tagged", topic="test", target_duration=2,
    ))
    scenes = _add_scenes(service, project, [
        "I thought everything was normal.",
        "Then the lights went out.",
    ])
    fake = _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
        "Then the lights went out.": "[shouting] Then the lights went out.",
    }))
    fake.model = "router-default"
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})

    script, warnings = service.tts.generate_performance_script(
        project.id, intensity="expressive", notes="keep it grounded",
    )
    assert warnings == []
    assert script.intensity == "expressive"
    assert script.model == "fake-local-model"
    assert fake.model == "router-default"
    assert all(call["model"] == "fake-local-model" for call in fake.complete_calls)
    assert [s.key for s in script.segments] == [f"scene:{scene.id}" for scene in scenes]
    assert script.source_sha256

    # The artifact is a portable, human-readable JSON file.
    path = service.store.project_path(project) / "narration" / "performance-tags.json"
    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["provider"] == "fish_s2_pro"
    assert on_disk["segments"][0]["tagged"].startswith("[calm]")

    # Reload round-trips through the pydantic model.
    reloaded = service.tts.get_performance_script(project.id)
    assert reloaded is not None
    assert reloaded.segments == script.segments
    assert service.tts.performance_script_is_stale(project.id, reloaded) is False


def test_generate_performance_script_sends_video_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="The Haunted House", topic="a true-crime story",
        target_duration=2, style="documentary", audience="true-crime fans",
        instructions="keep it tense",
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    fake = _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    service.tts.generate_performance_script(project.id)
    system = fake.complete_calls[0]["messages"][0]["content"]
    assert "About this video:" in system
    assert "The Haunted House" in system
    assert "true-crime story" in system
    assert "documentary" in system
    assert "true-crime fans" in system
    assert "keep it tense" in system


def test_generate_performance_script_override_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Override", topic="test", target_duration=2,
    ))
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "My custom narration line.": "[whispering] My custom narration line.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})

    script, warnings = service.tts.generate_performance_script(
        project.id, text="My custom narration line.",
    )
    assert warnings == []
    assert script.segments[0].key == "override"
    assert script.segments[0].scene_id is None
    assert script.segments[0].tagged == "[whispering] My custom narration line."


def test_generate_performance_script_without_narration_raises_409_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Empty", topic="test", target_duration=2,
    ))
    _enable_fake_llm(service, monkeypatch, lambda messages, calls: {"segments": []})
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    with pytest.raises(NoNarrationTextError):
        service.tts.generate_performance_script(project.id)


def test_narration_feeds_tagged_text_to_fish_and_keeps_scene_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Fish Tags", topic="test", target_duration=2,
    ))
    scenes = _add_scenes(service, project, [
        "I thought everything was normal.",
        "Then the lights went out.",
    ])
    profile = service.tts.create_voice_profile(
        project.id, name="Owner", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    backend = PromptRecordingBackend()
    service.registry.register(backend, name="fish_s2_pro", replace=True)
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
        "Then the lights went out.": "[shouting] Then the lights went out.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    service.tts.generate_performance_script(project.id)

    service.tts.generate(
        project.id,
        NarrationRequest(
            provider="fish_s2_pro", voice_profile_id=profile.id,
            use_performance_tags=True,
        ),
        job_id="tagged-run",
    )

    # The model received the tagged text, one chunk per scene.
    assert backend.prompts == [
        "[calm] I thought everything was normal.",
        "[shouting] Then the lights went out.",
    ]
    takes, active_id = service.tts.list_narration_takes(project.id)
    take = next(item for item in takes if item.id == active_id)
    chunks = service.tts.list_take_chunks(project.id, take.id)
    # Chunk records keep the exact tagged text that was sent (restartability).
    assert [chunk["text"] for chunk in chunks] == backend.prompts
    # Scene sync is preserved: each chunk still maps to its scene.
    assert [chunk["scene_id"] for chunk in chunks] == [scene.id for scene in scenes]
    assert take.settings["timing_mode"] == "scene_audio_v1"
    assert take.settings["performance_tags"]["enabled"] is True
    assert take.settings["performance_tags"]["segments_used"] == 2
    assert take.settings["performance_tags"]["segments_skipped"] == 0
    assert take.settings["performance_tags"]["model"] == "fake-local-model"
    # Captions still align against the clean authored transcript: the stored
    # prompt is the clean resolved text, never the tagged text.
    expected_prompt = "\n\n".join(
        scene.narration.strip() for scene in scenes if scene.narration.strip()
    )
    assert take.prompt == expected_prompt
    assert "[" not in take.prompt


def test_narration_other_provider_ignores_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Other Provider", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    backend = PromptRecordingBackend()
    service.registry.register(backend, name="chatterbox", replace=True)
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    service.tts.generate_performance_script(project.id)

    service.tts.generate(
        project.id,
        NarrationRequest(
            provider="chatterbox", use_performance_tags=True,
        ),
        job_id="clean-run",
    )

    # A non-S2-Pro provider must never receive bracket text.
    assert backend.prompts == ["I thought everything was normal."]
    takes, active_id = service.tts.list_narration_takes(project.id)
    take = next(item for item in takes if item.id == active_id)
    assert take.settings["performance_tags"] == {"enabled": False, "reason": "provider"}


def test_stale_segment_falls_back_to_clean_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Stale", topic="test", target_duration=2,
    ))
    scenes = _add_scenes(service, project, [
        "I thought everything was normal.",
        "Then the lights went out.",
    ])
    profile = service.tts.create_voice_profile(
        project.id, name="Owner", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    backend = PromptRecordingBackend()
    service.registry.register(backend, name="fish_s2_pro", replace=True)
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
        "Then the lights went out.": "[shouting] Then the lights went out.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    service.tts.generate_performance_script(project.id)

    # The creator edits the second scene's narration after tagging.
    scenes[1].narration = "Then the lights went out for good."
    service.database.save_scene(scenes[1])
    service.store.save_scene(project.slug, scenes[1])
    script = service.tts.get_performance_script(project.id)
    assert script is not None
    assert service.tts.performance_script_is_stale(project.id, script) is True

    service.tts.generate(
        project.id,
        NarrationRequest(
            provider="fish_s2_pro", voice_profile_id=profile.id,
            use_performance_tags=True,
        ),
        job_id="stale-run",
    )

    # Fresh scene keeps its tags; the edited scene falls back to clean text.
    assert backend.prompts == [
        "[calm] I thought everything was normal.",
        "Then the lights went out for good.",
    ]
    takes, active_id = service.tts.list_narration_takes(project.id)
    take = next(item for item in takes if item.id == active_id)
    assert take.settings["performance_tags"]["segments_used"] == 1
    assert take.settings["performance_tags"]["segments_skipped"] == 1


def test_override_run_uses_the_override_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Override Run", topic="test", target_duration=2,
    ))
    profile = service.tts.create_voice_profile(
        project.id, name="Owner", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    backend = PromptRecordingBackend()
    service.registry.register(backend, name="fish_s2_pro", replace=True)
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "My custom narration line.": "[whispering] My custom narration line.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    service.tts.generate_performance_script(project.id, text="My custom narration line.")

    service.tts.generate(
        project.id,
        NarrationRequest(
            provider="fish_s2_pro", voice_profile_id=profile.id,
            text="My custom narration line.", use_performance_tags=True,
        ),
        job_id="override-run",
    )

    assert backend.prompts == ["[whispering] My custom narration line."]
    takes, active_id = service.tts.list_narration_takes(project.id)
    take = next(item for item in takes if item.id == active_id)
    assert take.settings["timing_mode"] == "override"
    assert take.settings["performance_tags"]["segments_used"] == 1


def test_chunk_regeneration_reuses_stored_tagged_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Chunk Retry", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, [
        "I thought everything was normal.",
        "Then the lights went out.",
    ])
    profile = service.tts.create_voice_profile(
        project.id, name="Owner", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    backend = PromptRecordingBackend()
    service.registry.register(backend, name="fish_s2_pro", replace=True)
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
        "Then the lights went out.": "[shouting] Then the lights went out.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    service.tts.generate_performance_script(project.id)

    service.tts.generate(
        project.id,
        NarrationRequest(
            provider="fish_s2_pro", voice_profile_id=profile.id,
            use_performance_tags=True,
        ),
        job_id="original",
    )
    backend.prompts.clear()
    takes, active_id = service.tts.list_narration_takes(project.id)
    job = service.tts.queue_chunk_regeneration(project.id, active_id, 2)
    service.tts.run_chunk_regeneration_job(job.id)

    # The regenerated chunk re-sends the stored tagged text, not the clean source.
    assert backend.prompts == ["[shouting] Then the lights went out."]


def test_clear_performance_script_removes_the_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Clear", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    service.tts.generate_performance_script(project.id)
    assert service.tts.get_performance_script(project.id) is not None

    service.tts.clear_performance_script(project.id)
    assert service.tts.get_performance_script(project.id) is None


def test_save_performance_script_validates_and_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Hand Edit", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    script, _ = service.tts.generate_performance_script(project.id)

    # A hand edit that rewrites the script is rejected...
    bad = PerformanceScript.model_validate(script.model_dump())
    bad.segments[0].tagged = "[calm] I thought everything was fine."
    with pytest.raises(ValueError, match="changed or reordered"):
        service.tts.save_performance_script(project.id, bad)

    # ...but the creator can accept it with accept=True.
    saved = service.tts.save_performance_script(project.id, bad, accept=True)
    assert saved.segments[0].tagged == "[calm] I thought everything was fine."
    reloaded = service.tts.get_performance_script(project.id)
    assert reloaded is not None
    assert reloaded.segments[0].tagged == "[calm] I thought everything was fine."


def test_regenerate_performance_segment_retags_only_that_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Regen", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, [
        "I thought everything was normal.",
        "Then the lights went out.",
    ])

    def responder(messages, calls):
        user = messages[-1]["content"]
        if len(re.findall(r"^Segment \d+ \(", user, re.MULTILINE)) == 1:
            # Single-segment regeneration: return a fresh, different cue.
            return {"segments": [
                {"index": 0, "tagged": "[shouting] Then the lights went out."},
            ]}
        return {"segments": [
            {"index": 0, "tagged": "[calm] I thought everything was normal."},
            {"index": 1, "tagged": "[whispering] Then the lights went out."},
        ]}

    fake = _enable_fake_llm(service, monkeypatch, responder)
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    script, _ = service.tts.generate_performance_script(project.id)
    assert [s.tagged for s in script.segments] == [
        "[calm] I thought everything was normal.",
        "[whispering] Then the lights went out.",
    ]

    updated, warnings = service.tts.regenerate_performance_segment(
        project.id, script.segments[1].key,
    )
    assert warnings == []
    # Only the requested segment changed; the other kept its stored tags.
    assert updated.segments[0].tagged == "[calm] I thought everything was normal."
    assert updated.segments[1].tagged == "[shouting] Then the lights went out."
    # The regeneration re-sent only the one segment to the LLM.
    last_user = fake.complete_calls[-1]["messages"][-1]["content"]
    assert "Then the lights went out." in last_user
    assert "I thought everything was normal." not in last_user
    # The updated script is persisted.
    reloaded = service.tts.get_performance_script(project.id)
    assert reloaded is not None
    assert reloaded.segments[0].tagged == "[calm] I thought everything was normal."
    assert reloaded.segments[1].tagged == "[shouting] Then the lights went out."


def test_regenerate_performance_segment_sends_scene_outline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="The Haunted House", topic="a true-crime story",
        target_duration=2,
    ))
    _add_scenes(service, project, [
        "I thought everything was normal.",
        "Then the lights went out.",
        "We ran into the dark.",
    ])
    fake = _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
        "Then the lights went out.": "[shouting] Then the lights went out.",
        "We ran into the dark.": "[whispering] We ran into the dark.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    script, _ = service.tts.generate_performance_script(project.id)
    # Regenerate the middle scene.
    service.tts.regenerate_performance_segment(project.id, script.segments[1].key)
    system = fake.complete_calls[-1]["messages"][0]["content"]
    # The video subject is included...
    assert "About this video:" in system
    assert "The Haunted House" in system
    # ...and the outline marks the current scene with its adjacent narration.
    assert "Scene outline" in system
    assert "<-- current" in system
    assert "Immediately before" in system
    assert "Immediately after" in system
    assert "I thought everything was normal." in system  # previous scene narration
    assert "We ran into the dark." in system  # next scene narration


def test_regenerate_performance_segment_requires_an_existing_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="No Script", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    _enable_fake_llm(service, monkeypatch, lambda messages, calls: {"segments": []})
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    with pytest.raises(ValueError, match="no delivery-tag script"):
        service.tts.regenerate_performance_segment(project.id, "scene:x")


def test_regenerate_performance_segment_unknown_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Bad Key", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    service.tts.generate_performance_script(project.id)
    with pytest.raises(ValueError, match="unknown segment key"):
        service.tts.regenerate_performance_segment(project.id, "scene:nope")


def test_regenerate_performance_segment_requires_an_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="No LLM", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    script, _ = service.tts.generate_performance_script(project.id)
    # Drop the LLM after the script exists: regeneration must demand a model.
    monkeypatch.setattr(service.director, "llm", None)
    monkeypatch.setattr(service, "mock_mode", True)
    with pytest.raises(BackendError) as excinfo:
        service.tts.regenerate_performance_segment(project.id, script.segments[0].key)
    assert excinfo.value.code is BackendErrorCode.MODEL_SELECTION_REQUIRED


def test_regenerate_performance_segment_preserves_accepted_hand_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Hand Edit", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, [
        "I thought everything was normal.",
        "Then the lights went out.",
    ])
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
        "Then the lights went out.": "[shouting] Then the lights went out.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    script, _ = service.tts.generate_performance_script(project.id)

    # Hand-edit segment 0 into an invalid state and accept it.
    bad = PerformanceScript.model_validate(script.model_dump())
    bad.segments[0].tagged = "[calm] I thought everything was fine."
    service.tts.save_performance_script(project.id, bad, accept=True)

    # Regenerating segment 1 must not clobber the accepted edit on segment 0.
    updated, warnings = service.tts.regenerate_performance_segment(
        project.id, script.segments[1].key,
    )
    assert warnings == []
    assert updated.segments[0].tagged == "[calm] I thought everything was fine."
    assert updated.segments[1].tagged == "[shouting] Then the lights went out."
    reloaded = service.tts.get_performance_script(project.id)
    assert reloaded is not None
    assert reloaded.segments[0].tagged == "[calm] I thought everything was fine."
    assert reloaded.segments[1].tagged == "[shouting] Then the lights went out."


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

def _api_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    service = app.state.service
    monkeypatch.setattr(service.tts_workers, "ensure_running", lambda provider: False)
    monkeypatch.setattr(service.tts_workers, "stop", lambda provider: False)
    return app, service


def test_performance_tags_api_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service = _api_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="API Tags", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    client = TestClient(app)
    base = f"/api/projects/{project.id}/tts/performance-tags"

    # No script yet.
    response = client.get(base)
    assert response.status_code == 200
    body = response.json()
    assert body["script"] is None
    assert body["stale"] is False
    assert body["tag_count"] == 0
    assert body["llm"]["available"] is True
    assert body["llm"]["model"] == "fake-local-model"

    # Generate.
    response = client.post(base, json={"intensity": "subtle", "notes": "grounded"})
    assert response.status_code == 200
    body = response.json()
    assert body["warnings"] == []
    assert body["script"]["segments"][0]["tagged"].startswith("[calm]")
    assert body["script"]["intensity"] == "subtle"
    assert body["tag_count"] == 1

    # Without force the existing script is returned unchanged.
    response = client.post(base, json={})
    assert response.status_code == 200
    assert response.json()["warnings"] == []

    # GET now reports the script and its tag count.
    body = client.get(base).json()
    assert body["script"] is not None
    assert body["tag_count"] == 1
    assert body["stale"] is False

    # Hand edit that keeps the words is accepted.
    key = body["script"]["segments"][0]["key"]
    response = client.put(base, json={"segments": [{
        "key": key, "tagged": "[whispering] I thought everything was normal.",
    }]})
    assert response.status_code == 200
    assert response.json()["script"]["segments"][0]["tagged"].startswith("[whispering]")

    # A hand edit that rewrites the script is rejected with 422...
    response = client.put(base, json={"segments": [{
        "key": key, "tagged": "[calm] I thought everything was fine.",
    }]})
    assert response.status_code == 422
    assert "changed or reordered" in response.text

    # ...and is kept when the creator accepts it.
    response = client.put(
        f"{base}?accept=true",
        json={"segments": [{"key": key, "tagged": "[calm] I thought everything was fine."}]},
    )
    assert response.status_code == 200

    # Unknown segment key is a client error.
    response = client.put(base, json={"segments": [{"key": "scene:nope", "tagged": "x"}]})
    assert response.status_code == 422

    # DELETE removes the artifact.
    response = client.delete(base)
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert client.get(base).json()["script"] is None


def test_performance_tags_api_error_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service = _api_service(tmp_path, monkeypatch)
    client = TestClient(app)

    # Unknown project: 404 on every verb.
    assert client.get("/api/projects/missing/tts/performance-tags").status_code == 404
    assert client.delete("/api/projects/missing/tts/performance-tags").status_code == 404
    response = client.put(
        "/api/projects/missing/tts/performance-tags",
        json={"segments": [{"key": "override", "tagged": "[calm] x"}]},
    )
    assert response.status_code == 404
    response = client.post("/api/projects/missing/tts/performance-tags", json={})
    assert response.status_code == 404

    # Mock mode (no LLM): 409 with the structured model-selection code.
    project = service.create_project(ProjectCreate(
        title="No LLM", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    assert client.get(
        f"/api/projects/{project.id}/tts/performance-tags"
    ).json()["llm"] == {"available": False, "model": None}
    response = client.post(
        f"/api/projects/{project.id}/tts/performance-tags", json={},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "model_selection_required"

    # No narration text at all: 409.
    empty = service.create_project(ProjectCreate(
        title="Empty", topic="test", target_duration=2,
    ))
    _enable_fake_llm(service, monkeypatch, lambda messages, calls: {"segments": []})
    service.update_project(empty.id, {"selected_llm_model": "fake-local-model"})
    response = client.post(f"/api/projects/{empty.id}/tts/performance-tags", json={})
    assert response.status_code == 409
    assert "No planned narration" in response.text

    # Empty override text: 422.
    response = client.post(
        f"/api/projects/{project.id}/tts/performance-tags", json={"text": "   "},
    )
    assert response.status_code == 422

    # PUT with no existing script: 422.
    response = client.put(
        f"/api/projects/{project.id}/tts/performance-tags",
        json={"segments": [{"key": "override", "tagged": "[calm] x"}]},
    )
    assert response.status_code == 422
    assert "no delivery-tag script" in response.text


def test_performance_tags_generate_rejects_unknown_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service = _api_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Strict", topic="test", target_duration=2,
    ))
    client = TestClient(app)
    response = client.post(
        f"/api/projects/{project.id}/tts/performance-tags",
        json={"intensity": "yelling"},
    )
    assert response.status_code == 422


def test_performance_tags_regenerate_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service = _api_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="API Regen", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, [
        "I thought everything was normal.",
        "Then the lights went out.",
    ])

    def responder(messages, calls):
        user = messages[-1]["content"]
        if len(re.findall(r"^Segment \d+ \(", user, re.MULTILINE)) == 1:
            return {"segments": [
                {"index": 0, "tagged": "[shouting] Then the lights went out."},
            ]}
        return {"segments": [
            {"index": 0, "tagged": "[calm] I thought everything was normal."},
            {"index": 1, "tagged": "[whispering] Then the lights went out."},
        ]}

    _enable_fake_llm(service, monkeypatch, responder)
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    client = TestClient(app)
    base = f"/api/projects/{project.id}/tts/performance-tags"

    # Generate the full script first.
    response = client.post(base, json={"intensity": "balanced"})
    assert response.status_code == 200
    script = response.json()["script"]
    key1 = script["segments"][1]["key"]

    # Regenerate only the second segment.
    response = client.post(
        f"{base}/regenerate",
        json={"key": key1, "intensity": "expressive", "notes": "louder"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["warnings"] == []
    assert body["script"]["segments"][0]["tagged"] == "[calm] I thought everything was normal."
    assert body["script"]["segments"][1]["tagged"] == "[shouting] Then the lights went out."
    # The re-tagged segment is persisted.
    assert client.get(base).json()["script"]["segments"][1]["tagged"].startswith("[shouting]")


def test_performance_tags_regenerate_api_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, service = _api_service(tmp_path, monkeypatch)
    client = TestClient(app)

    # Unknown project: 404.
    response = client.post(
        "/api/projects/missing/tts/performance-tags/regenerate",
        json={"key": "scene:x"},
    )
    assert response.status_code == 404

    # A project with a stored script, then the LLM is dropped: 409.
    project = service.create_project(ProjectCreate(
        title="No LLM", topic="test", target_duration=2,
    ))
    _add_scenes(service, project, ["I thought everything was normal."])
    _enable_fake_llm(service, monkeypatch, _tagged_responder({
        "I thought everything was normal.": "[calm] I thought everything was normal.",
    }))
    service.update_project(project.id, {"selected_llm_model": "fake-local-model"})
    generated = client.post(
        f"/api/projects/{project.id}/tts/performance-tags", json={},
    )
    assert generated.status_code == 200
    real_key = generated.json()["script"]["segments"][0]["key"]
    monkeypatch.setattr(service.director, "llm", None)
    monkeypatch.setattr(service, "mock_mode", True)
    response = client.post(
        f"/api/projects/{project.id}/tts/performance-tags/regenerate",
        json={"key": real_key},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "model_selection_required"

    # LLM available but no stored script: 422.
    empty = service.create_project(ProjectCreate(
        title="Empty", topic="test", target_duration=2,
    ))
    _enable_fake_llm(service, monkeypatch, lambda messages, calls: {"segments": []})
    service.update_project(empty.id, {"selected_llm_model": "fake-local-model"})
    response = client.post(
        f"/api/projects/{empty.id}/tts/performance-tags/regenerate",
        json={"key": "scene:x"},
    )
    assert response.status_code == 422
    assert "no delivery-tag script" in response.text

    # Stored script but unknown segment key: 422 (checked before the LLM).
    response = client.post(
        f"/api/projects/{project.id}/tts/performance-tags/regenerate",
        json={"key": "scene:nope"},
    )
    assert response.status_code == 422
    assert "unknown segment key" in response.text
