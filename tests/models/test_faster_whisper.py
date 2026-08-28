from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.models import FasterWhisperBackend, GenerationRequest
from backend.models.errors import BackendError


class _FakeWhisperModel:
    def __init__(self, words: list[SimpleNamespace]) -> None:
        self.words = words

    def transcribe(self, *_args, **_kwargs):
        segments = [SimpleNamespace(words=self.words)]
        info = SimpleNamespace(language="en", language_probability=0.99)
        return segments, info


def _request(tmp_path) -> GenerationRequest:
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"local test audio")
    return GenerationRequest(
        job_id="captions",
        output_dir=tmp_path / "subtitles",
        prompt="align",
        seed=0,
        references=(audio,),
        settings={"language": "en"},
    )


def test_zero_duration_whisper_word_is_preserved(tmp_path) -> None:
    backend = FasterWhisperBackend(tmp_path, device="cpu")
    backend._model = _FakeWhisperModel([
        SimpleNamespace(start=1.0, end=1.2, word=" before"),
        SimpleNamespace(start=1.4, end=1.4, word=" a"),
        SimpleNamespace(start=1.6, end=1.9, word=" boundary"),
    ])

    result = backend.generate(_request(tmp_path))

    payload = json.loads(result.outputs[0].read_text(encoding="utf-8"))
    assert payload["words"] == [
        {"start_seconds": 1.0, "end_seconds": 1.2, "text": " before"},
        {"start_seconds": 1.4, "end_seconds": 1.41, "text": " a"},
        {"start_seconds": 1.6, "end_seconds": 1.9, "text": " boundary"},
    ]


def test_reversed_whisper_word_timestamp_still_fails(tmp_path) -> None:
    backend = FasterWhisperBackend(tmp_path, device="cpu")
    backend._model = _FakeWhisperModel([
        SimpleNamespace(start=2.0, end=1.9, word=" invalid"),
    ])

    with pytest.raises(BackendError, match="could not derive word timestamps"):
        backend.generate(_request(tmp_path))
