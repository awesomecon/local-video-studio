# API gaps and backend change requests

Integration notes for the Local Video Studio frontend and backend contract.

## Verified API surface (no gap)

All current backend routes have typed frontend methods in `js/api.js` with
matching response shapes (checked against `backend/api/main.py`):

| Route | Frontend method | Notes |
|---|---|---|
| `GET /health` | `health()` | `{status, mode: "mock" \| "local"}` |
| `GET /api/system/status` | `systemStatus()` | `environment`, `gpu.devices[]`, `active_model`, `ports`, `mock_mode` |
| `GET /api/models` | `models()` | `{models: {name: BackendDescriptor}, runtime: {name: ModelRuntimeStatus}}` |
| `GET /api/llm/models` | `llmModels()` | `{endpoint, models: [{id}], selected_model}`; 503 + backend error object when the LLM server is down |
| `PUT /api/llm/models` | `selectLlmModel()` | Validates discovery; optionally persists the project selection |
| `GET /api/projects` | `listProjects()` | `{projects: Project[]}` |
| `POST /api/projects` | `createProject()` | 201 → `ProjectSnapshot` |
| `GET /api/projects/{id}` | `getProject()` | `ProjectSnapshot` (project, scenes, assets, jobs, directory, stage_state) |
| `PATCH /api/projects/{id}` | `editProject()` | Merges portable settings and invalidates affected stages |
| `DELETE /api/projects/{id}` | `deleteProject()` | Permanently removes the portable directory and all database rows; 409 while jobs are queued/running |
| `GET /api/projects/{id}/assets/{asset_id}/file` | asset `url` | Project-scoped local media delivery |
| `POST /api/projects/{id}/plan` | `planProject()` | → `ProjectPlan` |
| `POST /api/projects/{id}/script` | `scriptProject()` | → `ProjectPlan` |
| `POST /api/projects/{id}/render` | `renderProject()` | Existing-media FFmpeg render; 202 → `GenerationJob`, 409 when inputs are missing |
| `PATCH /api/scenes/{id}` | `editScene()` | partial `SceneEdit` |
| `POST /api/scenes/{id}/generate` | `generateScene()` | 201 → `Asset` |
| `POST /api/scenes/{id}/regenerate` | `regenerateScene()` | 201 → `Asset` |
| `POST /api/scenes/{id}/approve` | `approveScene()` | → `Scene` |
| `GET /api/jobs` | `listJobs()` | `{jobs: GenerationJob[]}`; rows carry `executable`/`cancelable` UI hints (Retry/Cancel button gating; also on cancel/retry responses and SSE frames) |
| `POST /api/jobs/{id}/cancel` | `cancelJob()` | → `GenerationJob` |
| `POST /api/jobs/{id}/retry` | `retryJob()` | → `GenerationJob` |
| `GET /api/events` | SSE in `js/events.js` | `event: jobs` with full array; polling fallback |

## Resolved during integration

- Project settings can now be updated with `PATCH /api/projects/{id}`.
- Generated assets are exposed through project- and asset-scoped, traversal-safe local URLs.
- Caption files use the same asset endpoint.
- Local LLM model selection is available through `PUT /api/llm/models` and can be persisted to project metadata.

## Remaining gap

### No individual deterministic-stage control (minor)

- **Affected screen:** Export (re-run one deterministic output stage).
- **Verified:** `POST /api/projects/{id}/render` queues only existing-media
  timeline, preview, QC, final-video, and frame-extraction work. It never invokes
  LLM, TTS, or visual generation. `stage_state` reports per-stage status/outputs,
  but there is no route to re-run just one of those deterministic stages.
- **Requested change (optional):** `POST /api/projects/{id}/render/stages/{stage}`
  returning the queued `GenerationJob`. Until then the Export screen offers
  render, deterministic re-render, and job cancellation.

## Multi-shot contracts (Phase 3 frontend review, branch `frontend-shots`)

Verified working against commit f3490ff with typed methods in `js/api.js`
and the shared domain helper module `js/shots.js`:

