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
- `POST /api/projects/{project_id}/tts/narrations/import?name=...` (PCM WAV body; stores and
  activates an immutable user-recorded voiceover take)
- `GET /api/tts/gemini/key` (key availability only: `configured`, `source`, never the value)
- `PUT /api/tts/gemini/key` (stores the key in a user-private local secret file; `204`)
- `DELETE /api/tts/gemini/key` (deletes the stored key; `204`)
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

An imported recorded voiceover is copied to the project-owned narration-take library and activated
as `narration/master.wav`; the original WAV bytes are never altered. Classic rendering follows its
measured duration. Editorial rendering derives a proportional scene clock from the current script,
then retimes the deterministic composition canvas to the same master duration. Rebuild caption
alignment after selecting a recording when exact word-level caption timing is wanted.

## Fish S2 Pro and Higgs TTS 3 delivery tags

These endpoints manage independent delivery-tag scripts for `fish_s2_pro` and `higgs_tts_3`.
Each uses `narration/performance-tags-{provider}.json`; the legacy `performance-tags.json`
remains readable only for its recorded provider. Clean narration and captions are unchanged.
Pass `provider` in POST/PUT bodies or the GET/DELETE query string (default `fish_s2_pro`).
All reads, edits, regeneration, and deletion affect only that provider's script.

- `GET /api/projects/{project_id}/tts/performance-tags`
  Returns `{ script, stale, tag_count, llm, providers }`. `providers` maps both provider names
  to `{ script, stale, tag_count }` for switching editors. `script` is `null` when none exists; `stale`
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

## Gemini TTS key management

`gemini_tts` is the remote TTS provider (see `docs/gemini-tts.md`). Its key is resolved at
request time in this order: the `api_key_env` environment variable (default
`GEMINI_API_KEY`), then a file saved via `PUT /api/tts/gemini/key`.

- `GET /api/tts/gemini/key` returns `{ configured, invalid,
  source: "environment"|"file"|"none", api_key_env, secret_file, enabled, detail }`. It never
  returns the key or a fragment of it.
- `PUT /api/tts/gemini/key` accepts `{ "api_key": "..." }` and writes a user-private file under
  the application data directory (`0600` on POSIX, user-only ACL on Windows). Invalid keys (empty,
  whitespace, or control characters) are
  rejected with `422` and nothing is written. Successful saves return `204` without echoing the
  value.
- `DELETE /api/tts/gemini/key` removes the file (environment variables are untouched) and
  returns `204`.

`POST /api/projects/{project_id}/tts/generate` with `provider: "gemini_tts"` answers `409`
before queueing when the provider is disabled or no key is configured, and `422` when a voice
profile is combined with the provider (Gemini cannot clone). An optional `gemini_model`
request field overrides the configured model for that take (any well-formed Gemini TTS model
identifier; unknown IDs fail that take with a clear `model_unavailable` error). While generation
runs, only narration text, the optional delivery direction, and the key (as a
header) leave the machine.

`GET /api/tts/models` includes a `gemini_models` gallery (`[{ id, description }]`) and the
`default_model` on the `gemini_tts` entry; the Voice page builds its model picker from it.
