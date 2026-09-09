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
| `POST /api/projects/{id}/render/stages/{stage}` | `renderStage()` | Re-run one deterministic stage (`timeline`, `render_preview`, `quality_control`, `render_final`, `thumbnails`; `editorial_visual` for editorial); 202 → `GenerationJob`, 404 unknown stage/project, 400 inapplicable `editorial_visual`, 409 when a render/pipeline/stage job is in flight |
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
- Implicit shots are materialized on first mutation: `PATCH`/`DELETE /api/shots/<scene>-implicit`, all three overlay routes, and `generate`/`regenerate` now materialize the legacy projection server-side (`_resolve_shot` in `pipeline/service.py`), so the Scene Editor no longer needs the create-and-archive placeholder workaround.
- `shot_summary` carries a `stale` count and every shot payload carries a per-shot `stale` flag plus a `staleness` provenance marker (`{source_shot_id, reason, marked_at}`), so the Storyboard, Timeline, and Scene Editor surface real stale counts and an "approved but stale" explanation (approval keeps the marker by design; regeneration or media re-import clears it).
- Individual deterministic stages can now be re-run on their own: `POST /api/projects/{id}/render/stages/{stage}` queues a single `render_stage` job (backend `ffmpeg`) that runs only the matching `_ensure_*` runner — never LLM, TTS, or visual generation. Allowed stages are `timeline`, `render_preview`, `quality_control`, `render_final`, and `thumbnails`, plus `editorial_visual` for Editorial Mode projects (400 otherwise); an unknown stage is a 404 and an in-flight render/pipeline/stage job is a 409. The job is retryable and cancelable like other FFmpeg jobs, and the re-run's `stage_state` record is rewritten on force. The Export screen offers a per-stage "Re-run" button with a scope-scoped confirmation, wired to the shared live job feed.

## Remaining gap

None. The last open item — individual deterministic-stage control — was resolved
during integration (see above).

## Multi-shot contracts (Phase 3 frontend review, branch `frontend-shots`)

Verified working against commit f3490ff with typed methods in `js/api.js`
and the shared domain helper module `js/shots.js`:

| Route | Frontend method | Notes |
|---|---|---|
| `GET /api/scenes/{id}/shots` | `listSceneShots()` | stored shots + implicit projection, `{count, materialized, ready, approved, failed, stale, rendered_duration_seconds, scene_duration}` |
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
(with `implicit: true` on projected entries, plus per-shot `stale`/`staleness`)
plus `shot_summary` (including the `stale` count).

### Shot generation and scene rendering (implemented)

The Scene Editor queues `generate` and `regenerate` per shot and `render` per
scene. It requires visible shot-form changes to be saved or reverted first,
confirms forced regeneration, reports the queued job id, and leaves terminal
status/error reporting to the shared live job feed. Unsupported lane/type
combinations are rejected before a job row is created.
