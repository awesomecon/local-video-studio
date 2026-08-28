"""Thread-safe-by-connection SQLite repository.

Domain objects are retained as complete JSON documents while frequently queried
fields remain indexed columns. This keeps schema evolution straightforward and
avoids making SQLite the sole copy of portable project state.

Schema versions are upgraded through ordered, transaction-safe migrations in
``_MIGRATIONS``; fresh databases are created directly at the latest baseline.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.schemas import (
    Asset, GenerationAttempt, GenerationJob, JobStatus, Project, Scene, Shot,
)

SCHEMA_VERSION = 2

_BASELINE_TABLES = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(project_id, scene_index)
);
CREATE TABLE IF NOT EXISTS shots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
    shot_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(project_id, scene_id, shot_index)
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_id TEXT REFERENCES scenes(id) ON DELETE SET NULL,
    shot_id TEXT REFERENCES shots(id) ON DELETE SET NULL,
    asset_type TEXT NOT NULL,
    filepath TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_attempts (
    id TEXT PRIMARY KEY,
    asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    scene_id TEXT,
    shot_id TEXT,
    success INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_id TEXT REFERENCES scenes(id) ON DELETE SET NULL,
    shot_id TEXT,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_id TEXT REFERENCES scenes(id) ON DELETE SET NULL,
    shot_id TEXT,
    role TEXT NOT NULL,
    prompt TEXT NOT NULL,
    negative_prompt TEXT NOT NULL DEFAULT '',
    seed INTEGER,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_metadata (
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    version TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (backend, model, version)
);
CREATE TABLE IF NOT EXISTS render_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filepath TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_BASELINE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_scenes_project ON scenes(project_id, scene_index);
CREATE INDEX IF NOT EXISTS idx_shots_project_scene ON shots(project_id, scene_id, shot_index);
CREATE INDEX IF NOT EXISTS idx_assets_project_scene ON assets(project_id, scene_id);
CREATE INDEX IF NOT EXISTS idx_assets_project_shot ON assets(project_id, shot_id);
CREATE INDEX IF NOT EXISTS idx_attempts_asset ON generation_attempts(asset_id);
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs(status, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_jobs_project_shot ON jobs(project_id, shot_id);
"""

_BASELINE_SCHEMA = _BASELINE_TABLES + _BASELINE_INDEXES

_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


def _migration(target_version: int) -> Callable[[Callable[[sqlite3.Connection], None]],
                                                Callable[[sqlite3.Connection], None]]:
    def register(function: Callable[[sqlite3.Connection], None]) -> \
            Callable[[sqlite3.Connection], None]:
        _MIGRATIONS[target_version] = function
        return function

    return register


