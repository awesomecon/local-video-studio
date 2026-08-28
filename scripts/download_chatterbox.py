"""Download only the Chatterbox Multilingual V3 files used by the local worker."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download


FILES = (
    "ve.pt",
    "t3_mtl23ls_v3.safetensors",
    "s3gen.pt",
    "grapheme_mtl_merged_expanded_v1.json",
    "conds.pt",
    "Cangjie5_TC.json",
)
EXPECTED_BYTES = 3_450_000_000
MINIMUM_REMAINING_BYTES = 50 * 1024**3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("~/ai/models/tts/chatterbox-v3"),
    )
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination).free
    if free - EXPECTED_BYTES < MINIMUM_REMAINING_BYTES:
        raise SystemExit(
            f"download refused: expected remaining space is below 50 GiB ({free / 1024**3:.1f} GiB free)"
        )
    result = snapshot_download(
        repo_id="ResembleAI/chatterbox",
        revision="main",
        allow_patterns=list(FILES),
        local_dir=destination,
    )
    print(result)


if __name__ == "__main__":
    main()
