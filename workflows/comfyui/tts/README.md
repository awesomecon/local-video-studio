# TTS workflow templates (ComfyUI)

API-format workflow templates for the four-model voice-cloning comparison
(see `docs/local-tts.md`).

| Provider | Template | Node project | Status |
| --- | --- | --- | --- |
| `index_tts_2_5` | `index-tts-2.5-clone.workflow.json` | `T8mars/comfyui-indextts25-t8` @ `b3a0dcd` | installed; import verified; live run pending |
| `voxcpm2` | `voxcpm2-clone.workflow.json` | `Saganaki22/ComfyUI-VoxCPM2` @ `0e52a6c` | installed; import verified; live run pending |
| `fish_s2_pro` | `fish-s2-pro-clone.workflow.json` | `Saganaki22/ComfyUI-FishAudioS2` @ `521f33f` | installed; import verified; live run pending |
| `omnivoice` | none — isolated localhost worker, not ComfyUI | `k2-fsa/OmniVoice` Python API | isolated environment/import verified; live run pending |

`{{placeholder}}` values are substituted by
`backend/models/tts_comfyui.py` before submission. Each `.metadata.json`
sidecar records the pinned node commit, workflow version, output node,
substitution schema, and safety notes.

## Verification status

Templates are derived from each node project's own example workflows at the
pinned commits and remain marked `"verified": false`. On 2026-08-25 all three
custom-node packages and their workflow node IDs passed a fresh-process import
and registration check. They become verified only after a live run against
ComfyUI (`127.0.0.1:8188`) succeeds; `readiness()` on each backend validates
the required node classes through `GET /object_info` before any submission,
so an out-of-date template fails fast with a missing-node error instead of a
half-finished generation.

## Safety invariants

- No template selects an auto-download variant; checkpoints are resolved from
  the central model store via symlinks or explicit paths.
- `enable_asr` (VoxCPM2) stays false so auxiliary ASR weights are never fetched.
- Loaders that support it release VRAM after every run so sequential provider
  comparisons start from a clean card.
- Reference audio is uploaded to the ComfyUI input directory per request;
  nothing is sent beyond loopback.

The running ComfyUI process must be restarted by the operator before the newly
installed nodes appear in `/object_info`. The IndexTTS checkout includes a local
compatibility patch in `__init__.py` that adds its repository root to `sys.path`;
without it, the pinned release cannot resolve its bundled top-level `indextts`
package under ComfyUI's current custom-node loader.
