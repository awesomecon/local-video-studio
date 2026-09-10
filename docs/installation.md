# Installation

This page covers the **core studio from a source checkout**: an editable Python package
installation, mock mode, the browser UI, and a short CLI mock render. The core works without a
GPU, without PyTorch/CUDA, and without model downloads. Real model backends (local LLM, TTS,
image/video/music, captions) are optional and are installed separately — see
[model backends](models.md), [local LLM](local-llm.md), and
[GPU memory management](gpu-memory.md).

## Verification status

| Platform | Status |
| --- | --- |
| Ubuntu / Linux | Natively verified: CI runs the full Python suite on Ubuntu 24.04 with Python 3.11 and 3.12, and the runtime commands on this page (mock render, environment and port checks) were exercised on Ubuntu. |
| macOS | Documented against the official Python and FFmpeg sources; **not natively verified** (no macOS CI runner, no local macOS execution). |
| Windows (PowerShell) | Documented against the official Python and FFmpeg sources; **not natively verified** (no Windows CI runner, no local Windows execution). |

Installing a built wheel or sdist outside a source checkout (`pip install local-video-studio`) is
**not yet qualified**. Install the editable package from a source checkout, as below.

## Prerequisites (all platforms)

- Python **3.11 or 3.12**
- **FFmpeg** on `PATH` (required) and **ffprobe** (recommended; without it the media QC falls back
  to FFmpeg-only probing and is limited)
- Git
- No Node.js, no GPU stack, no model downloads.

### FFmpeg / ffprobe per OS

