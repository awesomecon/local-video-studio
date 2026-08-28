from __future__ import annotations

import io
import json
import logging
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.core import load_config
from backend.models import (
    BackendDescriptor,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
    TTSServiceBackend,
)
from backend.models.errors import BackendError, BackendErrorCode
from backend.rendering.mock_media import create_placeholder_image
from backend.schemas import AssetType, ProjectCreate, Scene, VisualType
from backend.tts.audio import join_wav_files
from backend.tts.chunking import chunk_narration
from backend.tts.models import NarrationRequest
from services.tts_worker.app import (
    ChatterboxProvider,
    GeneratePayload,
    QwenProvider,
    create_app as create_worker_app,
)


def wav_bytes(*, frames: int = 800, sample_rate: int = 8000, sample: int = 0) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(sample.to_bytes(2, byteorder="little", signed=True) * frames)
    return target.getvalue()


class RecordingBackend(GeneratorBackend):
    """In-memory TTS backend so tests never download models or start workers."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name="chatterbox", model_name="Fake Chatterbox", model_version="test",
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
        self.calls.append(request.job_id)
        output = request.output_dir / str(request.settings["filename"])
        output.write_bytes(wav_bytes(sample=1000))
        return GenerationResult(outputs=(output,), metadata={"backend": "chatterbox"})


def narration_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    service = app.state.service
    monkeypatch.setattr(service.tts_workers, "ensure_running", lambda provider: False)
    monkeypatch.setattr(service.tts_workers, "stop", lambda provider: False)
    return service


def test_chunking_prefers_paragraphs_and_splits_oversized_text() -> None:
    text = "First short paragraph.\n\nSecond short paragraph.\n\n" + "word " * 40
    chunks = chunk_narration(text, 5, words_per_second=2)
    assert chunks[0] == "First short paragraph.\n\nSecond short paragraph."
    assert all(len(chunk.split()) <= 10 for chunk in chunks[1:])
    assert " ".join(chunks).count("word") == 40


def test_wav_join_inserts_only_the_missing_boundary_pause(tmp_path: Path) -> None:
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    # The first chunk already ends with 25 ms and the second begins with 25 ms
    # of silence, so a 100 ms minimum requires only 50 ms of inserted PCM.
    voiced = (1000).to_bytes(2, byteorder="little", signed=True)
    silent = b"\0\0"
    first.write_bytes(_pcm_wav(voiced * 600 + silent * 200))
    second.write_bytes(_pcm_wav(silent * 200 + voiced * 600))
    duration = join_wav_files([first, second], tmp_path / "joined.wav", pause_ms=100)
    assert duration == pytest.approx(0.25)
    with wave.open(str(tmp_path / "joined.wav"), "rb") as joined:
        assert joined.getnframes() == 2000


def _pcm_wav(frames: bytes, *, sample_width: int = 2, sample_rate: int = 8000) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(sample_width)
        output.setframerate(sample_rate)
        output.writeframes(frames)
    return target.getvalue()


def test_wav_join_does_not_stack_pause_on_sufficient_generated_silence(tmp_path: Path) -> None:
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    first.write_bytes(wav_bytes(frames=800))
    second.write_bytes(wav_bytes(frames=800))

    duration = join_wav_files([first, second], tmp_path / "joined.wav", pause_ms=100)

    assert duration == pytest.approx(0.2)
    with wave.open(str(tmp_path / "joined.wav"), "rb") as joined:
        assert joined.getnframes() == 1600


def test_tts_service_adapter_validates_worker_and_output(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"provider": "chatterbox", "loaded": False})
        if request.url.path == "/load":
            return httpx.Response(200, json={"loaded": True})
        if request.url.path == "/generate":
            payload = json.loads(request.content)
            Path(payload["output_path"]).write_bytes(wav_bytes())
            return httpx.Response(200, json={
                "output_path": payload["output_path"],
                "metrics": {"peak_vram_gb": 4.5, "real_time_factor": 0.2},
            })
        if request.url.path == "/unload":
            return httpx.Response(200, json={"loaded": False})
        raise AssertionError(request.url)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    backend = TTSServiceBackend("chatterbox", "http://127.0.0.1:8193", client_factory=factory)
    assert backend.health()["status"] == "healthy"
    result = backend.generate(GenerationRequest(
        job_id="job", output_dir=tmp_path, prompt="Hello", seed=5,
        references=(tmp_path / "reference.wav",), settings={"filename": "0001.wav"},
    ))
    assert result.outputs[0].is_file()
    assert result.peak_vram_gb == 4.5


def test_tts_service_adapter_resets_canceled_jobs(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path in {"/jobs/job/cancel", "/jobs/job/reset_cancel"}:
            return httpx.Response(200, json={"canceled": request.url.path.endswith("/cancel")})
        raise AssertionError(request.url)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    def broken_factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(_raise_connect_error), **kwargs)

    backend = TTSServiceBackend("chatterbox", "http://127.0.0.1:8193", client_factory=factory)
    assert backend.cancel("job") is True
    backend.reset_cancel("job")
    assert seen == ["/jobs/job/cancel", "/jobs/job/reset_cancel"]
    unreachable = TTSServiceBackend("chatterbox", "http://127.0.0.1:8193", client_factory=broken_factory)
    with caplog.at_level(logging.DEBUG, logger="backend.models.tts_service"):
        unreachable.reset_cancel("job")
    TTSServiceBackend("chatterbox", None).reset_cancel("job")
    ignored = [record for record in caplog.records if "cancel reset ignored" in record.getMessage()]
    assert len(ignored) == 1
    assert "job job" in ignored[0].getMessage() and "server_not_running" in ignored[0].getMessage()


def _raise_connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class FakeWorkerProvider:
    name = "chatterbox"
    model = None

    def __init__(self, root: Path) -> None:
        self.model_path = root
        self.generated: list[str] = []

    def generate(self, payload: GeneratePayload) -> dict:
        self.generated.append(payload.job_id)
        Path(payload.output_path).write_bytes(wav_bytes())
        return {"output_path": str(payload.output_path), "metrics": {}}

    def unload(self) -> None:
        self.model = None


def test_worker_canceled_jobs_can_be_reset_and_retried(tmp_path: Path) -> None:
    provider = FakeWorkerProvider(tmp_path)
    client = TestClient(create_worker_app(provider, output_root=tmp_path))
    body = {"job_id": "job-1", "text": "Hello.", "output_path": str(tmp_path / "job-1.wav")}

    assert client.post("/jobs/job-1/cancel").json() == {"canceled": True}
    blocked = client.post("/generate", json=body)
    assert blocked.status_code == 409
    assert client.post("/generate", json=body).status_code == 409

    assert client.post("/jobs/job-1/reset_cancel").json() == {"canceled": False}
    retried = client.post("/generate", json=body)
    assert retried.status_code == 200

    assert client.post("/jobs/job-1/cancel").json() == {"canceled": True}
    assert client.post("/generate", json=body).status_code == 409
    assert client.post("/jobs/job-1/reset_cancel").json() == {"canceled": False}
    assert client.post("/generate", json=body).status_code == 200
    assert provider.generated == ["job-1", "job-1"]


def test_voice_profile_api_requires_consent_and_stays_portable(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    client = TestClient(app)
    project = app.state.service.create_project(ProjectCreate(
        title="Voice", topic="test", target_duration=1,
    ))
    endpoint = f"/api/projects/{project.id}/tts/voices?name=Owner&language=en"
    denied = client.post(endpoint, content=wav_bytes(), headers={"Content-Type": "audio/wav"})
    assert denied.status_code == 422
    saved = client.post(
        endpoint + "&authorized=true&transcript=hello", content=wav_bytes(),
        headers={"Content-Type": "audio/wav"},
    )
    assert saved.status_code == 201
    payload = saved.json()
    assert not Path(payload["reference_audio"]).is_absolute()
    listed = client.get(f"/api/projects/{project.id}/tts/voices").json()["voices"][0]
    assert listed["id"] == payload["id"]
    assert listed["url"].endswith(f"/tts/voices/{payload['id']}/file")
    playback = client.get(listed["url"])
    assert playback.status_code == 200
    assert playback.headers["content-type"].startswith("audio/wav")
    assert playback.content == wav_bytes()
    assert (app.state.service.store.project_path(project) / payload["reference_audio"]).is_file()

    other = app.state.service.create_project(ProjectCreate(
        title="Other Voice Project", topic="test", target_duration=1,
    ))
    denied_playback = client.get(
        f"/api/projects/{other.id}/tts/voices/{payload['id']}/file",
    )
    assert denied_playback.status_code == 404


def test_voice_profile_gain_boosts_saved_reference_audio(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    client = TestClient(app)
    project = app.state.service.create_project(ProjectCreate(
        title="Quiet Voice", topic="test", target_duration=1,
    ))
    source = wav_bytes(sample=1000)
    saved = client.post(
        f"/api/projects/{project.id}/tts/voices"
        "?name=Boosted&authorized=true&gain_db=6.0206",
        content=source, headers={"Content-Type": "audio/wav"},
    )
    assert saved.status_code == 201
    payload = saved.json()
    assert payload["gain_db"] == pytest.approx(6.0206)
    assert payload["source_audio_sha256"] != payload["audio_sha256"]

    playback = client.get(
        f"/api/projects/{project.id}/tts/voices/{payload['id']}/file",
    )
    with wave.open(io.BytesIO(playback.content), "rb") as boosted:
        sample = int.from_bytes(boosted.readframes(1), "little", signed=True)
    assert sample == 2000

    too_high = client.post(
        f"/api/projects/{project.id}/tts/voices"
        "?name=TooLoud&authorized=true&gain_db=25",
        content=source, headers={"Content-Type": "audio/wav"},
    )
    assert too_high.status_code == 422


def test_reference_free_requests_allow_qwen_custom_voice(tmp_path: Path) -> None:
    chatterbox = NarrationRequest(
        provider="chatterbox", voice_profile_id=None, text="Built-in narration.",
    )
    assert chatterbox.voice_profile_id is None

    qwen = NarrationRequest(
        provider="qwen_tts", voice_profile_id=None, text="Built-in narration.", speaker="Aiden",
    )
    assert qwen.speaker == "Aiden"

    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    project = app.state.service.create_project(ProjectCreate(
        title="No Reference", topic="test", target_duration=1,
    ))
    app.state.service.run_narration_job = lambda _job_id: None
    response = TestClient(app).post(
        f"/api/projects/{project.id}/tts/generate",
        json={
            "provider": "qwen_tts", "voice_profile_id": None, "text": "No reference.",
            "speaker": "Ryan", "voice_instruction": "Calm and clear.",
        },
    )
    assert response.status_code == 202


def test_qwen_worker_uses_custom_voice_without_reference(tmp_path: Path) -> None:
    import numpy as np

    class FakeQwen:
        def generate_custom_voice(self, **kwargs):
            assert kwargs == {
                "text": "Reference-free speech.", "language": "English", "speaker": "Ryan",
                "instruct": "Warm documentary delivery.", "temperature": 0.8,
            }
            return [np.zeros(2400, dtype=np.float32)], 24000

    provider = QwenProvider(tmp_path)
    provider.model = FakeQwen()
    waveform, sample_rate = provider._generate(GeneratePayload(
        job_id="custom", text="Reference-free speech.", output_path=tmp_path / "custom.wav",
        reference_audio=None, language="en", speaker="Ryan",
        voice_instruction="Warm documentary delivery.",
    ))
    assert sample_rate == 24000
    assert waveform.shape == (2400,)


def test_unplanned_narration_request_returns_clear_error_without_queueing(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    project = app.state.service.create_project(ProjectCreate(
        title="Needs Script", topic="test", target_duration=1,
    ))
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{project.id}/tts/generate",
        json={"provider": "chatterbox", "voice_profile_id": None, "text": None},
    )

    assert response.status_code == 422
    assert "Run planning from the Script screen" in response.text
    assert app.state.service.jobs.list(project.id) == []


def test_narration_uses_current_scene_records_without_plan_file(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    project = app.state.service.create_project(ProjectCreate(
        title="Scene Script", topic="test", target_duration=1,
    ))
    plan = app.state.service.ensure_plan(project.id)
    (app.state.service.store.project_path(project) / "plan.json").unlink()

    text = app.state.service.tts.resolve_narration_text(project.id, None)

    assert text == "\n\n".join(scene.narration for scene in plan.scenes)


def test_chatterbox_worker_uses_bundled_conditioning_without_reference(tmp_path: Path) -> None:
    import numpy as np

    builtin = object()

    class FakeChatterbox:
        sr = 24000
        conds = builtin

        def generate(self, text, **kwargs):
            assert text == "Built-in voice."
            assert kwargs["audio_prompt_path"] is None
            assert self.conds is builtin
            return np.zeros(2400, dtype=np.float32)

    provider = ChatterboxProvider(tmp_path)
    provider.model = FakeChatterbox()
    provider._builtin_conds = builtin
    waveform, sample_rate = provider._generate(GeneratePayload(
        job_id="builtin", text="Built-in voice.", output_path=tmp_path / "builtin.wav",
        reference_audio=None, language="en",
    ))
    assert sample_rate == 24000
    assert waveform.shape == (2400,)


def test_wav_join_uses_unsigned_silence_for_8bit_pcm(tmp_path: Path) -> None:
    def eight_bit_voice(frames: int) -> bytes:
        target = io.BytesIO()
        with wave.open(target, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(1)
            output.setframerate(8000)
            output.writeframes(b"\xff" * frames)
        return target.getvalue()

    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    first.write_bytes(eight_bit_voice(800))
    second.write_bytes(eight_bit_voice(800))

    join_wav_files([first, second], tmp_path / "joined.wav", pause_ms=100)

    with wave.open(str(tmp_path / "joined.wav"), "rb") as joined:
        assert joined.getsampwidth() == 1
        assert joined.getnframes() == 2400
        raw = joined.readframes(joined.getnframes())
    assert len(raw) == 2400
    assert raw[800:1600] == b"\x80" * 800
    assert set(raw) == {0x80, 0xff}


def test_narration_generations_create_immutable_takes_and_publish_master_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Master", topic="test", target_duration=1,
    ))
    backend = RecordingBackend()
    service.registry.register(backend, name="chatterbox", replace=True)

    root = service.store.project_path(project)
    master = root / "narration" / "master.wav"
    master.parent.mkdir(parents=True, exist_ok=True)
    stale_bytes = wav_bytes(frames=400, sample_rate=16000)
    master.write_bytes(stale_bytes)

    for round_index in range(2):
        service.tts.generate(
            project.id,
            NarrationRequest(provider="chatterbox", voice_profile_id=None, text=f"Take {round_index}."),
            job_id=f"job-{round_index}",
        )
        with wave.open(str(master), "rb") as published:
            assert published.getnframes() > 0

    takes = sorted((root / "narration" / "takes" / "chatterbox").glob("*.wav"))
    assert [take.name for take in takes] == ["job-0.wav", "job-1.wav"]
    assert all(take.read_bytes() == wav_bytes(sample=1000) for take in takes)
    manifest = json.loads((root / "narration" / "takes.json").read_text(encoding="utf-8"))
    assets, active_id = service.tts.list_narration_takes(project.id)
    assert len(assets) == 2
    assert manifest["active_asset_id"] == active_id == assets[-1].id
    assert manifest["active_file"] == "narration/takes/chatterbox/job-1.wav"
    assert master.read_bytes() == takes[-1].read_bytes()
    assert not list((root / "variants" / "archive").glob("master-*.wav"))
    assert not list((root / "narration").glob(".master.wav.*"))
    assert len(list((root / "narration" / "takes" / "chatterbox").glob("*.json"))) == 2

    assert service.database.delete_assets_for_path(project.id, "narration/takes/") == 2
    recovered, recovered_active_id = service.tts.list_narration_takes(project.id)
    assert [asset.id for asset in recovered] == [asset.id for asset in assets]
    assert recovered_active_id == active_id


def test_planned_narration_chunks_are_scene_bound_and_publish_measured_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Scene timing", topic="test", target_duration=2, resolution=(160, 90),
    ))
    scenes = [
        Scene(project_id=project.id, index=0, title="Opening", duration=1,
              narration="The opening line.", visual_type=VisualType.FLUX_STILL),
        Scene(project_id=project.id, index=1, title="Closing", duration=1,
              narration="The closing line.", visual_type=VisualType.FLUX_STILL),
    ]
    for scene in scenes:
        service.database.save_scene(scene)
        service.store.save_scene(project.slug, scene)
    backend = RecordingBackend()
    service.registry.register(backend, name="chatterbox", replace=True)

    service.tts.generate(
        project.id,
        NarrationRequest(provider="chatterbox", text=None, pause_ms=100),
        job_id="scene-bound",
    )

    takes, active_id = service.tts.list_narration_takes(project.id)
    take = next(item for item in takes if item.id == active_id)
    chunks = service.tts.list_take_chunks(project.id, take.id)
    assert [chunk["scene_id"] for chunk in chunks] == [scene.id for scene in scenes]
    assert [chunk["text"] for chunk in chunks] == [scene.narration for scene in scenes]
    assert take.settings["timing_mode"] == "scene_audio_v1"
    assert take.settings["inserted_pause_ms"] == [100]
    assert service.tts.active_scene_durations(project.id) == pytest.approx({
        scenes[0].id: 0.2,
        scenes[1].id: 0.1,
    })
    root = service.store.project_path(project)
    for scene in scenes:
        visual = create_placeholder_image(
            root / "scenes" / f"{scene.index + 1:03d}" / "visual.png",
            width=160, height=90, seed=scene.index, binaries=service.renderer.binaries,
        )
        service._record_asset(
            project, scene, visual, AssetType.IMAGE,
            GenerationResult(outputs=(visual,), metadata={
                "backend": "mock", "model": "placeholder", "seed": scene.index,
            }),
            role="visual",
        )
    timeline = service._build_timeline(project)
    assert [clip.duration_seconds for clip in timeline.clips] == pytest.approx([0.2, 0.1])
    assert timeline.duration_seconds == pytest.approx(0.3)
    assert timeline.metadata["duration_policy"] == "scene_aligned_narration_v1"
    assert timeline.metadata["scene_audio_synced"] is True


def test_regenerating_one_chunk_creates_and_activates_a_new_take(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Chunk retry", topic="test", target_duration=2,
    ))
    scenes = [
        Scene(project_id=project.id, index=index, duration=1, narration=text)
        for index, text in enumerate(("Keep this chunk.", "Replace this chunk."))
    ]
    for scene in scenes:
        service.database.save_scene(scene)
        service.store.save_scene(project.slug, scene)
    backend = RecordingBackend()
    service.registry.register(backend, name="chatterbox", replace=True)
    service.tts.generate(
        project.id, NarrationRequest(provider="chatterbox", text=None), job_id="original",
    )
    original_takes, original_active = service.tts.list_narration_takes(project.id)

    job = service.tts.queue_chunk_regeneration(project.id, original_active, 2)
    service.tts.run_chunk_regeneration_job(job.id)

    takes, active_id = service.tts.list_narration_takes(project.id)
    assert len(takes) == len(original_takes) + 1
    assert active_id != original_active
    active = next(item for item in takes if item.id == active_id)
    assert active.settings["derived_from_asset_id"] == original_active
    assert len(service.tts.list_take_chunks(project.id, active.id)) == 2
    assert backend.calls == ["original:1", "original:2", f"{job.id}:2"]
    assert service.jobs.get(job.id).status is not None


def test_narration_take_api_lists_and_activates_without_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    service = app.state.service
    monkeypatch.setattr(service.tts_workers, "ensure_running", lambda provider: False)
    monkeypatch.setattr(service.tts_workers, "stop", lambda provider: False)
    project = service.create_project(ProjectCreate(
        title="Take Library", topic="test", target_duration=1,
    ))
    backend = RecordingBackend()
    service.registry.register(backend, name="chatterbox", replace=True)
    root = service.store.project_path(project)

    for index in range(2):
        service.tts.generate(
            project.id,
            NarrationRequest(
                provider="chatterbox", voice_profile_id=None,
                text=f"Comparison take {index}.", seed=20001 + index,
            ),
            job_id=f"comparison-{index}",
        )
    client = TestClient(app)
    listing = client.get(f"/api/projects/{project.id}/tts/narrations")
    assert listing.status_code == 200
    assert listing.json()["active_asset_id"] == listing.json()["takes"][-1]["id"]
    listed_chunks = listing.json()["takes"][-1]["chunks"]
    assert len(listed_chunks) == 1
    assert listed_chunks[0]["text"] == "Comparison take 1."
    playback = client.get(listed_chunks[0]["url"])
    assert playback.status_code == 200
    assert playback.headers["content-type"].startswith("audio/wav")
    takes, active_id = service.tts.list_narration_takes(project.id)
    assert [take.settings["provider"] for take in takes] == ["chatterbox", "chatterbox"]
    assert [take.seed for take in takes] == [20001, 20002]
    assert active_id == takes[-1].id

    for stage in ("subtitles", "timeline", "render_preview", "render_final"):
        output = root / f"{stage}.marker"
        output.write_text(stage, encoding="utf-8")
        service._mark_stage(project, stage, [output], f"old-{stage}")
    selected = client.post(
        f"/api/projects/{project.id}/tts/narrations/{takes[0].id}/activate",
    )
    assert selected.status_code == 200
    assert selected.json()["active_asset_id"] == takes[0].id
    assert service.tts.list_narration_takes(project.id)[1] == takes[0].id
    gain = client.put(
        f"/api/projects/{project.id}/tts/narrations/{takes[0].id}/gain",
        json={"gain_db": 12},
    )
    assert gain.status_code == 200
    assert gain.json()["gain_db"] == 12
    assert service.tts.active_narration_gain(project.id) == 12
    listing = client.get(f"/api/projects/{project.id}/tts/narrations").json()
    assert listing["takes"][0]["gain_db"] == 12
    manifest = json.loads((root / "narration" / "takes.json").read_text(encoding="utf-8"))
    assert manifest["take_gains_db"] == {takes[0].id: 12.0}
    stages = service._read_stage_state(project)["stages"]
    assert stages["narration"]["job_id"] == f"activation:{takes[0].id}"
    assert not ({"subtitles", "timeline", "render_preview", "render_final"} & stages.keys())

    too_loud = client.put(
        f"/api/projects/{project.id}/tts/narrations/{takes[0].id}/gain",
        json={"gain_db": 25},
    )
    assert too_loud.status_code == 422

    other = service.create_project(ProjectCreate(
        title="Other Project", topic="test", target_duration=1,
    ))
    wrong_project = client.post(
        f"/api/projects/{other.id}/tts/narrations/{takes[0].id}/activate",
    )
    assert wrong_project.status_code == 404

    second_path = root / takes[1].filepath
    second_path.write_bytes(wav_bytes(frames=1600))
    tampered = client.post(
        f"/api/projects/{project.id}/tts/narrations/{takes[1].id}/activate",
    )
    assert tampered.status_code == 422
    assert "recorded hash" in tampered.text
    assert service.tts.list_narration_takes(project.id)[1] == takes[0].id


def test_canceled_narration_take_does_not_replace_active_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Canceled Take", topic="test", target_duration=1,
    ))
    service.registry.register(RecordingBackend(), name="chatterbox", replace=True)
    service.tts.generate(
        project.id,
        NarrationRequest(provider="chatterbox", text="Keep this take.", seed=10),
        job_id="initial",
    )
    root = service.store.project_path(project)
    master = root / "narration" / "master.wav"
    original_master = master.read_bytes()
    original_active = service.tts.list_narration_takes(project.id)[1]

    class CancelingBackend(RecordingBackend):
        def generate(self, request: GenerationRequest) -> GenerationResult:
            result = super().generate(request)
            service.jobs.cancel(request.job_id.split(":", 1)[0])
            return result

    service.registry.register(CancelingBackend(), name="chatterbox", replace=True)
    job = service.queue_narration(
        project.id,
        NarrationRequest(provider="chatterbox", text="Do not activate this.", seed=20),
    )

    output = service.run_narration_job(job.id)

    assert output.parent == root / "narration" / "takes" / "chatterbox"
    assert service.jobs.get(job.id).status.value == "canceled"
    assert master.read_bytes() == original_master
    takes, active_id = service.tts.list_narration_takes(project.id)
    assert len(takes) == 2
    assert active_id == original_active


def test_enhance_with_step_is_rejected_before_any_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Enhance", topic="test", target_duration=1,
    ))
    profile = service.tts.create_voice_profile(
        project.id, name="Owner", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    chatterbox = RecordingBackend()
    service.registry.register(chatterbox, name="chatterbox", replace=True)

    request = NarrationRequest(
        provider="chatterbox", voice_profile_id=profile.id, text="Hello.",
        enhance_with_step=True,
    )
    with pytest.raises(ValueError, match="Step enhancement is supported after Qwen"):
        service.tts.generate(project.id, request, job_id="job-enhance")
    assert chatterbox.calls == []


def test_api_rejects_step_enhancement_for_non_qwen_before_queueing(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    project = app.state.service.create_project(ProjectCreate(
        title="Enhance API", topic="test", target_duration=1,
    ))
    profile = app.state.service.tts.create_voice_profile(
        project.id, name="Owner", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{project.id}/tts/generate",
        json={
            "provider": "chatterbox", "voice_profile_id": profile.id, "text": "Hello.",
            "enhance_with_step": True,
        },
    )

    assert response.status_code == 422
    assert "Step enhancement is supported after Qwen" in response.text
    assert app.state.service.jobs.list(project.id) == []


def test_corrupt_voice_profiles_are_skipped_when_listing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    service = app.state.service
    project = service.create_project(ProjectCreate(
        title="Voices", topic="test", target_duration=1,
    ))
    healthy = service.tts.create_voice_profile(
        project.id, name="Healthy", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    broken_json = service.tts.create_voice_profile(
        project.id, name="Broken JSON", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    invalid_schema = service.tts.create_voice_profile(
        project.id, name="Invalid Schema", transcript="hello", language="en",
        authorized=True, audio=wav_bytes(),
    )
    voices_dir = service.store.project_path(project) / "voices"
    (voices_dir / broken_json.id / "profile.json").write_text("{not json", encoding="utf-8")
    (voices_dir / invalid_schema.id / "profile.json").write_text('{"name": 1}', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="backend.tts.manager"):
        listed = service.tts.list_voice_profiles(project.id)

    assert [item.id for item in listed] == [healthy.id]
    assert sum("Skipping unreadable voice profile" in record.getMessage() for record in caplog.records) == 2
    response = TestClient(app).get(f"/api/projects/{project.id}/tts/voices")
    assert response.status_code == 200
    assert [voice["id"] for voice in response.json()["voices"]] == [healthy.id]


def test_tts_service_timeout_maps_to_request_timeout(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = TTSServiceBackend("chatterbox", "http://127.0.0.1:8193", client_factory=factory)
    request = GenerationRequest(job_id="job", output_dir=tmp_path, prompt="Hello", seed=1)

    with pytest.raises(BackendError) as excinfo:
        backend.generate(request)
    assert excinfo.value.code is BackendErrorCode.REQUEST_TIMEOUT
    assert backend.cancel("job") is False


def test_tts_service_non_json_success_is_invalid_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/generate":
            return httpx.Response(200, text="accepted")
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, text="ok")
        raise AssertionError(request.url)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = TTSServiceBackend("chatterbox", "http://127.0.0.1:8193", client_factory=factory)
    request = GenerationRequest(job_id="job", output_dir=tmp_path, prompt="Hello", seed=1)

    with pytest.raises(BackendError) as excinfo:
        backend.generate(request)
    assert excinfo.value.code is BackendErrorCode.INVALID_RESPONSE
    assert backend.cancel("job") is False


def test_tts_service_http_error_surfaces_worker_detail(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={
            "detail": "Breeze API ended its audio stream before completion",
        })

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = TTSServiceBackend(
        "breeze_tts_2", "http://127.0.0.1:8195", client_factory=factory,
    )
    request = GenerationRequest(job_id="job", output_dir=tmp_path, prompt="Hello", seed=1)

    with pytest.raises(BackendError) as excinfo:
        backend.generate(request)
    assert excinfo.value.retryable is True
    assert "ended its audio stream" in str(excinfo.value)


def test_narration_stops_after_retryable_backend_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(
        title="Fail fast", topic="test", target_duration=1,
    ))

    class FailedWorkerBackend(RecordingBackend):
        def generate(self, request: GenerationRequest) -> GenerationResult:
            self.calls.append(request.job_id)
            raise BackendError(
                BackendErrorCode.INVALID_RESPONSE,
                "worker returned HTTP 500. inference failed",
                retryable=True,
            )

    backend = FailedWorkerBackend()
    service.registry.register(backend, name="chatterbox", replace=True)
    monkeypatch.setattr(service.tts, "_narration_chunks", lambda *_args: [
        {"text": "First chunk."}, {"text": "Second chunk."},
    ])
    request = NarrationRequest(provider="chatterbox", text="Two chunks.")
    job = service.queue_narration(project.id, request)

    with pytest.raises(RuntimeError, match="chunk 1.*inference failed"):
        service.tts.generate(
            project.id, request, job_id=job.id,
        )
    assert len(backend.calls) == 1


def test_worker_payload_derived_keys_beat_request_settings(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.update(payload)
        Path(payload["output_path"]).write_bytes(wav_bytes())
        return httpx.Response(200, json={"output_path": payload["output_path"], "metrics": {}})

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = TTSServiceBackend("chatterbox", "http://127.0.0.1:8193", client_factory=factory)
    settings = {
        "filename": "0001.wav",
        "output_path": str(tmp_path / "escape.wav"),
        "seed": 12345,
    }
    result = backend.generate(GenerationRequest(
        job_id="job", output_dir=tmp_path, prompt="Hello", seed=7, settings=settings,
    ))

    expected = (tmp_path / "0001.wav").resolve()
    assert seen["output_path"] == str(expected)
    assert seen["seed"] == 7
    assert result.outputs[0] == expected
    assert not (tmp_path / "escape.wav").exists()


class StubTTSBackend(GeneratorBackend):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.unload_calls: list[str] = []

    def descriptor(self):
        return BackendDescriptor(backend_name=self.provider, model_name=self.provider)

    def health(self):
        return {"status": "unhealthy"}

    def load(self):
        pass

    def unload(self):
        self.unload_calls.append("unload")

    def cancel(self, job_id: str) -> bool:
        return False

    def estimate_resources(self, request: GenerationRequest):
        return {}

    def generate(self, request: GenerationRequest) -> GenerationResult:
        raise AssertionError("stub should not generate")


def test_unload_tts_calls_backend_and_stops_isolated_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    backend = StubTTSBackend("qwen_tts")
    app.state.service.registry.register(backend, name="qwen_tts", replace=True)
    stopped: list[str] = []
    monkeypatch.setattr(app.state.service.tts_workers, "stop", lambda provider: (stopped.append(provider), True)[1])

    client = TestClient(app)
    response = client.post("/api/tts/qwen_tts/unload")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "qwen_tts"
    assert body["status"] == "unloaded"
    assert body["stopped_owned_worker"] is True
    assert backend.unload_calls == ["unload"]
    assert stopped == ["qwen_tts"]


def test_unload_tts_comfyui_adapter_calls_unload_but_does_not_stop_shared_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    backend = StubTTSBackend("fish_s2_pro")
    app.state.service.registry.register(backend, name="fish_s2_pro", replace=True)
    stopped: list[str] = []
    monkeypatch.setattr(app.state.service.tts_workers, "stop", lambda provider: (stopped.append(provider), True)[1])

    client = TestClient(app)
    response = client.post("/api/tts/fish_s2_pro/unload")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "fish_s2_pro"
    assert body["status"] == "unloaded"
    assert body["stopped_owned_worker"] is False
    assert backend.unload_calls == ["unload"]
    assert stopped == []


def test_unload_tts_unknown_provider_returns_503(tmp_path: Path) -> None:
    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    client = TestClient(app)
    response = client.post("/api/tts/does_not_exist/unload")
    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["code"] == "backend_unavailable"
    assert "does_not_exist" in body["detail"]["message"]
