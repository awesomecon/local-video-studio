# Contributing

Thanks for helping improve Local Video Studio. Open an issue before a large architectural change so
the approach and backend ownership can be agreed before implementation.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
python3 frontend/tests/static_checks.py
```

Tests must run without downloading model weights or contacting non-local services. Keep optional
model integrations behind the existing backend interfaces, preserve localhost-only defaults, and do
not add telemetry. Never commit API keys, private media, machine-local configuration, model weights,
generated projects, or caches.

By submitting a contribution, you agree that it is licensed under the repository's Apache License
2.0 and that you have the right to contribute it.
