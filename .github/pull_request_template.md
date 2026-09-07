## Summary

<!-- What changed, why it was needed, and any user-visible behavior. -->

## Validation

<!-- List focused/manual commands and results. Explain any check you could not run. -->

- [ ] `python -m pytest` passes, or omitted coverage is explained below.
- [ ] `python3 frontend/tests/static_checks.py` passes for frontend changes, or is not applicable.
- [ ] Automated tests remain offline and do not download model weights.

Validation notes:

## Safety checklist

### Privacy and local-first behavior

- [ ] No secrets, `.env` data, private paths, prompts, media, reference images, or voice samples are
      committed or exposed in logs and diagnostics.
- [ ] No telemetry or remote content upload was added without explicit user opt-in and
      documentation.
- [ ] New services bind to `127.0.0.1` by default and do not interfere with externally owned port
      1234.

### Models and downloads

- [ ] The change does not automatically download model weights.
- [ ] Any user-directed download larger than 1 GB reports the model, approximate size, destination,
      and free disk space before it begins.
- [ ] Model and cache behavior preserves at least 50 GB free on the configured target.

### GPU and dependencies

- [ ] The change does not automatically replace, downgrade, or reinstall NVIDIA drivers, CUDA,
      PyTorch, torchvision, or torchaudio.
- [ ] GPU-heavy jobs remain serialized, inspect available VRAM before loading, and do not terminate
      the user's local LLM service to reclaim VRAM.
- [ ] Backend-specific dependency conflicts remain isolated from the base environment.

## Additional notes

<!-- Mark non-applicable checklist items here and explain why. Note migrations, compatibility risks,
manual setup, screenshots, or follow-up work. -->
