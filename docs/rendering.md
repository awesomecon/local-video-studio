# Rendering and final output

FFmpeg performs all deterministic editing and final rendering. Generation backends produce media
assets; the renderer assembles them. The renderer discovers a system FFmpeg or falls back to the
bundled `imageio-ffmpeg` executable (see `docs/troubleshooting.md`).

## Pipeline order

```text
timeline → render_preview → quality_control → render_final → thumbnails
```

(Upstream stages — plan, script, narration, visuals, music, captions — feed the timeline; the
render stages themselves never invoke LLM, TTS, or generation backends.)

- `POST /api/projects/{id}/render` queues only the deterministic render stages from existing
  narration and current scene visuals. `{"force": true}` re-runs the render stages without
  regenerating content.
- Every stage is restartable: completion and outputs persist, and a restart resumes from the
  first incomplete stage.

## Duration policy: visuals follow the narration

The measured PCM duration of `narration/master.wav` is authoritative. When it is longer than the
planned scene total, the renderer **extends** the visual timeline — every scene's base duration
scales proportionally (the last scene absorbs floating-point residue), never cutting, speeding up,
or time-stretching the voice. Shorter narration does not contract the timeline.

- Scene videos that end before their assigned duration **hold their final decoded frame**
  (`tpad=stop_mode=clone` followed by an exact trim); they are never looped, and no black gap
  appears. Stills, title cards, and image-motion shots simply render for the adjusted duration.
- Music may loop to the final duration but never decides it.
- `timeline.json` records the policy (`extend_visuals_to_narration_v1`), planned total, measured
  narration duration, and a `visuals_extended` flag; recorded workflow versions are
  `timeline-v2` / `render-v2` so old-policy artifacts are never confused with new ones.
- Finalizer `apad`/`atrim`/`-t` remain as safety boundaries once the timeline is long enough.
  FFmpeg `-shortest` is deliberately not used, because it would make output length depend on
  whichever stream is longest.

## Preview and quality control

- `renders/preview.mp4` is rendered first; QC verifies its duration, configured preview
  resolution, and audio presence, and the report merges into `renders/qc.json`.
- Timeline QC accepts source-video duration differences (last-frame extension is renderer-owned),
  while still catching gaps, invalid transition timing, overflow, and corrupt sources.

## Final render

- Final assembly mixes narration and music under deterministic gain/ducking policy; generated H3
  clip audio is preview-only by default (narration and music are authoritative), and preserving
  per-clip native audio is a separate, explicit policy choice.
- `renders/final.mp4` is the delivery output, with final-video frame extraction for review.

## Thumbnails

Two complementary sources:

1. **Extracted frames** — three deterministic frames at 20%, 50%, and 80% of the completed video.
2. **Thumbnail Studio** — generated candidate artwork (Krea 2 Turbo or Ideogram 4 backgrounds)
   flattened with deterministic local typography for exact titles/hooks. Candidates are
   project-scoped and restartable; thumbnail-only edits invalidate thumbnail state only, and an
   unchanged plan reuses the saved artwork plan without regenerating.

## Captions

Caption alignment runs a locally stored Whisper `large-v3-turbo` (CTranslate2) model over
`narration/master.wav` — never over the script text — and records actual word timings in
`subtitles/word-timings.json`. SRT/ASS files are built from those timestamps, so captions track
the real audio. `GET /api/captions/models` exposes the descriptor, install guidance, health, and
readiness (the Models & System Status screen surfaces this); the optional dependency and model
directory must be configured first, and nothing is downloaded automatically.

## Determinism guarantees

- Same inputs, same FFmpeg identity, same settings → same outputs; render commands are argv
  arrays (no shell), and outputs publish atomically after probe/QC.
- Regenerating content archives the prior variant instead of destroying it, so renders stay
  reproducible against any kept asset set.
