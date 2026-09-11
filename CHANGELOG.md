# Changelog

## v0.2.0 — 2026-09-11

Cross-platform core support plus voice/pipeline improvements since `v0.1.0`
(2026-09-05: first tagged release; CI green on Python 3.11/3.12, mock
pipeline verified).

### Highlights

- **Core studio runs from a source checkout on Windows, macOS, and Linux.**
  Native CI passes the full core suite on Ubuntu 24.04 x64 (Python 3.11,
  3.12), Windows 2025 x64 (Python 3.12), and macOS 15 Intel (Python 3.12).
  See `docs/installation.md`, `docs/native-core-ci.md`, and
  `docs/cross-platform-qualification.md`.
- **Gemini TTS (remote, opt-in).** Google Gemini TTS joins the Voice page as
  the one cloud exception to the local-first studio (PR #29). Inert without
  a key; see `docs/gemini-tts.md`.
- **Single deterministic-stage re-run.** `POST
  /api/projects/{id}/render/stages/{stage}` re-runs one render stage in
  isolation, with per-stage Re-run buttons on the Export screen (PR #20).

### Cross-platform core (PRs #21–#28)

- **Stage 1 — installation docs (PR #21).** `docs/installation.md`
  rewritten with separate Windows PowerShell and macOS/Linux paths:
  explicit venv Python invocation (never activate), editable core install,
  FFmpeg/ffprobe prerequisites and `subtitles`-filter verification,
  mock-mode configuration, CLI and browser-UI mock renders with expected
  outputs, stop/restart semantics, and labeled verification gaps.
- **Stage 2 — native CI and smoke (PR #22).** `.github/workflows/ci.yml`
  matrix: Ubuntu 24.04 × Python 3.11/3.12, Windows 2025 × Python 3.12,
  macOS 15 Intel × Python 3.12. `scripts/ci_core_smoke.py` checks
  prerequisites, starts an isolated mock app on a loopback port, verifies
  health/root/assets over HTTP, boots the real UI in fresh Chromium with
  non-loopback requests blocked, round-trips project create/reopen across
  a server restart, and renders a half-second `MockGeneratorBackend` MP4
  verified with ffprobe. `scripts/ci_test_report.py` uploads sanitized
  JUnit (identities/counts/timings only).
- **Stages 1–2 integration (PR #22).** Portable path handling (`as_posix`
  at persistence boundaries), shared FFmpeg/Chromium executable discovery
  (`backend/rendering/binaries.py`), owned-process lifecycle with explicit
  bounded cancellation (`backend/rendering/process.py`), stable process
  identities, ancestor-cancellation lineage, core-vs-optional capability
  reporting (`backend/core/capabilities.py`, `/api/system/status`, Models
  page readiness panel), Chromium argv/discovery fixes, thumbnail font
  fallback to bundled Noto, and wheel resource allowlisting (`setup.py`,
  `MANIFEST.in`).
- **Stage 3 — portable project paths (PR #23).** `backend/schemas/paths.py`
  gains `WINDOWS_RESERVED_NAMES`, `resolve_asset_path`, and
  `safe_portable_filename` (reserved devices, trailing dots/spaces,
  forbidden characters, UTF-8 byte budget). All stored media references
  (assets, `takes.json`, stage-state outputs, render results, chunk paths,
  editorial/thumbnail sources, file-serving endpoints) route through the
  resolver; `timeline.json` persists canonical relative paths.
  `tests/test_project_transfer.py` covers native copy-across-roots
  recovery, reopen, render, legacy-error, and filename rules.
- **Stage 4 — lifecycle acceptance (PR #24).** 207-test acceptance recorded
  in the qualification doc; no code changes (cancellation, bounded cleanup,
  staged publication, retry, no-auto-resume restart).
- **Stage 5 — hostile-path rendering (PR #25).** Preview renders and frame
  extraction under accented, CJK, bracketed, and quoted native directories
  (`tests/test_render_portable_paths.py`); 53-test media acceptance
  recorded.
- **Stage 6 — readiness separation.** `/api/system/status` preserves the
  core/optional separation over HTTP (`tests/test_capabilities.py`); 8-test
  acceptance recorded.
- **Stage 7 — wheel acceptance (PR #27).** `tests/test_distribution_resources.py`
  builds and unpacks a wheel without installing dependencies, imports it
  away from the checkout, serves its UI, and completes a mock render;
  3-test acceptance recorded.
- **Stage 8 — portable transfer.** Canonical forward-slash paths at every
  persistence boundary, conservative legacy reads with actionable errors
  (never rebasing guesses), Windows-safe generated names, existing IDs and
  file bytes never rewritten by moves or recovery.
- **Docs (PR #28).** Native CI support status recorded in
  `docs/installation.md`, `docs/native-core-ci.md`, and
  `docs/cross-platform-qualification.md`.

### Voice / TTS

- **Gemini TTS provider (PR #29).** `GeminiTTSBackend` (httpx, no new
  dependencies) on the `v1beta generateContent` API; 24 kHz PCM written to
  WAV via stdlib `wave`. Models: `gemini-3.1-flash-tts-preview` (default),
  `gemini-2.5-flash-preview-tts` (fallback), `gemini-2.5-pro-preview-tts`
  (quality); well-formed IDs pass through, unknown models fail with
  `model_unavailable`, effective model recorded in chunk provenance. Preset
  voices only — reference-profile cloning rejected. Key from
  `GEMINI_API_KEY` (wins) or Voice-page file (mode `0600`); never returned,
  logged, or included in errors. Endpoints `GET/PUT/DELETE
  /api/tts/gemini/key`, provider-aware `/api/tts/models`, 409 before
  queueing when disabled/key-less, 422 for voice-profile combination.
  Frontend: Gemini panel (voice select, style prompt, key entry/clear,
  per-take model picker), Generate disabled until a key is present.
  Non-deterministic takes labeled as such. Privacy contract, setup, and
  limits in `docs/gemini-tts.md`.
- **Local TTS hardening (PRs #1, #3–#11).** Timing fixes, continuous
  chunking for short scripts with scene grouping guarantees, Higgs output
  budgets scaled by word count (engine cap 8,192 tokens, ceiling
  rejection), duplicate-narration guard, chunk-tail noise fix, sentence
  holds limited to pre-next-sentence silence, narration seed exposed in
  the UI, reference voices persisted across projects with safe deletion
  (409 while selected), provider-scoped delivery tags clarified.
- **Qwen thumbnails (PR #17).** Additional thumbnail generation modes.

### Pipeline, shots, rendering

- **Stage re-run endpoint (PR #20).** Queue/execute exactly one of
  `timeline`, `render_preview`, `quality_control`, `render_final`,
  `thumbnails` (`editorial_visual` gated to editorial projects),
  single-flight with full renders, cache reuse, retryable failures.
- **Shot staleness (PR #19).** Per-shot stale flag + provenance in
  snapshots/summaries; regeneration/import clears, approval never clears;
  Storyboard/Scene-Editor/Timeline badges including "Approved but stale";
  export preflight asks for regeneration first.
- **Implicit shots (PR #18).** Every shot mutation materializes a legacy
  scene's deterministic implicit projection first via a shared resolver;
  placeholder workaround removed.
- **Editorial timing (PR #2).** Cuts aligned to caption sentences,
  configurable sentence hold, Chromium layout test stabilized.

### Frontend / UI (PRs #13, #14, #16)

- Boot guard diagnostic (no perpetual "Loading..."), `<noscript>` message,
  not-found route, modal focus order, focus-ring radius fix, Thumbnails
  icon, Dashboard selection badge, disabled project switcher when empty,
  GPU slot text classes, `100dvh` layout, token/CSS hygiene, `el()`
  boolean-prop fix, timeline label-width token.
- Recovery toast naming each entry with View-to-Models navigation and
  boot dedup; persistent Project recovery panel on the Models page;
  stale-plan "null" text fix; editorial strip scrolling/fade and
  settings-row wrapping; timeline music-lane wording for recorded input.
- Editorial Chromium startup retried once with diagnostics (stderr tail,
  executable/argv attached).

### Docs, config, compatibility (PRs #5, #12)

- **GPU framing.** 24 GB is the tested reference card, not a requirement.
  README compatibility table (CPU-only / 8 / 12 / 16 / 24 GB / larger) and
  per-backend free-VRAM floors in `docs/gpu-memory.md`. Mock pipeline,
  graphics, editorial overlays, thumbnails, and FFmpeg rendering need no
  GPU. Single gate: `gpu.minimum_free_vram_gb_for_heavy_job` (default 20).
- **Breaking config cleanup.** Removed unread `HardwareConfig` keys
  `max_vram_gb`, `allow_cpu_offload`, `system_ram_budget_gb` (config is
  strict — delete those lines from any copied `config/local.yaml`).
- Contribution workflow: protected `main`, feature/fix/docs branches, PR
  template and safety checklist (`CONTRIBUTING.md`).

### CI and distribution (PRs #15, #22)

- Pinned `playwright==1.62.0`; `LVS_CHROME` executable selection with path
  logging on Ubuntu 24.04; missing Chrome fails setup loudly.
- Per-leg `test-report-python-<version>-attempt-<number>` artifacts
  (sanitized `pytest.xml` + `browser.txt`), 14-day retention, uploaded even
  on failure; compact dot output locally (`-v` for names).
- Wheel contract: default config, browser UI, workflow/model-prompt
  templates, licensed Noto fonts via an explicit allowlist; mutable
  projects/databases/caches stay outside the package; no auto-migration to
  OS-specific storage defaults; managed AI workers remain source-checkout
  components (wheel carries no model environments). No package published
  to PyPI by these tests.

### Verification recorded for this release

- Local Linux workstation: full Python suite **1204 passed** (incl. 26
  project-transfer, 34 media-process lifecycle, 4 editorial/retry);
  `frontend/tests/static_checks.py` passed; `127/127` JS logic tests.
- Stage acceptances: lifecycle **207**, media/hostile-path **53**,
  readiness **8**, wheel **3** — all passed.
- Gemini TTS: 17 offline tests plus 8 model-override tests; full suite
  **1225 → 1233 passed**.
- Native CI matrix (Ubuntu 24.04 × 3.11/3.12, Windows 2025 × 3.12,
  macOS 15 Intel × 3.12): prerequisites, isolated browser/server smoke,
  full suite, frontend checks, sanitized reports, wheel acceptance — all
  passed. Final Windows jobs ~10–11 minutes.

### Migration notes

- Requires Python 3.11 or 3.12; FFmpeg on `PATH` (or pre-existing
  `imageio-ffmpeg` fallback for `ffmpeg` only); **ffprobe recommended**
  (no bundled fallback — media QC limited without it). macOS needs a full
  static FFmpeg with libass (`subtitles` filter); Homebrew default lacks
  it.
- Delete `max_vram_gb`, `allow_cpu_offload`, `system_ram_budget_gb` from
  `config/local.yaml` if present (unknown keys are rejected).
- Tune `gpu.minimum_free_vram_gb_for_heavy_job` to your card; no GPU is
  required for the mock pipeline.
- Unportable legacy stage outputs read as incomplete with an actionable
  error — retry regenerates them. Moves/recovery never rewrite existing
  IDs, directory names, or file bytes.
- Gemini TTS is the only cloud path: narration text + optional style
  direction leave the machine only when you select that provider and start
  a generation with your own key. Everything else stays local.

### Explicitly out of scope for this release

- Hands-on testing on physical Windows/macOS machines; interactive
  microphone/recording and media-file-lock behavior.
- Apple Silicon, Windows ARM, Linux distributions outside Ubuntu 24.04 CI.
- Clean-machine packaged installers / PyPI publication.
- Individual optional AI backends, GPU drivers, VRAM sufficiency, Metal,
  ROCm, remote worker hardware.

### Commits since v0.1.0 (33)

PRs #1–#20, #22–#25, #27–#29 plus staged evidence commits, from TTS timing
and delivery controls through cross-platform installation (PR #21),
Stages 1–7 integration and qualification (PRs #22–#28), to Gemini TTS
(PR #29). See `git log v0.1.0..v0.2.0 --oneline` for the full list.
