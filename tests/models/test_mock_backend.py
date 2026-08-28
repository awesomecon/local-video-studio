from __future__ import annotations

import json
import wave

import pytest
from PIL import Image

from backend.models import GenerationRequest, MockGeneratorBackend


@pytest.mark.parametrize("kind", ["image", "tts", "music", "transcription", "llm"])
def test_mock_outputs_are_real_and_deterministic(tmp_path, kind):
    backend = MockGeneratorBackend()
    request = GenerationRequest(
        job_id=f"job-{kind}",
        output_dir=tmp_path / kind,
        prompt="Roman aqueducts",
        seed=42,
        duration_seconds=0.25,
        width=64,
        height=48,
        settings={"kind": kind},
    )
    first = backend.generate(request)
    hashes = first.metadata["content_hash"]
    second = backend.generate(request)
    assert hashes == second.metadata["content_hash"]
    assert all(path.stat().st_size > 0 for path in first.outputs)
    if kind == "image":
        assert Image.open(first.outputs[0]).size == (64, 48)
    elif kind in {"tts", "music"}:
        with wave.open(str(first.outputs[0])) as audio:
            assert audio.getnframes() > 0
    elif kind == "llm":
        assert json.loads(first.outputs[0].read_text())["scenes"][0]["seed"] == 43


def test_mock_video_is_playable_container(tmp_path):
    backend = MockGeneratorBackend()
    if not backend.ffmpeg_path:
        pytest.skip("FFmpeg unavailable")
    result = backend.generate(
        GenerationRequest(
            job_id="video",
            output_dir=tmp_path,
            prompt="test",
            seed=7,
            duration_seconds=0.2,
            width=64,
            height=48,
            settings={"kind": "video"},
        )
    )
    assert result.outputs[0].read_bytes()[4:8] == b"ftyp"
