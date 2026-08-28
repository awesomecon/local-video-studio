#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_LIGHTWEIGHT=0

usage() {
  printf '%s\n' \
    'Usage: scripts/bootstrap.sh [--install-lightweight]' \
    '' \
    'Inspects the current environment first. It never installs or changes CUDA,' \
    'NVIDIA drivers, PyTorch, model runtimes, or model weights.' \
    '' \
    '--install-lightweight  Install only this project and missing lightweight dependencies.'
}

case "${1:-}" in
  --install-lightweight) INSTALL_LIGHTWEIGHT=1 ;;
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

cd "$ROOT_DIR"
printf 'Local Video Studio bootstrap (inspection first)\n\n'

python scripts/check_environment.py

printf '\nShared cache defaults (override in config or environment):\n'
printf '  TORCH_HOME=%s\n' "${TORCH_HOME:-$HOME/ai/cache/torch}"
printf '  HF_HOME=%s\n' "${HF_HOME:-$HOME/ai/cache/huggingface}"
printf '  HUGGINGFACE_HUB_CACHE=%s\n' \
  "${HUGGINGFACE_HUB_CACHE:-$HOME/ai/cache/huggingface/hub}"
printf '  model root=%s\n' "${LOCAL_VIDEO_STUDIO_MODEL_ROOT:-$HOME/ai/models}"
printf '  project root=%s\n' "${LOCAL_VIDEO_STUDIO_PROJECT_ROOT:-$HOME/ai/projects}"

if [[ "$INSTALL_LIGHTWEIGHT" -eq 0 ]]; then
  printf '\nNo packages were installed. Review the report, then run:\n'
  printf '  scripts/bootstrap.sh --install-lightweight\n'
  exit 0
fi

python - <<'PY'
import importlib.util
import sys

required = ("fastapi", "httpx", "PIL", "psutil", "pydantic", "yaml", "uvicorn")
missing = [name for name in required if importlib.util.find_spec(name) is None]
print("Missing lightweight modules:", ", ".join(missing) if missing else "none")
if importlib.util.find_spec("torch") is None:
    print("NOTICE: PyTorch is absent. Bootstrap will not install it.")
else:
    import torch
    print(f"Preserving existing PyTorch {torch.__version__} at {torch.__file__}")
PY

printf '\nInstalling the local package and declared lightweight dependencies only.\n'
python -m pip install -e .
printf '\nBootstrap complete. No GPU stack or model weights were changed.\n'
