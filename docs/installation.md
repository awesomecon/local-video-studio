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
| macOS | Native Intel CI is configured; **qualification pending actual runner results**. Apple Silicon is a separate target. |
| Windows (PowerShell) | Native x64 CI is configured; **qualification pending actual runner results**. Windows ARM is a separate target. |

The build now includes core runtime resources and tests an unpacked wheel outside
the checkout. Clean-machine and native release qualification remain pending; this
does not mean a package has been published to PyPI. Use the editable source-checkout
instructions below. See [qualification](cross-platform-qualification.md).

## Prerequisites (all platforms)

- Python **3.11 or 3.12**
- **FFmpeg** (required unless an existing `imageio-ffmpeg` installation provides its bundled
  binary) and **ffprobe** (recommended — there is no bundled ffprobe fallback; without it the
  media QC falls back to FFmpeg-only probing and is limited)
- Git
- No Node.js, no GPU stack, no model downloads.

### FFmpeg / ffprobe per OS

- **Windows**: install an official Windows build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)
  (the builds linked from [ffmpeg.org](https://www.ffmpeg.org/download.html)), or
  `winget install Gyan.FFmpeg`. Add the build's `bin` directory to `PATH`.
- **macOS**: Homebrew's default `ffmpeg` bottle is built **without libass**, so
  caption burn-in fails with a missing `subtitles` filter. Install a full
  static build instead — the Intel builds at [evermeet.cx](https://evermeet.cx/ffmpeg/)
  are compiled with `--enable-libass` (the keg-only `ffmpeg-full` formula is the
  Homebrew alternative). Apple Silicon is a separate, unqualified target.
- **Debian/Ubuntu Linux**: `sudo apt-get update && sudo apt-get install --yes ffmpeg` (installs
  both `ffmpeg` and `ffprobe`).

Verify on any platform (the second command must list a `subtitles` filter;
without it, caption burn-in is rejected before rendering starts):

```text
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters | grep " subtitles "
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
repository importable as `backend.*` and adds three console scripts — `lvs-mock-render`,
`local-video-studio`, and `lvs-ideogram-prompt` (builds Ideogram 4 prompts for the optional
backend) — in `.venv\Scripts\` (Windows) or `.venv/bin` (macOS/Linux); the commands on this page
do not depend on them.

On a Bash host, `scripts/bootstrap.sh` reports the environment first, and
`scripts/bootstrap.sh --install-lightweight` performs the same lightweight install. Note that it
invokes bare `python`, so it acts on whichever environment your shell has selected — activate
`.venv` first (or otherwise select it) if you want it to target the virtual environment. The direct
venv-Python commands above remain the primary path.

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

On a Bash host, `scripts/doctor.sh` combines the environment report, the port check, and a
secret-safety note into one run (see [Troubleshooting](troubleshooting.md)).

Many report warnings concern **optional AI dependencies or external services, not core
mock-mode prerequisites**: "PyTorch is unavailable" (expected in a fresh core environment — the
report itself notes mock mode remains usable with FFmpeg), CUDA/`nvidia-smi`/free-VRAM notes,
torch-family version conflicts, and the optional backend compatibility lines.
`check_ports.py --verify-external` likewise reports the external local LLM (port 1234) and ComfyUI
(port 8188) identities, which only matter once you connect those services. The core prerequisites
are Python 3.11/3.12, FFmpeg, and Git; ffprobe is recommended (without it the report notes
"ffprobe is not on PATH; media QC will be limited" and the app falls back to FFmpeg-only
probing). The 50 GiB free-space policy matters for the model/cache targets before downloading
weights.

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

Expected output — the command prints JSON with the project directory and final MP4. Paths are
printed expanded (an absolute path under the configured project root, or the path you pass to
`--output-root`), not as `~`:

```json
{
  "project_id": "…",
  "project_directory": "/home/username/ai/projects/how-roman-aqueducts-worked",
  "final_mp4": "/home/username/ai/projects/how-roman-aqueducts-worked/renders/final.mp4"
}
```

The project directory (under the configured project root, default `~/ai/projects`, or your
`--output-root`) is portable and human-readable: `plan.json`, `script/`, `narration/master.wav`,
`music/`, `scenes/` (one directory per scene with prompt and visual), `renders/preview.mp4`,
`renders/qc.json`, `renders/final.mp4`, `subtitles/`, and `thumbnails/`. Each CLI invocation
creates a **new project**; when the slug directory already exists, the new one receives a `-2`
(then `-3`, ...) suffix. Rerunning a *stage* of an existing project is a different operation: do it
from the web UI or API (for example **Re-render final video** or a job retry), where
already-completed stages are kept and only missing outputs are rebuilt.

## 6. Start the browser UI

Start the service in a shell where mock mode is configured (step 4).

### Windows (PowerShell)

```powershell
& .venv\Scripts\python.exe -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8009 --timeout-graceful-shutdown 10
```

### macOS / Linux

```bash
.venv/bin/python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8009 --timeout-graceful-shutdown 10
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

**Stop the application**: press `Ctrl+C` in the terminal; uvicorn shuts down gracefully. Project
files and completed stage outputs persist. Running jobs do not survive a restart: at startup they
are marked failed with "backend restarted before this job finished; retry to resume from completed
stages". Retry them explicitly from the Job Monitor (**Retry**); the retry re-runs the top-level
stage, keeps already-completed stages, and rebuilds only what is missing.

## Browsing the UI vs. headless Chromium

These are separate requirements:

- **Browsing the UI** only needs an ordinary desktop web browser. The frontend talks to the local
  API over HTTP and has no special browser-engine dependency.
- **Headless Chromium** is a backend rendering dependency, used only for **Editorial Mode**
  (the `editorial_visual` stage — the app fails with "Chromium is required for Editorial Mode
  rendering" when it is missing) and for **Graphic Screen** visuals (the `chromium-headless`
  renderer). The Classic mock pipeline on this page does not need it.

Discovery: the app first honors the `LVS_CHROME` environment variable (path to a browser binary),
then checks Chrome/Chromium on `PATH` and conventional Windows, macOS and Linux install locations.
Linux Snap dispatchers prefer the underlying Chromium binary when available.
It renders with a throw-away profile and never uses your everyday browser profile. Install
a Chromium/Chrome build for your OS if you plan to use Editorial Mode or Graphic Screens.

## Configuration precedence

1. `config/default.yaml` — portable defaults (paths `~/ai/models`, `~/ai/projects`, `~/ai/cache`;
   bind `127.0.0.1`; backend port `8009`; port `1234` reserved for the external local LLM).
2. `config/local.yaml` — machine-local overrides (git-ignored; copy `config/local.example.yaml`).
3. Environment variables — `LOCAL_VIDEO_STUDIO__SECTION__KEY` structured overrides, plus the
   documented single-value aliases such as `LOCAL_VIDEO_STUDIO_MODEL_ROOT`,
   `LOCAL_VIDEO_STUDIO_PROJECT_ROOT`, and `LOCAL_VIDEO_STUDIO_CACHE_ROOT`;
   `LOCAL_VIDEO_STUDIO_MOCK_MODE` for mock mode.
4. Per-invocation CLI flags where supported: `--config` on the diagnostic scripts
   (`scripts/check_environment.py`, `scripts/check_ports.py`) and `--output-root` / `--database`
   on the mock-render CLI (`backend.pipeline.cli` has no `--config`).

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

- **Windows (PowerShell)**: native CI results still need review. Commands were
  checked against the repository entry points and standard PowerShell path/invocation syntax; a
  Windows machine is still needed to confirm the venv layout, `winget` package versions, and
  FFmpeg-on-`PATH` behavior.
- **macOS**: not executed natively. Confirm the Homebrew/python.org interpreter layout and FFmpeg
  on `PATH` on a Mac.
- **Python 3.11 vs 3.12 on Windows/macOS**: CI covers both versions on Ubuntu only.
- **Headless Chromium on Windows/macOS**: platform-aware discovery is implemented; native
  browser execution still needs qualification.
- **Wheel/sdist installation outside a source checkout**: see the artifact checks and remaining
  clean-machine/native gaps in [qualification](cross-platform-qualification.md).
