from pathlib import Path

import pytest

from backend.core import load_config
from backend.pipeline import PipelineService
from backend.schemas import ProjectCreate


def service(tmp_path: Path) -> PipelineService:
    config = load_config(environ={})
    return PipelineService(
        config,
        database_path=tmp_path / "app" / "studio.sqlite3",
        project_root=tmp_path / "projects",
        temp_root=tmp_path / "app" / "tmp",
        mock_mode=True,
    )


def test_project_snapshot_recovers_scenes_from_plan_when_db_is_empty(tmp_path: Path) -> None:
    pipeline = service(tmp_path)
    project = pipeline.create_project(
        ProjectCreate(
            title="Roman Aqueducts",
            topic="how Roman aqueducts worked",
            target_duration=3,
            resolution=(320, 180),
            fps=12,
        )
    )
    plan = pipeline.ensure_plan(project.id)
    assert len(plan.scenes) > 0

    snapshot = pipeline.project_snapshot(project.id)
    assert len(snapshot["scenes"]) == len(plan.scenes)

    with pipeline.database.connection() as connection:
        for scene in pipeline.database.list_scenes(project.id):
            connection.execute("DELETE FROM scenes WHERE id=?", (scene.id,))

    empty_snapshot = pipeline.project_snapshot(project.id)
    assert len(empty_snapshot["scenes"]) == len(plan.scenes)
    assert {s["id"] for s in empty_snapshot["scenes"]} == {s.id for s in plan.scenes}
