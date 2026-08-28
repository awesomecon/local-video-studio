"""Mocked contract tests for the four-model voice-cloning comparison providers."""

from __future__ import annotations

import io
import json
import sys
import time
import wave
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.core import load_config
from backend.models import (
    BackendDescriptor,
    GenerationRequest,
    GenerationResult,
    GeneratorBackend,
)
from backend.models.registry import BackendRegistry
from backend.schemas import ProjectCreate, Scene, VisualType
from backend.tts.models import NarrationRequest
from services.tts_worker.app import BreezeProvider, GeneratePayload, OmniVoiceProvider

COMPARISON_PROVIDERS = ("fish_s2_pro", "voxcpm2", "omnivoice", "index_tts_2_5", "breeze_tts_2")


def wav_bytes(*, frames: int = 800, sample_rate: int = 8000, sample: int = 0) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(sample.to_bytes(2, byteorder="little", signed=True) * frames)
    return target.getvalue()


class RecordingComparisonBackend(GeneratorBackend):
    """In-memory stand-in so tests never download models or start workers."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls: list[str] = []
        self.settings: list[dict] = []
        self.load_calls = 0
        self.unload_calls = 0

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_name=self.provider,
            model_name=f"Fake {self.provider}",
            model_version="test",
            quantization="bf16",
        )

    def health(self) -> dict:
        return {"status": "healthy"}

    def load(self) -> None:
        self.load_calls += 1

    def unload(self) -> None:
        self.unload_calls += 1

    def cancel(self, job_id: str) -> bool:
        return False

    def estimate_resources(self, request: GenerationRequest) -> dict:
        return {}

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request.job_id)
        self.settings.append(dict(request.settings))
        output = request.output_dir / str(request.settings["filename"])
        output.write_bytes(wav_bytes(sample=1000))
        return GenerationResult(outputs=(output,), metadata={
            "backend": self.provider,
            "provider": self.provider,
            "model": f"Fake {self.provider}",
            "model_version": "test",
            "workflow_version": "test-v1",
            "node_commit_sha": "0" * 40,
            "quantization": "bf16",
        })


def narration_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from backend.api.main import create_app

    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    service = app.state.service
    monkeypatch.setattr(service.tts_workers, "ensure_running", lambda provider: False)
    monkeypatch.setattr(service.tts_workers, "stop", lambda provider: False)
    return service


def authorized_profile(service, project_id: str) -> str:
    profile = service.tts.create_voice_profile(
        project_id,
        name="Narrator",
        transcript="Reference words.",
        language="en",
        authorized=True,
        audio=wav_bytes(),
    )
    return profile.id


# ---------------------------------------------------------------------------
# Contracts


def test_narration_request_accepts_comparison_providers_with_controls() -> None:
    assert NarrationRequest(provider="breeze_tts_2").breeze_mode == "eager"
    for provider in COMPARISON_PROVIDERS:
        request = NarrationRequest(
            provider=provider, voice_profile_id="p", text="Hello.",
            guidance_scale=2.5, inference_timesteps=20, num_steps=32, speed=1.0,
        )
        assert request.provider == provider
    with pytest.raises(ValidationError):
        NarrationRequest(provider="unknown_model")
    with pytest.raises(ValidationError):
        NarrationRequest(provider="voxcpm2", guidance_scale=-1)
    with pytest.raises(ValidationError):
        NarrationRequest(provider="omnivoice", speed=5.0)


def test_comparison_providers_require_an_authorized_voice_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(title="Consent", topic="t", target_duration=1))
    for provider in COMPARISON_PROVIDERS:
        with pytest.raises(ValueError, match="requires an authorized reference voice"):
            service.tts.generate(
                project.id,
                NarrationRequest(provider=provider, voice_profile_id=None, text="Hi."),
                job_id=f"job-{provider}",
            )


@pytest.mark.parametrize("provider", COMPARISON_PROVIDERS)
def test_comparison_narration_flow_persists_chunks_and_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(
        ProjectCreate(title=f"Flow {provider}", topic="t", target_duration=2,
                      resolution=(160, 90)),
    )
    scenes = [
        Scene(project_id=project.id, index=index, duration=1, narration=text,
              visual_type=VisualType.FLUX_STILL)
        for index, text in enumerate(("First scene line.", "Second scene line."))
    ]
    for scene in scenes:
        service.database.save_scene(scene)
    profile_id = authorized_profile(service, project.id)
    backend = RecordingComparisonBackend(provider)
    service.registry.register(backend, name=provider, replace=True)

    service.tts.generate(
        project.id,
        NarrationRequest(provider=provider, voice_profile_id=profile_id, text=None, seed=77),
        job_id="flow-job",
    )

    root = service.store.project_path(project)
    takes, active_id = service.tts.list_narration_takes(project.id)
    take = next(item for item in takes if item.id == active_id)
    assert (root / "narration" / "master.wav").is_file()
    chunks = service.tts.list_take_chunks(project.id, take.id)
    assert [chunk["text"] for chunk in chunks] == [scene.narration for scene in scenes]
    assert all(chunk["seed"] == 77 + position for position, chunk in enumerate(chunks))
    # Chunk sidecars persist the reproducibility metadata from the result.
    sidecar_dirs = list((root / "audio").glob(f"*/*/"))
    assert any(directory.name == "flow-job" for directory in sidecar_dirs)
    sidecars = sorted((root / "audio").rglob("0001.json"))
    payload = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert payload["provider"] == provider
    assert payload["node_commit_sha"] == "0" * 40
    assert payload["workflow_version"] == "test-v1"
    assert payload["voice_profile_id"] == profile_id
    assert backend.load_calls == 1
    assert backend.unload_calls == 1
    # Asset rows carry provider, checkpoint revision, and workflow version.
    assert take.backend == provider
    assert take.settings["request"]["voice_profile_id"] == profile_id


def test_chunk_regeneration_survives_for_comparison_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = narration_service(tmp_path, monkeypatch)
    project = service.create_project(ProjectCreate(title="Retry", topic="t", target_duration=2))
    scenes = [
        Scene(project_id=project.id, index=index, duration=1, narration=text)
        for index, text in enumerate(("Keep this.", "Replace this."))
    ]
    for scene in scenes:
        service.database.save_scene(scene)
    profile_id = authorized_profile(service, project.id)
    backend = RecordingComparisonBackend("index_tts_2_5")
    service.registry.register(backend, name="index_tts_2_5", replace=True)
    service.tts.generate(
        project.id,
        NarrationRequest(provider="index_tts_2_5", voice_profile_id=profile_id, text=None),
        job_id="original",
    )
    original_takes, original_active = service.tts.list_narration_takes(project.id)

    job = service.tts.queue_chunk_regeneration(project.id, original_active, 2)
    service.tts.run_chunk_regeneration_job(job.id)

    takes, active_id = service.tts.list_narration_takes(project.id)
    assert len(takes) == len(original_takes) + 1
    assert active_id != original_active
    assert backend.calls == ["original:1", "original:2", f"{job.id}:2"]


@pytest.mark.parametrize("provider", ("fish_s2_pro", "voxcpm2", "index_tts_2_5"))
def test_comfyui_tts_generation_does_not_require_a_managed_worker(
    tmp_path: Path, provider: str,
) -> None:
    from backend.api.main import create_app

    app = create_app(
        load_config(environ={}), database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    service = app.state.service
    assert provider not in service.tts_workers.specs
    project = service.create_project(
        ProjectCreate(title=f"External {provider}", topic="t", target_duration=1),
    )
    profile_id = authorized_profile(service, project.id)
    backend = RecordingComparisonBackend(provider)
    service.registry.register(backend, name=provider, replace=True)

    output = service.tts.generate(
        project.id,
        NarrationRequest(provider=provider, voice_profile_id=profile_id, text="Hello."),
        job_id=f"external-{provider}",
    )

    assert output.is_file()
    _, active_id = service.tts.list_narration_takes(project.id)
    regeneration = service.tts.queue_chunk_regeneration(project.id, active_id, 1)
    regenerated = service.tts.run_chunk_regeneration_job(regeneration.id)
    assert regenerated.is_file()
    assert backend.calls == [f"external-{provider}:1", f"{regeneration.id}:1"]


def test_externally_started_service_tts_does_not_require_a_managed_worker(
    tmp_path: Path,
) -> None:
    from backend.api.main import create_app

    config = load_config(environ={
        "LOCAL_VIDEO_STUDIO__BACKENDS__QWEN_TTS__MANAGED": "false",
    })
    app = create_app(
        config, database_path=tmp_path / "studio.sqlite3",
        project_root=tmp_path / "projects", temp_root=tmp_path / "tmp", mock_mode=True,
    )
    service = app.state.service
    assert "qwen_tts" not in service.tts_workers.specs
    project = service.create_project(
        ProjectCreate(title="External Qwen", topic="t", target_duration=1),
    )
    backend = RecordingComparisonBackend("qwen_tts")
    service.registry.register(backend, name="qwen_tts", replace=True)

    output = service.tts.generate(
        project.id, NarrationRequest(provider="qwen_tts", text="Hello."),
        job_id="external-qwen",
    )

    assert output.is_file()
    assert backend.calls == ["external-qwen:1"]


# ---------------------------------------------------------------------------
# Discovery and configuration


def test_registry_registers_all_eight_tts_providers() -> None:
    config = load_config(environ={})
    registry = BackendRegistry.from_config(config.model_dump(mode="python"), mock_mode=True)
    names = set(registry.names())
    for name in (*COMPARISON_PROVIDERS, "qwen_tts", "step_audio_editx", "chatterbox"):
        assert name in names
    from backend.models.base import Capability

    for name in COMPARISON_PROVIDERS:
        descriptor = registry.get(name).descriptor()
        assert Capability.TEXT_TO_SPEECH in descriptor.capabilities


def test_registry_uses_fish_model_preset_instead_of_generic_ace_default() -> None:
    config = load_config(environ={})
    registry = BackendRegistry.from_config(config.model_dump(mode="python"), mock_mode=False)

    backend = registry.get("fish_s2_pro")

    assert backend._model_preset == "s2-pro"


def test_default_config_discovers_comparison_backends_and_reserves_port() -> None:
    config = load_config(environ={})
    assert config.backends.omnivoice.endpoint == "http://127.0.0.1:8194"
    assert config.backends.omnivoice.managed is True
    assert config.backends.omnivoice.model_path is not None
    assert config.backends.index_tts_2_5.model_path is not None
    assert 8194 in config.ports.reserved


def test_non_loopback_comparison_endpoint_is_rejected() -> None:
    from backend.core.config import ConfigurationError

    environ = {
        "LOCAL_VIDEO_STUDIO__BACKENDS__OMNIVOICE__ENDPOINT": "http://10.0.0.5:8194",
    }
    with pytest.raises(ConfigurationError):
        load_config(environ=environ)


def test_managed_worker_supervisor_discovers_omnivoice(tmp_path: Path) -> None:
    from backend.workers.tts_processes import TTSWorkerSupervisor

    model_path = tmp_path / "OmniVoice"
    model_path.mkdir()
    config = load_config(environ={
        "LOCAL_VIDEO_STUDIO__BACKENDS__OMNIVOICE__MODEL_PATH": str(model_path),
    })
    supervisor = TTSWorkerSupervisor.from_config(
        config, output_root=Path("/tmp/lvs-out"),
    )
    assert "omnivoice" in supervisor.specs
    spec = supervisor.specs["omnivoice"]
    assert spec.endpoint == "http://127.0.0.1:8194"
    assert spec.model_path == model_path
    assert spec.model_path.is_dir()


def test_supervisor_rejects_port_conflicts_between_managed_providers() -> None:
    from backend.workers.tts_processes import TTSWorkerSpec, TTSWorkerSupervisor

    specs = {
        "qwen_tts": TTSWorkerSpec(
            provider="qwen_tts", endpoint="http://127.0.0.1:8191",
            python_path=Path("/bin/python"), model_path=Path("/tmp"), tokenizer_path=None,
            startup_timeout_seconds=15,
        ),
        "omnivoice": TTSWorkerSpec(
            provider="omnivoice", endpoint="http://127.0.0.1:8191",
            python_path=Path("/bin/python"), model_path=Path("/tmp"), tokenizer_path=None,
            startup_timeout_seconds=15,
        ),
    }
    with pytest.raises(ValueError, match="port 8191"):
        TTSWorkerSupervisor(specs, output_root=Path("/tmp/lvs-out"),
                            cache_root=Path("/tmp/lvs-cache"), log_root=Path("/tmp/lvs-logs"))


# ---------------------------------------------------------------------------
# OmniVoice isolated worker


class FakeOmniVoiceModel:
    sampling_rate = 24000

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return [[0.0] * 100]


def test_omnivoice_worker_maps_payload_onto_official_api(tmp_path: Path) -> None:
    provider = OmniVoiceProvider(tmp_path / "OmniVoice")
    model = FakeOmniVoiceModel()
    provider.model = model
    reference = tmp_path / "reference.wav"
    reference.write_bytes(wav_bytes())

    waveform, sample_rate = provider._generate(GeneratePayload(
        job_id="job-1", text="Cloned speech.", output_path=tmp_path / "out.wav",
        reference_audio=reference, reference_text="Reference words.",
        language="en", seed=11, num_step=16, guidance_scale=3.0, speed=0.95,
    ))

    assert sample_rate == 24000
    assert len(waveform) == 100
    call = model.calls[0]
    assert call["text"] == "Cloned speech."
    assert call["ref_audio"] == str(reference)
    assert call["ref_text"] == "Reference words."
    assert call["language"] == "en"
    assert call["num_step"] == 16
    assert call["guidance_scale"] == 3.0
    assert call["speed"] == 0.95


def test_omnivoice_worker_requires_a_reference_file(tmp_path: Path) -> None:
    provider = OmniVoiceProvider(tmp_path / "OmniVoice")
    provider.model = FakeOmniVoiceModel()
    payload = GeneratePayload(
        job_id="job-1", text="No reference.", output_path=tmp_path / "out.wav",
        reference_audio=None,
    )
    with pytest.raises(ValueError, match="reference WAV"):
        provider.generate(payload)


def test_worker_cli_registers_the_fourth_provider() -> None:
    import services.tts_worker.app as worker_app

    assert "omnivoice" in worker_app.ProviderName.__args__
    assert "breeze_tts_2" in worker_app.ProviderName.__args__


# ---------------------------------------------------------------------------
# Breeze TTS 2 thin worker (official breeze_infer.api child)


class _DummyChild:
    def __init__(self) -> None:
        self.terminated = False
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _fake_breeze_api(port: int, recorder: list, fail_first_409: bool = False):
    """In-test stand-in implementing the pinned official API contract.

    Uses raw Starlette (not FastAPI): FastAPI's parameter injection
    (``UploadFile`` / ``Request``) is flaky under pytest in this environment,
    while raw ``request.form()`` (python-multipart) parses the provider's
    multipart body reliably. The official API's contract is simply "a
    ``ref_audio`` file part in the multipart body", which is what this
    verifies.
    """
    import threading

    import numpy as np
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse, Response

    state = {"first": True}

    async def health(request) -> JSONResponse:
        return JSONResponse({"status": "ok", "sample_rate": 24000})

    async def speech(request) -> Response:
        form = await request.form()
        if fail_first_409 and state["first"]:
            state["first"] = False
            return Response(status_code=409, content=b'{"detail": "busy"}',
                            media_type="application/json")
        ref_audio = form.get("ref_audio")
        ref_bytes = len(ref_audio.file.read()) if ref_audio is not None else 0
        recorder.append({
            "text": str(form.get("text", "")),
            "instruction": str(form.get("instruction", "")),
            "cfg_scale": float(form.get("cfg_scale", 1.0)),
            "ref_text": str(form.get("ref_text", "")),
            "seed": int(form.get("seed", 42)),
            "ref_bytes": ref_bytes,
        })
        return Response(
            content=np.arange(64, dtype=np.int16).tobytes(),
            media_type="audio/pcm", headers={"X-Sample-Rate": "24000"},
        )

    app = Starlette(routes=[
        Route("/health", health),
        Route("/v1/audio/speech", speech, methods=["POST"]),
    ])

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    import urllib.request

    deadline = time.time() + 15
    last_error = "no response yet"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return server
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"fake Breeze API did not start: {last_error}")


def _breeze_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str):
    import services.tts_worker.app as worker_app

    # Breeze is an HTTP child-service adapter; the parent worker must not need
    # PyTorch merely to proxy its generated waveform.
    monkeypatch.setitem(sys.modules, "torch", None)
    source = tmp_path / "breeze-source"
    (source / "breeze_infer").mkdir(parents=True)
    (source / "breeze_infer" / "api.py").write_text("# pinned fake\n", encoding="utf-8")
    model_dir = tmp_path / "Breeze-TTS-2"
    model_dir.mkdir()
    (model_dir / "lvs-pinned-revision.json").write_text(
        json.dumps({"revision": "c1c8ca18" * 4}), encoding="utf-8",
    )
    port = _free_port()
    env = "LVS_BREEZE_EAGER_PORT" if mode == "eager" else "LVS_BREEZE_FAST_PORT"
    monkeypatch.setenv("LVS_BREEZE_TTS_SOURCE", str(source))
    monkeypatch.setenv("LVS_BREEZE_TTS_SHA", "ca632ce6c4d05f7985da4eab29b1a5d445b43f7b")
    monkeypatch.setenv(env, str(port))
    recorder: list[dict] = []
    server = _fake_breeze_api(port, recorder)
    provider = BreezeProvider(model_dir)
    provider.load()
    children: list[_DummyChild] = []

    def spawn(command: list[str], environment: dict | None = None) -> _DummyChild:
        child = _DummyChild()
        child.command = command
        children.append(child)
        return child

    monkeypatch.setattr(provider, "_spawn", spawn)
    monkeypatch.setattr(provider, "_free_vram_gb", lambda: 30.0)
    return {
        "provider": provider, "recorder": recorder, "children": children,
        "server": server, "port": port,
    }


def test_breeze_worker_maps_payload_onto_official_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _breeze_fixture(tmp_path, monkeypatch, "eager")
    provider: BreezeProvider = fixture["provider"]
    reference = tmp_path / "reference.wav"
    reference.write_bytes(wav_bytes())

    payload = GeneratePayload(
        job_id="job-1", text="Cloned narration.", output_path=tmp_path / "out.wav",
        reference_audio=reference, reference_text="Exact reference words.",
        language="en", seed=20001, breeze_mode="eager",
    )
    result = provider.generate(payload)
    assert Path(result["output_path"]).suffix == ".wav"
    metrics = result["metrics"]
    assert metrics["breeze_mode"] == "eager"
    assert metrics["breeze_cfg_scale_used"] == 1.0  # plain clone default
    assert metrics["breeze_code_revision"] != "unrecorded"
    assert metrics["breeze_hf_revision"] == "c1c8ca18" * 4
    call = fixture["recorder"][0]
    assert call["text"] == "Cloned narration."
    assert call["instruction"] == "Speak clearly and naturally."
    assert call["ref_text"] == "Exact reference words."
    assert call["seed"] == 20001
    assert call["ref_bytes"] > 0
    provider.unload()
    assert fixture["children"][-1].terminated


def test_breeze_cfg_auto_rule_and_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _breeze_fixture(tmp_path, monkeypatch, "fast")
    provider: BreezeProvider = fixture["provider"]
    reference = tmp_path / "reference.wav"
    reference.write_bytes(wav_bytes())

    direction = GeneratePayload(
        job_id="job-2", text="Directed.", output_path=tmp_path / "a.wav",
        reference_audio=reference, reference_text="Ref.", seed=1,
        voice_instruction="calm, documentary narrator pace",
    )
    provider.generate(direction)
    assert provider._active_mode == "fast"
    command = fixture["children"][-1].command
    assert "--fast-all" not in command
    assert "--fast-backbone-prefill" not in command
    assert "--fast-text-encoder" in command
    assert "--fast-backbone-decode" in command
    assert "--fast-depth-decoder" in command
    assert "--fast-codec" in command
    assert fixture["recorder"][-1]["cfg_scale"] == 4.0  # direction present
    assert fixture["recorder"][-1]["instruction"] == "calm, documentary narrator pace"

    explicit = GeneratePayload(
        job_id="job-3", text="Explicit cfg.", output_path=tmp_path / "b.wav",
        reference_audio=reference, reference_text="Ref.", seed=2,
        voice_instruction="calm, documentary narrator pace", guidance_scale=2.5,
    )
    provider.generate(explicit)
    assert fixture["recorder"][-1]["cfg_scale"] == 2.5  # user override wins


def test_breeze_unknown_mode_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _breeze_fixture(tmp_path, monkeypatch, "eager")
    provider: BreezeProvider = fixture["provider"]
    reference = tmp_path / "reference.wav"
    reference.write_bytes(wav_bytes())
    with pytest.raises(ValueError, match="breeze_mode"):
        provider._generate(GeneratePayload(
            job_id="job-4", text="Turbo.", output_path=tmp_path / "c.wav",
            reference_audio=reference, reference_text="Ref.", seed=3,
            breeze_mode="turbo",
        ))


def test_breeze_fast_mode_refused_without_vram_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _breeze_fixture(tmp_path, monkeypatch, "fast")
    provider: BreezeProvider = fixture["provider"]
    monkeypatch.setattr(provider, "_free_vram_gb", lambda: 8.0)
    with pytest.raises(RuntimeError, match="eager"):
        provider._generate(GeneratePayload(
            job_id="job-5", text="Fast but no VRAM.", output_path=tmp_path / "d.wav",
            reference_audio=None, reference_text="", seed=4, breeze_mode="fast",
        ))


def test_breeze_engine_swap_keeps_one_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    eager = _breeze_fixture(tmp_path, monkeypatch, "eager")
    provider: BreezeProvider = eager["provider"]
    reference = tmp_path / "reference.wav"
    reference.write_bytes(wav_bytes())
    provider._generate(GeneratePayload(
        job_id="job-6a", text="Eager take.", output_path=tmp_path / "e.wav",
        reference_audio=reference, reference_text="Ref.", seed=5, breeze_mode="eager",
    ))
    fast_port = _free_port()
    monkeypatch.setenv("LVS_BREEZE_FAST_PORT", str(fast_port))
    fast_recorder: list[dict] = []
    server = _fake_breeze_api(fast_port, fast_recorder)
    provider._generate(GeneratePayload(
        job_id="job-6b", text="Fast take.", output_path=tmp_path / "f.wav",
        reference_audio=reference, reference_text="Ref.", seed=6, breeze_mode="fast",
    ))
    assert provider._active_mode == "fast"
    assert len(eager["children"]) == 2  # one per engine, swapped
    assert eager["children"][0].terminated  # eager child stopped
    assert fast_recorder[0]["text"] == "Fast take."
    server.should_exit = True


def test_breeze_retries_single_flight_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.tts_worker.app as worker_app  # noqa: F401

    source = tmp_path / "breeze-source"
    (source / "breeze_infer").mkdir(parents=True)
    (source / "breeze_infer" / "api.py").write_text("# pinned fake\n", encoding="utf-8")
    model_dir = tmp_path / "Breeze-TTS-2"
    model_dir.mkdir()
    port = _free_port()
    monkeypatch.setenv("LVS_BREEZE_TTS_SOURCE", str(source))
    monkeypatch.setenv("LVS_BREEZE_FAST_PORT", str(port))
    recorder: list[dict] = []
    _fake_breeze_api(port, recorder, fail_first_409=True)
    provider = BreezeProvider(model_dir)
    provider.load()
    child = _DummyChild()
    monkeypatch.setattr(provider, "_spawn", lambda command, environment=None: child)
    monkeypatch.setattr(provider, "_free_vram_gb", lambda: 30.0)
    reference = tmp_path / "reference.wav"
    reference.write_bytes(wav_bytes())
    provider._generate(GeneratePayload(
        job_id="job-7", text="Retried after 409.", output_path=tmp_path / "g.wav",
        reference_audio=reference, reference_text="Ref.", seed=7,
    ))
    assert recorder[-1]["text"] == "Retried after 409."
