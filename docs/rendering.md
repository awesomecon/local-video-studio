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

### Editorial Mode caption styles

Editorial Mode separates captions from the composition's own text elements. The user picks a
style (`PATCH .../editorial/settings`, `caption_style`; "Off" is `captions_enabled: false`):

| style | behavior |
| --- | --- |
| `editorialPhrase` (default) | documentary phrase beats: 2–5 spoken words (up to 7 per line), one line preferred, condensed sans with a thin rust rule; emphasized phrases render as a cream paper block |
| `quietDocumentary` | same phrase beats, smaller and never highlighted |
| `oneLine` | short single-line beats (≤ 8 words / 30 chars) |
| `oneWord` | one word per beat |
| `standard` | the pre-existing large centered captions, burned from the shared ASS track like Classic Mode |

Documentary styles are rendered inside the deterministic Editorial master (a caption layer in the
same compiled HTML that preview and export share, so both show identical captions). `standard`
keeps the shared `subtitles/` ASS path, and Classic Mode always uses that path — Editorial changes
never touch Classic captions, and vice versa.

- Beats are built from `subtitles/word-timings.json` when available, otherwise from evenly spaced
  scene-narration timings, so preview and export agree. Phrase grouping splits on pauses,
  sentence ends, and planner emphasis spans.
- Highlight selection is planner metadata only (`caption_emphasis`: short verbatim phrases with
  `emphasis: "keyPhrase"`); the renderer decides the look. Without metadata a deterministic
  fallback emphasizes numeral phrases first, then all-caps phrases, and never highlights two
  back-to-back beats.
- Each beat gets a safe position from the renderer-owned layout regions of the active composition:
  lower-left is preferred, lower-center/mid-left/upper-left are fallbacks, and lower-right is
  never offered (Shorts UI zone). Beats overlap no composition content by more than a small
  margin; when every candidate collides the renderer falls back to the lower-left band.
- Major reveals own the screen: a fullscreen moment (e.g. the ELON reveal) hides captions that
  overlap it by ≥ 0.25 s, and a big headline hides captions that duplicate ≥ 60 % of its words.
  A caption that merely lingers into the first 250 ms of a reveal stays up.
- Font sizes live in a 1080×1920 / 1920×1080 design space (phrase normal ~48 px / highlight
  ~58 px portrait, smaller in landscape) and scale with the project's render dimensions.
- Beats and their positions persist to `editorial/captions.json`; per-composition clip digests
  include the caption cues, and re-running caption alignment invalidates the Editorial visual when
  the master bakes captions in. Changing the style (or captions on/off) invalidates
  `editorial_visual` whenever the master's caption content changes on either side.

## AI Edit Plan revisions

Editorial projects expose two instruction-led revision scopes from the composition list:

- **Revise this composition with AI** keeps the composition id, start, duration, template,
  narration references, caption references, and protected media fixed while allowing the planner
  to revise approved elements and motion events.
- **Revise sequence with AI** may add, split, remove, retime, or reorder compositions while
  preserving a contiguous narration-led timeline. Any composition containing locked, imported,
  evidence, or manually replaced media must remain present with the same template and exact
  protected bindings.

Revisions use a two-phase contract. `POST /api/projects/{id}/editorial/revisions` generates a
validated proposal and stores it under `editorial/revisions/` without changing the active Edit
Plan. The UI shows a structural before/after summary. Only the explicit Apply action calls `POST
/api/projects/{id}/editorial/revisions/{revision_id}/apply`. Application fails safely if the
active Edit Plan, script, narration timings, or registered assets changed after the preview was
generated. The planner still returns schema-validated composition data only; it cannot author
HTML, CSS, JavaScript, source paths, remote URLs, templates, or motion names outside the renderer
allowlists.

## Determinism guarantees

- Same inputs, same FFmpeg identity, same settings → same outputs; render commands are argv
  arrays (no shell), and outputs publish atomically after probe/QC.
- Regenerating content archives the prior variant instead of destroying it, so renders stay
  reproducible against any kept asset set.
