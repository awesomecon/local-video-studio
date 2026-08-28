# Local Video Studio engineering rules

## Private machine context

- Before repository work, check whether `AGENTS.local.md` exists in the repository root.
- If it exists, read it completely after this file. It contains private machine-specific context.
- `AGENTS.local.md` must never be committed, quoted in public diagnostics, or treated as a place for
  secrets. API keys and tokens remain environment variables only.

## Non-negotiable safety

- Read this file before repository work.
- Never inspect `.env` or secret files unless the user explicitly asks. Secrets are read only from named environment variables at runtime and are never logged, persisted, committed, or returned by diagnostics.
- Never replace, downgrade, or reinstall NVIDIA drivers, CUDA, PyTorch, torchvision, or torchaudio automatically. Inspect first. Backend-specific incompatibilities belong in isolated environments.
- Never download model weights automatically. Before any download larger than 1 GB, report the model, approximate size, destination, and free disk space, then require an explicit user action.
- Preserve at least 50 GB free on a model/cache target. Use only user-configured writable model and cache directories.
- Port 1234 is externally owned by the user's local LLM. Never bind, start, stop, restart, or kill anything on that port.
- Bind application services to `127.0.0.1` by default. Never kill a process to resolve a port conflict.
- Do not expose prompts, media, scripts, reference images, or voice samples to remote services unless the user explicitly enables one. No telemetry by default.
- GPU-heavy jobs are serialized on a single configured GPU. Inspect system-wide VRAM before loading; never terminate the user's LLM server to reclaim VRAM.

## Architecture and quality

- Keep reasoning enabled for the user's local LLM. For director/script generation, preserve the
  configured reasoning budget; do not disable thinking as a latency optimization unless the user
  explicitly changes this preference.
- Keep the mock pipeline working before real-model integration. Automated tests never download large weights.
- Keep backends behind `GeneratorBackend`; prefer local service adapters for dependency-heavy models.
- Every stage and scene is restartable. Persist prompts, negative prompts, seeds, model/version, quantization, workflow version, settings, attempts, and file hashes.
- FFmpeg performs deterministic editing and final rendering.
- Projects remain portable and human-readable on disk; SQLite is an index, not the only source of truth.
- Use type hints, structured errors, localhost-safe defaults, and sanitized logs.
- Use `apply_patch` for repository edits. Do not touch unrelated user changes.

## Parallel development

- The main checkout is the integration workspace and owns `pyproject.toml`, lock files, this file, `.gitignore`, README, shared contracts, dependency resolution, and migrations.
- Use Git worktrees for parallel workers. Workers stay on their assigned branches, modify only owned paths, run focused tests, and report commits. The integration agent inspects and merges.
- Never install dependencies concurrently or run heavyweight GPU tests in parallel.

Owned paths:

- core: `backend/core/`, `backend/schemas/`, `backend/storage/`, `config/`, `scripts/check_environment.py`, `scripts/check_ports.py`, `scripts/ui_shots.py`
- media: `backend/rendering/`, `backend/timeline/`, media tests
- models: `backend/models/`, `backend/workers/`, model tests
- frontend: `frontend/`

## Validation

- Do not install missing development tools automatically. Validate Python changes with
  `python -m pytest` and frontend changes with `python3 frontend/tests/static_checks.py`.
- Machine-specific runtime facts belong in ignored `AGENTS.local.md`, never in this tracked file.

## UI inspection (headless browser)

Use this to see the real UI, verify frontend changes, or hunt for console errors. It runs
headless against the local backend; no display server is needed.

1. Ask the user to start the backend if it is not already running (agents must not start or
   stop it themselves when the environment forbids background processes):
   `python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8009`
   The frontend is served by the same process at the root (`http://127.0.0.1:8009/#/`).
2. Capture every hash route plus a per-page console report:
   `python3 scripts/ui_shots.py`
   Useful flags: `--project <uuid>|none` (default: newest project, seeded into
   sessionStorage so project-bound pages render with data), `--only dashboard,thumbnails`,
   `--full` for full-page screenshots, `--out <dir>` (default `/tmp/lvs-shots`),
   `--settle <seconds>` (default 4). Exit code 1 means a page threw an uncaught exception.
   Read the PNGs to visually review pages.
3. Frontend static checks (no browser needed): `python3 frontend/tests/static_checks.py`.
   Note: it is a standalone script, not a pytest module - run it directly.
4. Set `LVS_CHROME` when Chromium is not discoverable automatically. The script uses a throw-away
   profile and requires `websocket-client` from the `dev` extra. Never reuse the user's real Chrome
   profile, and never point the browser at non-localhost origins.
5. Remember: ES module graphs fail atomically. One syntax error in any statically imported
   frontend module blanks the entire app to the index.html "Loading..." state - if a capture
   shows only "Loading...", check the console report for a SyntaxError first.
