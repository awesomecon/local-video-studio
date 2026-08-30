# Local TTS services

Local Video Studio keeps dependency-heavy speech models in isolated Python environments (or behind
the shared ComfyUI service). The dashboard talks to one loopback worker per provider; it does not
import model packages into the application environment.

Providers, by runtime:

| Provider | Runtime | Port(s) |
| --- | --- | --- |
| Qwen3-TTS 1.7B Base / CustomVoice | isolated venv worker | 8191 |
| Step-Audio-EditX | isolated venv worker | 8192 |
| Chatterbox Multilingual V3 | isolated venv worker | 8193 |
| OmniVoice (k2-fsa) | isolated venv worker | 8194 |
| Breeze TTS 2 | managed worker + one official-API child | 8195 (worker), 8196 eager / 8197 fast (children) |
| Fish Audio S2 Pro | shared ComfyUI workflow | 8188 |
| VoxCPM2 2B | shared ComfyUI workflow | 8188 |
| IndexTTS 2.5 | shared ComfyUI workflow | 8188 |

All ports bind to `127.0.0.1` and are reserved in `config/default.yaml` so no other configured
endpoint can claim them.

## Installed layout (reference example)

All paths below are configurable. The reference installation used `~/ai/services/...`
environments and a central model root (`<model-root>`) on a secondary data volume; any writable
location works.

