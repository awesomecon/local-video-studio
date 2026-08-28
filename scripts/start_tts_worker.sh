#!/usr/bin/env bash
set -euo pipefail

provider="${1:-}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_root="${LVS_TTS_OUTPUT_ROOT:-${HOME}/ai/projects}"
export LVS_AI_CACHE_ROOT="${LVS_AI_CACHE_ROOT:-${HOME}/ai/cache}"

case "${provider}" in
  qwen_tts)
    python_bin="${LVS_QWEN_PYTHON:-${HOME}/ai/services/Qwen3-TTS/.venv/bin/python}"
    model_path="${LVS_QWEN_MODEL:-${HOME}/ai/models/tts/qwen/Qwen3-TTS-12Hz-1.7B-Base}"
    port="${LVS_QWEN_PORT:-8191}"
    extra=()
    ;;
  step_audio_editx)
    python_bin="${LVS_STEP_PYTHON:-${HOME}/ai/services/Step-Audio-EditX/.venv/bin/python}"
    model_path="${LVS_STEP_MODEL:-${HOME}/ai/models/tts/step/Step-Audio-EditX}"
    tokenizer_path="${LVS_STEP_TOKENIZER:-${HOME}/ai/models/tts/step/Step-Audio-Tokenizer}"
    port="${LVS_STEP_PORT:-8192}"
    extra=(--tokenizer-path "${tokenizer_path}")
    ;;
  chatterbox)
    python_bin="${LVS_CHATTERBOX_PYTHON:-${HOME}/ai/services/chatterbox/.venv/bin/python}"
    model_path="${LVS_CHATTERBOX_MODEL:-${HOME}/ai/models/tts/chatterbox-v3}"
    port="${LVS_CHATTERBOX_PORT:-8193}"
    extra=()
    ;;
  *)
    echo "usage: $0 {qwen_tts|step_audio_editx|chatterbox}" >&2
    exit 2
    ;;
esac

if [[ ! -x "${python_bin}" ]]; then
  echo "isolated Python not found: ${python_bin}" >&2
  exit 1
fi
if [[ ! -d "${model_path}" ]]; then
  echo "model directory not found: ${model_path}" >&2
  exit 1
fi

cd "${repo_root}"
exec "${python_bin}" -m services.tts_worker.app \
  --provider "${provider}" \
  --model-path "${model_path}" \
  --output-root "${output_root}" \
  --host 127.0.0.1 \
  --port "${port}" \
  "${extra[@]}"
