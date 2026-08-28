"""Contract tests for ComfyUI voice-cloning TTS adapters (no weights loaded)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.models import GenerationRequest
from backend.models.errors import BackendError, BackendErrorCode
from backend.models.tts_comfyui import (
    FishS2ProBackend,
    IndexTTS25Backend,
    VoxCPM2Backend,
)
import backend.models.tts_comfyui as tts_comfyui_module


WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "workflows" / "comfyui" / "tts"

PROVIDER_STEMS = {
    IndexTTS25Backend: ("index-tts-2.5-clone", "4"),
    VoxCPM2Backend: ("voxcpm2-clone", "3"),
    FishS2ProBackend: ("fish-s2-pro-clone", "3"),
}


def _factory(handler):
    transport = httpx.MockTransport(handler)
    return lambda **kwargs: httpx.Client(transport=transport, **kwargs)


def _reference(tmp_path: Path) -> Path:
    path = tmp_path / "reference.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    return path


def _request(tmp_path: Path, *, seed: int = 20001, **settings) -> GenerationRequest:
    return GenerationRequest(
        job_id="job-1",
        output_dir=tmp_path / "out",
        prompt="Hello there.",
        seed=seed,
        references=(_reference(tmp_path),),
        settings={"language": "en", "reference_text": "Reference words.", **settings},
    )


def _success_outputs(node_id: str) -> dict:
    return {node_id: {"audio": [{"filename": "chunk_00001_.wav", "subfolder": "", "type": "output"}]}}


def _generate_handler(state: dict, *, view_size: int | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "reference.wav", "subfolder": "lvs"})
        if request.url.path == "/prompt":
            state["prompt"] = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "prompt-9"})
        if request.url.path.startswith("/history/"):
            if state.get("history_broken"):
                return httpx.Response(200, text="<html>not json</html>")
            return httpx.Response(200, json={
                "prompt-9": {
                    "status": {"status_str": state.get("status_str", "success")},
                    "outputs": state.get("outputs", {}),
                }
            })
        if request.url.path == "/view":
            size = view_size if view_size is not None else len(b"fake-wav")
            return httpx.Response(200, content=b"x" * size)
        raise AssertionError(request.url)

    return handler


def _backend(cls, handler, **kwargs):
    kwargs.setdefault("workflows_dir", WORKFLOWS_DIR)
    kwargs.setdefault("poll_interval", 0)
    return cls(client_factory=_factory(handler), **kwargs)


def _run_success(tmp_path, cls) -> tuple[dict, object]:
    stem, output_node = PROVIDER_STEMS[cls]
    state: dict = {"outputs": _success_outputs(output_node)}
    backend = _backend(cls, _generate_handler(state))
    result = backend.generate(_request(tmp_path))
    return state, result


@pytest.mark.parametrize("cls", list(PROVIDER_STEMS), ids=lambda cls: cls.PROVIDER)
def test_generation_submits_the_pinned_graph_and_returns_wav(tmp_path, cls):
    state, result = _run_success(tmp_path, cls)
    assert result.outputs[0].name == "chunk_00001_.wav"
    assert result.outputs[0].read_bytes() == b"x" * len(b"fake-wav")
    assert result.outputs[0].parent == tmp_path / "out"
    assert result.metadata["provider"] == cls.PROVIDER
    assert result.metadata["backend"] == cls.PROVIDER
    submitted = state["prompt"]
    expected_nodes = set(json.loads(
        (WORKFLOWS_DIR / f"{PROVIDER_STEMS[cls][0]}.workflow.json").read_text(encoding="utf-8")
    ))
    assert set(submitted) == expected_nodes


def test_index_template_substitutions_build_the_clone_graph(tmp_path):
    state: dict = {"outputs": _success_outputs("4")}
    backend = _backend(
        IndexTTS25Backend, _generate_handler(state), model_path="/central/IndexTTS-2.5",
    )
    result = backend.generate(_request(tmp_path))
    submitted = state["prompt"]
    assert submitted["1"]["inputs"]["custom_model_path"] == "/central/IndexTTS-2.5"
    assert submitted["1"]["inputs"]["release_after_run"] is False
    assert submitted["2"]["inputs"]["audio"] == "lvs/reference.wav"
    assert submitted["3"]["inputs"]["text"] == "Hello there."
    assert submitted["3"]["inputs"]["seed"] == 20001
    assert submitted["3"]["inputs"]["language"] == "EN"
    assert submitted["3"]["inputs"]["speaker_audio"] == ["2", 0]
    assert submitted["4"]["inputs"]["filename_prefix"].startswith("lvs/tts/index_tts_2_5/")
    assert result.outputs


def test_missing_reference_audio_fails_before_any_http_call(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500)

    backend = _backend(VoxCPM2Backend, handler)
    request = GenerationRequest(
        job_id="job", output_dir=tmp_path, prompt="text", seed=1, references=(),
        settings={"language": "en"},
    )
    with pytest.raises(BackendError) as excinfo:
        backend.generate(request)
    assert excinfo.value.code is BackendErrorCode.BACKEND_UNAVAILABLE
    assert calls == []


def test_seed_is_clamped_into_the_node_range(tmp_path):
    state: dict = {"outputs": _success_outputs("3")}
    backend = _backend(FishS2ProBackend, _generate_handler(state))
    huge_seed = 2**40 + 7
    result = backend.generate(_request(tmp_path, seed=huge_seed))
    submitted_seed = state["prompt"]["2"]["inputs"]["seed"]
    assert submitted_seed == huge_seed % (2**31)
    assert 0 <= submitted_seed <= FishS2ProBackend.SEED_MAX
    assert result.metadata["seed_submitted"] == submitted_seed


def test_fish_template_enables_compiled_generation_with_a_fixed_token_limit(tmp_path):
    state: dict = {"outputs": _success_outputs("3")}
    backend = _backend(FishS2ProBackend, _generate_handler(state))
    backend.generate(_request(tmp_path))

    inputs = state["prompt"]["2"]["inputs"]
    assert inputs["compile_model"] is True
    assert inputs["max_new_tokens"] == 1024


def test_outputs_are_limited_to_expected_node_and_category(tmp_path):
    state: dict = {
        "outputs": {
            "3": {"audio": [{"filename": "decoy.wav", "subfolder": "", "type": "output"}]},
            "4": {
                "images": [{"filename": "preview.png", "subfolder": "", "type": "output"}],
                "audio": [
                    {"filename": "b_second.wav", "subfolder": "", "type": "output"},
                    {"filename": "a_first.wav", "subfolder": "", "type": "output"},
                ],
            },
        }
    }
    backend = _backend(IndexTTS25Backend, _generate_handler(state))
    result = backend.generate(_request(tmp_path))
    assert [path.name for path in result.outputs] == ["a_first.wav", "b_second.wav"]
    assert all(path.parent == tmp_path / "out" for path in result.outputs)


def test_comfy_flac_output_is_converted_to_pcm_wav(tmp_path, monkeypatch):
    state: dict = {"outputs": _success_outputs("3")}
    state["outputs"]["3"]["audio"][0]["filename"] = "chunk.flac"
    backend = _backend(FishS2ProBackend, _generate_handler(state))

    def fake_run(argv):
        Path(argv[-1]).write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")

    monkeypatch.setattr(tts_comfyui_module, "require_ffmpeg", lambda: Path("ffmpeg"))
    monkeypatch.setattr(tts_comfyui_module, "run_media_process", fake_run)

    result = backend.generate(_request(tmp_path))

    assert result.outputs[0].name == "chunk.wav"
    assert result.outputs[0].read_bytes().startswith(b"RIFF")


def test_workflow_failure_status_is_invalid_response(tmp_path):
    state: dict = {
        "status_str": "error",
        "outputs": {},
    }
    backend = _backend(VoxCPM2Backend, _generate_handler(state))
    with pytest.raises(BackendError) as excinfo:
        backend.generate(_request(tmp_path))
    assert excinfo.value.code is BackendErrorCode.INVALID_RESPONSE


def test_completed_without_retrievable_audio_is_invalid_response(tmp_path):
    state: dict = {"outputs": {}}
    backend = _backend(FishS2ProBackend, _generate_handler(state))
    with pytest.raises(BackendError) as excinfo:
        backend.generate(_request(tmp_path))
    assert excinfo.value.code is BackendErrorCode.INVALID_RESPONSE


def test_oversized_output_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_comfyui_module, "_MAX_OUTPUT_BYTES", 8)
    state: dict = {"outputs": _success_outputs("4")}
    backend = IndexTTS25Backend(
        client_factory=_factory(_generate_handler(state, view_size=9)),
        workflows_dir=WORKFLOWS_DIR,
        poll_interval=0,
    )
    with pytest.raises(BackendError) as excinfo:
        backend.generate(_request(tmp_path))
    assert excinfo.value.code is BackendErrorCode.INVALID_RESPONSE


def test_timeout_is_flagged_retryable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "reference.wav", "subfolder": "lvs"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "prompt-9"})
        if request.url.path.startswith("/history/"):
            return httpx.Response(200, json={})
        raise AssertionError(request.url)

    backend = _backend(
        VoxCPM2Backend, handler, generation_timeout=0.05, poll_interval=0.001,
    )
    with pytest.raises(BackendError) as excinfo:
        backend.generate(_request(tmp_path))
    assert excinfo.value.code is BackendErrorCode.REQUEST_TIMEOUT
    assert excinfo.value.retryable


def test_malformed_history_response_is_invalid_response(tmp_path):
    state: dict = {"history_broken": True}
    backend = _backend(VoxCPM2Backend, _generate_handler(state))
    with pytest.raises(BackendError) as excinfo:
        backend.generate(_request(tmp_path))
    assert excinfo.value.code is BackendErrorCode.INVALID_RESPONSE


def test_voxcpm_controls_map_onto_node_fields(tmp_path):
    state: dict = {"outputs": _success_outputs("3")}
    backend = _backend(VoxCPM2Backend, _generate_handler(state))
    backend.generate(_request(
        tmp_path,
        voice_instruction="warm documentary host",
        guidance_scale=3.5,
        inference_timesteps=25,
    ))
    clone = state["prompt"]["2"]["inputs"]
    assert clone["cfg_value"] == 3.5
    assert clone["inference_timesteps"] == 25
    assert clone["voice_description"] == "warm documentary host"
    assert clone["enable_asr"] is False
    assert clone["force_offload"] is False
    assert clone["model_name"] == "VoxCPM2"
    assert clone["prompt_text"] == "Reference words."
    assert "language" not in clone


def test_voxcpm_defaults_apply_when_controls_are_unset(tmp_path):
    state: dict = {"outputs": _success_outputs("3")}
    backend = _backend(VoxCPM2Backend, _generate_handler(state))
    backend.generate(_request(tmp_path))
    clone = state["prompt"]["2"]["inputs"]
    assert clone["cfg_value"] == 2.0
    assert clone["inference_timesteps"] == 10
    assert clone["voice_description"] == ""


def test_fish_keeps_model_loaded_between_narration_chunks(tmp_path):
    state: dict = {"outputs": _success_outputs("3")}
    backend = _backend(FishS2ProBackend, _generate_handler(state))

    backend.generate(_request(tmp_path))

    assert state["prompt"]["2"]["inputs"]["keep_model_loaded"] is True


def test_readiness_reports_missing_nodes_against_object_info():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.33.0"}})
        if request.url.path == "/object_info":
            return httpx.Response(200, json={"LoadAudio": {}, "SaveAudio": {}})
        raise AssertionError(request.url)

    backend = _backend(IndexTTS25Backend, handler)
    report = backend.readiness()
    assert report["comfyui_healthy"] is True
    assert report["ready"] is False
    assert "T8_IndexTTS25_ModelLoader" in report["missing_nodes"]
    assert "T8_IndexTTS25_Generate" in report["missing_nodes"]


def test_readiness_reports_unhealthy_comfyui():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    backend = _backend(FishS2ProBackend, handler)
    report = backend.readiness()
    assert report["comfyui_healthy"] is False
    assert report["ready"] is False


def test_readiness_flags_a_missing_template(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}})
        if request.url.path == "/object_info":
            return httpx.Response(200, json={})
        raise AssertionError(request.url)

    backend = _backend(FishS2ProBackend, handler, workflows_dir=tmp_path)
    report = backend.readiness()
    assert report["template_present"] is False
    assert report["ready"] is False


@pytest.mark.parametrize("cls", list(PROVIDER_STEMS), ids=lambda cls: cls.PROVIDER)
def test_templates_are_well_formed_and_match_metadata(cls):
    stem = PROVIDER_STEMS[cls][0]
    workflow = json.loads((WORKFLOWS_DIR / f"{stem}.workflow.json").read_text(encoding="utf-8"))
    metadata = json.loads((WORKFLOWS_DIR / f"{stem}.metadata.json").read_text(encoding="utf-8"))
    class_types = {node["class_type"] for node in workflow.values()}
    assert class_types == set(metadata["required_nodes"])
    assert metadata["output_node_id"] in workflow
    assert workflow[metadata["output_node_id"]]["class_type"] in metadata["required_nodes"]
    assert metadata["capability"] == "text_to_speech"
    assert metadata["verified"] is False
    assert metadata["safety"]["no_auto_download"] is True
    assert metadata["node_commit_sha"]

    declared = {item["key"] for item in metadata["substitutions"]}
    used: set[str] = set()
    for node in workflow.values():
        for value in node["inputs"].values():
            if isinstance(value, str):
                used |= {
                    token[2:-2] for token in value.split()
                    if token.startswith("{{") and token.endswith("}}")
                }
    assert used <= declared, f"undeclared placeholders: {used - declared}"
