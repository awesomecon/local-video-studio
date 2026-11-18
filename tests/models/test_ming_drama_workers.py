"""Speech adapters are exercised without weights, network or a CUDA runtime."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.config import AppConfig, load_config
from backend.models.tts_service import TTSServiceBackend
from backend.tts.performance import validate_tagged
from backend.workers.tts_processes import TTSWorkerSupervisor
from scripts.setup_drama_ming import GIB, require_space
from services.tts_worker import ming_drama
from services.tts_worker.app import GeneratePayload
from services.tts_worker.ming_drama import MingSmallProvider, MingTinyProvider, validate_bundle


def payload(tmp_path: Path, **changes) -> GeneratePayload:
    return GeneratePayload(job_id="speech", text="Hello world.", output_path=tmp_path / "out.wav", **changes)


class FakeMing:
    sample_rate = 44100

    def synthesize(self, **kwargs):
        self.kwargs = kwargs
        return [0.1, 0.2]

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(waveform=[0.1, 0.2])


def test_small_uses_its_own_api_and_voice_design_contract(tmp_path: Path) -> None:
    provider = MingSmallProvider(tmp_path)
    provider.model = FakeMing()
    audio, sr = provider._generate(payload(tmp_path, ming_task="voice_design", voice_instruction="Warm narrator"))
    assert sr == 44100 and audio
    assert provider.model.kwargs["task"] == "VOICE_DESIGN"
    assert provider.model.kwargs["controls"] == {"style": "Warm narrator"}
    assert "instruction" not in provider.model.kwargs


@pytest.mark.parametrize("language,task", [("en", "seed"), ("ja", "multilingual")])
def test_tiny_cloning_maps_language_and_transcript(tmp_path: Path, language: str, task: str) -> None:
    provider = MingTinyProvider(tmp_path)
    provider.model = FakeMing()
    provider._generate(payload(tmp_path, reference_audio=tmp_path / "ref.wav", reference_text="Reference.", language=language, seed=42))
    kwargs = provider.model.kwargs
    assert kwargs["task"] == task
    assert kwargs["seed"] == 42
    assert kwargs["prompt_text"] == "Reference."
    assert kwargs["target_language"] == language


def test_tiny_freeform_does_not_send_forbidden_transcript(tmp_path: Path) -> None:
    provider = MingTinyProvider(tmp_path)
    provider.model = FakeMing()
    provider._generate(payload(tmp_path, ming_task="freeform", voice_instruction="Speak gently",
                               reference_audio=tmp_path / "ref.wav", reference_text="Reference."))
    assert provider.model.kwargs["instruction"] == "Speak gently"
    assert provider.model.kwargs["prompt_text"] is None
    assert provider.model.kwargs["target_language"] is None


def test_tiny_without_reference_uses_builtin_voice(tmp_path: Path) -> None:
    provider = MingTinyProvider(tmp_path)
    provider.model = FakeMing()
    provider._generate(payload(tmp_path))
    assert provider.model.kwargs["task"] == "ip"
    assert provider.model.kwargs["ip"] == "US_Emma"
    assert provider.model.kwargs["target_language"] is None


@pytest.mark.parametrize("cls", [MingSmallProvider, MingTinyProvider])
def test_voice_design_rejects_reference_and_requires_direction(tmp_path: Path, cls) -> None:
    provider = cls(tmp_path)
    with pytest.raises(ValueError, match="instruction"):
        provider._generate(payload(tmp_path, ming_task="voice_design"))
    with pytest.raises(ValueError, match="deselect"):
        provider._generate(payload(tmp_path, ming_task="voice_design", voice_instruction="Warm",
                                   reference_audio=tmp_path / "ref.wav"))


def test_missing_bundle_fails_before_gpu_or_model_import(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ming_drama, "require_vram", lambda _: pytest.fail("must not inspect GPU"))
    with pytest.raises(FileNotFoundError, match="Incomplete"):
        MingTinyProvider(tmp_path).load()


def test_bundle_rejects_missing_shard_and_wrong_revision(tmp_path: Path) -> None:
    repo, revision, _ = ming_drama.RELEASES["ming_tts_tiny"]
    receipt = {"repo": repo, "revision": revision, "files": [{"path": "weights", "size": 1}]}
    (tmp_path / "lvs-release.json").write_text(json.dumps(receipt))
    with pytest.raises(FileNotFoundError, match="incomplete"):
        validate_bundle(tmp_path, "ming_tts_tiny")
    receipt["revision"] = "different"
    (tmp_path / "lvs-release.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="pinned"):
        validate_bundle(tmp_path, "ming_tts_tiny")


def test_storage_budget_accounts_for_download_not_just_current_free(tmp_path: Path, monkeypatch) -> None:
    import scripts.setup_drama_ming as installer

    monkeypatch.setattr(installer.shutil, "disk_usage", lambda _: SimpleNamespace(free=60 * GIB))
    require_space(tmp_path, 9 * GIB)
    with pytest.raises(RuntimeError, match="50 GiB"):
        require_space(tmp_path, 11 * GIB)


def test_three_workers_share_python_but_have_distinct_ports() -> None:
    config = load_config(environ={})
    supervisor = TTSWorkerSupervisor.from_config(config, output_root=Path("/tmp/projects"))
    names = ("fish_drama_3", "ming_tts_tiny", "ming_tts_05b")
    assert len({supervisor.specs[name].endpoint for name in names}) == 3
    assert supervisor.specs["ming_tts_tiny"].python_path == supervisor.specs["ming_tts_05b"].python_path
    command = supervisor._command(supervisor.specs["fish_drama_3"])
    assert "--runtime-path" in command and "--runtime-port" in command


def test_remote_ming_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="localhost"):
        AppConfig(backends={"ming_tts_tiny": {"enabled": True, "endpoint": "https://example.com"}})


def test_drama_performance_script_uses_square_bracket_rules() -> None:
    assert not validate_tagged("Hello there.", "[whisper] Hello there.", "fish_drama_3")
    assert validate_tagged("Hello there.", "[whisper] Goodbye there.", "fish_drama_3")


def test_missing_installation_is_reported_without_http(tmp_path: Path) -> None:
    backend = TTSServiceBackend("ming_tts_tiny", "http://127.0.0.1:8203", local_config={
        "enabled": True, "model_path": str(tmp_path / "missing"),
    }, client_factory=lambda **_: pytest.fail("must not call HTTP"))
    assert backend.health()["status"] == "not_installed"


def test_cancel_targets_only_the_narration_jobs_current_chunk() -> None:
    from backend.tts.manager import TTSManager

    calls = []
    backend = SimpleNamespace(cancel=lambda chunk: calls.append(chunk))
    manager = TTSManager(SimpleNamespace())
    manager._active_speech["job"] = (backend, "job:2")
    manager.cancel_active("other")
    assert not calls
    manager.cancel_active("job")
    assert calls == ["job:2"]


def test_canceled_worker_job_does_not_load_a_model(tmp_path: Path, monkeypatch) -> None:
    provider = MingTinyProvider(tmp_path)
    monkeypatch.setattr(provider, "load", lambda: pytest.fail("must not load"))
    provider.cancel("speech")
    with pytest.raises(ValueError, match="canceled"):
        provider.generate(payload(tmp_path))
