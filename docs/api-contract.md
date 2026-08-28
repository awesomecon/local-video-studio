# Local API contract

The API binds to loopback by default and uses `/api` as its prefix.

Core endpoints:

- `GET /api/system/status`
- `GET /api/models` (descriptors plus Studio-known lifecycle/ownership state)
- `GET /api/llm/models` (optional `project_id` returns that project's explicit selection)
- `PUT /api/llm/models`
- `GET /api/jobs`
- `POST /api/projects`
- `GET /api/projects/{project_id}`
- `PATCH /api/projects/{project_id}`
- `GET /api/projects/{project_id}/assets/{asset_id}/file`
- `POST /api/projects/{project_id}/plan`
- `POST /api/projects/{project_id}/script`
- `POST /api/projects/{project_id}/render`
- `POST /api/scenes/{scene_id}/generate`
- `POST /api/scenes/{scene_id}/regenerate`
- `POST /api/scenes/{scene_id}/approve`
- `POST /api/jobs/{job_id}/cancel`
- `GET /api/events` (server-sent job progress events)

Errors use `{ "detail": { "code": string, "message": string, "action": string|null } }`.
Authorization headers and secret environment-variable values are never represented in responses.

## Render contract

`POST /api/projects/{project_id}/render` queues deterministic local media assembly only. It consumes
the project's existing narration and current scene visuals, plus existing optional music/caption
inputs. It runs timeline, preview, quality-control, final FFmpeg render, and final-video frame
extraction stages. It never plans scripts or invokes LLM, TTS, or visual-generation backends.

The endpoint returns `409` before queueing when required existing inputs are missing or invalid.
`{"force": true}` rebuilds only the deterministic render stages; it does not regenerate content.
Queued jobs use `stage: "render"`, `backend: "ffmpeg"`, and expose the active step in
`parameters.current_stage`.

## Fish S2 Pro delivery tags

These endpoints manage the optional, Fish-S2-Pro-only delivery-tag script. The tagged text is a
separate portable artifact (`narration/performance-tags.json`); the clean scene narration and
caption transcript are never modified, and cues are never sent to any other TTS provider.

- `GET /api/projects/{project_id}/tts/performance-tags`
  Returns `{ script, stale, tag_count, llm }`. `script` is `null` when none exists; `stale`
  is true when a stored segment's source no longer matches the current narration; `llm`
  reports whether the local LLM is available and which model would be used.
- `POST /api/projects/{project_id}/tts/performance-tags`
  Body `{ intensity?: "subtle"|"balanced"|"expressive", notes?: string, text?: string|null,
  force?: bool }`. `text` overrides the narration source (the Script-override path); omit it to
  tag the planned scene narration. Without `force`, an existing script is returned unchanged.
  Returns `{ script, tag_count, warnings }`. Synchronous, like `POST /plan`.
  Errors: `404` unknown project, `409` no narration text or no
  LLM selected, `422` validation failure, `502` other backend error.
- `POST /api/projects/{project_id}/tts/performance-tags/regenerate`
  Body `{ key, intensity?: "subtle"|"balanced"|"expressive", notes?: string }`. Re-tags a
  single segment (by its stored `key`) with the local LLM; every other segment keeps its
  stored tags. The regenerated segment is validated (and repaired) by the tagger, so it is
  always safe to persist, and any previously-accepted hand edits on the other segments are
  preserved. Returns `{ script, tag_count, warnings }`. Synchronous, like `POST /plan`.
  Errors: `404` unknown project, `409` no LLM selected, `422` no script or unknown `key`,
  `502` other backend error.
- `PUT /api/projects/{project_id}/tts/performance-tags`
  Body `{ segments: [{ key, tagged }] }`. Saves hand-edited tagged text; each segment is
  validated against its clean source (same spoken words plus balanced, nonempty,
  single-line cues under a length-scaled anti-over-tagging ceiling; open-domain cue
  descriptions may be multilingual and combined). `?accept=true` keeps a hand edit the
  validator dislikes and logs it as `manually_edited`. Errors: `404` unknown project or no
  script, `422` validation failure.
- `DELETE /api/projects/{project_id}/tts/performance-tags`
  Removes the stored script. Returns `{ deleted: true }`. `404` for an unknown project.
