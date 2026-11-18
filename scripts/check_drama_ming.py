#!/usr/bin/env python3
"""Run one local speech smoke check, then release the worker's model."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.tts_worker.app import GeneratePayload
from services.tts_worker.ming_drama import DramaProvider, MingSmallProvider, MingTinyProvider
from services.tts_worker.speech_releases import MODEL_SUBDIRS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--provider", choices=tuple(MODEL_SUBDIRS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--reference-text", default="")
    parser.add_argument("--language", default="en")
    parser.add_argument("--task", choices=("tts", "voice_design", "freeform"), default="tts")
    parser.add_argument("--direction", default="")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    cache = root / "cache/drama-ming"
    (cache / "tmp").mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "LVS_AI_CACHE_ROOT": str(cache), "XDG_CACHE_HOME": str(cache),
        "HF_HOME": str(cache / "huggingface"), "HUGGINGFACE_HUB_CACHE": str(cache / "huggingface/hub"),
        "TORCH_HOME": str(cache / "torch"), "TRITON_CACHE_DIR": str(cache / "triton"),
        "TORCH_EXTENSIONS_DIR": str(cache / "torch_extensions"), "TMPDIR": str(cache / "tmp"),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
    })
    provider = {"fish_drama_3": DramaProvider, "ming_tts_tiny": MingTinyProvider,
                "ming_tts_05b": MingSmallProvider}[args.provider](
        root / MODEL_SUBDIRS[args.provider], source_path=root / "services/ming-tts/source",
        runtime_path=root / "services/drama-rs/drama", runtime_port=8202,
    )
    try:
        result = provider.generate(GeneratePayload(
            job_id="local-speech-smoke", text="The morning light falls across the quiet room.",
            output_path=args.output.expanduser().resolve(), seed=42,
            reference_audio=args.reference, reference_text=args.reference_text,
            language=args.language, ming_task=args.task, voice_instruction=args.direction,
        ))
        args.output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        provider.unload()


if __name__ == "__main__":
    main()
