# Example production workflow

This document records a generalized production workflow for a long-form video with a few derived
vertical Shorts. It is a creative workflow, not an installation guide. Backend setup is documented
in [`local-krea2.md`](local-krea2.md), [`local-ideogram4.md`](local-ideogram4.md), and
[`h3.md`](h3.md).

The central rule is simple:

> Generate the picture with the model best suited to the picture, but render important wording with
> a deterministic local text layer.

Text-capable generators can create excellent integrated typography, but their bounding boxes are
conditioning rather than hard layout constraints. When spelling, dates, quotations, references,
titles, or calls to action must be exact, the final text should come from `text_overlay_still` or
`graphic_screen`.

## Example project layout

Working projects are stored outside the source checkout, in a user-chosen projects directory
(`<projects-root>`). A series with one long-form video and a few vertical Shorts might look like:

| Production | Project directory | Format |
| --- | --- | --- |
| Long form | `<projects-root>/<series>/long-form` | 16:9 |
| Short 1 | `<projects-root>/<series>/short-01` | 9:16 |
| Short 2 | `<projects-root>/<series>/short-02` | 9:16 |

Each project remains restartable. The saved `plan.json`, scene records, prompts, generated media,
source manifests, and archived variants are the production record.

## Choose the visual method by purpose

| Need | Preferred visual type | Generator or renderer | Why |
| --- | --- | --- | --- |
| Cinematic scene with no essential words | `krea2_still` or `image_motion` | Krea 2 Turbo | Strong ordinary imagery; FFmpeg can add a controlled push or pan. |
| Cinematic scene with one or two short exact phrases | `text_overlay_still` | Krea by default, then local compositor | The model supplies a text-free background and the app supplies exact Unicode text. |
| Diagram, timeline, verse, quotation, comparison, or information panel | `graphic_screen` | Local HTML/CSS and Chromium | Exact content, deliberate hierarchy, and predictable alignment. |
| Integrated sign, label, poster, cover, or experimental thumbnail lettering | `ideogram4_still` | Ideogram 4 Quick or Precise | Native generated typography and object/text composition. Review every character. |
| A shot whose value comes from visible movement | `h3_audiovisual` | MiniMax H3 | Use for motion, action, transformation, or a living establishing shot—not merely because video is available. |
| A factual person, spacecraft, launch, product, or historical artifact | `reused_media` or imported shots | Licensed/public-domain source | More truthful and often more recognizable than generation. Preserve provenance. |
| A final thumbnail title or other delivery-critical wording | Generated background plus deterministic compositor | Krea/Ideogram background, local text | The title cannot mutate, duplicate, or move outside the safe area. |

Qwen-Image-2512 remains useful as a comparison or alternate text-capable still backend, but the same
rule applies: use deterministic text when the wording must be guaranteed.

## End-to-end production order

1. **Write narration first.** Break it into short visual beats. Every narration beat must have an
   assigned visual; do not leave a scene empty just because the narration continues across a cut.
2. **Mark protected text.** Extract every date, name, quotation, reference, title, and CTA exactly as
   it should appear. Preserve capitalization, punctuation, Unicode, and line breaks.
3. **Assign a visual type.** Use the table above. Do not ask one generator to solve imagery, factual
   accuracy, motion, typography, and layout simultaneously.
4. **Build stills and graphic screens.** Generate text-free image backgrounds before flattening exact
   text. Render information-heavy cards as Graphic Screens.
5. **Review static composition.** Fix subject placement, mobile-safe margins, text density, and visual
   hierarchy before spending GPU time on video.
6. **Generate H3 shots.** Reserve H3 for scenes where movement improves the story. Generate these
   serially on the single GPU.
7. **Attach reused media.** Import licensed or public-domain assets as separate shots, set shot
   durations to the narration, and save author, license, source URL, and any publication caveats.
8. **Render narration, captions, music, and final edit.** FFmpeg is authoritative for timing and final
   assembly. Final exports use narration and music rather than H3's native audio unless the project
   deliberately changes that policy.
9. **Perform visual QC.** Watch the sequence at normal speed and inspect each text frame at full size.

