"""Tests for ACEStepComfyUIBackend."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from backend.models import ACEStepComfyUIBackend, Capability, GenerationRequest
from backend.models.errors import BackendError, BackendErrorCode


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / "workflows" / "comfyui"


def test_descriptor_reports_music_capability() -> None:
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
    )
    descriptor = backend.descriptor()
    assert descriptor.backend_name == "ace_step_comfyui"
    assert Capability.TEXT_TO_MUSIC in descriptor.capabilities
    assert descriptor.heavyweight is True
    assert descriptor.vram_required_gb == 20.0
    assert "audio" in descriptor.supported_outputs


def test_descriptor_supported_inputs() -> None:
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
    )
    descriptor = backend.descriptor()
    expected = {
        "prompt", "lyrics", "seed", "duration", "bpm",
        "time_signature", "language", "key_scale",
        "generate_audio_codes", "workflow", "model_filename",
    }
    assert expected.issubset(set(descriptor.supported_inputs))


def test_load_workflow_returns_dict() -> None:
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
    )
    workflow = backend._load_workflow(None)
    assert isinstance(workflow, dict)
    assert "1" in workflow


def test_load_workflow_for_preset() -> None:
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
    )
    turbo = backend._load_workflow_for_preset("xl_turbo")
    sft = backend._load_workflow_for_preset("xl_sft")
    assert isinstance(turbo, dict)
    assert isinstance(sft, dict)
    assert backend.model_name == "xl_turbo"


def test_load_workflow_for_preset_failure_leaves_backend_state_unchanged(tmp_path: Path) -> None:
    """A failed preset resolution must not corrupt the configured model's state.

    Regression: preset resolution used to swap ``self.model_name`` without
    try/finally, so a missing workflow left the backend pointed at the preset
    with stale ``_workflow_metadata``/``_expected_output_node_id``.
    """
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    for name in (
        "ace-step-1.5-xl-turbo.workflow.json",
        "ace-step-1.5-xl-turbo.metadata.json",
    ):
        shutil.copy2(WORKFLOW_DIR / name, workflows_dir / name)
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=workflows_dir,
    )
    backend._resolve_workflow()
    baseline_metadata = backend._workflow_metadata
    baseline_node = backend._expected_output_node_id

    # SFT workflow is absent on disk: resolution must fail without state drift.
    with pytest.raises(BackendError) as excinfo:
        backend._load_workflow_for_preset("xl_sft")
    assert excinfo.value.code == BackendErrorCode.BACKEND_UNAVAILABLE
    assert backend.model_name == "xl_turbo"
    assert backend._workflow_metadata == baseline_metadata
    assert backend._expected_output_node_id == baseline_node

    # A successful preset resolution is fully local too.
    backend._load_workflow_for_preset("xl_turbo")
    assert backend._workflow_metadata == baseline_metadata
    assert backend._expected_output_node_id == baseline_node


def test_generate_submits_provided_workflow_instead_of_disk_resolution(tmp_path: Path) -> None:
    """settings["workflow"] must be the submitted document, not the on-disk one.

    Regression: the backend re-resolved from ``self.model_name`` on disk, so a
    preset other than the configured model was validated by readiness but the
    wrong preset's workflow was submitted to ComfyUI.
    """
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            posted.update(json.loads(request.content))
            return httpx.Response(200, json={"prompt_id": "prompt-ace"})
        if request.url.path == "/history/prompt-ace":
            return httpx.Response(200, json={
                "prompt-ace": {
                    "status": {"status_str": "success"},
                    "outputs": {
                        "11": {"audio": [{"filename": "song.wav", "subfolder": "", "type": "output"}]},
                    },
                }
            })
        if request.url.path == "/view":
            return httpx.Response(200, content=b"fake-audio")
        raise AssertionError(request.url.path)

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
        poll_interval=0,
        client_factory=factory,
    )
    provided = {"7": {"class_type": "ACEProvidedNode", "inputs": {"text": "{{prompt}}"}}}
    result = backend.generate(
        GenerationRequest(
            job_id="job",
            output_dir=tmp_path,
            prompt="epic score",
            seed=5,
            settings={
                "workflow": provided,
                "workflow_metadata": {"quantization": "fp8"},
                "output_node_id": "11",
                "output_category": "audio",
            },
        )
    )

    submitted = posted["prompt"]
    assert list(submitted) == ["7"]
    assert submitted["7"] == {"class_type": "ACEProvidedNode", "inputs": {"text": "epic score"}}
    # The provided output-node metadata drives retrieval, not disk metadata.
    assert backend._expected_output_node_id == "11"
    assert backend._workflow_metadata == {"quantization": "fp8"}
    assert result.outputs[0].name == "song.wav"
    # The deepcopy keeps the caller's document immutable.
    assert provided["7"]["inputs"]["text"] == "{{prompt}}"


def test_readiness_returns_structure_when_healthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.0.0"}})
        if request.url.path == "/object_info":
            return httpx.Response(200, json={})
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
        client_factory=factory,
    )
    result = backend.readiness()
    assert "comfyui_healthy" in result
    assert "turbo" in result
    assert "sft" in result
    assert "combo_choices" in result
    assert "duration_range" in result


def test_readiness_reports_missing_nodes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.0.0"}})
        if request.url.path == "/object_info":
            return httpx.Response(200, json={})
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
        client_factory=factory,
    )
    result = backend.readiness()
    assert set(result["turbo"]["missing_nodes"]) == {
        "UNETLoader", "DualCLIPLoader", "VAELoader",
        "TextEncodeAceStepAudio1.5", "EmptyAceStep1.5LatentAudio",
        "ConditioningZeroOut", "ModelSamplingAuraFlow", "KSampler",
        "VAEDecodeAudio", "SaveAudioMP3",
    }


def test_readiness_reports_missing_files() -> None:
    info = {
        "UNETLoader": {
            "input": {
                "required": {
                    "unet_name": [{"combo": ["other_model.safetensors"]}],
                }
            }
        },
        "DualCLIPLoader": {
            "input": {
                "required": {
                    "clip_name1": [{"combo": ["other_clip.safetensors"]}],
                    "clip_name2": [{"combo": ["other_clip2.safetensors"]}],
                }
            }
        },
        "VAELoader": {
            "input": {
                "required": {
                    "vae_name": [{"combo": ["other_vae.safetensors"]}],
                }
            }
        },
        "TextEncodeAceStepAudio1.5": {"input": {"required": {}}},
        "RandomNoise": {"input": {"required": {}}},
        "BasicScheduler": {"input": {"required": {}}},
        "BasicGuider": {"input": {"required": {}}},
        "KSamplerSelect": {"input": {"required": {}}},
        "SamplerCustomAdvanced": {"input": {"required": {}}},
        "VAEDecodeAudio": {"input": {"required": {}}},
        "SaveAudio": {"input": {"required": {}}},
    }
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
    )
    missing = backend._missing_files_from_metadata(
        json.loads((WORKFLOW_DIR / "ace-step-1.5-xl-turbo.metadata.json").read_text()),
        info,
    )
    assert "acestep_v1.5_xl_turbo_bf16.safetensors" in missing
    assert "qwen_0.6b_ace15.safetensors" in missing
    assert "qwen_4b_ace15.safetensors" in missing
    assert "ace_1.5_vae.safetensors" in missing


def test_retrieve_outputs_filters_by_node_and_category(tmp_path: Path) -> None:
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
    )
    backend._expected_output_node_id = "11"
    backend._expected_output_category = "audio"

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/view":
            captured["view_called"] = True
            captured["filename"] = request.url.params.get("filename")
            return httpx.Response(200, content=b"fake-audio")
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    backend._client_factory = factory

    record = {
        "outputs": {
            "9": {"images": [{"filename": "preview.png", "subfolder": "", "type": "output"}]},
            "11": {"audio": [{"filename": "music.wav", "subfolder": "", "type": "output"}]},
        }
    }
    outputs = backend._retrieve_outputs(record, tmp_path)
    assert len(outputs) == 1
    assert outputs[0].name == "music.wav"
    assert captured.get("view_called") is True


def test_retrieve_outputs_ignores_other_nodes_when_expected_node_set(tmp_path: Path) -> None:
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
    )
    backend._expected_output_node_id = "11"
    backend._expected_output_category = "audio"

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/view":
            captured["called"] = True
            return httpx.Response(200, content=b"x")
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    backend._client_factory = factory

    record = {
        "outputs": {
            "9": {"audio": [{"filename": "wrong.wav", "subfolder": "", "type": "output"}]},
        }
    }
    outputs = backend._retrieve_outputs(record, tmp_path)
    assert outputs == []
    assert captured.get("called") is not True


def test_retrieve_outputs_rejects_oversized_payload(tmp_path: Path) -> None:
    backend = ACEStepComfyUIBackend(
        endpoint="http://127.0.0.1:8188",
        model_name="xl_turbo",
        workflows_dir=WORKFLOW_DIR,
    )
    backend._expected_output_node_id = "11"
    backend._expected_output_category = "audio"
    backend.MAX_OUTPUT_BYTES = 100

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/view":
            return httpx.Response(200, content=b"x" * 200)
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return httpx.Client(transport=transport, **kwargs)

    backend._client_factory = factory

    record = {"outputs": {"11": {"audio": [{"filename": "big.wav", "subfolder": "", "type": "output"}]}}}
    with pytest.raises(BackendError) as exc_info:
        backend._retrieve_outputs(record, tmp_path)
    assert exc_info.value.code == BackendErrorCode.INVALID_RESPONSE
