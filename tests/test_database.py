import sqlite3
from pathlib import Path

import pytest

from backend.schemas import (
    Asset, AssetType, GenerationAttempt, GenerationJob, Project, Scene,
    Shot, ShotStatus, ShotTransitionKind,
)
from backend.storage import StudioDatabase

FIXTURES = Path(__file__).parent / "fixtures"


def project() -> Project:
    return Project(title="Aqueducts", topic="Roman water", target_duration=60,
                   slug="aqueducts")


def make_database(tmp_path: Path) -> StudioDatabase:
    database = StudioDatabase(tmp_path / "studio.sqlite3")
    database.initialize()
    return database


def build_legacy_v1_database(path: Path) -> sqlite3.Connection:
    """Create a real pre-migration database from the frozen v1 baseline."""
    item = project()
    scene = Scene(project_id=item.id, index=0, duration=5)
    connection = sqlite3.connect(path)
    connection.executescript((FIXTURES / "schema_v1.sql").read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO projects (id, slug, title, status, created_at, updated_at,"
        " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item.id, item.slug, item.title, item.status.value,
         item.created_at.isoformat(), item.updated_at.isoformat(),
         item.model_dump_json()),
    )
    connection.execute(
        "INSERT INTO scenes (id, project_id, scene_index, status, payload_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (scene.id, scene.project_id, scene.index, scene.status.value,
         scene.model_dump_json()),
    )
    connection.commit()
    return connection


def table_columns(database: StudioDatabase, table: str) -> set[str]:
    with database.connection() as connection:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_database_initializes_all_required_tables(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    with sqlite3.connect(database.path) as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "scenes", "shots", "assets", "generation_attempts", "jobs",
            "prompts", "model_metadata", "render_metadata"} <= names


