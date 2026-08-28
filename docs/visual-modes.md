# Visual modes

The Scene Editor shows the current readiness of every visual mode. “Not currently wired” means the
mode remains part of the portable project schema and mock planning flow, but cannot generate real
media when the application runs with mock mode disabled.

| Mode | Status | Current behavior |
| --- | --- | --- |
| Qwen-Image-2512 text still | Wired | Generates a local still through native ComfyUI for signs, posters, packaging, and other scenes with embedded lettering. |
| Krea 2 Turbo still image | Wired | Generates a local still through the native ComfyUI Krea 2 Turbo workflow. |
| FLUX still image | Not currently wired | Retained for planning; real FLUX dispatch is not connected. |
| Image motion | Wired | Generates its source still through selectable local Krea 2 Turbo or Qwen-Image-2512, then applies deterministic FFmpeg camera motion during rendering. |
| Wan video | Not currently wired | Retained for ordinary generated-video planning; real Wan dispatch is not connected. |
| MiniMax H3 AV shot | Wired | Generates synchronized local video and native audio through ComfyUI. |
| MiniMax H3 reference video | Not currently wired | Reserved for a future Ref2VA integration; Ref2VA is intentionally not installed or connected. |
| Title card | Not currently wired | Retained for planning; real title-card generation is not connected. |
| Diagram | Not currently wired | Retained for planning; real diagram generation is not connected. |
| Reused media | Not currently wired | Existing-media selection and copying are not connected. |
| Transition only | Not currently wired | Special no-media scene handling is not connected; ordinary transitions remain supported separately. |
| Custom | Not currently wired | Custom workflow selection and dispatch are not connected. |

The current image-motion renderer uses a controlled FFmpeg push-in for actionable camera
instructions. Static instructions such as `locked`, `locked-off`, `none`, `no motion`, and `static`
do not animate the still.
