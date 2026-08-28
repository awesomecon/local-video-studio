from pathlib import Path

import pytest

from backend.schemas import Project, ProjectPlan, Scene
from backend.storage import ProjectStore, slugify


def test_slugify_is_safe_and_readable() -> None:
    assert slugify("How Roman Aqueducts Worked!") == "how-roman-aqueducts-worked"
    assert "/" not in slugify("../Unsafe")


def test_portable_project_layout_and_round_trip(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = Project(title="Aqueducts", topic="Water", target_duration=30, slug="aqueducts")
    scene = Scene(project_id=project.id, index=0, duration=4, visual_prompt="stone arches")
    directory = store.create_project(project, [scene])
    plan = ProjectPlan(project_id=project.id, title=project.title,
                       target_duration=30, scenes=[scene])
    store.save_plan(project.slug, plan)
    assert store.load_project(project.slug) == project
    assert store.load_scene(project.slug, 0) == scene
    assert store.load_plan(project.slug) == plan
    assert (directory / "scenes/001/prompt.json").is_file()
    assert (directory / "variants/archive").is_dir()


def test_create_refuses_existing_project_directory(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = Project(title="Aqueducts", topic="Water", target_duration=30, slug="aqueducts")
    store.create_project(project)
    with pytest.raises(FileExistsError):
        store.create_project(project)


def test_rejected_variant_is_archived_not_deleted(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = Project(title="Aqueducts", topic="Water", target_duration=30, slug="aqueducts")
    directory = store.create_project(project)
    variant = directory / "scenes/variant.mp4"
    variant.write_bytes(b"variant")
    archived = store.archive_variant(project.slug, "scenes/variant.mp4")
    assert archived.read_bytes() == b"variant"
    assert not variant.exists()


def test_archive_cannot_escape_project(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = Project(title="Aqueducts", topic="Water", target_duration=30, slug="aqueducts")
    store.create_project(project)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="inside the project"):
        store.archive_variant(project.slug, "../outside.mp4")


def test_delete_project_removes_directory_and_content(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = Project(title="Aqueducts", topic="Water", target_duration=30, slug="aqueducts")
    scene = Scene(project_id=project.id, index=0, duration=4, visual_prompt="stone arches")
    directory = store.create_project(project, [scene])
    deleted = store.delete_project(project)
    assert deleted == directory
    assert not directory.exists()


def test_delete_project_refuses_missing_or_invalid_slug(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.delete_project("missing-project")
    with pytest.raises(ValueError):
        store.delete_project("../outside")
