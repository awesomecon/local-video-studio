"""Validation tests for ACE-Step 1.5 XL ComfyUI workflow assets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / "workflows" / "comfyui"
TURBO_WORKFLOW = WORKFLOW_DIR / "ace-step-1.5-xl-turbo.workflow.json"
SFT_WORKFLOW = WORKFLOW_DIR / "ace-step-1.5-xl-sft.workflow.json"
TURBO_METADATA = WORKFLOW_DIR / "ace-step-1.5-xl-turbo.metadata.json"
SFT_METADATA = WORKFLOW_DIR / "ace-step-1.5-xl-sft.metadata.json"


def test_turbo_workflow_is_valid_api_format() -> None:
    assert TURBO_WORKFLOW.is_file()
    workflow = json.loads(TURBO_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    for node_id, node in workflow.items():
        assert isinstance(node_id, str)
        assert isinstance(node, dict)
        assert "class_type" in node
        assert "inputs" in node


def test_sft_workflow_is_valid_api_format() -> None:
    assert SFT_WORKFLOW.is_file()
    workflow = json.loads(SFT_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    for node_id, node in workflow.items():
        assert isinstance(node_id, str)
        assert isinstance(node, dict)
        assert "class_type" in node
        assert "inputs" in node


def test_turbo_metadata_has_required_keys() -> None:
    assert TURBO_METADATA.is_file()
    metadata = json.loads(TURBO_METADATA.read_text(encoding="utf-8"))
    assert metadata["capability"] == "text_to_music"
    assert metadata["workflow_version"] == "ace-step-1.5-xl-turbo-comfy-v1"
    assert metadata["output_category"] == "audio"
    assert metadata["quantization"] == "bf16"
    assert isinstance(metadata["substitutions"], list)
    assert isinstance(metadata["required_files"], list)
    assert metadata["output_node_id"] in {"11"}


def test_sft_metadata_has_required_keys() -> None:
    assert SFT_METADATA.is_file()
    metadata = json.loads(SFT_METADATA.read_text(encoding="utf-8"))
    assert metadata["capability"] == "text_to_music"
    assert metadata["workflow_version"] == "ace-step-1.5-xl-sft-comfy-v1"
    assert metadata["output_category"] == "audio"
    assert isinstance(metadata["substitutions"], list)
    assert isinstance(metadata["required_files"], list)


def test_all_placeholders_are_defined_in_metadata() -> None:
    for workflow_path, metadata_path in [
        (TURBO_WORKFLOW, TURBO_METADATA),
        (SFT_WORKFLOW, SFT_METADATA),
    ]:
        workflow_text = workflow_path.read_text(encoding="utf-8")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        substitution_keys = {s["key"] for s in metadata["substitutions"]}
        import re
        placeholders = set(re.findall(r"\{\{(\w+)\}\}", workflow_text))
        assert not placeholders - substitution_keys, (
            f"Undocumented placeholders in {workflow_path.name}: {placeholders - substitution_keys}"
        )


def test_no_unresolved_placeholders_after_full_substitution() -> None:
    from backend.models.comfyui import substitute_workflow
    substitutions = {
        "prompt": "test",
        "lyrics": "",
        "seed": 12345,
        "duration": 30.0,
        "bpm": 120,
        "time_signature": "4",
        "language": "en",
        "key_scale": "C major",
        "generate_audio_codes": True,
        "model_filename": "acestep_v1.5_xl_turbo_bf16.safetensors",
        "filename_prefix": "local-video-studio/ace-step-music",
    }
    for workflow_path in (TURBO_WORKFLOW, SFT_WORKFLOW):
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        substituted = substitute_workflow(workflow, substitutions)
        workflow_text = json.dumps(substituted)
        assert "{{" not in workflow_text, f"Unresolved placeholder in {workflow_path.name}"


def test_substitute_workflow_preserves_native_types() -> None:
    from backend.models.comfyui import substitute_workflow
    workflow = {"1": {"inputs": {"seed": "{{seed}}", "flag": "{{flag}}"}}}
    result = substitute_workflow(workflow, {"seed": 42, "flag": True})
    assert result["1"]["inputs"]["seed"] == 42
    assert result["1"]["inputs"]["flag"] is True


def test_output_node_exists_in_workflow() -> None:
    for workflow_path, metadata_path in [
        (TURBO_WORKFLOW, TURBO_METADATA),
        (SFT_WORKFLOW, SFT_METADATA),
    ]:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        output_node_id = metadata["output_node_id"]
        assert output_node_id in workflow, f"Output node {output_node_id} missing from {workflow_path.name}"
        assert workflow[output_node_id]["class_type"] == "SaveAudioMP3"


def test_workflows_match_installed_ace_graph_contract() -> None:
    for workflow_path in (TURBO_WORKFLOW, SFT_WORKFLOW):
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        assert workflow["4"]["inputs"]["clip"] == ["2", 0]
        assert "tags" in workflow["4"]["inputs"]
        assert "timesignature" in workflow["4"]["inputs"]
        assert "keyscale" in workflow["4"]["inputs"]
        assert workflow["5"]["class_type"] == "EmptyAceStep1.5LatentAudio"
        assert workflow["5"]["inputs"]["batch_size"] == 1
        assert workflow["8"]["class_type"] == "KSampler"


def test_sampling_values_are_fixed() -> None:
    for metadata_path in (TURBO_METADATA, SFT_METADATA):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        sampling = metadata["sampling"]
        assert isinstance(sampling["steps"], int)
        assert isinstance(sampling["cfg"], (int, float))
        assert isinstance(sampling["sampler_name"], str)
        assert isinstance(sampling["scheduler"], str)
        assert sampling["denoise"] == 1.0
