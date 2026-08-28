# Local Krea 2 Turbo still images

Local Video Studio uses the open Krea 2 Turbo FP8 checkpoint through the existing localhost-only
ComfyUI service. Prompts and outputs remain local. The application never starts, stops, or modifies
the user's llama.cpp router on port 1234.

## Installed model files

The reference installation maps the user's central ComfyUI model root (`<model-root>`, any writable
directory such as `~/ai/models` or a user-writable directory on a secondary data volume) through
`~/ai/services/ComfyUI/extra_model_paths.yaml`.

| Component | Path | Size |
| --- | --- | ---: |
| Turbo diffusion model | `diffusion_models/diffusion_models/krea2_turbo_fp8_scaled.safetensors` | 13,141,730,784 bytes |
| Qwen3-VL 4B encoder | `text_encoders/text_encoders/qwen3vl_4b_fp8_scaled.safetensors` | 5,242,467,968 bytes |
| Qwen Image VAE | `vae/vae/qwen_image_vae.safetensors` | 253,806,246 bytes |

The doubled component directories match the reference installation's existing `extra_model_paths.yaml`
mapping. The files were downloaded directly into the model root, without a duplicate model cache.
After the download, the data volume retained well above the project's 50 GB reserve.

## Runtime

Start ComfyUI only on loopback:

```bash
cd ~/ai/services/ComfyUI
venv/bin/python main.py --listen 127.0.0.1 --port 8188
```

Start Local Video Studio in real-backend mode in another terminal:

```bash
cd <project-root>
unset LOCAL_VIDEO_STUDIO_MOCK_MODE
python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8009
```

In the website, open a scene, select **Krea 2 Turbo still image**, choose a Krea canvas, save the
scene, and then select **Regenerate visual**. The generated PNG is recorded with backend
`krea2_comfyui`, model `Krea 2 Turbo`, quantization `fp8_scaled`, and workflow
`krea2-turbo-fp8-v1`.

## Sampling and canvas behavior

The versioned API workflow is `workflows/comfyui/krea2-turbo.workflow.json`. It uses native
ComfyUI nodes, eight steps, CFG 1.0, the `er_sde` sampler, the `simple` scheduler, and zeroed
negative conditioning. The authored negative prompt is passed separately through the
backend's `negative_prompt` parameter; it is not embedded in the positive prompt, which
prevents the image model from rendering it as literal text.

The `auto` canvas uses 1344x768 for landscape, 768x1344 for portrait, and 1024x1024 for square
projects. Explicit canvases must be at least 256 pixels per side, aligned to 16 pixels, and no more
than 1,048,576 pixels total.

## GPU safety

The first Krea generation uses the system-wide 20 GiB free-VRAM gate. Consecutive Krea still and
Krea-backed Image Motion scenes reuse the resident Krea stack, so they do not fail the cold-load gate
merely because that same stack now occupies VRAM. Image Motion can also select Qwen-Image-2512; that
choice uses the Qwen canvas and requested-text controls and switches the resident ComfyUI family.
Switching to H3 releases the current still-image stack first. The Models & System Status screen also
provides a manual **Release ComfyUI VRAM** action.

If the local LLM router holds the card before a cold load, generation still waits for the user to
unload that model in the router application. Local Video Studio never unloads or terminates the
externally owned service on port 1234.

## License note

Krea 2 is governed by the Krea 2 Community License. Commercial use under that license is limited by
its revenue threshold, and deployments require reasonable content filtering or review. Generated
scenes should be reviewed before distribution. This note is operational guidance, not legal advice.
