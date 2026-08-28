"""Command-line mock acceptance render."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.core import load_config
from backend.pipeline.service import PipelineService
from backend.schemas import ProjectCreate


def _resolution(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("resolution must look like 640x360") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("resolution must be positive")
    return width, height


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--title")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--resolution", type=_resolution, default=(640, 360))
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--database", type=Path)
    arguments = parser.parse_args()
    config = load_config()
    service = PipelineService(
        config,
        database_path=arguments.database,
        project_root=arguments.output_root,
        temp_root=arguments.output_root / ".tmp" if arguments.output_root else None,
        mock_mode=True,
    )
    request = ProjectCreate(
        title=arguments.title or arguments.topic,
        topic=arguments.topic,
        target_duration=arguments.duration,
        resolution=arguments.resolution,
    )
    project = service.create_project(request)
    final = service.run_project(project.id)
    print(json.dumps({
        "project_id": project.id,
        "project_directory": str(service.store.project_path(project)),
        "final_mp4": str(final),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
