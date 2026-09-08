# Local Video Studio

Local Video Studio is a local-first, restartable AI video-production application for Ubuntu. It
runs your own models on your hardware, keeps prompts and media on your machine, uses FFmpeg for
deterministic assembly, and stores projects in portable, human-readable directories. GPU and VRAM
needs depend on which optional model backends you enable; the deterministic mock pipeline works
without any GPU or model downloads.

Real image, video, speech, music, and caption models are optional local backends with separate
installation and licensing requirements.

The project was developed and tested on a 24 GB-class NVIDIA GPU. All current TTS, image, video,
and music backends work on that card; on any other GPU you choose the backends, quantizations,
and presets that fit. The table below is an honest per-card guide; the per-backend details live in
[GPU memory management](docs/gpu-memory.md) and [model backends](docs/models.md).

> **Repository note (Sep 5, 2026):** the `main` history was cleaned up; no code changed. If an
> existing clone reports divergent branches on pull, run `git fetch origin`, then
> `git checkout main && git reset --hard origin/main` (back up any unpushed work first).

## Requirements

- Python 3.11 or 3.12
- FFmpeg and ffprobe on `PATH` (or an existing `imageio-ffmpeg` installation)
- Git
- NVIDIA/CUDA only for optional real-model backends

## GPU compatibility

| Card size | Runs |
| --- | --- |
| No GPU / CPU | The whole deterministic mock pipeline, Graphics Screens, editorial overlays, thumbnails, and FFmpeg rendering; caption alignment on CPU. No GPU is ever required for the application itself. |
| 8 GB | Lighter TTS (Chatterbox, IndexTTS 2.5, and the ~8 GiB providers with headroom), captions on CPU or small models, graphics and editing. |
| 12 GB | Everything above plus most still-image generation with smaller presets and lighter TTS (Qwen3-TTS 1.7B, OmniVoice, VoxCPM2, Breeze eager). |
| 16 GB | 12 GB features plus video via Wan-class models, heavier TTS (Fish S2 Pro), and still models with more headroom. |
| 24 GB (reference) | All current backends: H3, Krea 2 Turbo, Qwen-Image-2512, Ideogram 4, ACE-Step 1.5 XL, Higgs TTS 3, and every TTS provider. |
| Larger | Everything, with more headroom; nothing in the Studio caps larger cards. |

These are typical free-VRAM floors observed on the reference card, not exact requirements. One
setting, `gpu.minimum_free_vram_gb_for_heavy_job` (default 20), gates the heavy cold loads; tune it
to your card. Full per-backend numbers and smaller-card alternatives: see
[GPU memory management](docs/gpu-memory.md).

## Quick start (no model downloads)

```bash
git clone https://github.com/awesomecon/local-video-studio.git
cd local-video-studio
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

scripts/bootstrap.sh
export LOCAL_VIDEO_STUDIO_MOCK_MODE=1
python -m backend.pipeline.cli \
  --topic "How Roman aqueducts worked" \
  --duration 30 \
  --resolution 640x360 \
  --output-root ./projects
```

The command prints the portable project directory and `renders/final.mp4`. Rerunning completed
stages reuses their saved outputs.

To use the local web interface:

```bash
export LOCAL_VIDEO_STUDIO_MOCK_MODE=1
python scripts/check_ports.py --verify-external
python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8009
```

Open `http://127.0.0.1:8009/`. The frontend is plain HTML, CSS, and JavaScript served by FastAPI;
there is no Node.js build step.

## Configuration and privacy

Portable defaults are in `config/default.yaml`. For machine paths, copy
`config/local.example.yaml` to ignored `config/local.yaml`; environment overrides take precedence.
Never store API keys in YAML. Port 1234 is reserved for an externally managed OpenAI-compatible
local LLM, and the application never starts or stops that service.

Local Video Studio binds to loopback by default, does not enable telemetry, and never downloads
model weights automatically. See [installation](docs/installation.md),
[architecture](docs/architecture.md), [local LLM](docs/local-llm.md),
[model backends](docs/models.md), [multi-shot scenes](docs/shots.md), and
[rendering](docs/rendering.md).

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest
python3 frontend/tests/static_checks.py
```

The `main` branch is protected. Make every code or documentation change on a separate feature, fix,
or documentation branch, then merge it through a pull request after the required checks and review
pass. Do not commit or push changes directly to `main`. See [CONTRIBUTING.md](CONTRIBUTING.md) for
the complete workflow and safety checklist.

The project is under active development. Start in mock mode and treat optional real-model setup as
experimental until its backend-specific health check succeeds.

## License

Local Video Studio's original source code and documentation are licensed under the
[Apache License 2.0](LICENSE).

Model weights, third-party dependencies, ComfyUI custom nodes, fonts, media, and other externally
sourced materials retain their own licenses and usage restrictions. Review those terms separately
before redistribution or commercial use.
