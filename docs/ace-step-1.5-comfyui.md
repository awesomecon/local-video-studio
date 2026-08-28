# ACE-Step 1.5 XL via ComfyUI — Setup and Operation

## Overview

Local Video Studio generates background music through the existing ComfyUI service at
`127.0.0.1:8188` using ACE-Step 1.5 XL native ComfyUI workflows. No separate ACE service,
port, environment, or API key is required.

This setup targets a 24 GB-class NVIDIA GPU; available headroom varies by card and desktop load. The
Studio uses:
- **XL Turbo** (`acestep_v1.5_xl_turbo_bf16.safetensors`) — 8-step daily driver
- **XL SFT** (`acestep_v1.5_xl_sft_bf16.safetensors`) — slower quality preset (optional)
- **ACE encoders** `qwen_0.6b_ace15.safetensors` + `qwen_4b_ace15.safetensors`
- **VAE** `ace_1.5_vae.safetensors`

These match the official ComfyUI XL Turbo template from `Comfy-Org/ace_step_1.5_ComfyUI_files`.

## 1. Compatibility inspection (read-only)

Before installing models, verify the ComfyUI instance exposes the required nodes:

```bash
python scripts/inspect_comfyui_nodes.py
```

This writes `comfyui_ace_inspection.json` with:
- ComfyUI version
- Required-node presence (derived from the canonical workflow JSON)
- `TextEncodeAceStepAudio1.5` input schema and combo choices
- Loader choices for UNET, CLIP, and VAE (model filenames visible through the API)

**Note:** `/object_info` reports model filenames ComfyUI sees, but does NOT reliably disclose the physical filesystem directories that supplied them. ComfyUI searches its normal model directory plus paths from `extra_model_paths.yaml`. Actual installation paths must be resolved from ComfyUI configuration or Studio configuration, not from this inspector.

Required native nodes:
- `UNETLoader`
- `DualCLIPLoader`
- `VAELoader`
- `TextEncodeAceStepAudio1.5`
- `EmptyAceStep1.5LatentAudio`
- `ConditioningZeroOut`
- `ModelSamplingAuraFlow`
- `KSampler`
- `VAEDecodeAudio`
- `SaveAudioMP3`

If any node is missing, install or update the ComfyUI custom nodes that provide them.
Do not modify the ComfyUI service configuration; if a restart is needed, use the
existing service management procedure.

## 2. Model installation (user-managed)

### 2.1 Exact file inventory

REQUIRED — XL TURBO (matches official ComfyUI template):

| File | ComfyUI folder | Approx size |
|------|---------------|-------------|
| `acestep_v1.5_xl_turbo_bf16.safetensors` | `diffusion_models/` | 9.97 GB |
| `qwen_0.6b_ace15.safetensors` | `text_encoders/` | 1.19 GB |
| `qwen_4b_ace15.safetensors` | `text_encoders/` | 8.38 GB |
| `ace_1.5_vae.safetensors` | `vae/` | 337 MB |
| | **Total** | **~19.9 GB** |

OPTIONAL LATER — XL SFT:

| File | ComfyUI folder | Approx size |
|------|---------------|-------------|
| `acestep_v1.5_xl_sft_bf16.safetensors` | `diffusion_models/` | 9.97 GB |

**Source repository:** `Comfy-Org/ace_step_1.5_ComfyUI_files` on HuggingFace.

### 2.2 Download procedure

1. **Inspect ComfyUI's model inventory**, then resolve its configured model directories separately:
   ```bash
   python scripts/inspect_comfyui_nodes.py  # writes comfyui_ace_inspection.json
   ```
   The inspector confirms filenames visible to ComfyUI but cannot disclose physical paths. Resolve
   the actual directories from the documented local installation or user-authorized ComfyUI
   configuration; do not assume a generic path when `extra_model_paths.yaml` may redirect it.

