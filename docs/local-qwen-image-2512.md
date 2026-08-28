# Local Qwen-Image-2512 text stills

Local Video Studio uses Qwen-Image-2512 through the existing localhost-only ComfyUI service for
images where readable words must belong naturally inside the scene: signs, posters, packages, or
photographed displays. Use **Graphic Screen** for titles, tables, charts, UI, and any text that must
be character-perfect; generated lettering still requires visual review.

## Required model files

The application never downloads these weights. Install them only after checking the destination and
preserving the configured 50 GB free-space reserve.

| Component | ComfyUI model filename | Approximate size |
| --- | --- | ---: |
| FP8 diffusion model | `diffusion_models/qwen_image_2512_fp8_e4m3fn.safetensors` | 20.4 GB |
| FP8 text encoder | `text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` | 9.4 GB |
| VAE | `vae/qwen_image_vae.safetensors` | 0.25 GB |

The official ComfyUI blueprint also offers a Lightning LoRA. Studio does not require it because its
default Qwen workflow uses the 50-step base-quality recipe.

## Use

Open a scene in the website and select **Qwen-Image-2512 text still**. Enter one requested visible
string per line and choose a Qwen canvas. `auto` resolves to 1664x928, 928x1664, or 1328x1328 from
the project aspect ratio. Generation records the prompt, negative prompt, requested strings, seed,
canvas, model/version, FP8 quantization, workflow version, attempt, and output hash.

The first Qwen generation uses the system-wide 20 GiB free-VRAM gate. Qwen reuses its resident
ComfyUI model family for consecutive scenes. Switching between Qwen, Krea, H3, or ACE releases the
previous ComfyUI family first; Studio never stops the external LLM on port 1234.

Qwen-Image-2512 is Apache-2.0 licensed. Review the upstream model card and license before
distribution; this note is operational guidance, not legal advice.
