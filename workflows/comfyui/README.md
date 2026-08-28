# ComfyUI workflows

Store API-format workflow JSON here. Each workflow should include companion metadata documenting its
capability, version, required local checkpoints, substitution keys, and expected output nodes.

The repository does not ship checkpoint files and does not download them automatically. Workflow node
graphs depend on the locally installed ComfyUI nodes/models, so templates must be validated in the
user's instance before production use.

## MiniMax H3 AV (base)

`minimax-h3-av.workflow.json` is the base text/video-to-audiovisual workflow.

- Capability: local text/image-to-audiovisual (H3)
- Workflow version: `minimax-h3-av-v1`
- Substitutions: `prompt`, `seed`, `width`, `height`, `length` (frame count at 24 fps, snapped to
  the 17k+5 grid by the pipeline)
- Required files: MiniMax H3 checkpoint and VAE per the installed ComfyUI H3 custom nodes
- Output node: video save node (configured in workflow)

## MiniMax H3 AV with first-frame continuity

`minimax-h3-av-first-frame.workflow.json` extends the base workflow with a `LoadImage` node and a
`first_frame` input on `MiniMaxH3ImageToVideo`. The pipeline populates `first_frame_image` when
H3 continuity is enabled and a predecessor visual asset exists.

- Capability: local image/video-to-audiovisual with first-frame conditioning (H3)
- Workflow version: `minimax-h3-av-first-frame-v1`
- Substitutions: same as base plus `reference_image` (ComfyUI filename returned by uploading the
  extracted predecessor last frame; populated only when continuity is enabled)
- Required files: same as base plus any image decoder needed by `LoadImage`
- Output node: same as base

Continuity rules are enforced in the pipeline and director: predecessor must be an earlier H3 scene,
the two scenes must resolve to the same canvas, and the predecessor must have a current visual asset.
The director materializes continuity blocks automatically for consecutive H3 scenes; the Scene Editor
lets users override predecessor selection and group naming.

## Krea 2 Turbo FP8

`krea2-turbo.workflow.json` is an API-format native-node text-to-image workflow.

- Capability: local text-to-image
- Workflow version: `krea2-turbo-fp8-v1`
- Substitutions: `prompt`, `seed`, `width`, `height`
- Required files: `krea2_turbo_fp8_scaled.safetensors`,
  `qwen3vl_4b_fp8_scaled.safetensors`, and `qwen_image_vae.safetensors`
- Sampling: 8 steps, CFG 1.0, `er_sde`, `simple`, zeroed negative conditioning
- Output node: `9` (`SaveImage`)

The distilled Turbo checkpoint runs with zeroed negative conditioning, so the scene's
`negative_prompt` is currently inert for this workflow. The pipeline must not embed it as a
positive `Avoid:` clause either: the Qwen-VL text encoder renders such clauses literally as
garbled on-screen text. Keep exclusions out of the positive prompt and describe only what the
frame should contain. See `docs/local-krea2.md`.

## Qwen-Image-2512 FP8

`qwen-image-2512.workflow.json` is the native-node, quality-oriented text-to-image workflow used
when readable lettering must be part of the generated scene.

- Capability: local text-to-image with improved embedded-text rendering
- Workflow version: `qwen-image-2512-fp8-v1`
- Substitutions: `prompt`, `negative_prompt`, `seed`, `width`, `height`
- Required files: `qwen_image_2512_fp8_e4m3fn.safetensors`,
  `qwen_2.5_vl_7b_fp8_scaled.safetensors`, and `qwen_image_vae.safetensors`
- Sampling: 50 steps, CFG 4.0, `euler`, `simple`, AuraFlow shift 3.1
- Output node: `10` (`SaveImage`)

The optional Lightning LoRA from ComfyUI's blueprint is deliberately omitted: this workflow favors
the base model's quality over four-step generation. See `docs/local-qwen-image-2512.md`.

## Ideogram 4 (local, structured JSON prompts)