2. **Check free space** on the resolved target:
   - Minimum practical: 25–30 GB
   - Recommended: 40–50 GB (protects against HF cache duplication, temp downloads, future checkpoints, generated audio)

3. **Create a temporary download directory:**
   ```bash
   TMP_DIR="$HOME/.cache/local-video-studio/ace15-download"
   mkdir -p "$TMP_DIR"
   ```

4. **Download the four Turbo files** (requires explicit user approval):
   ```bash
   hf download Comfy-Org/ace_step_1.5_ComfyUI_files \
     split_files/diffusion_models/acestep_v1.5_xl_turbo_bf16.safetensors \
     split_files/text_encoders/qwen_0.6b_ace15.safetensors \
     split_files/text_encoders/qwen_4b_ace15.safetensors \
     split_files/vae/ace_1.5_vae.safetensors \
     --local-dir "$TMP_DIR"
   ```

5. **Copy to ComfyUI model directories** (use actual paths from step 1):
   ```bash
   COMFY_MODELS="/path/to/comfyui/models"  # from inspection

   cp "$TMP_DIR/split_files/diffusion_models/acestep_v1.5_xl_turbo_bf16.safetensors" \
      "$COMFY_MODELS/diffusion_models/"

   cp "$TMP_DIR/split_files/text_encoders/qwen_0.6b_ace15.safetensors" \
      "$COMFY_MODELS/text_encoders/"

   cp "$TMP_DIR/split_files/text_encoders/qwen_4b_ace15.safetensors" \
      "$COMFY_MODELS/text_encoders/"

   cp "$TMP_DIR/split_files/vae/ace_1.5_vae.safetensors" \
      "$COMFY_MODELS/vae/"
   ```

6. **Verify hashes** against the published SHA256 checksums.

7. **Restart ComfyUI** if it needs to scan new files. The Studio does not restart ComfyUI.

### 2.3 Post-download verification

1. Run `python scripts/inspect_comfyui_nodes.py` again.
2. Confirm all required nodes are present.
3. Confirm all four Turbo files appear in the appropriate loader choices.
4. Open the Studio Music screen → confirm Turbo shows **ready**.

## 3. Normal operation

1. Ensure ComfyUI is healthy on `127.0.0.1:8188`.
2. Enable ACE in `config/default.yaml` or project settings:
   ```yaml
   backends:
     ace_step:
       enabled: true
       provider: comfyui
       model: xl_turbo
       thinking: true
   ```
