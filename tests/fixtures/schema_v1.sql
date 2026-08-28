-- Exact pre-change (schema version 1) SQLite baseline for upgrade tests.
-- Mirrors backend/storage/database.py before the shots migration.
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
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_id TEXT REFERENCES scenes(id) ON DELETE SET NULL,
    asset_type TEXT NOT NULL,
    filepath TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_attempts (
    id TEXT PRIMARY KEY,
    asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    success INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_id TEXT REFERENCES scenes(id) ON DELETE SET NULL,
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
CREATE INDEX IF NOT EXISTS idx_scenes_project ON scenes(project_id, scene_index);
CREATE INDEX IF NOT EXISTS idx_assets_project_scene ON assets(project_id, scene_id);
CREATE INDEX IF NOT EXISTS idx_attempts_asset ON generation_attempts(asset_id);
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs(status, priority DESC, created_at ASC);
INSERT OR IGNORE INTO schema_info(version) VALUES (1);
