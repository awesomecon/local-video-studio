#!/usr/bin/env bash
# Pin-install the reviewed ComfyUI custom nodes for the four-model TTS comparison.
#
# SAFETY: this script is never run by agents or automated tests. The user runs
# it manually, one node at a time, and restarts ComfyUI between installs.
# It installs only into the existing ComfyUI venv and never touches CUDA,
# torch, torchvision, torchaudio, or any system package. If a requirements
# install attempts to change torch/torchaudio versions, abort and audit first
# (see docs/local-tts.md).
#
# Usage:
#   scripts/install_tts_nodes.sh indextts   # T8mars/comfyui-indextts25-t8
#   scripts/install_tts_nodes.sh voxcpm     # Saganaki22/ComfyUI-VoxCPM2
#   scripts/install_tts_nodes.sh fish       # Saganaki22/ComfyUI-FishAudioS2

set -euo pipefail

COMFYUI="${COMFYUI:-${HOME}/ai/services/ComfyUI}"
PYTHON="${COMFYUI}/venv/bin/python"
CUSTOM_NODES="${COMFYUI}/custom_nodes"

case "${1:-}" in
  indextts)
    REPO="https://github.com/T8mars/comfyui-indextts25-t8"
    PIN="b3a0dcdd4c43a5fbc07e9b0e9f7880e7d4f6e094"
    DIR="comfyui-indextts25-t8"
    ;;
  voxcpm)
    REPO="https://github.com/Saganaki22/ComfyUI-VoxCPM2"
    PIN="0e52a6cd006769e030d0e1ae907ffebbbfac4f5f"
    DIR="ComfyUI-VoxCPM2"
    ;;
  fish)
    REPO="https://github.com/Saganaki22/ComfyUI-FishAudioS2"
    PIN="521f33fe79c081da314dc905ce399c62edb24749"
    DIR="ComfyUI-FishAudioS2"
    ;;
  *)
    echo "usage: $0 {indextts|voxcpm|fish}" >&2
    exit 2
    ;;
esac

[ -x "${PYTHON}" ] || { echo "ComfyUI venv python not found: ${PYTHON}" >&2; exit 1; }

cd "${CUSTOM_NODES}"
if [ -d "${DIR}" ]; then
  echo "${DIR} already exists; refusing to overwrite." >&2
  exit 1
fi

echo "==> Cloning ${REPO} @ ${PIN}"
git clone "${REPO}" "${DIR}"
git -C "${DIR}" checkout --detach "${PIN}"

REQS=("${DIR}/requirements.txt")
if [ "${1}" = "indextts" ] && [ -f "${DIR}/requirements-modelscope.txt" ]; then
  REQS+=("${DIR}/requirements-modelscope.txt")
fi

for req in "${REQS[@]}"; do
  echo "==> Auditing ${req} for torch/CUDA rewrites before installing:"
  if grep -Ei "^\s*(torch|torchvision|torchaudio|nvidia[^ ]*)" "${req}"; then
    echo "!! ${req} references torch/NVIDIA packages — review manually before proceeding." >&2
    exit 1
  fi
  "${PYTHON}" -m pip install -r "${req}"
done

case "${1}" in
  fish)
    echo "==> Installing descript-audio-codec stack with --no-deps (per node README):"
    "${PYTHON}" -m pip install --no-deps descript-audio-codec "descript-audiotools>=0.7.2"
    ;;
esac

echo "==> Done. Restart ComfyUI, then verify readiness:"
echo "    curl -s http://127.0.0.1:8188/object_info | python3 -m json.tool | head"
echo "    Templates: workflows/comfyui/tts/ (readiness() validates node presence)."