3. Open the Music screen in the Studio UI.
4. Select style, mood, model quality, and other parameters.
5. Click **Generate music**.
6. The Studio splits the project into long musical **movements** (default ~60 s,
   configurable via *Movement length* in the Music screen; boundaries follow
   each scene's `music_mood`). Each movement is submitted as its own ACE
   generation with a derived seed and mood-aware prompt, normalized to its
   exact planned duration, and all movements are stitched with short fade dips
   into `music/background.wav`. Per-movement clips live under
   `music/movements/` and `music/manifest.json` records prompts, seeds,
   moods, and boundaries. Finished movements are reused when a run fails or a
   single movement is regenerated from the Music screen.
7. After retrieval, the Studio calls `/free` to request ACE weight release. The Studio polls until VRAM actually drops.

## 4. Readiness checks

Open the Music screen readiness panel or call:

```bash
curl http://127.0.0.1:8009/api/music/models
```

The response includes:
- `comfyui_healthy`: ComfyUI service status
- `turbo.ready` / `sft.ready`: whether the preset is fully available
- `turbo.missing_nodes` / `turbo.missing_files`: exact missing dependencies
- `combo_choices.language`, `combo_choices.key_scale`, `combo_choices.time_signature`:
  valid values for the installed `TextEncodeAceStepAudio1.5` node
- `duration_range.min` / `duration_range.max`: supported duration in seconds
- `comfyui_resident`: currently resident ComfyUI model family
- `vram`: system-wide VRAM snapshot. Target maximum free VRAM; do not hardcode a rejection threshold.

### Common missing-node resolutions

| Missing node | Action |
|---|---|
| `TextEncodeAceStepAudio1.5` | Install/update ComfyUI ACE custom node |
| `DualCLIPLoader` | Update ComfyUI core nodes |
| Any other native node | Update ComfyUI to a version that includes the node |

**Note:** Required node types are derived from the canonical workflow JSON, not hardcoded. If the official template changes, readiness stays correct.

### Common missing-file resolutions

| Missing file | Action |
|---|---|
| `acestep_v1.5_xl_turbo_bf16.safetensors` | Download XL Turbo DiT to `models/diffusion_models/` |
| `qwen_0.6b_ace15.safetensors` | Download small ACE encoder to `models/text_encoders/` |
| `qwen_4b_ace15.safetensors` | Download large ACE encoder to `models/text_encoders/` |
| `ace_1.5_vae.safetensors` | Download ACE VAE to `models/vae/` |

## 5. VRAM recovery

If the Studio reports insufficient VRAM:

1. Open the Models screen and click **Release** for the resident ComfyUI family.
2. If the external LLM occupies VRAM, unload its model in its router UI (port 1234).
   The Studio never controls that process.
3. Retry music generation.

**Note:** A 24 GB GPU must accommodate the ~10 GB DiT, ~9.6 GB text encoders, and runtime tensors, but ComfyUI can offload pieces to system RAM. Do not reject generation solely because free VRAM is <20 GB unless actual benchmarking establishes that threshold. Warn when another application is consuming substantial VRAM.

## 6. Cancellation and memory release

- **Queued job:** delete prompt from ComfyUI queue.
- **Running job:** interrupt the active prompt via `/interrupt`. Only call `/interrupt` after confirming the currently running ComfyUI prompt belongs to this music job.
- **After cancellation/completion:** call `POST /free` with `{"unload_models": true, "free_memory": true}`, then poll `nvidia-smi` or `/system_stats` until VRAM actually drops. `/free` sets flags that ComfyUI processes asynchronously; do not assume memory is released the instant the request returns.

## 7. Manual workflow validation

To test the ACE workflow manually without exposing project data:

1. Open ComfyUI at `http://127.0.0.1:8188`.
2. Load `workflows/comfyui/ace-step-1.5-xl-turbo.workflow.json` via the ComfyUI
   "Load" button (API format).
3. Replace placeholders with test values:
   - `{{prompt}}` → `"documentary background music, curious, instrumental, no vocals"`
   - `{{lyrics}}` → `""`
   - `{{seed}}` → `30001`
   - `{{duration}}` → `30`
   - `{{bpm}}` → `90`
   - `{{time_signature}}` → `4`
   - `{{language}}` → `en`
   - `{{key_scale}}` → `C major`
   - `{{generate_audio_codes}}` → `true`
   - `{{model_filename}}` → `acestep_v1.5_xl_turbo_bf16.safetensors`
   - `{{filename_prefix}}` → `local-video-studio/ace-step-music`
4. Run the workflow and inspect the output in `output/local-video-studio/`.

## 8. Configuration reference

```yaml
backends:
  ace_step:
    enabled: false                # master switch
    provider: comfyui             # must be comfyui for v1
    model: xl_turbo               # xl_turbo or xl_sft
    workflow_path: null           # null = checked-in workflow for model preset
    thinking: true                # maps to generate_audio_codes
    poll_interval_seconds: 0.5    # ComfyUI history polling interval
    generation_timeout_seconds: 1800  # max generation time
```

Project music settings (persisted in `project.settings.music`):

```json
{
  "style": "documentary",
  "mood": "curious",
  "instrumental": true,
  "backend": "ace_step_comfyui",
  "model": "xl_turbo",
  "thinking": true,
  "seed": 30001,
  "bpm": 90,
  "key_scale": "C major",
  "time_signature": "4",
  "language": "en"
}
```
