#!/usr/bin/env python3
"""Recover a completed Fish narration job whose ComfyUI chunks were saved as FLAC."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import load_config
from backend.models.base import GenerationResult
from backend.pipeline.service import PipelineService
from backend.rendering.binaries import require_ffmpeg
from backend.rendering.process import run_media_process
from backend.schemas import AssetType
from backend.tts.audio import wav_duration
from backend.tts.models import NarrationRequest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_slug")
    parser.add_argument("job_id")
    parser.add_argument("--no-activate", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(environ={})
    service = PipelineService(config, initialize=False, mock_mode=False)
    project = service.database.get_project_by_slug(args.project_slug)
    if project is None:
        raise SystemExit(f"unknown project: {args.project_slug}")
    job = service.jobs.get(args.job_id)
    if job is None or job.project_id != project.id or job.backend != "fish_s2_pro":
        raise SystemExit("job is missing, belongs to another project, or is not Fish S2 Pro")

    request = NarrationRequest.model_validate(job.parameters)
    root = service.store.project_path(project)
    chunk_dir = root / "audio" / "fish" / args.job_id
    sidecars = sorted(chunk_dir.glob("[0-9][0-9][0-9][0-9].json"))
    if not sidecars:
        raise SystemExit(f"no failed chunk sidecars found in {chunk_dir}")

    ffmpeg = require_ffmpeg()
    outputs: list[Path] = []
    specs: list[dict] = []
    for expected, sidecar in enumerate(sidecars, start=1):
        item = json.loads(sidecar.read_text(encoding="utf-8"))
        if int(item.get("chunk", -1)) != expected:
            raise SystemExit(f"non-contiguous chunk sidecar: {sidecar}")
        sources = sorted(chunk_dir.glob(f"{args.job_id}-{expected}_*.flac"))
        if len(sources) != 1:
            raise SystemExit(f"expected one FLAC source for chunk {expected}, found {len(sources)}")
        target = chunk_dir / f"{expected:04d}.wav"
        if not target.is_file():
            run_media_process([
                str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-y", "-i", str(sources[0]), "-map_metadata", "-1", "-vn",
                "-c:a", "pcm_s16le", str(target),
            ])
        duration = wav_duration(target)
        item.update({"status": "completed", "error": None, "duration": duration})
        service.tts._atomic_json(sidecar, item)
        outputs.append(target)
        specs.append(item)

    take = root / "narration" / "takes" / "fish_s2_pro" / f"{args.job_id}.wav"
    join_result = service.tts._publish_master_join(outputs, take, pause_ms=request.pause_ms)
    chunk_records = [
        {
            "index": index,
            "text": item["text"],
            "scene_id": item.get("scene_id"),
            "scene_index": item.get("scene_index"),
            "scene_title": item.get("scene_title"),
            "duration": wav_duration(output),
            "seed": int(item["seed"]),
            "provider": "fish_s2_pro",
            "job_id": args.job_id,
            "filepath": output.relative_to(root).as_posix(),
        }
        for index, (item, output) in enumerate(zip(specs, outputs, strict=True), start=1)
    ]
    prompt = "\n\n".join(str(item["text"]) for item in specs)
    result = GenerationResult(
        outputs=(take,),
        metadata={
            "backend": "fish_s2_pro",
            "model": "Fish Audio S2 Pro",
            "model_version": "fish-s2-pro-saganaki-comfy-v3",
            "quantization": "bfloat16",
            "workflow_version": "tts-narration-v3",
            "seed": request.seed,
            "prompt": prompt,
            "settings": {
                "role": "narration_take",
                "provider": "fish_s2_pro",
                "job_id": args.job_id,
                "voice_profile_id": request.voice_profile_id,
                "built_in_voice": False,
                "speaker": None,
                "chunk_count": len(outputs),
                "chunk_seconds": request.chunk_seconds or 30.0,
                "pause_ms": request.pause_ms,
                "duration": join_result.duration_seconds,
                "inserted_pause_ms": [
                    round(value * 1000) for value in join_result.inserted_pause_seconds
                ],
                "scene_script_sha256": service.tts._scene_script_hash(project.id),
                "timing_mode": "scene_audio_v1" if request.text is None else "override",
                "chunks": chunk_records,
                "scene_durations": service.tts._scene_durations(
                    chunk_records, join_result.inserted_pause_seconds,
                ),
                "request": request.model_dump(mode="json", exclude={"text"}),
                "recovered_from": "comfyui_flac_output",
            },
        },
    )
    asset = service._record_asset(
        project, None, take, AssetType.NARRATION, result,
        role="narration_take", record_attempt=False,
    )
    service.tts._atomic_json(take.with_suffix(".json"), asset.model_dump(mode="json"))
    if not args.no_activate:
        service.tts.activate_take(project.id, asset.id)
    print(json.dumps({
        "asset_id": asset.id,
        "take": str(take),
        "chunks": len(outputs),
        "duration_seconds": join_result.duration_seconds,
        "activated": not args.no_activate,
    }, indent=2))


if __name__ == "__main__":
    main()
