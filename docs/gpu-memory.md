# GPU memory management

The reference configuration was tested on a 24 GB-class GPU; usable headroom varies by card and
desktop load. The Studio works on other GPUs too: on smaller cards some backends may need a
different quantization, smaller presets, or a different model, and the Studio's VRAM probes adapt
to whatever capacity the card actually has. Local Video Studio reads system-wide usage, not just
memory allocated by its own Python process. Desktop graphics and the external local LLM can reduce
available capacity.

## Compatibility by GPU size

There is no card-size requirement for any backend. The reference card is 24 GB-class; the numbers
below are the typical free-VRAM needs observed on that card, and they are floors, not exact
requirements. Smaller cards run the backends that fit (sometimes with a different quantization or
preset); larger cards run everything with room to spare. Nothing in the Studio blocks a larger
card, and a smaller card never needs to terminate anything: jobs that cannot fit fail with an
actionable message instead.

| Capability | Backend | Typical free VRAM (observed on the 24 GB reference card) | On smaller cards |
| --- | --- | --- | --- |
| Video | MiniMax H3 (bf16, cold load) | ~20 GiB | Compact canvas presets reduce runtime tensors; a quantized checkpoint or a smaller video model via ComfyUI for the model itself |
| Video | Wan | ~16 GiB | A smaller/quantized model via ComfyUI |
| Video | FLUX | ~12 GiB | fp8/int8 variants |
| Stills | Krea 2 Turbo (fp8) | ~20 GiB | Smaller canvas presets; a different image model via ComfyUI if the weights do not fit |
| Stills | Qwen-Image-2512 (fp8) | ~20 GiB | Smaller canvas presets; a different image model |
| Stills | Ideogram 4 (NF4) | ~22 GiB | A different quantization or a different image backend |
| Music | ACE-Step 1.5 XL (bf16) | ~20 GiB (gate); ~19 GB of weights | Quantized encoder set, more CPU offload, or a different music model |
| TTS | Step-Audio-EditX | ~16 GiB | Use a lighter provider |
| TTS | Fish Audio S2 Pro (bf16) | ~14 GiB | Use a lighter provider |
| TTS | Higgs TTS 3 4B | ~18 GiB (worker gate) | Lower `LVS_HIGGS_TTS_MIN_FREE_GB` and the static-memory fraction, or use a lighter provider |
| TTS | Breeze TTS 2 | eager ~10 GiB, fast ~14 GiB | Use the eager engine; a lighter provider on very small cards |
| TTS | Qwen3-TTS 1.7B, OmniVoice, VoxCPM2 2B | ~8 GiB each | Fine on 12 GB+ cards; tight on 8 GB cards |
| TTS | Chatterbox, IndexTTS 2.5 | ~6 GiB each | Fit 8 GB cards |
| Captions | Faster-Whisper `large-v3-turbo` | ~5 GiB on CUDA | Runs on CPU with no VRAM |

**One knob gates the heavy cold loads.** The Krea 2, Qwen-Image, Ideogram 4, H3, and ACE-Step XL
cold loads all require `gpu.minimum_free_vram_gb_for_heavy_job` (default 20 GiB) of system-wide
free VRAM before dispatch. On a smaller card, lower it to what your card can actually provide and
disable the backends whose weights do not fit (Models & System Status shows each backend's
requirement). On a larger card you may raise it to reserve more headroom; nothing in the code
caps larger cards.

**No GPU at all** is enough for the deterministic mock pipeline, Graphics Screens, editorial
overlays, thumbnails, and FFmpeg rendering, and caption alignment can run on CPU. The studio,
scripting, and editing features do not require a GPU at all.

Default policy:

- require 20 GiB free before a heavyweight job;
- run one heavyweight job at a time;
- fail with an actionable message rather than terminating another process;
- retain the active ComfyUI model family across consecutive scenes of the same type;
- unload ComfyUI models before switching model families, and expose a manual release control;
- allow backend-side offload to system RAM (the H3 worker's CPU offload, ComfyUI's memory
  offload); the Studio itself does not configure offload budgets;
- keep lightweight orchestration and FFmpeg work running independently where safe.

The Models & System Status screen shows the ComfyUI family retained by this Studio process and has a
**Release ComfyUI VRAM** button. Image Motion retains whichever still-image family the scene selects:
Krea 2 Turbo or Qwen-Image-2512. Switching to a different ComfyUI family releases the previous family
before loading the next.

If the LLM occupies substantial VRAM, unload its model in the external router application or wait.
Port 1234 remains externally owned, so Local Video Studio closes its request connection after script
generation but does not control the router's model lifecycle. Parallel worktrees are for code and CPU
tests, never parallel H3/FLUX/Wan inference.

## MiniMax H3 VRAM gate

MiniMax H3 cold-load needs roughly 20 GiB free on the tested 24 GB-class card (smaller cards can
use H3's smaller canvas presets, a quantized checkpoint, or another video backend). Before dispatch, the pipeline probes
system-wide free VRAM and raises a structured `INSUFFICIENT_VRAM` error (mapped to HTTP 409) when the
threshold is not met. The error message lists concrete remediation steps:

1. Release cached ComfyUI models from Models & System Status.
2. Unload the externally managed LLM in its router UI.
3. Retry after VRAM is free.

`/api/system/status` includes `h3_readiness` so the UI can warn the user before they request
generation. The Fast / Safe preset (896×512 / 512×896) is the most VRAM-efficient; Standard and High
use larger canvases and have shorter validated duration caps. When H3 is already resident, readiness
reports that same-family reuse does not require cold-load headroom even if free VRAM is below 20 GiB.
