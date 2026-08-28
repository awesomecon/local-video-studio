# Local Video Studio

Local Video Studio is a local-first, restartable AI video-production application for Ubuntu and a
single 24 GB-class NVIDIA GPU. It keeps prompts and media on your machine, uses FFmpeg for
deterministic assembly, and stores projects in portable, human-readable directories.

The deterministic mock pipeline works without model downloads. Real image, video, speech, music,
and caption models are optional local backends with separate installation and licensing requirements.

## Requirements

- Python 3.11 or 3.12
- FFmpeg and ffprobe on `PATH` (or an existing `imageio-ffmpeg` installation)
- Git
- NVIDIA/CUDA only for optional real-model backends

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

The project is under active development. Start in mock mode and treat optional real-model setup as
experimental until its backend-specific health check succeeds.

## License

Local Video Studio's original source code and documentation are licensed under the
[Apache License 2.0](LICENSE).

Model weights, third-party dependencies, ComfyUI custom nodes, fonts, media, and other externally
sourced materials retain their own licenses and usage restrictions. Review those terms separately
before redistribution or commercial use.
