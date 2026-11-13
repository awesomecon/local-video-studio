# Local Qwen-Image-2.1

Qwen-Image-2.1 uses a separate ComfyUI endpoint, `http://127.0.0.1:8200`, configured
under `backends.qwen_image_21`. Keep the existing ComfyUI installation for other
models. The new runtime must support `TextEncodeQwenImage21` and the Qwen-Image-2.1
model architecture; the older Qwen-Image-2512 runtime is insufficient.

Download only after checking disk space and approving the download. The workflow
uses these files from [Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1):

| ComfyUI model folder | File | Size |
| --- | --- | --- |
| diffusion_models | qwen_image_2.1_int8_convrot.safetensors | 7.26 GB |
| text_encoders | qwen3vl_8b_int8_convrot.safetensors | 9.35 GB |
| vae | qwen_image_2.1_vae_bf16.safetensors | 0.68 GB |

Select **Qwen-Image-2.1 still image** for scenes or image-lane shots, or choose
**Qwen-Image-2.1** in a scene's image-model menu. Thumbnail Studio offers both
artwork with an exact text overlay and native text rendering. Image Motion can
use Qwen-Image-2.1 through the image-model menu. Native generated lettering
requires visual review. Reference-image editing is not wired in this workflow.

The workflow uses 25 Euler/simple steps and CFG 1, matching the official ComfyUI
template without its optional prompt-enhancer model. It records model version,
INT8 quantization, prompts, seed, workflow version, attempts, and output hashes.
GPU jobs require the configured free-VRAM threshold. Unload the external LLM
before generation if it occupies that memory.

Krea, Ideogram, and Qwen-Image-2512 adapters and workflows remain available;
removing their local weights requires reinstalling those weights before reuse.
Qwen-Image-2.1 uses the upstream Qwen Research license.