## Long-form rhythm

A long-form video uses a documentary rhythm instead of one visual method throughout:

- Krea stills establish people, places, machinery, institutions, and speculative environments.
- Image Motion turns selected stills into restrained documentary movement without inventing an
  entirely new video shot.
- Graphic Screens handle chapter cards, quotations, comparisons, dates, and explanatory sequences.
  They also replace diagrams that look weak as generic generated imagery.
- H3 is concentrated where visible action or editorial energy matters more than static illustration.
- Reused or real media should be preferred for factual modern people, organizations, events, and
  devices when an appropriately licensed source is available.

This alternation prevents the video from becoming a slideshow while keeping long factual passages
readable. A Graphic Screen should still feel designed: one dominant idea, a clear grid, restrained
supporting text, and meaningful visual symbols. It should not resemble an empty schematic or a page
of body copy.

### Graphic Screen rules

- Use Graphic Screens for the *explanation*, not merely for a title pasted onto a blank field.
- Give the screen one clear visual argument: a timeline, comparison, convergence, sequence, or labeled
  relationship.
- Keep text boxes separate and non-overlapping. Connector lines stop before their labels.
- Maintain at least 8% vertical and 6% horizontal safe margins.
- Prefer a few large labels over many small annotations.
- If the scene is cinematic but needs a short label, use `text_overlay_still` instead.

## Shorts approach

Shorts are mini mysteries derived from the long form: they present a hook and a little evidence
quickly, then stop before the full explanation so the long-form video remains the destination.

- Generate the hook frame as a generated background plus exact deterministic text (a date, a title,
  or a two-line claim).
- Use one or two H3 shots where motion carries the reveal.
- Use Graphic Screens for dated cards, sequence cards, and explanation or comparison panels.
- Close on a call-to-action frame that points back to the full video.
- Keep on-screen wording to roughly three to five important words at a time unless a quotation
  requires a deliberate reading pause. Narration carries the detail; on-screen text carries the hook.

## Still-image workflow

### Ordinary cinematic image

1. Write a visual prompt describing subject, setting, lighting, lens/composition, time period, and
   aspect ratio.
2. Keep requested lettering out of the prompt.
3. Generate with Krea 2 Turbo.
4. If the scene is `image_motion`, add only a restrained camera move that the still can support.
5. Regenerate if the subject, period, or composition is wrong; do not attempt to hide a bad base image
   with text.

### Generated image with exact text

1. Select `text_overlay_still`.
2. Put each distinct exact string on its own `text_in_image` line.
3. Choose a layout such as `hook`, `reveal`, `quote`, `cta`, or `auto`.
4. Generate a background with no visible words, letters, logos, captions, signs, or watermarks.
5. Let Local Video Studio fit and flatten the exact text into `visual.png`.
6. Inspect the mobile crop and make sure CTA text stays above platform controls.

The raw generated art remains in `generated-background.png`; the finished text composite is
`visual.png`. Regeneration archives earlier variants instead of destroying the production history.

## Ideogram workflow

Use **Quick Generation** when starting from a normal language prompt. The app protects literal text,
runs the vendored official Ideogram Magic Prompt instructions through the configured local LLM,
normalizes the result to canonical Ideogram JSON, saves it, and then generates the image.

Use **Precise Text & Layout** when supplying native Ideogram/KJNodes JSON. Text elements retain their
literal `text` values, and boxes use Ideogram's `[y_min, x_min, y_max, x_max]` order on the 0–1000
grid. Precise mode bypasses the local LLM, but it still cannot force generated pixels to obey a box as
if it were a clipping rectangle.

For thumbnails:

1. Keep the headline short—ideally two to four words.
2. Generate or regenerate the Magic Prompt separately.
3. Read the saved structured JSON before loading Ideogram.
4. Keep one primary subject, at most one supporting object, one title, and optionally one kicker.
5. Use non-overlapping subject and text regions.
6. If a word must be correct for publication, generate a text-free candidate and apply the final
   thumbnail text with the deterministic compositor.

