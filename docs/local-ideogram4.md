# Local Ideogram 4 NF4

Ideogram 4 is the preferred still-image generator when words must be part of the generated picture.
Krea remains the ordinary cinematic-image backend and Qwen Image remains available for comparison.

## Isolation and endpoints

Ideogram's official runtime requires Torch 2.11 or newer. It therefore runs in an isolated ComfyUI
checkout under a configurable service root (for example `~/ai/services/ComfyUI-Ideogram4`, or a
user-writable directory on a secondary data volume) on `127.0.0.1:8190`. The existing
ComfyUI service on port 8188 is not modified, restarted, or stopped.

Run `scripts/install_ideogram4.sh` only after reviewing disk space, accepting the model's gated
non-commercial license, and exporting a read-only `HF_TOKEN`. The script never writes the token to
disk. By default, the app starts the installed service on the first uncached Ideogram generation,
keeps it warm, and stops it when the app shuts down. Set `backends.ideogram4_local.managed: false`
if you prefer to run `scripts/start_ideogram4.sh` yourself.

## Prompt modes and contract

The application never sends prompts to a hosted prompt service. Quick Generation runs the official
open-source Ideogram Magic Prompt v1 system instructions through the configured local LLM, then
validates and normalizes the result. The exact upstream file is vendored at
`backend/models/prompts/ideogram4_magic_prompt_v1.txt` (Ideogram commit `990fe1c4e950`, file SHA-256
`5f6386382a1d9676f597be93884fc2346b62b923638da8df8b47d4d94a728f5e`). If the LLM is unavailable or
returns malformed JSON after one controlled repair, a valid deterministic caption is used and the
warning is persisted.

Precise Text & Layout accepts native Ideogram 4 JSON directly. The same JSON is accepted by the
default `Ideogram4PromptBuilderKJ` import and exported without coordinate or text translation. Both
modes converge on the official three-part caption:

- `high_level_description` (officially optional, always emitted by Quick mode)
- ordered `style_description`
- `compositional_deconstruction` with `background` and ordered `elements`

Exact lettering is represented as `type: "text"` elements with a dedicated literal `text` field.
Bounding boxes use `[y_min, x_min, y_max, x_max]` coordinates normalized to 0–1000. Palettes use
uppercase `#RRGGBB`. Boxes are optional; supplied boxes require positive width and height. Photo
styles use ordered keys `aesthetics`, `lighting`, `photo`, `medium`; art styles use `aesthetics`,
`lighting`, `medium`, `art_style`. Optional palettes come last. Unknown fields are rejected instead
of silently discarded, and the compact serializer uses UTF-8 with no alphabetical key sorting.

Quick mode protects quoted and explicitly supplied strings with placeholders before local LLM
expansion, restores them deterministically afterward, and verifies each literal in its own text
element. Precise mode does not rewrite supplied text, element ordering, or coordinates.

Build or inspect prompts without loading Ideogram weights:

```bash
lvs-ideogram-prompt --prompt-mode quick --aspect-ratio 16:9 \
  --show-prompt-json 'A neon motel sign saying "PALM BEACH"'

lvs-ideogram-prompt --prompt-mode precise --prompt-json poster.json \
  --show-prompt-json --save-prompt-json canonical-poster.json
```

The scene editor exposes a first-class **Ideogram 4 still image** type for both legacy scenes and
materialized multi-shot scenes. It uses the same Quick/Precise choice as other Ideogram routes;
Precise JSON is validated immediately before generation, while Quick exact-text lines are protected.

Thumbnail Studio's Ideogram option exposes both modes through the same pipeline builder. Quick sends
the actual 16:9 target ratio to Magic Prompt and protects the saved title and hook as exact literals.
Precise validates native Ideogram/KJNodes JSON directly and bypasses the local LLM.
Scene, comparison, and thumbnail generation therefore share one canonical validator and serializer
immediately before their respective Ideogram workflows.

Thumbnail Magic Prompts are stored independently at
`thumbnails/ideogram-magic-prompt.json`, keyed by a fingerprint of the saved thumbnail plan. The
Thumbnail Studio shows both readable JSON and the exact compact string sent to Ideogram, and offers
**Regenerate Magic Prompt** without loading Ideogram. Candidate generation reuses a current saved
prompt. A new or changed prompt is written before the Ideogram worker starts or VRAM is checked, so
an out-of-VRAM failure still leaves an inspectable, reusable prompt on disk.

After Quick expansion, Thumbnail Studio applies a collision-safe layout: it retains at most two
visual objects and gives the subject, optional secondary object, upper kicker, and lower title
non-intersecting native regions. Each text element receives one explicit color rather than the full
scene palette. Precise plans must contain the exact saved title and hook as literal `text` elements.

Ideogram bounding boxes are conditioning controls, not hard clipping rectangles. Generated pixels
can still contain misspelled, duplicated, cropped, or invented lettering even when the canonical
JSON is exact. Keep integrated copy short. Use text-free artwork plus the deterministic local
thumbnail compositor for delivery-critical wording.

Newly scripted `ideogram4_still` scenes with short required text default to validated Precise JSON
with text bands separated from the central visual region. Scene and shot editors may override every
native box. Dense quotations, documents, tables, and long factual wording remain better suited to
Graphic Screens or deterministic overlays.

For a generated scene whose wording must be exact, prefer the first-class `text_overlay_still`
visual type. It may use Ideogram as a text-free background generator, but Local Video Studio—not
Ideogram—renders and flattens the final text. This avoids invented manuscript copy, duplicated
headlines, and spelling errors while retaining Ideogram artwork when desired. Krea 2 Turbo is the
default background engine for this mode.

The API workflow selects `4.0 NF4` and the official `4.0 Default 20` sampler preset. Prompt mode does
not change sampling, seed, canvas, model loading, or image saving. Ideogram 4 has
no negative-prompt input; negative prompts are retained only in generation provenance.

## Start and verify

```bash
# Optional when managed startup is disabled:
scripts/start_ideogram4.sh
curl -fsSL http://127.0.0.1:8190/object_info/Ideogram4PipelineLoader
```

The app reuses a compatible service already running on port 8190 and never stops a service it did
not start. If another service occupies the port, generation fails safely instead of terminating it.
The first real generation loads the gated NF4 weights and can use nearly all of a
24 GB GPU, so ensure system-wide VRAM is free first.

The startup script enables Hugging Face offline mode after installation. This avoids authenticated
metadata requests during generation, so `HF_TOKEN` is needed for installation only and should be
unset afterward.
