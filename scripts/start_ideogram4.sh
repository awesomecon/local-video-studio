#!/usr/bin/env bash
set -euo pipefail

install_root="${LVS_IDEOGRAM_ROOT:-${HOME}/ai/services/ComfyUI-Ideogram4}"
cache_root="${LVS_IDEOGRAM_CACHE:-${HOME}/ai/cache}"
port="${LVS_IDEOGRAM_PORT:-8190}"
python_bin="${install_root}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "isolated Ideogram runtime not found; run scripts/install_ideogram4.sh first" >&2
  exit 1
fi
if ss -ltn | awk '{print $4}' | grep -Eq "(^|:)$port$"; then
  echo "port ${port} is already occupied; no process was stopped" >&2
  exit 1
fi

export HF_HOME="${LVS_IDEOGRAM_HF_HOME:-${cache_root}/huggingface}"
export IDEOGRAM4_REPO="${install_root}/ideogram-4-core"
# The gated snapshot is installed ahead of time. Offline mode prevents the
# official loader from requiring HF_TOKEN again for metadata HEAD requests and
# keeps generation fully local. Set LVS_IDEOGRAM_OFFLINE=0 only for maintenance.
export HF_HUB_OFFLINE="${LVS_IDEOGRAM_OFFLINE:-1}"
model_cache="${HF_HOME}/hub/models--ideogram-ai--ideogram-4-nf4"
if [[ -z "${IDEOGRAM4_WEIGHTS_PATH:-}" ]]; then
  if [[ ! -f "${model_cache}/refs/main" ]]; then
    echo "Ideogram NF4 cache is incomplete: missing ${model_cache}/refs/main" >&2
    exit 1
  fi
  model_revision="$(<"${model_cache}/refs/main")"
  export IDEOGRAM4_WEIGHTS_PATH="${model_cache}/snapshots/${model_revision}"
fi
if [[ ! -f "${IDEOGRAM4_WEIGHTS_PATH}/model_index.json" ]]; then
  echo "Ideogram NF4 snapshot is incomplete: ${IDEOGRAM4_WEIGHTS_PATH}" >&2
  exit 1
fi
cd "${install_root}"
exec "${python_bin}" main.py \
  --listen 127.0.0.1 \
  --port "${port}" \
  --disable-auto-launch \
  --fp16-intermediates