- **Windows**: install an official Windows build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)
  (the builds linked from [ffmpeg.org](https://www.ffmpeg.org/download.html)), or
  `winget install Gyan.FFmpeg`. Add the build's `bin` directory to `PATH`.
- **macOS**: `brew install ffmpeg` (Homebrew; the installation route ffmpeg.org recommends for
  macOS).
- **Debian/Ubuntu Linux**: `sudo apt-get update && sudo apt-get install --yes ffmpeg` (installs
  both `ffmpeg` and `ffprobe`).

Verify on any platform:

```text
ffmpeg -version
ffprobe -version
```

The application never installs FFmpeg itself. It locates `ffmpeg` on `PATH`; if the Python package
`imageio-ffmpeg` happens to already be present in the environment, its bundled executable is used
as a fallback (the core package does not declare or install it).

### Python per OS

- **Windows**: the official installer from [python.org](https://www.python.org/downloads/) (3.11 or
  3.12), or `winget install Python.Python.3.12` (`Python.Python.3.11` works too). The installer
  registers the `py` launcher.
- **macOS**: the official installer from python.org, or `brew install python@3.12` /
  `python@3.11`. macOS may ship a different `python3`; use an explicit `python3.12` (or
  `python3.11`) interpreter for the virtual environment.
- **Debian/Ubuntu Linux**: `sudo apt-get install python3.12 python3.12-venv` (or `python3.11`).

## 1. Create a virtual environment

Work from the source checkout root (clone the repository first). Every command on this page calls
the virtual environment's Python **directly**, so the environment is never activated.

### Windows (PowerShell)

```powershell
py -3.12 -m venv .venv
& .venv\Scripts\python.exe --version
```

(`py -3.11 -m venv .venv` if you installed 3.11; if `python` on `PATH` is already 3.11/3.12,
`python -m venv .venv` is equivalent.)

### macOS / Linux (bash or zsh)

```bash
python3.12 -m venv .venv
.venv/bin/python --version
```

## 2. Install the core package (editable)

### Windows (PowerShell)

```powershell
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -e .
```

### macOS / Linux

```bash
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

This installs only the declared core dependencies (FastAPI, uvicorn, Pydantic, Pillow, PyYAML,
psutil, httpx, python-multipart, websocket-client). It does not install PyTorch/CUDA, does not
download model weights, and never touches an existing GPU stack. The editable install makes the
repository importable as `backend.*` and adds two console scripts — `lvs-mock-render` and
`local-video-studio` — in `.venv\Scripts\` (Windows) or `.venv/bin` (macOS/Linux); the commands on
this page do not depend on them.

On a Bash host you may instead use the existing inspection-first wrapper: `scripts/bootstrap.sh`
reports the environment first, and `scripts/bootstrap.sh --install-lightweight` performs this same
lightweight install. It is a convenience wrapper, not a requirement.

## 3. Verify the installation

### Windows (PowerShell)

```powershell
& .venv\Scripts\python.exe scripts/check_environment.py
& .venv\Scripts\python.exe scripts/check_ports.py --verify-external
```

### macOS / Linux

```bash
.venv/bin/python scripts/check_environment.py
.venv/bin/python scripts/check_ports.py --verify-external
```

- `check_environment.py` prints a secret-free report: Python version, FFmpeg/ffprobe discovery,
  configured disk targets (50 GiB free-space policy), and compatibility notes. Use `--skip-cuda`
  in restricted sandboxes where `nvidia-smi` probes are unavailable.
- `check_ports.py` reports the state of the configured ports without claiming or terminating
  anything; port 1234 remains reserved for your externally managed local LLM.

## 4. Configure mock mode

Mock mode runs the full deterministic pipeline with mock backends: no local LLM, no TTS, image,
video, or music models, and no downloads.

- The **CLI mock render** (`-m backend.pipeline.cli`) always runs in mock mode; no variable is
  needed.
- The **browser UI / API** reads `LOCAL_VIDEO_STUDIO_MOCK_MODE` (`1`, `true`, or `yes`,
  case-insensitive) from the environment of the shell that starts it.

### Windows (PowerShell)

```powershell
$env:LOCAL_VIDEO_STUDIO_MOCK_MODE = "1"
```

### macOS / Linux

```bash
export LOCAL_VIDEO_STUDIO_MOCK_MODE=1
```

The variable is per-shell and per-process; the application does not read `.env` files. Remove it
later (`Remove-Item Env:LOCAL_VIDEO_STUDIO_MOCK_MODE` on Windows, `unset
LOCAL_VIDEO_STUDIO_MOCK_MODE` on bash/zsh) to run against real backends.

## 5. Run a short mock render (CLI)

### Windows (PowerShell)

```powershell
& .venv\Scripts\python.exe -m backend.pipeline.cli --topic "How Roman aqueducts worked" --duration 8 --resolution 640x360
```

### macOS / Linux

```bash
.venv/bin/python -m backend.pipeline.cli \
  --topic "How Roman aqueducts worked" --duration 8 --resolution 640x360
```

Options: `--topic` (required), `--title`, `--duration` in seconds (default 30), `--resolution
WxH` (default 640x360), `--output-root`, `--database`.

Expected output — the command prints JSON with the project directory and final MP4:

```json
{
  "project_id": "…",
  "project_directory": "~/ai/projects/how-roman-aqueducts-worked",
  "final_mp4": "~/ai/projects/how-roman-aqueducts-worked/renders/final.mp4"
}
```

The project directory (under the configured project root, default `~/ai/projects`, or your
`--output-root`) is portable and human-readable: `plan.json`, `script/`, `narration/master.wav`,
`music/`, `scenes/` (one directory per scene with prompt and visual), `renders/preview.mp4`,
`renders/qc.json`, `renders/final.mp4`, `subtitles/`, and `thumbnails/`. Rerunning completed
stages reuses their saved outputs.

## 6. Start the browser UI

Start the service in a shell where mock mode is configured (step 4).

### Windows (PowerShell)

```powershell
& .venv\Scripts\python.exe -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8009
```

### macOS / Linux

```bash
.venv/bin/python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8009
```

The installed `local-video-studio` console script runs the same app and auto-selects a free port
in 8000–8999 when 8009 is already in use; reserved ports (including 1234) are never claimed.

Open `http://127.0.0.1:8009/` in any modern desktop web browser. The frontend is plain HTML, CSS,
and JavaScript served by FastAPI; there is no Node.js or npm build step. `GET /health` reports
`{"status": "ok", "mode": "mock"}` in mock mode.

**Short mock render in the UI** (Classic style):

1. **New Project** → title and topic, a short target duration (for example 10 s), and a low
   resolution such as 640×360 for speed → **Create project**.
2. **Script** → **Run planning** (mock plan; no LLM required).
3. **Storyboard** → **Generate all** (mock visual for every scene).
4. **Voice** → **Generate narration** (mock narration).
5. **Export** → **Render final video**, and watch progress in **Job Monitor**.

Expected: the Export screen shows the completed final output with a project-scoped local URL, and
the project directory under the configured project root contains `renders/final.mp4`.

**Stop the application**: press `Ctrl+C` in the terminal. Uvicorn shuts down gracefully; jobs and
stage state persist, and a restart resumes from the first incomplete stage.

## Browsing the UI vs. headless Chromium

These are separate requirements:

- **Browsing the UI** only needs an ordinary desktop web browser. The frontend talks to the local
  API over HTTP and has no special browser-engine dependency.
- **Headless Chromium** is a backend rendering dependency, used only for **Editorial Mode**
  (the `editorial_visual` stage — the app fails with "Chromium is required for Editorial Mode
  rendering" when it is missing) and for **Graphic Screen** visuals (the `chromium-headless`
  renderer). The Classic mock pipeline on this page does not need it.

Discovery: the app first honors the `LVS_CHROME` environment variable (path to a browser binary),
then checks Linux-specific snap Chromium locations, then `chromium` or `chromium-browser` on
`PATH`. It renders with a throw-away profile and never uses your everyday browser profile. Install
a Chromium/Chrome build for your OS if you plan to use Editorial Mode or Graphic Screens.

## Configuration precedence

1. `config/default.yaml` — portable defaults (paths `~/ai/models`, `~/ai/projects`, `~/ai/cache`;
   bind `127.0.0.1`; backend port `8009`; port `1234` reserved for the external local LLM).
2. `config/local.yaml` — machine-local overrides (git-ignored; copy `config/local.example.yaml`).
3. Environment variables — `LOCAL_VIDEO_STUDIO__SECTION__KEY` structured overrides, plus the
   documented single-value aliases such as `LOCAL_VIDEO_STUDIO_MODEL_ROOT`,
   `LOCAL_VIDEO_STUDIO_PROJECT_ROOT`, and `LOCAL_VIDEO_STUDIO_CACHE_ROOT`;
   `LOCAL_VIDEO_STUDIO_MOCK_MODE` for mock mode.
4. Per-invocation CLI flags (`--config`, `--output-root`, `--database`).

Secrets are read only from named environment variables (for example `LOCAL_LLM_API_KEY`); never
store them in YAML. Keep at least 50 GiB free on model and cache targets.

## Optional model setup (separate from the core)

- **Local LLM**: run your existing OpenAI-compatible server on `127.0.0.1:1234`, export
  `LOCAL_LLM_API_KEY` (value never committed), and see [local LLM](local-llm.md). The application
  only verifies and uses the server; it never claims port 1234 or starts another LLM runtime.
- **Media backends** (TTS, stills, video, music, captions): add them incrementally, each in its
  own isolated environment, following [model backends](models.md) and the per-backend documents.
  Large model downloads are always explicit and preceded by size, destination, and free-space
  review. Check [GPU memory management](gpu-memory.md) for the free-VRAM a backend typically
  needs before enabling it.
- **Development extras** (tests only, still no GPU stack):
  `.venv/bin/python -m pip install -e '.[dev]'` (Windows:
  `& .venv\Scripts\python.exe -m pip install -e ".[dev]"`).
- [Troubleshooting](troubleshooting.md) covers ports, missing FFmpeg, VRAM, and interrupted
  generation.

## Native verification gaps

- **Windows (PowerShell)**: not executed natively (no Windows machine or CI runner). Commands were
  checked against the repository entry points and standard PowerShell path/invocation syntax; a
  Windows machine is still needed to confirm the venv layout, `winget` package versions, and
  FFmpeg-on-`PATH` behavior.
- **macOS**: not executed natively. Confirm the Homebrew/python.org interpreter layout and FFmpeg
  on `PATH` on a Mac.
- **Python 3.11 vs 3.12 on Windows/macOS**: CI covers both versions on Ubuntu only.
- **Headless Chromium on Windows/macOS**: discovery is OS-agnostic code, but only Linux execution
  has been verified.
- **Wheel/sdist installation outside a source checkout**: not yet qualified.
