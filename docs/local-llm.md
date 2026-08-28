# Local LLM integration

Local Video Studio uses the user's existing OpenAI-compatible LLM server as its text brain.
The server is externally managed: the application never starts, stops, restarts, or unloads it,
and port `127.0.0.1:1234` is reserved for it. Planning, scripting, prompting, and QC calls all go
to that one local endpoint; nothing is sent to remote inference services.

## Configuration

- `llm.base_url` — default `http://127.0.0.1:1234/v1`;
- `llm.api_key_env` — the key is read only from the named environment variable
  (`LOCAL_LLM_API_KEY` by default). It is never written to config, logged, or echoed;
- `llm.timeout_seconds` — default 600; plan/script generation on a reasoning model can be slow,
  so keep the timeout generous rather than truncating requests;
- `llm.model: auto` — discover the server's models from `GET /v1/models` on first use. A specific
  model ID can be selected instead; the selection persists per project
  (`GET`/`PUT /api/llm/models`) and `auto` falls back to the first discovered model.

Roles (`llm_roles`: `writer`, `director`, `prompt_engineer`, `qc`) may each select their own model
or stay `auto`, so a strong reasoning model can serve the director while a smaller model serves
faster roles.

## Director generation contract

Script/plan generation is schema-constrained JSON: the director receives a `DirectorPlanDraft`
schema (outline, scenes, narration, visual prompts, backends, transitions, seeds) via the router's
`response_format`, keeps the system prompt concise, and the application deterministically
materializes IDs, indexes, statuses, and durations from the returned draft.

- Reasoning ("thinking") stays enabled by user preference: the director gets a fixed
  10,000-token reasoning budget. Do not shrink it as a latency optimization; script quality is
  the priority.
- The completion budget is computed per request: `10000 + 2048 + 640 × scene ceiling`, so long
  projects are not truncated into a retryable `finish_reason=length`.

### Scene-count bounds

For a project with a fixed target duration, the director is asked for:

- minimum `clamp(ceil(target / 20), 3, 24)` scenes (about one scene per 20 seconds), and
- maximum `min(128, max(minimum, ceil(target / 5)))` scenes, capped by the project scene ceiling
  (`MAX_PROJECT_SCENES = 128`).

Projects in LLM-duration mode send no requested runtime: the model owns the runtime (authored
scene durations are adopted as the project target), and the bound is `3 … 128`.

## Robustness behavior

The backend classifies failures so the UI shows an actionable, structured error rather than a
generic connection failure:

- `finish_reason: length` → the response was truncated at its token limit: a retryable error
  advising a shorter script or a larger completion limit (never an attempt to parse truncated JSON);
- refusal or `content_filter` → retryable structured error;
- missing/malformed completion content → non-retryable invalid-response error.

Recoverable metadata (outline, titles, indexes, durations, visual prompts, backends, transitions,
seeds, …) is normalized to safe defaults instead of rejecting the whole script: a scene missing
narration gets a project-specific fallback, invalid enums/aliases/durations are repaired, and the
strict schema sent to the model is preserved unchanged. Only fundamentally unusable results are
fatal: no parseable JSON, a non-object plan root, or no usable scenes. Validation diagnostics
report field paths and counts without echoing model-authored script content, and no key value,
authorization header, or completion text is ever printed.

## Safety

- The LLM endpoint is verified, not assumed: identity is checked (a non-OpenAI service on the
  port is reported as incompatible rather than talked to).
- The application closes its request connections after generation; it does not control the
  server's model lifecycle. If the loaded model occupies too much VRAM for a cold model load
  elsewhere in the pipeline, the user unloads it in their own LLM application.
- Model IDs are discovered, never hardcoded; nothing about the LLM download is triggered by the
  application.
