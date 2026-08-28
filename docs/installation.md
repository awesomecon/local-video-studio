# Installation

## 1. Inspect before changing anything

```bash
scripts/bootstrap.sh
scripts/doctor.sh
```

Bootstrap is inspection-only unless `--install-lightweight` is supplied. It will not change NVIDIA
drivers, CUDA, PyTorch, or model weights. Save the generated report locally when comparing a later
environment change.

## 2. Install lightweight application dependencies

Use the current environment only if the doctor classifies it as compatible:

```bash
scripts/bootstrap.sh --install-lightweight
```

This installs the local package and ordinary API/configuration dependencies. It deliberately does not
install PyTorch. Never use `sudo pip`.

## 3. Configure local paths

Defaults use `~/ai/cache`, `~/ai/models`, and `~/ai/projects`. Copy
`config/local.example.yaml` to ignored `config/local.yaml` for machine-specific paths, or use
`LOCAL_VIDEO_STUDIO_*` environment variables. Do not store secrets in either YAML file. Keep at
least 50 GiB free on model and cache targets.

## 4. Run mock mode first

```bash
export LOCAL_VIDEO_STUDIO_MOCK_MODE=1
lvs-mock-render --topic "How Roman aqueducts worked" --duration 30
```

The command prints the final project and MP4 paths. Validate this before configuring any real model.

To use the browser interface, start the FastAPI service and open its loopback URL. No Node.js or npm
installation is used or required; FastAPI serves the zero-build static frontend from the repository.

## 5. Connect the existing local LLM

Start the user's server separately on `127.0.0.1:1234`, then export its secret without committing it:

```bash
export LOCAL_LLM_API_KEY='your-local-server-key'
scripts/doctor.sh
```

Local Video Studio only verifies and uses the server. It never claims port 1234 or starts another LLM
runtime. Do not put the real key in `config/default.yaml`.

## 6. Add real media backends incrementally

Configure ComfyUI first, then FLUX/Wan workflows, narration/transcription/music workers, and finally
H3. Follow each backend document and run its health check before enabling it. Large model downloads
must be explicit and preceded by size, destination, and free-space review.

For Ideogram 4 NF4, follow `docs/local-ideogram4.md`. Its installer creates a separate runtime under
a configurable service root and requires the gated model license plus an environment-only `HF_TOKEN`
before it downloads weights. Once installed, the app starts that isolated service on demand by
default.
