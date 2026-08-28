# GPU memory management

The reference configuration uses a 24 GB-class GPU; usable headroom varies. Local Video Studio reads
system-wide usage, not just memory allocated by its own Python process. Desktop graphics and the
external local LLM can reduce available capacity.

Default policy:

- require 20 GiB free before a heavyweight job;
- run one heavyweight job at a time;
- fail with an actionable message rather than terminating another process;
- retain the active ComfyUI model family across consecutive scenes of the same type;
- unload ComfyUI models before switching model families, and expose a manual release control;
- allow configured CPU offload within the configured system-RAM budget (56 GiB default);
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

MiniMax H3 cold-load needs roughly 20 GiB free on a 24 GB-class card. Before dispatch, the pipeline probes
system-wide free VRAM and raises a structured `INSUFFICIENT_VRAM` error (mapped to HTTP 409) when the
threshold is not met. The error message lists concrete remediation steps:

1. Release cached ComfyUI models from Models & System Status.
2. Unload the externally managed LLM in its router UI.
3. Retry after VRAM is free.

`/api/system/status` includes `h3_readiness` so the UI can warn the user before they request
generation. The Fast / Safe preset (896×512 / 512×896) is the most VRAM-efficient; Standard and High
use larger canvases and have shorter validated duration caps. When H3 is already resident, readiness
reports that same-family reuse does not require cold-load headroom even if free VRAM is below 20 GiB.