def _add_column(connection: sqlite3.Connection, table: str, column: str,
                definition: str) -> None:
    """Add one column when absent so upgrades stay idempotent."""
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@_migration(2)
def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Introduce shots and nullable shot ownership on child records.

    Statements run individually (never executescript) so the caller's
    BEGIN IMMEDIATE transaction stays intact and a failure rolls the whole
    upgrade back.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS shots (
               id TEXT PRIMARY KEY,
               project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
               scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
               shot_index INTEGER NOT NULL,
               status TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               UNIQUE(project_id, scene_id, shot_index)
           )"""
    )
    _add_column(connection, "assets", "shot_id",
                "TEXT REFERENCES shots(id) ON DELETE SET NULL")
    _add_column(connection, "generation_attempts", "scene_id", "TEXT")
    _add_column(connection, "generation_attempts", "shot_id", "TEXT")
    _add_column(connection, "jobs", "shot_id", "TEXT")
    _add_column(connection, "prompts", "shot_id", "TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_shots_project_scene "
        "ON shots(project_id, scene_id, shot_index)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_assets_project_shot ON assets(project_id, shot_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_project_shot ON jobs(project_id, shot_id)"
    )


class StudioDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            version = self._detect_schema_version(connection)
            if version is None:
                connection.executescript(_BASELINE_SCHEMA)
                connection.execute("INSERT OR IGNORE INTO schema_info(version) VALUES (?)",
                                   (SCHEMA_VERSION,))
                return
            while version < SCHEMA_VERSION:
                target = version + 1
                migration = _MIGRATIONS.get(target)
                if migration is None:
                    raise RuntimeError(
                        f"no migration registered for schema version {target}"
                    )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migration(connection)
                    connection.execute(
                        "INSERT OR IGNORE INTO schema_info(version) VALUES (?)", (target,)
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                version = target
            # Idempotent catch-up for any table or index a partially applied
            # historical upgrade could have left out.
            connection.executescript(_BASELINE_SCHEMA)

    @staticmethod
    def _detect_schema_version(connection: sqlite3.Connection) -> int | None:
        """Return the stored version, 1 for a pre-versioning database, or None when fresh."""
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"projects", "scenes"} & tables:
            return None
        if "schema_info" not in tables:
            return 1
        row = connection.execute("SELECT MAX(version) AS version FROM schema_info").fetchone()
        stored = row["version"] if row is not None else None
        return int(stored) if stored is not None else 1

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
        finally:
            connection.close()

    def create_project(self, project: Project) -> Project:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO projects
                   (id, slug, title, status, created_at, updated_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project.id, project.slug, project.title, project.status.value,
                 project.created_at.isoformat(), project.updated_at.isoformat(),
                 project.model_dump_json()),
            )
        return project

    def update_project(self, project: Project) -> Project:
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE projects SET slug=?, title=?, status=?, updated_at=?, payload_json=?
                   WHERE id=?""",
                (project.slug, project.title, project.status.value, project.updated_at.isoformat(),
                 project.model_dump_json(), project.id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"project not found: {project.id}")
        return project

    def get_project(self, project_id: str) -> Project | None:
        row = self._one("SELECT payload_json FROM projects WHERE id=?", (project_id,))
        return Project.model_validate_json(row["payload_json"]) if row else None

    def get_project_by_slug(self, slug: str) -> Project | None:
        row = self._one("SELECT payload_json FROM projects WHERE slug=?", (slug,))
        return Project.model_validate_json(row["payload_json"]) if row else None

    def list_projects(self) -> list[Project]:
        return [Project.model_validate_json(row["payload_json"])
                for row in self._all("SELECT payload_json FROM projects ORDER BY created_at DESC")]

    def delete_project(self, project_id: str) -> bool:
        """Delete a project's index rows in one transaction.

        Scenes, shots, assets, jobs, prompts, and render metadata cascade
        through the projects foreign key; generation attempts are removed
        explicitly because their foreign keys null out instead of cascading.
        """
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """DELETE FROM generation_attempts
                       WHERE asset_id IN (SELECT id FROM assets WHERE project_id=?)
                          OR job_id IN (SELECT id FROM jobs WHERE project_id=?)
                          OR shot_id IN (SELECT id FROM shots WHERE project_id=?)""",
                    (project_id, project_id, project_id),
                )
                cursor = connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
                deleted = bool(cursor.rowcount)
                connection.commit()
                return deleted
            except Exception:
                connection.rollback()
                raise

    def save_scene(self, scene: Scene) -> Scene:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO scenes (id, project_id, scene_index, status, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id,
                   scene_index=excluded.scene_index, status=excluded.status,
                   payload_json=excluded.payload_json""",
                (
                    scene.id, scene.project_id, scene.index, scene.status.value,
                    scene.model_dump_json(),
                ),
            )
        return scene

    def get_scene(self, scene_id: str) -> Scene | None:
        row = self._one("SELECT payload_json FROM scenes WHERE id=?", (scene_id,))
        return Scene.model_validate_json(row["payload_json"]) if row else None

    def list_scenes(self, project_id: str) -> list[Scene]:
        rows = self._all(
            "SELECT payload_json FROM scenes WHERE project_id=? ORDER BY scene_index", (project_id,)
        )
        return [Scene.model_validate_json(row["payload_json"]) for row in rows]

    def save_shot(self, shot: Shot) -> Shot:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO shots (id, project_id, scene_id, shot_index, status, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id,
                   scene_id=excluded.scene_id, shot_index=excluded.shot_index,
                   status=excluded.status, payload_json=excluded.payload_json""",
                (
                    shot.id, shot.project_id, shot.scene_id, shot.index, shot.status.value,
                    shot.model_dump_json(),
                ),
            )
        return shot

    def get_shot(self, shot_id: str) -> Shot | None:
        row = self._one("SELECT payload_json FROM shots WHERE id=?", (shot_id,))
        return Shot.model_validate_json(row["payload_json"]) if row else None

    def list_shots(self, project_id: str | None = None,
                   scene_id: str | None = None) -> list[Shot]:
        clauses, parameters = [], []
        if project_id is not None:
            clauses.append("project_id=?")
            parameters.append(project_id)
        if scene_id is not None:
            clauses.append("scene_id=?")
            parameters.append(scene_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT payload_json FROM shots{where} ORDER BY scene_id, shot_index",  # noqa: S608
            tuple(parameters),
        )
        return [Shot.model_validate_json(row["payload_json"]) for row in rows]

    def delete_shot(self, shot_id: str) -> bool:
        """Delete a shot and clear ownership references in one transaction.

        The foreign keys null the indexed columns, but the embedded JSON
        documents are the authoritative payloads readers validate, so asset,
        job, and attempt documents are rewritten with ``shot_id: null`` here.
        """
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for table, model in (
                    ("assets", Asset),
                    ("jobs", GenerationJob),
                    ("generation_attempts", GenerationAttempt),
                ):
                    rows = connection.execute(
                        f"SELECT id, payload_json FROM {table} WHERE shot_id=?",  # noqa: S608
                        (shot_id,),
                    ).fetchall()
                    for row in rows:
                        document = model.model_validate_json(row["payload_json"])
                        document = document.model_copy(update={"shot_id": None})
                        connection.execute(
                            f"UPDATE {table} SET payload_json=? WHERE id=?",  # noqa: S608
                            (document.model_dump_json(), row["id"]),
                        )
                cursor = connection.execute("DELETE FROM shots WHERE id=?", (shot_id,))
                deleted = bool(cursor.rowcount)
                connection.commit()
                return deleted
            except Exception:
                connection.rollback()
                raise

    def save_asset(self, asset: Asset) -> Asset:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO assets
                   (id, project_id, scene_id, shot_id, asset_type, filepath, created_at,
                    payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET scene_id=excluded.scene_id,
                   shot_id=excluded.shot_id, asset_type=excluded.asset_type,
                   filepath=excluded.filepath, payload_json=excluded.payload_json""",
                (asset.id, asset.project_id, asset.scene_id, asset.shot_id,
                 asset.type.value, str(asset.filepath), asset.created_at.isoformat(),
                 asset.model_dump_json()),
            )
        return asset

    def get_asset(self, asset_id: str) -> Asset | None:
        row = self._one("SELECT payload_json FROM assets WHERE id=?", (asset_id,))
        return Asset.model_validate_json(row["payload_json"]) if row else None

    def list_assets(self, project_id: str, scene_id: str | None = None,
                    shot_id: str | None = None) -> list[Asset]:
        clauses = ["project_id=?"]
        parameters: list[Any] = [project_id]
        if scene_id is not None:
            clauses.append("scene_id=?")
            parameters.append(scene_id)
        if shot_id is not None:
            clauses.append("shot_id=?")
            parameters.append(shot_id)
        rows = self._all(
            "SELECT payload_json FROM assets "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at",
            tuple(parameters),
        )
        return [Asset.model_validate_json(row["payload_json"]) for row in rows]

    def delete_assets_for_path(self, project_id: str, filepath_prefix: str) -> int:
        """Remove asset index rows under a project-relative directory prefix."""
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM assets WHERE project_id=? AND filepath LIKE ? ESCAPE '\\'",
                (project_id, filepath_prefix.replace("\\", "\\\\").replace("%", "\\%")
                 .replace("_", "\\_") + "%"),
            )
            return int(cursor.rowcount)

    def delete_asset(self, asset_id: str) -> bool:
        """Remove one asset index row after its media was intentionally discarded."""
        with self.connection() as connection:
            cursor = connection.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            return cursor.rowcount > 0

    def save_attempt(self, attempt: GenerationAttempt) -> GenerationAttempt:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO generation_attempts
                   (id, asset_id, job_id, scene_id, shot_id, success, created_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (attempt.id, attempt.asset_id, attempt.job_id, attempt.scene_id,
                 attempt.shot_id, int(attempt.success), attempt.created_at.isoformat(),
                 attempt.model_dump_json()),
            )
        return attempt

    def list_attempts(self, *, asset_id: str | None = None,
                      job_id: str | None = None,
                      scene_id: str | None = None,
                      shot_id: str | None = None) -> list[GenerationAttempt]:
        clauses, parameters = [], []
        if asset_id is not None:
            clauses.append("asset_id=?")
            parameters.append(asset_id)
        if job_id is not None:
            clauses.append("job_id=?")
            parameters.append(job_id)
        if scene_id is not None:
            clauses.append("scene_id=?")
            parameters.append(scene_id)
        if shot_id is not None:
            clauses.append("shot_id=?")
            parameters.append(shot_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT payload_json FROM generation_attempts{where} "  # noqa: S608
            "ORDER BY created_at",
            tuple(parameters),
        )
        return [GenerationAttempt.model_validate_json(row["payload_json"]) for row in rows]

    def list_scene_attempts(self, scene_id: str) -> list[GenerationAttempt]:
        return self.list_attempts(scene_id=scene_id)

    def save_job(self, job: GenerationJob) -> GenerationJob:
        with self.connection() as connection:
            self._save_job(connection, job)
        return job

    def _save_job(self, connection: sqlite3.Connection, job: GenerationJob) -> None:
        connection.execute(
            """INSERT INTO jobs
               (id, project_id, scene_id, shot_id, stage, status, priority, attempt_count,
                max_attempts, created_at, updated_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET scene_id=excluded.scene_id,
               shot_id=excluded.shot_id, stage=excluded.stage, status=excluded.status,
               priority=excluded.priority, attempt_count=excluded.attempt_count,
               max_attempts=excluded.max_attempts, updated_at=excluded.updated_at,
               payload_json=excluded.payload_json""",
            (job.id, job.project_id, job.scene_id, job.shot_id, job.stage, job.status.value,
             job.priority, job.attempt_count, job.max_attempts, job.created_at.isoformat(),
             job.updated_at.isoformat(), job.model_dump_json()),
        )

    def get_job(self, job_id: str) -> GenerationJob | None:
        row = self._one("SELECT payload_json FROM jobs WHERE id=?", (job_id,))
        return GenerationJob.model_validate_json(row["payload_json"]) if row else None

    def list_jobs(self, project_id: str | None = None,
                  status: JobStatus | None = None,
                  shot_id: str | None = None) -> list[GenerationJob]:
        clauses, parameters = [], []
        if project_id is not None:
            clauses.append("project_id=?")
            parameters.append(project_id)
        if status is not None:
            clauses.append("status=?")
            parameters.append(status.value)
        if shot_id is not None:
            clauses.append("shot_id=?")
            parameters.append(shot_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT payload_json FROM jobs{where} "  # noqa: S608
            "ORDER BY priority DESC, created_at",
            tuple(parameters),
        )
        return [GenerationJob.model_validate_json(row["payload_json"]) for row in rows]

    def update_job_in_transaction(
        self,
        job_id: str,
        updater: Callable[[GenerationJob], GenerationJob],
    ) -> GenerationJob:
        """Read, validate, and write a job atomically under BEGIN IMMEDIATE.

        The updater runs while the write transaction is held, so a concurrent
        cancel cannot interleave with a completion and lose either update.
        Any exception raised by the updater rolls the transaction back.
        Raises KeyError when the job does not exist.
        """
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload_json FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise KeyError(f"job not found: {job_id}")
                job = GenerationJob.model_validate_json(row["payload_json"])
                updated = updater(job)
                self._save_job(connection, updated)
                connection.commit()
                return updated
            except Exception:
                connection.rollback()
                raise

    def claim_queued_job(self, now: datetime) -> GenerationJob | None:
        """Atomically claim the highest-priority retry-eligible queued job."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT payload_json FROM jobs
                       WHERE status=? AND attempt_count < max_attempts
                       ORDER BY priority DESC, created_at ASC LIMIT 1""",
                    (JobStatus.QUEUED.value,),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                job = GenerationJob.model_validate_json(row["payload_json"])
                job.status = JobStatus.PREPARING
                job.attempt_count += 1
                job.started_at = now
                job.updated_at = now
                job.error = None
                self._save_job(connection, job)
                connection.commit()
                return job
            except Exception:
                connection.rollback()
                raise

    def record_prompt(self, project_id: str, role: str, prompt: str, *,
                      scene_id: str | None = None, shot_id: str | None = None,
                      negative_prompt: str = "", seed: int | None = None,
                      settings: dict[str, Any] | None = None,
                      created_at: datetime) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO prompts
                   (project_id, scene_id, shot_id, role, prompt, negative_prompt, seed,
                    settings_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, scene_id, shot_id, role, prompt, negative_prompt, seed,
                 json.dumps(settings or {}, sort_keys=True), created_at.isoformat()),
            )
            return int(cursor.lastrowid)

    def upsert_model_metadata(self, backend: str, model: str, version: str,
                              metadata: dict[str, Any], updated_at: datetime) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO model_metadata
                   (backend, model, version, metadata_json, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(backend, model, version) DO UPDATE SET
                   metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                (backend, model, version, json.dumps(metadata, sort_keys=True),
                 updated_at.isoformat()),
            )

    def record_render_metadata(self, project_id: str, filepath: str,
                               metadata: dict[str, Any], created_at: datetime) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO render_metadata
                   (project_id, filepath, metadata_json, created_at) VALUES (?, ?, ?, ?)""",
                (project_id, filepath, json.dumps(metadata, sort_keys=True),
                 created_at.isoformat()),
            )
            return int(cursor.lastrowid)

    def _one(self, sql: str, parameters: tuple[Any, ...]) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(sql, parameters).fetchone()

    def _all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return list(connection.execute(sql, parameters).fetchall())
