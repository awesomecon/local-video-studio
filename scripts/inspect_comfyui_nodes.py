"""Read-only ComfyUI node inspector for ACE-Step 1.5 XL workflow validation.

This script queries the configured ComfyUI instance and prints the schemas
required to build and validate the ACE-Step 1.5 XL API workflows. It does
not modify ComfyUI, download models, or run inference.

Note: /object_info reports which model filenames ComfyUI sees, but does NOT
reliably disclose the physical filesystem directories that supplied them.
ComfyUI searches its normal model directory plus paths from
extra_model_paths.yaml. Actual installation paths must be resolved from
ComfyUI configuration, not from this inspector.

Run manually before finalizing workflow JSON:
    python scripts/inspect_comfyui_nodes.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

COMFYUI_ENDPOINT = "http://127.0.0.1:8188"
WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "workflows" / "comfyui"
WORKFLOW_PATHS = (
    WORKFLOW_DIR / "ace-step-1.5-xl-turbo.workflow.json",
    WORKFLOW_DIR / "ace-step-1.5-xl-sft.workflow.json",
)
REQUIRED_FILES = [
    ("diffusion_models", "acestep_v1.5_xl_turbo_bf16.safetensors"),
    ("diffusion_models", "acestep_v1.5_xl_sft_bf16.safetensors"),
    ("text_encoders", "qwen_0.6b_ace15.safetensors"),
    ("text_encoders", "qwen_4b_ace15.safetensors"),
    ("vae", "ace_1.5_vae.safetensors"),
]


def fetch_object_info() -> dict[str, Any]:
    with httpx.Client(timeout=30) as client:
        response = client.get(f"{COMFYUI_ENDPOINT}/object_info")
        response.raise_for_status()
        return response.json()


def fetch_system_stats() -> dict[str, Any]:
    with httpx.Client(timeout=30) as client:
        response = client.get(f"{COMFYUI_ENDPOINT}/system_stats")
        response.raise_for_status()
        return response.json()


def inspect_node(info: dict[str, Any], node_name: str) -> dict[str, Any]:
    node = info.get(node_name, {})
    return {
        "input": node.get("input", {}),
        "output": node.get("output", {}),
        "category": node.get("category", ""),
    }


def find_loader_choices(info: dict[str, Any], node_name: str, input_name: str) -> list[str]:
    node = info.get(node_name, {})
    input_spec = node.get("input", {}).get("required", {})
    field = input_spec.get(input_name, [])
    return extract_choices(field)


def find_combo_choices(info: dict[str, Any], node_name: str, input_name: str) -> list[str] | None:
    node = info.get(node_name, {})
    input_spec = node.get("input", {}).get("required", {})
    field = input_spec.get(input_name, [])
    choices = extract_choices(field)
    return choices or None


def extract_choices(field: Any) -> list[str]:
    if not isinstance(field, list) or not field:
        return []
    if isinstance(field[0], list):
        return [str(item) for item in field[0]]
    for candidate in field:
        if not isinstance(candidate, dict):
            continue
        values = candidate.get("options", candidate.get("combo", []))
        if isinstance(values, list):
            return [str(item) for item in values]
    return []


def required_nodes() -> frozenset[str]:
    node_types: set[str] = set()
    for path in WORKFLOW_PATHS:
        workflow = json.loads(path.read_text(encoding="utf-8"))
        node_types.update(
            str(node["class_type"])
            for node in workflow.values()
            if isinstance(node, dict) and "class_type" in node
        )
    return frozenset(node_types)


def main() -> int:
    print(f"Querying {COMFYUI_ENDPOINT} ...")
    try:
        info = fetch_object_info()
        stats = fetch_system_stats()
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    comfyui_version = stats.get("system", {}).get("comfyui_version", "unknown")
    print(f"ComfyUI version: {comfyui_version}\n")

    # Check required nodes
    print("=== Required nodes ===")
    expected_nodes = required_nodes()
    missing_nodes = sorted(expected_nodes - set(info.keys()))
    if missing_nodes:
        print(f"MISSING: {missing_nodes}")
    else:
        print("All required nodes present.")

    # TextEncodeAceStepAudio1.5 schema
    print("\n=== TextEncodeAceStepAudio1.5 inputs ===")
    ace_node = inspect_node(info, "TextEncodeAceStepAudio1.5")
    for name, spec in ace_node["input"].get("required", {}).items():
        print(f"  {name}: {spec}")

    # Combo choices
    print("\n=== Combo choices from TextEncodeAceStepAudio1.5 ===")
    combo_fields = {"language": "language", "key_scale": "keyscale", "time_signature": "timesignature"}
    for public_name, node_name in combo_fields.items():
        choices = find_combo_choices(info, "TextEncodeAceStepAudio1.5", node_name)
        print(f"  {public_name}: {choices}")

    # Loader choices
    print("\n=== Loader choices ===")
    for node, field in [
        ("UNETLoader", "unet_name"),
        ("DualCLIPLoader", "clip_name1"),
        ("VAELoader", "vae_name"),
    ]:
        choices = find_loader_choices(info, node, field)
        matching = [c for c in choices if "ace" in c.lower() or "acestep" in c.lower()]
        print(f"  {node}.{field} (ACE-related): {matching}")

    # Required files
    print("\n=== Required file inventory ===")
    for folder, filename in REQUIRED_FILES:
        all_files: list[str] = []
        for node, field in [
            ("UNETLoader", "unet_name"),
            ("DualCLIPLoader", "clip_name1"),
            ("DualCLIPLoader", "clip_name2"),
            ("VAELoader", "vae_name"),
        ]:
            choices = find_loader_choices(info, node, field)
            for c in choices:
                if filename.lower() in c.lower():
                    all_files.append(f"{node}.{field}: {c}")
        if all_files:
            print(f"  {folder}/{filename}:")
            for item in all_files:
                print(f"    {item}")
        else:
            print(f"  {folder}/{filename}: NOT FOUND in loader choices")

    # Save schema to file
    output: dict[str, Any] = {
        "comfyui_version": comfyui_version,
        "required_nodes_present": len(missing_nodes) == 0,
        "missing_nodes": missing_nodes,
        "ace_step_node": ace_node,
        "combo_choices": {
            public_name: find_combo_choices(info, "TextEncodeAceStepAudio1.5", node_name)
            for public_name, node_name in combo_fields.items()
        },
        "file_inventory": {},
    }
    for folder, filename in REQUIRED_FILES:
        matches = []
        for node, field in [
            ("UNETLoader", "unet_name"),
            ("DualCLIPLoader", "clip_name1"),
            ("DualCLIPLoader", "clip_name2"),
            ("VAELoader", "vae_name"),
        ]:
            for c in find_loader_choices(info, node, field):
                if filename.lower() in c.lower():
                    matches.append({"node": node, "field": field, "choice": c})
        output["file_inventory"][f"{folder}/{filename}"] = matches

    out_path = "comfyui_ace_inspection.json"
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
    print(f"\nWrote inspection to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
