#!/usr/bin/env bash
set -euo pipefail

runtime_only=false
if [[ "${1:-}" == "--runtime-only" ]]; then
  runtime_only=true
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--runtime-only]" >&2
  exit 2
fi

install_root="${LVS_IDEOGRAM_ROOT:-${HOME}/ai/services/ComfyUI-Ideogram4}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cache_root="${LVS_IDEOGRAM_CACHE:-${HOME}/ai/cache}"
comfy_commit="c67885b14556cf3e4e061862925282d403d09862"
node_commit="c05545d71e61b7ce47534a972eaeefd958a3719f"
core_commit="990fe1c4e950bb9e9dc90e01c0ad98ba434f83c2"

install_root="$(realpath -m -- "${install_root}")"
cache_root="$(realpath -m -- "${cache_root}")"
hf_home="$(realpath -m -- "${LVS_IDEOGRAM_HF_HOME:-${cache_root}/huggingface}")"
uv_cache="$(realpath -m -- "${LVS_IDEOGRAM_UV_CACHE:-${cache_root}/uv}")"
if [[ "${install_root}" == "/" || "${install_root}" == "${HOME}" ]]; then
  echo "refusing unsafe Ideogram install root: ${install_root}" >&2
  exit 1
fi

mkdir -p "$(dirname "${install_root}")" "${hf_home}" "${uv_cache}"
free_kib="$(df -Pk "$(dirname "${install_root}")" | awk 'NR==2 {print $4}')"
required_kib=$((70 * 1024 * 1024))
if (( free_kib < required_kib )); then
  echo "Ideogram NF4 installation requires at least 70 GiB free to preserve the 50 GiB reserve." >&2
  exit 1
fi

clone_or_pin() {
  local url="$1"
  local destination="$2"
  local commit="$3"
  if [[ ! -d "${destination}/.git" ]]; then
    git clone "${url}" "${destination}"
  elif [[ "$(git -C "${destination}" remote get-url origin)" != "${url}" ]]; then
    echo "unexpected git origin in ${destination}; refusing to modify it" >&2
    exit 1
  fi
  git -C "${destination}" fetch --depth 1 origin "${commit}"
  git -C "${destination}" checkout --detach "${commit}"
}

clone_or_pin \
  "https://github.com/Comfy-Org/ComfyUI.git" \
  "${install_root}" \
  "${comfy_commit}"
clone_or_pin \
  "https://github.com/ideogram-oss/ComfyUI-Ideogram4.git" \
  "${install_root}/custom_nodes/ComfyUI-Ideogram4" \
  "${node_commit}"
clone_or_pin \
  "https://github.com/ideogram-oss/ideogram-4.git" \
  "${install_root}/ideogram-4-core" \
  "${core_commit}"

apply_local_patch() {
  local checkout="$1"
  local patch_file="$2"
  if git -C "${checkout}" apply --reverse --check "${patch_file}" >/dev/null 2>&1; then
    return
  fi
  git -C "${checkout}" apply --check "${patch_file}"
  git -C "${checkout}" apply "${patch_file}"
}

apply_local_patch \
  "${install_root}/ideogram-4-core" \
  "${repo_root}/scripts/patches/ideogram4-local-snapshot.patch"
apply_local_patch \
  "${install_root}/custom_nodes/ComfyUI-Ideogram4" \
  "${repo_root}/scripts/patches/ideogram4-node-local-snapshot.patch"

export UV_CACHE_DIR="${uv_cache}"
if [[ ! -x "${install_root}/.venv/bin/python" ]]; then
  uv venv --python 3.12 "${install_root}/.venv"
fi
python_bin="${install_root}/.venv/bin/python"

# This environment is isolated deliberately. Never install these packages into
# the user's existing ComfyUI or application Python environment.
uv pip install --python "${python_bin}" \
  "torch==2.11.0" "torchvision==0.26.0" "torchaudio==2.11.0"
uv pip install --python "${python_bin}" -r "${install_root}/requirements.txt"
uv pip install --python "${python_bin}" -e "${install_root}/ideogram-4-core"

if [[ "${runtime_only}" == true ]]; then
  echo "Ideogram runtime installed; gated NF4 weights were not downloaded."
  exit 0
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "HF_TOKEN is required after accepting ideogram-ai/ideogram-4-nf4 terms." >&2
  echo "The token is consumed from the environment only and is never saved by this script." >&2
  exit 1
fi

export HF_HOME="${hf_home}"
"${install_root}/.venv/bin/hf" download ideogram-ai/ideogram-4-nf4
echo "Ideogram 4 NF4 runtime and weights are installed under ${install_root}."
