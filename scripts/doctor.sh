#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

printf 'Local Video Studio doctor\n'
printf '=========================\n\n'
python scripts/check_environment.py

printf '\nPort and external-service checks\n'
printf '%s\n' '--------------------------------'
python scripts/check_ports.py --verify-external

printf '\nSecret safety\n'
printf '%s\n' '-------------'
if [[ -n "${LOCAL_LLM_API_KEY:-}" ]]; then
  printf 'LOCAL_LLM_API_KEY: configured (value hidden)\n'
else
  printf 'LOCAL_LLM_API_KEY: not configured\n'
fi

printf '\nDoctor is diagnostic only; it did not install packages or modify services.\n'