| Provider | Environment/source | Model weights | Port |
| --- | --- | --- | --- |
| Qwen3-TTS 1.7B Base | `~/ai/services/Qwen3-TTS/.venv` | `<model-root>/tts/qwen/Qwen3-TTS-12Hz-1.7B-Base` | 8191 |
| Qwen3-TTS 1.7B CustomVoice | same Qwen environment | `<model-root>/tts/qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | 8191 |
| Step-Audio-EditX | `~/ai/services/Step-Audio-EditX/.venv` | `~/ai/models/tts/step/Step-Audio-EditX` and `Step-Audio-Tokenizer` | 8192 |
| Chatterbox Multilingual V3 | `~/ai/services/chatterbox/.venv` | `~/ai/models/tts/chatterbox-v3` | 8193 |
| OmniVoice | `<service-root>/OmniVoice/.venv` (locked environment) | `<model-root>/tts/omnivoice/OmniVoice` (about 3.1 GB) | 8194 |
| Breeze TTS 2 | `<service-root>/breeze-tts/.venv` | `<model-root>/tts/breeze/Breeze-TTS-2` (about 7.0 GB) | 8195 |

Chatterbox's selected Multilingual V3 inference files are installed at the
listed model path; unrelated checkpoints from the larger repository were not downloaded.

## Automatic worker lifecycle

The dashboard starts the selected worker automatically when narration begins,
waits for it to become healthy, loads the model, and stops the worker after the
job. Qwen-to-Step jobs stop Qwen before starting Step. Workers bind only to
`127.0.0.1`, and the supervisor never stops a process it did not start.

The commands below remain available for diagnostics, but normal dashboard use
does not require them:

```bash
scripts/start_tts_worker.sh qwen_tts
scripts/start_tts_worker.sh step_audio_editx
scripts/start_tts_worker.sh chatterbox
```

The script binds only to `127.0.0.1`. Override paths with `LVS_QWEN_MODEL`,
`LVS_STEP_MODEL`, `LVS_STEP_TOKENIZER`, `LVS_CHATTERBOX_MODEL`, or
`LVS_TTS_OUTPUT_ROOT`. Worker caches default to `~/ai/cache` and can be moved
with `LVS_AI_CACHE_ROOT`. Enable/configure the matching backend in the application
configuration when its worker is running.

## Additional voice-cloning providers

### ComfyUI-based providers (Fish S2 Pro, VoxCPM2, IndexTTS 2.5)

These three providers run as versioned API workflows on the shared ComfyUI service (port 8188) —
see `workflows/comfyui/tts/README.md` for the templates and `scripts/install_tts_nodes.sh` for
installing the community custom nodes they require. Node checkouts are pinned to specific commits
and audited before use; one node per model, never two alternatives for the same model at once.

- **Fish Audio S2 Pro** — bfloat16, roughly 14 GiB VRAM, the heaviest provider. Note: the S2 Pro
  *weights* are under the Fish Audio Research License (research/non-commercial only). Fine for
  personal use; do not ship in commercial projects. The node auto-installs a couple of pip
  packages at ComfyUI import time, so install it when nothing else depends on the shared venv.
- **VoxCPM2 2B** — bf16, roughly 8 GiB; cloning plus voice design. Its ASR-proofread option would
  download an extra model on first use; Studio's templates pin it off.
- **IndexTTS 2.5** — roughly 6 GiB, the lightest production candidate; model resolution uses an
  explicit local `custom_model_path`, so no download path is reachable from the node.

### OmniVoice

Isolated worker on port 8194 wrapping the official `k2-fsa/OmniVoice` Python API
(`OmniVoice.from_pretrained` + `model.generate`), run inside a locked venv so its dependency set
cannot alter ComfyUI's environment. Supports reusable voice-clone prompts (project-scoped and
hashed).

### Breeze TTS 2

`BreezeBlue/Breeze-TTS-2` (≈3.5B parameters, ≈7.0 GB weights) as the fifth standalone worker:

- The managed worker (port 8195) speaks the LVS protocol and owns **one official `breeze_infer.api`
  child at a time** — one process = one engine mode. `eager` (default, ≈7.7 GiB VRAM) runs on
  port 8196; `fast` (≈14.4 GiB, lower latency, includes warmup) on 8197. Switching engines
  cleanly terminates the old child; an `atexit` hook guarantees a dashboard kill cannot orphan a
  7–14 GiB GPU process.
- The `fast` engine is gated by the standard heavy-job VRAM policy (20 GiB free by default) and
  refuses with an "use eager" suggestion when headroom is missing; eager needs only about 10 GiB.
- Voice direction: an optional instruction string with an automatic CFG rule — explicit
  `guidance_scale` wins, otherwise 4.0 when a voice instruction is present, 1.0 for plain cloning.
- Inline expressive events in the script (e.g. `(laugh)`, `(sigh)`) are supported by the model.
- Weights and self-hosted outputs are **non-commercial** under the Breeze license; the UI and this
  note carry that restriction.
- The source is pinned (code SHA recorded in descriptors and take metadata); the shared application
  Python environment is never touched (the venv carries its own Torch build).
- The checkout root is resolved in order: the `LVS_BREEZE_TTS_SOURCE` environment variable, then
  the checkout that owns the venv running the worker (`<checkout>/.venv` — how the supervisor
  launches it, so a relocated checkout works as long as `python_path` points at that venv), then
  `~/ai/services/breeze-tts`.

## Voice and output behavior

- Chatterbox can use its bundled built-in voice without any reference upload.
- Qwen uses the Base checkpoint for authorized voice cloning and automatically
  switches to the sibling CustomVoice checkpoint for its nine built-in speakers.
  Set `LVS_QWEN_CUSTOM_VOICE_MODEL` when that checkpoint is stored elsewhere.
- The Voice page accepts PCM WAV references only and requires explicit
  authorization confirmation.
- Qwen and Chatterbox cache reusable reference conditioning while loaded.
- Step cloning requires the exact reference transcript.
- Qwen output can optionally be passed through Step for emotion/style/speed
  editing. Original Qwen chunks remain in `audio/qwen/`; edits go in
  `audio/step/`.
- Chunk WAV and JSON metadata are independent and retry-safe. `master.wav` is
  joined without lossy encoding.
- Workers report model load time, generation time, output duration, real-time
  factor, and CUDA memory usage.

## Fish S2 Pro delivery tags

Fish Audio S2 Pro understands `[square bracket]` delivery cues — tone, emotion,
and sound effects — that it interprets without speaking them. The Voice page
offers a Fish-S2-Pro-only panel that turns the narration you would actually
generate (planned scene narration, or the Script-override text) into a
cue-annotated version using the local LLM (`service.director.llm`, the user's
loopback model on port 1234 — this app never binds that port).

How it works:

- The tagger sends the clean narration to the local LLM in bounded batches and
  asks it to return the same words with `[cue]` markers added. It never rewrites
  the script: the spoken words must come back identical, only cues may be added.
- Every returned segment is validated against its clean source. A segment that
  fails (for example the LLM rewrote a word, left a cue unbalanced, or over-tagged
  the line) is regenerated individually: the tagger re-asks the local LLM for just
  that segment and tells it exactly what failed last time, including the previous
  attempt. A segment that still fails after the repair degrades to its clean source
  and is reported in `warnings`, so one bad segment never fails the whole run.
- The result is stored in a separate, portable project file
  (`narration/performance-tags.json`). The clean scene narration and the caption
  transcript are never touched, so cues can never leak into captions or into any
  other TTS model.
- Every segment is editable in the UI. Edits are validated against the clean
  source: the spoken words must be identical, cues must be balanced, nonempty,
  and single-line, no new all-caps runs may appear, and the cue count must stay
  under a length-scaled anti-over-tagging ceiling (a short line earns 3; longer
  narration earns more). A hand edit the validator dislikes can be kept with
  `?accept=true` and is logged as `manually_edited`.
- Each stored segment also has a **Regenerate** button that re-tags just that
  segment with the local LLM (using the panel's current intensity and notes)
  while every other segment keeps its stored tags — including any previously
  accepted hand edits. The re-tagged segment is validated and repaired exactly
  like a full run, so it is always safe to persist.
- Chunking is cue-aware: a `[cue]` stays glued to the sentence it directs and is
  never emitted as a cue-only chunk, so picture sync is preserved. Each chunk
  record stores the exact tagged text that was sent, so regenerating a chunk
  reproduces the same audio.
- Enabling the panel's **Use delivery tags** toggle makes the next narration run
  feed the tagged text to `fish_s2_pro` only. Any other provider ignores the
  tags entirely. If a stored segment's source no longer matches the current
  narration it is skipped and counted as stale.

The local LLM keeps its reasoning enabled; the tagger uses a modest dedicated
thinking budget rather than disabling thinking for latency. If no LLM is
available (for example in mock mode) the endpoint returns a clean `409` instead
of touching the network.

S2 Pro's tag vocabulary is open-domain, not a fixed allowlist. The tagger is
guided by Fish Audio's documented, well-tested examples:

- breathing and reactions: `[sigh]`, `[inhale]`, `[exhale]`, `[gasp]`,
  `[panting]`, `[clears throat]`;
- vocal sounds: `[laughing]`, `[chuckling]`, `[giggle]`, `[sobbing]`,
  `[crying]`, `[groan]`;
- pacing: `[pause]`, `[short pause]`, `[long pause]`;
- voice style: `[whispering]`, `[soft voice]`, `[loud voice]`, `[shouting]`,
  `[low voice]`;
- emotion and emphasis: `[excited]`, `[angry]`, `[sad]`, `[surprised]`,
  `[emphasis]`;
- open descriptions such as `[professional broadcast tone]`, `[pitch up]`, or
  `[voice rough from crying, trying to sound normal]`.

Tags may be written in the narration language and placed immediately before
the word or phrase they affect. A tag applies until the next tag or the end of
the sentence. The validator therefore permits long, multilingual, combined,
and uppercase natural-language directions while still requiring the spoken
words to remain identical to the clean source.