def test_project_scene_asset_and_attempt_round_trip(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    item = database.create_project(project())
    scene = database.save_scene(Scene(project_id=item.id, index=0, duration=5))
    shot = database.save_shot(Shot(project_id=item.id, scene_id=scene.id, index=0,
                                   duration_seconds=5))
    asset = database.save_asset(Asset(
        project_id=item.id, scene_id=scene.id, shot_id=shot.id, type=AssetType.IMAGE,
        filepath=Path("scenes/001/reference.png"), backend="mock", model="mock-v1", seed=7,
    ))
    attempt = database.save_attempt(GenerationAttempt(
        asset_id=asset.id, job_id=None, scene_id=scene.id, shot_id=shot.id,
        backend="mock", model="mock-v1", seed=7, success=True, duration_seconds=0.1,
    ))
    assert database.get_project(item.id) == item
    assert database.list_scenes(item.id) == [scene]
    assert database.list_shots(item.id, scene.id) == [shot]
    assert database.list_assets(item.id, scene.id) == [asset]
    assert database.list_attempts(asset_id=asset.id) == [attempt]
    assert database.list_attempts(shot_id=shot.id) == [attempt]
    assert database.list_attempts(scene_id=scene.id) == [attempt]


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    scene = Scene(project_id="missing", index=0, duration=5)
    try:
        database.save_scene(scene)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("missing project should violate the foreign key")


# ---------------------------------------------------------------------------
# Schema migration


def test_fresh_database_is_created_at_latest_schema_version(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    with sqlite3.connect(database.path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_info").fetchone()[0]
    assert version == 2
    assert "shot_id" in table_columns(database, "assets")
    assert "shot_id" in table_columns(database, "jobs")
    assert "shot_id" in table_columns(database, "prompts")
    assert {"scene_id", "shot_id"} <= table_columns(database, "generation_attempts")


def test_v1_database_upgrades_to_v2_without_touching_payloads(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy = build_legacy_v1_database(path)
    legacy.close()

    database = StudioDatabase(path)
    database.initialize()

    with sqlite3.connect(database.path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_info").fetchone()[0]
        payloads = [row[0] for row in connection.execute(
            "SELECT payload_json FROM projects")]
        scene_payloads = [row[0] for row in connection.execute(
            "SELECT payload_json FROM scenes")]
    assert version == 2
    assert len(payloads) == 1 and Project.model_validate_json(payloads[0]).slug == "aqueducts"
    assert len(scene_payloads) == 1
    assert "shot_id" in table_columns(database, "assets")
    assert "shot_id" in table_columns(database, "jobs")
    assert "shot_id" in table_columns(database, "prompts")
    assert "shot_id" in table_columns(database, "generation_attempts")

    # The upgraded repository is fully usable.
    item = database.get_project_by_slug("aqueducts")
    assert item is not None
    scene = database.list_scenes(item.id)[0]
    shot = Shot(project_id=item.id, scene_id=scene.id, index=0, duration_seconds=2.5)
    assert database.save_shot(shot) == shot


def test_upgrade_is_idempotent_across_repeated_initializes(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    build_legacy_v1_database(path).close()

    first = StudioDatabase(path)
    first.initialize()
    second = StudioDatabase(path)
    second.initialize()
    third = StudioDatabase(path)
    third.initialize()

    with sqlite3.connect(path) as connection:
        versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_info ORDER BY version")]
        shots_tables = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='shots'"
        ).fetchone()[0]
    assert versions[-1] == 2
    assert shots_tables == 1


def test_partial_upgrade_completes_on_next_initialize(tmp_path: Path) -> None:
    """A v1 database that already has the shots table but no new columns heals."""
    path = tmp_path / "partial.sqlite3"
    connection = build_legacy_v1_database(path)
    connection.executescript("""
        CREATE TABLE shots (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
            shot_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(project_id, scene_id, shot_index)
        );
    """)
    connection.commit()
    connection.close()

    database = StudioDatabase(path)
    database.initialize()
    with sqlite3.connect(database.path) as reopened:
        version = reopened.execute("SELECT MAX(version) FROM schema_info").fetchone()[0]
    assert version == 2
    assert "shot_id" in table_columns(database, "assets")


def test_shot_round_trip_orders_by_index_and_enforces_unique_positions(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    item = database.create_project(project())
    scene = database.save_scene(Scene(project_id=item.id, index=0, duration=12))
    first = database.save_shot(Shot(project_id=item.id, scene_id=scene.id, index=0,
                                    duration_seconds=6, title="a"))
    second = database.save_shot(Shot(project_id=item.id, scene_id=scene.id, index=1,
                                     duration_seconds=4, title="b"))
    assert database.list_shots(item.id) == [first, second]

    duplicate = Shot(id=second.id, project_id=item.id, scene_id=scene.id, index=0,
                     duration_seconds=4)
    with pytest.raises(sqlite3.IntegrityError):
        database.save_shot(duplicate)

    # Deleting the scene cascades to its shots.
    with database.connection() as connection:
        connection.execute("DELETE FROM scenes WHERE id=?", (scene.id,))
    assert database.list_shots(item.id) == []


def test_deleting_shot_nulls_asset_ownership(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    item = database.create_project(project())
    scene = database.save_scene(Scene(project_id=item.id, index=0, duration=8))
    shot = database.save_shot(Shot(project_id=item.id, scene_id=scene.id, index=0,
                                   duration_seconds=8,
                                   status=ShotStatus.READY))
    asset = database.save_asset(Asset(
        project_id=item.id, scene_id=scene.id, shot_id=shot.id, type=AssetType.VIDEO,
        filepath=Path("scenes/001/shots/001/visual.mp4"),
        backend="mock", model="mock-v1", seed=1,
    ))
    assert database.delete_shot(shot.id)
    reloaded = database.get_asset(asset.id)
    assert reloaded is not None and reloaded.shot_id is None


def test_transition_kind_values_survive_storage(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    item = database.create_project(project())
    scene = database.save_scene(Scene(project_id=item.id, index=0, duration=10))
    shot = database.save_shot(Shot(
        project_id=item.id, scene_id=scene.id, index=0, duration_seconds=5,
    ))
    stored = database.get_shot(shot.id)
    assert stored is not None
    assert stored.transition_in.kind in {
        ShotTransitionKind.CUT, ShotTransitionKind.CROSSFADE,
    }
