from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.schemas import (
    Asset, AssetType, GenerationAttempt, GenerationJob, JobStatus, Project,
    ProjectPlan, Scene, SceneStatus,
)


def make_project() -> Project:
    return Project(title="Roman Aqueducts", topic="How Roman aqueducts worked",
                   target_duration=480, slug="roman-aqueducts")


def test_project_and_plan_round_trip() -> None:
    project = make_project()
    scene = Scene(project_id=project.id, index=0, duration=8, narration="Water flows.")
    plan = ProjectPlan(project_id=project.id, title=project.title, outline=["Gravity"],
                       scenes=[scene], target_duration=project.target_duration)
    restored = ProjectPlan.model_validate_json(plan.model_dump_json())
    assert restored.scenes[0].project_id == project.id


def test_asset_path_must_be_portable() -> None:
    project = make_project()
    with pytest.raises(ValidationError, match="project-relative"):
        Asset(project_id=project.id, type=AssetType.IMAGE,
              filepath=Path("/tmp/leak.png"), backend="mock", model="mock", seed=1)


def test_failed_attempt_requires_error() -> None:
    with pytest.raises(ValidationError, match="require an error"):
        GenerationAttempt(backend="mock", seed=1, success=False)


def test_completed_job_requires_full_progress() -> None:
    project = make_project()
    with pytest.raises(ValidationError, match="progress=1"):
        GenerationJob(project_id=project.id, stage="render", status=JobStatus.COMPLETED)


def test_locked_scene_is_synchronized_without_recursive_validation() -> None:
    project = make_project()
    scene = Scene(project_id=project.id, index=0, duration=2, status=SceneStatus.LOCKED)
    assert scene.locked
