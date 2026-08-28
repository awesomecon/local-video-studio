# Troubleshooting

## Port is occupied

The error includes a PID/process when safely discoverable. Stop it manually, configure another port,
or explicitly use it if identity verification confirms a supported external service. The application
never kills it. Port 1234 is reserved and cannot be selected for an application-owned service.

## Local LLM is unavailable

Run `scripts/doctor.sh`. It distinguishes connection refusal, authentication failure, a non-compatible
service, an unavailable model, and timeout. Confirm the server is running on `127.0.0.1:1234` and that
`LOCAL_LLM_API_KEY` is exported. The key value will not appear in the report.

## FFmpeg is not on PATH

The renderer also detects the executable bundled by `imageio-ffmpeg`. Install system FFmpeg only if
you need codecs/features absent from that build; do not change CUDA or PyTorch for FFmpeg.

## Heavy job reports insufficient VRAM

This is based on system-wide free memory. Close optional GPU applications or manually unload the local
LLM model, then retry. Never run heavyweight integration tests concurrently.

## Backend dependency conflict

Do not modify the working application environment. Create a backend-specific environment/service,
point it at centralized caches, and configure its loopback endpoint. The environment checker prints
known conflicts and classifies the main environment separately.

## Interrupted or failed generation

Restart the worker/API. Interrupted jobs are recovered into a retryable state. Retry only the failed
stage or scene; partial/rejected outputs remain under the project's variants archive for inspection.