| Route | Frontend method | Notes |
|---|---|---|
| `GET /api/scenes/{id}/shots` | `listSceneShots()` | stored shots + implicit projection, `{count, materialized, ready, approved, failed, rendered_duration_seconds, scene_duration}` |
| `POST /api/scenes/{id}/shots` | `createShot()` | 201 → `Shot`; materializes the implicit shot first on legacy scenes |
| `PATCH /api/shots/{shot_id}` | `editShot()` | partial edit; `index` moves reorder atomically; 409 when locked |
| `DELETE /api/shots/{shot_id}` | `deleteShot()` | guarded archive; returns `{deleted_shot_id, archived_assets[], remaining_shots, scene_reverted_to_implicit}` |
| `POST /api/shots/{id}/approve` | `approveShot()` | also the only endpoint that materializes an implicit id |
| `POST /api/shots/{id}/generate` / `.../regenerate` | `generateShot()` / `regenerateShot()` | queues `shot_generate`; regenerate forces replacement and archives current media |
| `POST /api/scenes/{id}/render` | `renderScene()` | queues deterministic `scene_render` compilation through FFmpeg |
| `POST /api/shots/{id}/overlays` | `addShotOverlay()` | 201 → updated `Shot` |
| `PATCH/DELETE /api/shots/{id}/overlays/{overlay_id}` | `patchShotOverlay()` / `removeShotOverlay()` | partial cue edit / removal |
| `PATCH /api/overlays/{overlay_id}?project_id=` | `patchProjectOverlay()` | project-scope resolution for embedded cues |

Snapshot integration verified too: each scene payload carries `shots[]`
(with `implicit: true` on projected entries) plus `shot_summary`.

### Gap: implicit shots are refused by every mutation endpoint except approve (workaround shipped)

- **Affected screens:** Scene Editor shot strip/forms for any scene that
  predates stored shots (i.e. every existing project).
- **Verified:** `GET .../shots` projects a deterministic implicit shot with id
  `<scene-id>-implicit`, but `PATCH /api/shots/<scene>-implicit`,
  `DELETE /api/shots/<scene>-implicit`, and all three overlay routes return
  404 `shot not found` for that id, because `_update_shot_locked`,
  `_delete_shot_locked`, and `_editable_shot_context` resolve through
  `database.get_shot()` and never materialize. Only `POST .../approve`
  special-cases the implicit id (`pipeline/service.py` `approve_shot`).
- **Frontend workaround:** before the first save/move/archive/overlay
  mutation of an implicit shot, the Scene Editor creates a placeholder shot
  at index 0 (which materializes the projection verbatim) and immediately
  archives that placeholder, leaving exactly the materialized shot behind.
  This works but costs two extra requests and briefly inserts a row.
- **Requested change (integration):** materialize-on-first-mutation inside
  `update_shot` / `delete_shot` / `_editable_shot_context` (mirroring
  `approve_shot`), or expose an explicit
  `POST /api/scenes/{scene_id}/shots/materialize`.

### Gap: shot_summary has ready/approved/failed but no stale count

- **Affected screens:** Storyboard cards and Timeline (the plan asks cards to
  show "stale/error counts").
- **Verified:** `shot_summary` = `{count, materialized, ready, approved,
  failed, rendered_duration_seconds}`; neither it nor the shot payloads carry
  a stale flag (H3 continuity staleness is computed per-scene elsewhere).
- **Current behavior:** the Storyboard shows `n/m ready`, a failed badge, and
  a derived "pending" remainder (`count − ready − failed`) instead of a real
  stale count.
- **Requested change:** add `stale` to `shot_summary` (and a per-shot flag)
  when the Phase 4 stale-dependency graph lands.

### Shot generation and scene rendering (implemented)

The Scene Editor queues `generate` and `regenerate` per shot and `render` per
scene. It requires visible shot-form changes to be saved or reverted first,
confirms forced regeneration, reports the queued job id, and leaves terminal
status/error reporting to the shared live job feed. Unsupported lane/type
combinations are rejected before a job row is created.
