# Core platform qualification

The core studio needs Python and FFmpeg, not an AI runtime. Installing a model,
running its managed worker, and connecting to an existing HTTP service are
separate capabilities. A successful Linux run or a test with simulated Windows
paths does not establish Windows support.

## Implementation stages

1. Native-shell editable-install instructions: `docs/installation.md`.
2. Native CI and isolated browser/server smoke: `docs/native-core-ci.md`.
3. Canonical relative asset paths, conservative legacy reads, and portable new
   project directory names. Existing project IDs and directory names are retained.
4. Explicit job cancellation, bounded owned-process cleanup, and staged render
   publication. Failed/canceled work can be retried; restart does not automatically
   resume jobs. Previous completed renders remain until replacement succeeds.
5. Shared executable discovery, selected FFmpeg encoder/subtitle checks, bundled
   Noto fonts, and real rendering tests with punctuation and Unicode in paths.
6. Core readiness and optional backend restrictions reported separately.
7. Wheel runtime resources and an acceptance test outside the source checkout.
8. Portable project transfer: canonical forward-slash paths at every
   persistence boundary (`backend/schemas/paths.py`), a single resolving
   helper for all stored media references, conservative legacy reads with
   actionable errors instead of rebasing guesses, Windows-safe generated
   names, and a native copy-across-roots recovery/render test
   (`tests/test_project_transfer.py`) that the CI matrix executes on each OS.
   Existing project IDs, directory names, and file bytes are never rewritten
   by moves or recovery.

## Distribution contract

The wheel includes the default configuration, browser UI, workflow templates,
model prompt templates, and licensed Noto fonts. Builds use an explicit resource
allowlist; private local configuration and development files are excluded.
Mutable projects, databases and caches use configured locations outside the
installed package. Source-checkout local configuration remains supported.
There is no automatic migration to new OS-specific storage defaults.

`tests/test_distribution_resources.py` builds and unpacks a wheel without
installing dependencies. An isolated subprocess imports that distribution away
from the checkout, serves its UI through an ASGI test client, and completes a mock
pipeline render. It uses the existing interpreter dependencies, so this is not
proof of a clean-machine installer or dependency-resolution result. CI's clean
core environment supplies that complementary check. No package is published by
these tests.

Managed AI worker scripts are optional source-checkout components. A wheel does
not contain their model environments. A compatible external HTTP worker can be
used without a local launcher, subject to the existing endpoint/privacy settings.

## Native CI evidence and remaining manual checks

The required native CI results were reviewed and passed for Ubuntu 24.04 x64
(Python 3.11/3.12), Windows 2025 x64 (Python 3.12), and macOS 15 Intel
(Python 3.12). This establishes core source-checkout support for those covered
configurations through automated native CI. It does not claim hands-on testing
on physical Windows or macOS machines. Apple Silicon and Windows ARM remain
separate, unqualified targets.

For each target, retain runner/tool versions and test outcomes, then check:

- Editable installation and wheel acceptance, without AI packages or model files.
- Browser startup, project creation/reopen, uploads/downloads and media playback.
- Mock generation, editing an existing scene, and rendering the updated timeline.
- Paths with spaces, Unicode, apostrophes and long nested parent directories.
- Copy a project from another OS, reopen it and render without rewriting IDs.
- Cancel an active render; verify its previous completed output survives, its
  temporary output is removed, unrelated processes survive, and retry works.
- Shut down with active work, reopen, and explicitly retry interrupted jobs.
- Optional Chromium features, subtitles and bundled fonts in actual output.

Browser microphone/recording behavior and media-file locks during playback need
interactive native checks. Windows process-tree cleanup also needs native stress
testing; deliberately detached descendants are outside the current containment
guarantee. Python frame producers must cooperate with cancellation between calls.
Passing the core checks makes no claim about individual AI models, GPU drivers,
VRAM sufficiency, Metal, ROCm, or remote worker hardware.

## Recorded verification status

On a local Linux workstation run of this branch: full Python suite
1204 passed (including 26 project-transfer tests: copy-across-roots recovery,
reopen, mock render, unportable-reference handling, portable round-trip
archiving, disk-free hostile-name sanitizing, symlinked-root timeline
serialization, and generated-name rules); `tests/test_media_process_lifecycle.py` 34 passed (includes the
POSIX late-group-member escalation case); editorial/render-stage/serial-worker
retry cases 4 passed; Stage 4 lifecycle acceptance 207 passed with no code
changes (media-process lifecycle, job queue including restart recovery,
service shutdown/cancel boundaries, render-stage API, worker lanes,
editorial, media rendering, multishot archive/rollback publication, graphic
publication restore, and regeneration archiving: canceled/failed work stays
retryable, restarts fail active jobs instead of resuming them, and staged
publication keeps the last good output until its replacement succeeds);
Stage 5 media acceptance 53 passed with three new hostile-path cases
(executable discovery, encoder/subtitle feature checks, real preview render
and frame extraction under accented, CJK, bracketed, and quoted native
paths, bundled-font identity, and wheel resource inclusion);
Stage 6 readiness acceptance 8 passed with one new serving-layer case
(`/api/system/status` preserves the core/optional separation over HTTP:
optional absence cannot decide core status, host labels stay descriptive);
Stage 7 wheel acceptance 3 passed with no code changes (distribution
resources plus CI infrastructure: the wheel carries its resource allowlist,
imports away from the checkout, serves its UI, and completes a mock render);
`frontend/tests/static_checks.py` passed;
`frontend/tests/run_js_tests.py` 127/127 passed. Live UI screenshots were then
captured against the user's running backend (`scripts/ui_shots.py`, throw-away
Chromium profile): every route rendered with no uncaught page exceptions; the
Models page shows the new "Core studio / Ready" readiness panel with per-tool
badges. Export/Thumbnails return 500 only for the single indexed project whose
on-disk directory is absent (SQLite references a slug with no directory; the
same `load_project` FileNotFoundError path exists on the base commit, so this
is pre-existing data state, not a branch regression). Remaining per-page
resource 404s are missing media assets for that same project, and the Models
503 is an optional AI service probe when its worker is down.

The full required matrix also passed on native GitHub-hosted Ubuntu 24.04,
Windows 2025 x64, and macOS 15 Intel runners. Each job completed prerequisite
installation and discovery, the isolated browser/server smoke, the full Python
suite, frontend static checks, report sanitization and wheel acceptance. Final
Windows jobs completed in roughly 10-11 minutes after the Chromium startup and
parallel-test fixes. These automated native results qualify the covered core
configurations as supported. Hands-on physical Windows/macOS testing,
interactive microphone/recording and media-lock checks, Apple Silicon, Windows
ARM, and individual optional AI backends remain outside that claim.
