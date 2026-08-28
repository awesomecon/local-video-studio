# Multi-shot scenes

A scene is the narration and review unit; a **shot** is one timed visual beat inside the scene.
This lets a single narration scene hold a sequence — e.g. archival photo → generated still →
Graphic Screen label — with independent timing, transitions, and overlays, instead of one
scene = one visual.

## Model

```text
Project
  Scene                 narration/editorial unit
    Shot                sequential visual beat
      Visual asset      image, video, graphic screen, or imported media
      Overlay cues      exact text, graphic, or image overlays
      Audio cues        ambience / effect / native-clip audio
```

### Shots

A `Shot` (schema version 2, `backend/schemas/shots.py`) records:

- `lane` — editorial truth/source policy: `real` (imported archival/editorial media with
  provenance), `image` (local generated stills), `h3` (local motion generation), or `html`
  (deterministic local exact text / Graphic Screen);
- `visual_type` and `selected_backend` — the production implementation for that lane;
- `visual_prompt`, `negative_prompt`, `camera_instruction`, `seed`, `settings`;
- `duration_seconds` with `start_mode` `fixed` (never retimed) or `weighted` (the timing
  compiler may adjust it to fit the narration clock);
- `source_asset_id` plus optional `source_in_seconds` / `source_out_seconds` for trimming
  source media;
- `transition_in` — the incoming intra-scene transition;
- `references` with typed roles (`source_evidence`, `composition`, `style`, `character`,
  `first_frame`, `continuity`) and `status` (`draft … approved`, `failed`), `locked`, `settings`.

Scene-level fields remain as a compatibility projection: a legacy scene with no stored shots
compiles as one implicit shot.

### Intra-scene transitions

Supported kinds: `cut`, `crossfade` (`dissolve` is a stored alias that compiles identically),
`fade_through_black`, `dip_to_white`. A transition overlap must be shorter than both adjacent
shots. The scene's rendered duration is `sum(shot durations) − sum(intra-scene overlaps)`.

### Overlays and audio cues

Overlay cues (`exact_text`, `graphic`, `image`) carry canvas-pixel placement (one of nine named
anchors, optional size, `contain`/`cover`/`stretch` fit, opacity, and fade in/out windows that
must fit inside the shot). Exact text is rasterized through the same sanitized local
Chromium/HTML path as Graphic Screens — no client-authored HTML/CSS or FFmpeg expressions reach
the renderer. Audio cues (`ambience`, `effect`, `native_clip`) mix per a policy
(`mute`, `under_narration`, `foreground`); generated H3 native audio defaults to `mute` so
narration stays authoritative.

## Timing compiler

`compile_scene_plan` (in `backend/timeline/shots.py`) snaps every boundary to the project's
frame grid (`1/fps`). If the compiled scene length is within one frame of the measured narration
clock it is accepted as-is; otherwise the delta is distributed across `weighted` shots, keeping
every duration positive and strictly longer than its incoming overlap. With no weighted shots and
a mismatch, compilation fails with a structured `ShotTimingError` — fixed or locked shots are
never silently stretched.

## Storage and database

- Portable layout: `scenes/<NNN>/shots/<MMM>/shot.json` (plus per-shot media). The numbered
  directory is re-synced on every write so reordering/removal leaves no orphaned directories;
  stable identity is the shot ID inside `shot.json`, never the number.
- SQLite carries a real `shots` table (`UNIQUE(project_id, scene_id, shot_index)`, complete
  payload) and nullable `shot_id` columns on assets/jobs/attempts/prompts, upgraded from
  version 1 by ordered, transaction-safe migrations. `PROJECT_SCHEMA_VERSION` / `SCHEMA_VERSION`
  are 2; version-1 projects open and render unchanged.

## API surface

```text
GET    /api/scenes/{scene_id}/shots
POST   /api/scenes/{scene_id}/shots
PATCH  /api/shots/{shot_id}
DELETE /api/shots/{shot_id}
POST   /api/shots/{shot_id}/approve
POST   /api/shots/{shot_id}/generate
POST   /api/shots/{shot_id}/regenerate
POST   /api/scenes/{scene_id}/reused-media        (import REAL-lane assets)
POST   /api/scenes/{scene_id}/imported-image      (import generated/other images)
POST   /api/shots/{shot_id}/reused-media
POST   /api/shots/{shot_id}/imported-image
POST   /api/shots/{shot_id}/overlays
PATCH  /api/shots/{shot_id}/overlays/{overlay_id}
DELETE /api/shots/{shot_id}/overlays/{overlay_id}
PATCH  /api/overlays/{overlay_id}
```

Imported REAL assets record provenance (title, creator, source URL, access date, license note)
and a `documentary_evidence` / `editorial_context` / `illustration` classification; generated
material that could be mistaken for evidence carries a visible badge.

## Staleness and restartability

Regenerating a shot invalidates the shots that depend on it (directly or transitively, e.g.
through H3 first-frame continuity) as **stale** — their existing media stays reviewable until
the user regenerates them in order. Generation is restartable per shot: prompts, seeds, model
identity, workflow versions, attempts, and output hashes persist with every asset and attempt.

## Frontend

The Scene Editor shows an ordered shot strip below the narration form: add, duplicate, split,
reorder, archive, and a selected-shot form (lane, visual type, prompt/source, duration, motion,
transition, seed) plus overlay cues and a provenance panel for REAL assets. The Timeline expands
a scene into its shots and intra-scene transitions, laid out from backend timing data including
transition overlaps.

## Documentary production

The vertical-slice fixture `tests/fixtures/multishot_vertical_slice.yaml` pins the manifest shape
and lane/overlay vocabulary for a 79-scene documentary (representative scenes 1, 4, 15, 24, 47,
56, 79), with narration copied verbatim from the supplied outline. A bulk production-plan
importer is not wired yet; scenes and shots are authored through the UI or the shot API above.
The production method (lane choice, protected text, reused-media provenance) is documented in
[`production-workflow.md`](production-workflow.md).