`ideogram4-local.workflow.json` is the text-to-image workflow for scenes whose picture must contain
readable words (thumbnails, title cards, posters, labeled maps, infographics, signs, document
screenshots, UI mockups). Ideogram 4 was added because Qwen-Image-2512's embedded-text rendering was
not strong enough for these; Qwen stays registered for fallback and side-by-side comparison, and Krea
remains the generator for ordinary cinematic scenes without text.

- Capability: local text-to-image with strong embedded-text rendering
- Workflow version: `ideogram4-nf4-v1`
- Substitutions: `prompt_json` (canonical structured JSON built by
  `backend/models/ideogram_prompt.py` immediately before encoding), `seed`, `width`, `height`
- Required runtime: the official `ideogram-oss/ComfyUI-Ideogram4` nodes and the gated
  `ideogram-ai/ideogram-4-nf4` weights. The workflow uses the published
  `Ideogram4PipelineLoader` / `Ideogram4Generate` contract and selects `4.0 NF4`.
- Output node: `3` (`SaveImage`)
- Notes: Quick mode uses the vendored official open-source Magic Prompt v1 instructions with the
  configured local LLM; Precise mode validates native Ideogram/KJNodes JSON directly. No hosted
  prompt service is used. Both carry exact strings in dedicated `type: "text"` elements, normalized
  `[y_min, x_min, y_max, x_max]` bounding boxes, and cinematic style fields. It is validated
  (`validate_ideogram_prompt_json`) before submission. Ideogram 4 does not expose a negative-prompt
input; the scene negative prompt remains provenance only.

`ideogram4-thumbnail-local.workflow.json` is the thumbnail-specific quality path. It keeps the
same local NF4 weights and structured JSON contract, but uses the `4.0 Quality 48` sampler at a
1536×864 working canvas before Thumbnail Studio normalizes the published PNG to 1280×720. Its
caption uses the same local Quick-mode Magic Prompt builder as scene and comparison generation,
with the real 16:9 ratio and protected title/hook literals; Thumbnail Studio then applies its
deterministic native 0–1000 text and subject boxes.

- Workflow version: `ideogram4-thumbnail-nf4-quality48-v2`
- Sampling: 48 steps (45 guided construction steps plus 3 lower-guidance polish steps)
- Notes: this separate workflow avoids slowing ordinary Ideogram storyboard scenes. Thumbnail
  exclusions are expressed inside the structured JSON because `Ideogram4Generate` has no
  negative-prompt input.

## ACE-Step 1.5 XL Turbo

`ace-step-1.5-xl-turbo.workflow.json` is an API-format native-node text-to-music workflow.

- Capability: local text-to-music (ACE-Step 1.5 XL Turbo)
- Workflow version: `ace-step-1.5-xl-turbo-comfy-v1`
- Substitutions: `prompt`, `lyrics`, `seed`, `duration`, `bpm`, `time_signature`, `language`, `key_scale`, `generate_audio_codes`, `model_filename`, `filename_prefix`
- Required files: `acestep_v1.5_xl_turbo_bf16.safetensors`, `qwen_0.6b_ace15.safetensors`, `qwen_4b_ace15.safetensors`, `ace_1.5_vae.safetensors`
- Sampling: AuraFlow shift 3.0; 8 steps, CFG 1.0, `euler`, `simple`, denoise 1.0
- Output node: `11` (`SaveAudioMP3`)
- Notes: uses `qwen_4b_ace15` to match the official ComfyUI XL template.

## ACE-Step 1.5 XL SFT

`ace-step-1.5-xl-sft.workflow.json` is the slower quality preset.

- Capability: local text-to-music (ACE-Step 1.5 XL SFT)
- Workflow version: `ace-step-1.5-xl-sft-comfy-v1`
- Substitutions: same as Turbo
- Required files: `acestep_v1.5_xl_sft_bf16.safetensors`, `qwen_0.6b_ace15.safetensors`, `qwen_4b_ace15.safetensors`, `ace_1.5_vae.safetensors`
- Sampling: AuraFlow shift 3.0; 50 steps, CFG 7.0, `euler`, `simple`, denoise 1.0
- Output node: `11` (`SaveAudioMP3`)
- Notes: offered only when the SFT checkpoint and workflow are ready.