Magic Prompts are persisted before the Ideogram worker checks VRAM. A failed Ideogram model load
therefore does not erase the prompt. When the thumbnail plan is unchanged, candidate generation can
reuse the saved caption; use **Regenerate Magic Prompt** only when a new caption is wanted.

## H3 workflow

H3 is not a replacement for every still. Use it when the viewer needs to see something happen:

- a group of characters reacting, or a camera moving through a set;
- a subject in a controlled setting while instruments respond;
- a launch, transformation, mechanical action, or performance;
- a continuity chain whose next shot must begin from the previous H3 frame.

Before generation, verify that the prompt describes one achievable action and that a 9:16 or 16:9
composition has enough safe space. After generation, inspect the beginning, middle, and end for face
drift, object morphing, unwanted words, duplicated limbs, and continuity breaks. H3 durations are
rounded to its native frame grid, so final narration timing belongs to the edit rather than to an
assumed exact model duration.

Generated clips are reviewed at native resolution and accepted only when they remain visually stable
and contain no unwanted generated text.

## Reused-media workflow

Use real media only when its license and provenance are known. Each imported asset should record:

- creator or organization;
- original source page, not merely a copied image URL;
- license and version;
- whether attribution or share-alike terms apply;
- scene and shot where the asset is used;
- any publication review still required.

Acceptable sources include public-domain agency footage (for example NASA media) and creator-licensed
images whose license version is recorded. Retain any reference material that cannot be reused as a
reference only, and never ship it. Confirm attribution and share-alike compliance before publication.

Project-level provenance is stored in `reused-media/sources.json`; imported shot copies live under
the applicable scene's `imports/` directory.

## GPU and local-LLM handoff

Heavy model families are run serially on a single-GPU workstation.

1. Build and save scripts, Graphic Screens, protected text, and prompt JSON first.
2. For Ideogram Quick mode, let the local LLM create the Magic Prompt while it is reachable.
3. Confirm that the Magic Prompt is visibly saved.
4. If the local LLM occupies too much VRAM for a cold Ideogram or H3 load, unload its model manually
   in the user's LLM application. Do not stop its server on port 1234.
5. Generate consecutive images from one ComfyUI family together to benefit from resident-model reuse.
6. Release ComfyUI VRAM before switching to another heavy family when necessary.
7. Generate H3 jobs one at a time.

If the local LLM is unreachable, Quick Ideogram uses a deterministic valid fallback and records a
warning. That fallback preserves protected text, but it is intentionally less imaginative. Precise
mode and already-saved Magic Prompts do not require a live LLM.

## Review checklist

Before final rendering, verify all of the following:

- Every narration beat has a visual and no scene is accidentally blank.
- Every important string matches the script exactly, including line breaks and Unicode characters.
- No generated background contains accidental lettering behind the deterministic overlay.
- Important text stays inside horizontal and vertical safe margins.
- Graphic Screens communicate one clear relationship and are not empty decoration.
- Generated people and historical objects are plausible for the stated period.
- H3 clips remain stable from first frame to last and contain no unwanted text.
- Reused-media licenses, creators, URLs, and obligations are recorded.
- The sequence alternates visual forms intentionally rather than repeating the same composition.
- The full edit is reviewed at normal speed on both a large display and a phone-sized preview.

## What did not work, and why

- **Long headlines generated inside Ideogram:** the model duplicated, misspelled, cropped, and
  rearranged wording even when the structured JSON was correct. Shorter text improved the result,
  but deterministic overlays are still safer.
- **Treating Ideogram boxes as hard rectangles:** the 0–1000 boxes guide composition; they do not
  guarantee containment or prevent the model from inventing additional type.
- **Asking image models to make dense diagrams:** the result looked sparse or generic. Graphic Screen
  HTML/CSS provides a more intentional explanatory design.
- **Narration without a visual beat:** several-second scenes can appear empty when script
  segmentation and visual assignment do not match. Every narration segment gets its own scene or is
  deliberately merged into a neighboring timed visual.
- **Using generated imagery for everything:** factual modern scenes benefit from licensed real media,
  while motion-specific scenes benefit from H3. A mixed visual language produces the strongest edit.