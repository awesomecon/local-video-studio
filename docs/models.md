# Model backends

All generators implement one lifecycle contract: descriptor/capabilities, health, load, unload,
generate, cancel, and resource estimation. A descriptor records model/version, quantization, device,
VRAM needs, and supported inputs/outputs.

## Existing local LLM

The primary text backend is the user's OpenAI-compatible server at `http://127.0.0.1:1234/v1`.
Model IDs are discovered from `/models`; none is hardcoded. The selected ID is persisted, but the API
key is read only from `LOCAL_LLM_API_KEY`. JSON output is validated and repaired conservatively.
Generation budgets, scene-count bounds, and robustness behavior are documented in
`docs/local-llm.md`.

## ComfyUI, FLUX, and Wan

ComfyUI remains a separate local service. FLUX image tasks and Wan video tasks initially use reusable
workflow JSON with prompt, seed, and input substitutions. Outputs are downloaded only from the local
service. Custom workflows are supported without coupling the orchestration API to node implementations.

## MiniMax H3

H3 is a premium local worker/service for FL2VA, Ref2VA, image/text/reference-conditioned video, and
native audiovisual output when the installed model supports it. Cloud-only functionality is disabled.
See `docs/h3.md`.

## Local still images

Krea 2 Turbo is the default ordinary-still and Image Motion source. Image Motion scenes can instead
select Qwen-Image-2512 when readable lettering belongs inside the generated image; FFmpeg applies the
same camera move after either backend creates the still. Graphic Screen remains the deterministic
choice for character-perfect titles, tables, charts, and interfaces. See `docs/local-krea2.md` and
`docs/local-qwen-image-2512.md`.

`Generated background + exact text` is the first-class hybrid still type for cinematic artwork
that also needs guaranteed wording. It generates a text-free background with Krea 2 Turbo by
default (Ideogram or Qwen may be selected as the background engine), keeps the literal strings out
of that request, and then flattens locally rendered Unicode typography over the result. The saved
`visual.png` is the finished composite, while `generated-background.png` preserves the raw artwork.
Use one distinct text region per `text_in_image` line; layouts are automatically fitted to the
mobile-safe area. The available layout presets are `auto`, `hook`, `reveal`, `quote`, and `cta`.
Automatic mode recognizes short centered reveals, long two-region quotations, and stacked
`FULL VIDEO` calls to action. Background generation always receives explicit no-text directions;
an empty visual prompt falls back to the scene title and narration instead of producing an
unconditioned texture. The v3 compositor uses smaller, translucent adaptive scrims and keeps CTA
baselines above the lower mobile-control area; it archives the prior composite when an existing
background is recomposited.

Use Graphic Screen—not the hybrid still—for timelines, diagrams, Scripture, long quotations,
tables, and other scenes where typography is the whole composition. The hybrid still is for
cinematic artwork that happens to need one or two concise exact-text regions. Graphic Screen v2
prompting requires separate non-overlapping text boxes, explicit grid/flex gaps, bounded font sizes,
and 8% vertical / 6% horizontal mobile-safe margins; connector lines must stop before labels.

Ideogram 4 NF4 is available as a first-class scene and shot still type and handles imagery that
requires readable embedded text. It runs in a separate localhost
ComfyUI service on port 8190 so its Torch requirement cannot alter the shared ComfyUI environment.
Quick mode constructs and validates Ideogram JSON with the vendored open-source Magic Prompt and the
configured local LLM; Precise mode accepts native Ideogram/KJNodes JSON. Neither calls a remote
Magic Prompt service. Thumbnail Studio persists its caption before VRAM/model loading and exposes a
separate regenerate-and-preview action. It also supports plan-backed Precise JSON that bypasses the
LLM and validates the exact title and hook. Quick thumbnails receive collision-safe object/text
regions; delivery-critical lettering should still use the deterministic compositor. See
`docs/local-ideogram4.md`.

## Multi-shot scenes

Scenes can contain multiple timed shots (REAL/IMAGE/H3/HTML lanes, intra-scene transitions,
overlays, and audio cues) with narration-clock fitting; see `docs/shots.md`. Rendering, the
narration-extended duration policy, preview/QC, thumbnails, and captions are documented in
`docs/rendering.md`.

## Narration, music, and transcription

Chatterbox supplies narration when installed in a compatible backend environment. Reference voices
must be explicitly provided and authorized. ACE-Step produces instrumental documentary music by
default. Caption alignment uses a locally stored Faster-Whisper `large-v3-turbo` CTranslate2 model
after `narration/master.wav` is complete. It records word timings in
`subtitles/word-timings.json`, then creates SRT/ASS from those actual audio timestamps rather than
estimating from script length. The optional `captions` dependency and a local model directory must be
configured first; neither packages nor weights are downloaded automatically. Mock renders retain
deterministic placeholder timings.

## Compatibility policy

No adapter may silently install packages or weights. A backend reports `available`, `unconfigured`, or
`incompatible` with exact remediation. If it pins a conflicting PyTorch version, only that worker gets
an isolated environment; shared caches prevent duplicate checkpoint downloads.
