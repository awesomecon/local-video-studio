# ComfyUI integration

The default external endpoint is `http://127.0.0.1:8188`. An occupied port is accepted only after a
known ComfyUI endpoint returns the expected structure.

The adapter supports health/identity checks, workflow submission, recursive prompt and seed
substitution, local image upload, history polling, local output retrieval, and interrupt/cancellation
when supported. Reusable workflow templates live in `workflows/comfyui/`.

ComfyUI manages its own Python/model environment. Local Video Studio neither installs ComfyUI nor
changes its Torch build. Configure both applications to use the same Hugging Face/model cache where
their loaders support it.

Workflows must declare their intended capability, expected substitution keys, output node(s), and a
workflow version. FLUX is used for storyboards/references/thumbnails; Wan is the lower-cost motion
engine. Validate a workflow manually in ComfyUI before enabling it for unattended jobs.
