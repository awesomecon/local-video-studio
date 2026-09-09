# Contributing

Thanks for helping improve Local Video Studio. Please read `AGENTS.md` before working in the
repository. Open an issue before a large architectural change so the approach and affected backend
ownership can be agreed before implementation.

## Workflow

The `main` branch is protected. All code and documentation changes must go through a pull request:

1. Create a focused feature, fix, or documentation branch from current `main`.
2. Make scoped changes without modifying unrelated work.
3. Run the relevant validation locally.
4. Push the branch and open a pull request using the repository template.
5. Merge only after required checks and review pass.

Do not commit or push directly to `main`. Keep dependency and lock-file changes intentional and
explain them in the pull request.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
python3 frontend/tests/static_checks.py
```

CI runs the Python and frontend checks on Python 3.11 and 3.12. A focused test command is acceptable
during development, but run the full relevant suite before requesting review. Automated tests must
not download model weights or contact non-local services.

CI explicitly selects the Google Chrome executable on GitHub's Ubuntu 24.04 runner through
`LVS_CHROME` and logs its path and version. The browser version follows runner image updates;
it is not pinned to an exact release. Missing Chrome fails setup instead of silently skipping
browser tests or selecting a different browser.

Test output stays compact: each passing test appears as a dot. For individual test names locally,
use `python -m pytest -v`. Each CI job uploads a `test-report-python-<version>-attempt-<number>`
artifact, retained for 14 days, even when tests fail. Download it from the workflow run's
**Artifacts** section. It contains `pytest.xml` (JUnit XML with each test's name, result, and
duration, including setup and teardown) and `browser.txt` (the selected browser path and version).
If setup fails before pytest runs, there may be no test report to upload.

## Safety and architecture

- Never commit secrets, `.env` files, private media or prompts, voice samples, machine-local
  configuration, model weights, generated projects, or caches.
- Preserve local-first behavior: bind services to `127.0.0.1`, add no telemetry by default, and do
  not send project content to remote services without explicit user opt-in.
- Never automatically replace, downgrade, or reinstall NVIDIA drivers, CUDA, PyTorch, torchvision,
  or torchaudio. Isolate backend-specific dependency conflicts.
- Never download model weights automatically. Any user-directed download larger than 1 GB must
  disclose the model, approximate size, destination, and available disk space first.
- Serialize GPU-heavy work on the configured GPU, inspect available VRAM before loading, and never
  stop the user's external local-LLM service to reclaim resources.
- Keep model integrations behind `GeneratorBackend`, retain the mock pipeline, and keep automated
  tests lightweight and offline.
- Preserve restartability and provenance: prompts, seeds, model and workflow versions, settings,
  attempts, and file hashes must remain recoverable in portable project data.

If a checklist item in the pull-request template does not apply, mark it as not applicable and say
why in the PR notes.

By submitting a contribution, you agree that it is licensed under the repository's Apache License
2.0 and that you have the right to contribute it.
