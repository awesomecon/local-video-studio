# Native core CI (Stage 2)

The workflow runs the full Python suite, frontend static checks, and a separate
required native smoke on each matrix entry:

| Runner | Architecture | Python |
| --- | --- | --- |
| `ubuntu-24.04` | x64 | 3.11, 3.12 |
| `windows-2025` | x64 | 3.12 |
| `macos-15-intel` | x64 (Intel) | 3.12 |

Runner labels and architectures follow the [GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
This matrix does not cover Apple Silicon, Windows ARM, or every Linux distribution.
Adding jobs is not evidence of native success: all four remote results must be
reviewed before claiming the covered configurations work.

## CI prerequisites and execution

Only hosted CI installs dependencies: `.[dev]` (core plus test dependencies),
Playwright, FFmpeg through apt/Homebrew/Chocolatey, and Chromium through
[Playwright's browser installer](https://playwright.dev/python/docs/browsers).
No AI extras or models are installed. The prerequisite check rejects installed
Torch-family and faster-whisper packages in CI. The Python version, actual native
architecture, runner label, FFmpeg, ffprobe, Chromium and Playwright versions are
recorded. Package versions are currently resolved at run time rather than locked.

`python scripts/ci_core_smoke.py --prerequisites` verifies tools and writes the
Chromium executable into `GITHUB_ENV` for application browser discovery.
`python scripts/ci_core_smoke.py` then:

1. Creates temporary configuration with all data/cache/model paths isolated and
   service backends disabled; starts a mock application in an owned child process.
   The test intercepts only the module-level default configuration read, preventing
   private `config/local.yaml` and environment overrides from being consumed.
2. Binds an OS-assigned loopback port without a probe/rebind race; checks mock health,
   the root document, and every shipped JavaScript, CSS and SVG asset over HTTP.
3. Boots the actual UI in a fresh Chromium session, blocking non-loopback page
   requests, and checks that the shell loads without uncaught JavaScript errors.
4. Creates a synthetic project through HTTP, gracefully stops its server, starts
   a new server on the same temporary data, and reopens that project through HTTP.
5. Generates a half-second MP4 through `MockGeneratorBackend` and probes video,
   audio, dimensions and duration through ffprobe. This is a backend mock-media
   check, not a full timeline render or a real-model generation check.
6. Closes its browser, joins only its owned server processes, and removes temporary
   data. A forced server shutdown fails the test.

Missing prerequisites fail the smoke. The full suite independently checks required
tool discovery and converts FFmpeg/ffprobe/Chromium skips into failures in CI.
There is no `continue-on-error`. Linux Snap policy is explicitly Linux-only;
dispatch-path resolution is unit-test simulation without privileged symlinks.
Other fake worker/platform tests do not establish native model-service support.
Optional local OCR tests may still skip; their outcomes remain visible in JUnit.

## Reports and acceptance

Artifacts are separated by runner, architecture, Python version and run attempt.
They include prerequisite versions, smoke milestones and sanitized JUnit outcomes.
JUnit exports retain case identities/counts/timings and failure/error/skip elements,
but omit messages, tracebacks, captured streams, hostnames and custom properties.
Raw JUnit is not uploaded. No environment dump, application data, browser profile,
media, prompts, screenshots or private configuration is uploaded. Detailed failure
diagnostics remain in the CI job log; artifacts intentionally omit those payloads.

Required remote checks, not executable on a Linux workstation:

- Dispatch this workflow or run it on a PR; inspect all four required job results.
- Confirm recorded OS/architecture/version and that every smoke milestone completed.
- Confirm the full Python suite and frontend checks executed on each native runner;
  inspect failed and skipped case identities rather than relying only on totals.
- Verify Windows process shutdown, temporary-file cleanup, path handling and FFmpeg
  execution on `windows-2025`; verify the same on Intel `macos-15-intel`, including
  Chromium startup and Homebrew FFmpeg installation.
- Confirm missing tools cannot yield a passing job and sanitized artifacts are
  available after failures. Configure repository required checks separately if needed.

Runtime portability failures exposed by these jobs belong to a subsequent stage.
Do not weaken their assertions or make required jobs optional to hide failures.

## Local verification status (Linux only)

Local Linux run of this branch: full Python suite 1166 passed; frontend static
checks and 127/127 JS logic tests passed. Playwright is not installed locally
and was not added; its CI smoke remains pending. No Windows or macOS native
results have been reviewed in this pass, so no claim is made about those
configurations.
