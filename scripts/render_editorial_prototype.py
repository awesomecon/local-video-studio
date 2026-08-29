#!/usr/bin/env python3
"""Render the first vertical Editorial Mode acceptance prototype."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.editorial import EditorialRenderer, build_project_mars_prototype


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/lvs-editorial-prototype/prototype.mp4")
    args = parser.parse_args()
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    plan = build_project_mars_prototype()
    plan_path = output.with_name("edit-plan.json")
    preview_path = output.with_name("preview.html")
    plan_path.write_text(
        json.dumps(plan.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    EditorialRenderer().render(plan, output, preview_html=preview_path)
    print(output)
    print(preview_path)
    print(plan_path)


if __name__ == "__main__":
    main()
