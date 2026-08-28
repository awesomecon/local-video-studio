#!/usr/bin/env python3
"""Print a non-mutating, secret-free Local Video Studio environment report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import load_config
from backend.core.environment import format_markdown, inspect_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--output", type=Path, help="Write the report instead of printing it")
    parser.add_argument("--skip-cuda", action="store_true",
                        help="Skip CUDA/nvidia-smi probes (useful in restricted sandboxes)")
    arguments = parser.parse_args()
    report = inspect_environment(load_config(arguments.config), probe_cuda=not arguments.skip_cuda)
    content = (json.dumps(report.model_dump(mode="json"), indent=2) + "\n"
               if arguments.as_json else format_markdown(report))
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
