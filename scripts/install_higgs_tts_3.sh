#!/usr/bin/env bash
set -euo pipefail

# Installs Higgs TTS 3 into an isolated SGLang-Omni environment. This script
# never changes system CUDA, NVIDIA drivers, or the application's Python env.
# Model download is opt-in because the checkpoint is about 9.3 GB.

revision="7989a5ed273587eb335a30f907235d1d1fbbf986"
model_revision="239f63fb7b02b1aa085f98d9efae5e35cc5523e8"
service_root="${LVS_HIGGS_SERVICE_ROOT:-${HOME}/ai/services/sglang-omni}"
model_root="${LVS_HIGGS_MODEL:-${HOME}/ai/models/tts/higgs/bosonai-higgs-tts-3-4b}"
cache_root="${LVS_AI_CACHE_ROOT:-${HOME}/ai/cache}"
download=false

if [[ "${1:-}" == "--download" ]]; then
  download=true
elif [[ -n "${1:-}" ]]; then
  echo "usage: $0 [--download]" >&2
  exit 2
fi

for command in git uv nvidia-smi; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "required command not found: ${command}" >&2
    exit 1
  fi
done

echo "Higgs TTS 3 model: bosonai/higgs-tts-3-4b (about 9.3 GB)"
echo "Model destination: ${model_root}"
echo "Runtime destination: ${service_root}"
df -h "$(dirname "${model_root}")" 2>/dev/null || df -h "${HOME}"
nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free --format=csv,noheader

available_kb="$(df -Pk "$(dirname "${model_root}")" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
if [[ -z "${available_kb}" ]]; then
  available_kb="$(df -Pk "${HOME}" | awk 'NR==2 {print $4}')"
fi
# Leave 50 GiB free after a conservative 20 GiB allowance for weights,
# environment packages, and temporary download data.
if (( available_kb < 70 * 1024 * 1024 )); then
  echo "refusing setup: target needs at least 70 GiB free to preserve the 50 GiB reserve" >&2
  exit 1
fi

if [[ ! -d "${service_root}/.git" ]]; then
  if [[ -e "${service_root}" ]]; then
    echo "refusing setup: runtime destination exists but is not a Git checkout: ${service_root}" >&2
    exit 1
  fi
  mkdir -p "$(dirname "${service_root}")"
  git clone https://github.com/sgl-project/sglang-omni.git "${service_root}"
fi
if [[ -n "$(git -C "${service_root}" status --porcelain)" ]]; then
  echo "refusing setup: SGLang-Omni checkout has local changes: ${service_root}" >&2
  exit 1
fi
current_revision="$(git -C "${service_root}" rev-parse HEAD)"
if [[ "${current_revision}" != "${revision}" ]]; then
  git -C "${service_root}" fetch --depth 1 origin "${revision}"
  git -C "${service_root}" checkout --detach "${revision}"
fi

if [[ ! -x "${service_root}/.venv/bin/python" ]]; then
  uv venv "${service_root}/.venv" -p 3.12
fi
uv pip install --prerelease=allow --python "${service_root}/.venv/bin/python" -e "${service_root}"
# SGLang requires a prerelease package elsewhere in its dependency graph, but
# unconstrained prerelease resolution can pair CUDA 13.4 NVCC with PyTorch's
# CUDA 13.0 headers. Keep the compiler and headers on the same release.
uv pip install --python "${service_root}/.venv/bin/python" \
  "nvidia-cuda-crt==13.0.88" \
  "nvidia-cuda-nvcc==13.0.88" \
  "nvidia-nvjitlink==13.0.88" \
  "nvidia-nvvm==13.0.88"

if [[ "${download}" != true ]]; then
  echo "Runtime installed. Model download was skipped."
  echo "Run '$0 --download' when you are ready for the approximately 9.3 GB checkpoint download."
  exit 0
fi

mkdir -p "${model_root}" "${cache_root}/huggingface"
export HF_HOME="${cache_root}/huggingface"
export HUGGINGFACE_HUB_CACHE="${HF_HOME}/hub"
"${service_root}/.venv/bin/hf" download bosonai/higgs-tts-3-4b \
  --revision "${model_revision}" \
  --local-dir "${model_root}"

cat > "${model_root}/lvs-pinned-revision.json" <<EOF
{"model":"bosonai/higgs-tts-3-4b","revision":"${model_revision}","runtime_revision":"${revision}"}
EOF
echo "Higgs TTS 3 is installed. Local Video Studio will start it on demand."
